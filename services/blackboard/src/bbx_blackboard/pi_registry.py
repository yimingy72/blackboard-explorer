"""Primary platform registry and narrowly scoped, encrypted Pi credentials."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import socket
import ssl
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from bbx_contracts.models import ModelConfig, Price
from bbx_contracts.pi_models import (
    ModelId,
    PiModelDirectory,
    PiModelInput,
    PiModelRef,
    PiModelVersion,
)
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import insert, select, text

from bbx_blackboard.auth import principal, require_user_or_service
from bbx_blackboard.platform import PlatformStore, config_lock
from bbx_blackboard.settings import Settings
from bbx_blackboard.store.schema import platform_configs

KIND = "pi-models"
RUNTIME_HEADER = "x-bbx-pi-runtime"
router = APIRouter(tags=["platform-pi-models"])


def trusted_header(request: Request, name: str, expected: str) -> None:
    value = request.headers.getlist(name)
    settings: Settings = request.app.state.settings
    if (
        len(expected) < 32
        or expected == settings.service_token.get_secret_value()
        or len(value) != 1
        or not hmac.compare_digest(value[0].encode(), expected.encode())
    ):
        raise HTTPException(401, "A dedicated Pi service identity is required")


def normalized_url(raw: str) -> tuple[str, str]:
    if raw != raw.strip() or any(ord(char) < 33 or ord(char) == 127 for char in raw):
        raise ValueError("Invalid model URL")
    u = urlsplit(raw)
    if (
        u.scheme not in {"http", "https"}
        or not u.hostname
        or u.username is not None
        or u.password is not None
        or u.query
        or u.fragment
        or "\\" in raw
        or "%" in u.netloc
    ):
        raise ValueError("Invalid model URL")
    host = u.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = u.port
    if port == 0:
        raise ValueError("Invalid port")
    default = 443 if u.scheme == "https" else 80
    origin = f"{u.scheme}://{host}" + (f":{port}" if port and port != default else "")
    return origin + u.path.rstrip("/"), origin


def peer_tls(settings: Settings) -> ssl.SSLContext | Literal[True]:
    return (
        ssl.create_default_context(cafile=settings.pi_model_peer_ca_file)
        if settings.pi_model_peer_ca_file
        else True
    )


def playground_runtime_url(settings) -> str | None:
    token = settings.pi_model_runtime_token.get_secret_value()
    if len(token) < 32 or token == settings.service_token.get_secret_value():
        return None
    try:
        url, origin = normalized_url(settings.pi_playground_runtime_url)
        target = urlsplit(url)
        if url != origin or (
            target.scheme != "https"
            and target.hostname not in {"agent-runtime", "localhost", "127.0.0.1", "::1"}
        ):
            return None
    except ValueError:
        return None
    return origin + "/playground"


def display_pi_model(model: PiModelVersion) -> ModelConfig:
    rates = model.usd_rates
    if rates is None:
        raise HTTPException(422, "The model has no verified USD rates")
    return ModelConfig(
        provider="openai_compatible",
        model=model.id,
        base_url="provider-default",
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        supports_vision="image" in model.input_modalities,
        reasoning_effort="none",
        price=Price(
            billing_mode="fixed",
            currency="USD",
            off_peak=False,
            cache_hit_per_m=rates.cache_read,
            cache_miss_per_m=rates.input,
            output_per_m=rates.output,
        ),
    )


class PiRegistry:
    def __init__(self, platform: PlatformStore, settings: Settings):
        self.platform, self.settings = platform, settings
        self.enabled = bool(settings.pi_site_id)

    def operator(self, request: Request) -> bool:
        identity = principal(request)
        return (
            self.enabled
            and bool(self.settings.pi_model_authority)
            and identity.kind == "user"
            and bool(self.settings.pi_model_operator)
            and identity.name == self.settings.pi_model_operator
        )

    def allowed_url(self, raw: str) -> str:
        try:
            url, origin = normalized_url(raw)
            if urlsplit(url).scheme != "https":
                raise ValueError("Upstream models require HTTPS")
            approved = set()
            for item in self.settings.pi_model_allowed_origins:
                value, candidate = normalized_url(item)
                if value != candidate:
                    raise ValueError("An approved model origin cannot have a path")
                approved.add(candidate)
            if approved and origin not in approved:
                raise ValueError("The model origin is not approved for this deployment")
            return url
        except ValueError as error:
            raise HTTPException(422, "模型地址不是已批准的上游地址") from error

    @staticmethod
    def version(row: dict[str, Any]) -> PiModelVersion:
        return PiModelVersion.model_validate(
            {
                **row["config"],
                "id": row["name"],
                "model_revision": row["version"],
                "label": row["label"],
                "enabled": row["enabled"],
                "has_secret": bool(row["secret_ciphertext"]),
            }
        )

    async def local_list(self) -> list[PiModelVersion]:
        t = platform_configs
        async with self.platform.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(t)
                    .where(t.c.kind == KIND)
                    .distinct(t.c.name)
                    .order_by(t.c.name, t.c.version.desc())
                )
            ).mappings()
            return [self.version(dict(row)) for row in rows if not row["deleted"]]

    async def list(self) -> list[PiModelVersion]:
        if not self.enabled:
            return []
        if not self.settings.pi_model_authority:
            raise HTTPException(503, "平台模型目录权威未配置")
        return await self.local_list()

    async def selected(self, ref: PiModelRef) -> PiModelVersion:
        chosen = next((model for model in await self.list() if model.id == ref.model_id), None)
        if chosen is None or chosen.model_revision != ref.model_revision:
            raise HTTPException(409, "模型已更新，请重新选择")
        if (
            not chosen.enabled
            or not chosen.has_secret
            or not chosen.has_text_limits()
            or not chosen.tool_call
            or chosen.usd_rates is None
        ):
            raise HTTPException(422, "此模型未启用、配置未完善、缺密钥、未定价或未启用工具调用")
        return chosen

    async def frozen(self, ref: PiModelRef) -> tuple[PiModelVersion, str]:
        if not self.enabled or not self.settings.pi_model_authority:
            raise HTTPException(503, "Pi 模型配置未启用")
        selected = self.version(await self.platform.get(KIND, ref.model_id, ref.model_revision))
        key = (await self.platform.credentials(KIND, ref.model_id, ref.model_revision))["secret"]
        if (
            selected.id != ref.model_id
            or selected.model_revision != ref.model_revision
            or not selected.has_text_limits()
            or not isinstance(key, str)
            or not key
        ):
            raise HTTPException(409, "原模型凭据不可用")
        self.allowed_url(selected.base_url)
        return selected, key

    async def verify_public_host(self, url: str) -> None:
        target = urlsplit(url)
        try:
            async with asyncio.timeout(5):
                records = await asyncio.get_running_loop().getaddrinfo(
                    target.hostname,
                    target.port or 443,
                    type=socket.SOCK_STREAM,
                )
            addresses = {ipaddress.ip_address(item[4][0]) for item in records}
            if (
                not addresses
                or len(addresses) > 32
                or any(not address.is_global or address.is_multicast for address in addresses)
            ):
                raise ValueError("Model origin is not public")
        except (OSError, ValueError) as error:
            raise HTTPException(
                422, "上游须为可解析到公网的 HTTPS 地址；此版本不接私网模型"
            ) from error

    async def save(self, name: str, body: PiModelInput, actor: str) -> PiModelVersion:
        url = self.allowed_url(body.base_url)
        await self.verify_public_host(url)
        t = platform_configs
        new_secret = body.secret.get_secret_value() if body.secret else None
        if (new_secret and body.clear_secret) or (new_secret and len(new_secret) > 8192):
            raise HTTPException(422, "请选择保存新密钥或清除旧密钥")
        async with self.platform.engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": config_lock(KIND, name)}
            )
            prior = (
                (
                    await conn.execute(
                        select(t)
                        .where(t.c.kind == KIND, t.c.name == name)
                        .order_by(t.c.version.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            revision = prior["version"] if prior else None
            if revision != body.expected_revision:
                raise HTTPException(409, "模型已更新，请刷新后编辑")
            key = prior["secret_ciphertext"] if prior else None
            if key and not (new_secret or body.clear_secret) and prior is not None:
                old = prior["config"]
                if (
                    old["provider"] != body.provider
                    or normalized_url(old["base_url"])[1] != normalized_url(url)[1]
                ):
                    raise HTTPException(422, "更换上游地址时，请重新填写或清除旧密钥")
            if new_secret:
                key = self.platform.cipher.encrypt(new_secret.encode()).decode()
            elif body.clear_secret:
                key = None
            config = body.model_dump(
                mode="json",
                exclude={"secret", "clear_secret", "expected_revision", "label", "enabled"},
            )
            config["base_url"] = url
            row = (
                (
                    await conn.execute(
                        insert(t)
                        .values(
                            kind=KIND,
                            name=name,
                            version=(revision or 0) + 1,
                            label=body.label,
                            config=config,
                            credential_source="stored",
                            secret_ciphertext=key,
                            enabled=body.enabled,
                            created_by=actor,
                        )
                        .returning(t)
                    )
                )
                .mappings()
                .one()
            )
            return self.version(dict(row))


def registry(request: Request) -> PiRegistry:
    result = request.app.state.pi_registry
    if result is None:
        raise HTTPException(503, "模型目录存储不可用")
    return result


@router.get("/api/platform/pi-models", response_model=PiModelDirectory)
async def list_models(request: Request, response: Response) -> dict:
    require_user_or_service(request)
    response.headers["Cache-Control"] = "no-store"
    r = registry(request)
    can_manage = r.operator(request)
    return {
        "enabled": r.enabled,
        "can_manage": can_manage,
        "can_test": can_manage and playground_runtime_url(r.settings) is not None,
        "models": await r.list(),
    }


@router.post("/api/platform/pi-models", response_model=PiModelVersion)
async def new_model(request: Request, body: PiModelInput) -> PiModelVersion:
    r = registry(request)
    if not r.operator(request):
        raise HTTPException(403, "只有主站指定操作者可以管理模型")
    return await r.save(f"model-{uuid4().hex[:16]}", body, principal(request).name)


@router.put("/api/platform/pi-models/{name}", response_model=PiModelVersion)
async def save_model(request: Request, name: ModelId, body: PiModelInput) -> PiModelVersion:
    r = registry(request)
    if not r.operator(request):
        raise HTTPException(403, "只有主站指定操作者可以管理模型")
    if body.expected_revision is None:
        raise HTTPException(422, "必须提交原模型版本")
    return await r.save(name, body, principal(request).name)
