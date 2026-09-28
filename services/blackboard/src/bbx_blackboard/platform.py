"""Versioned platform connections; plaintext credentials never enter public documents."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from bbx_contracts.billing import supports_deepseek_schedule
from bbx_contracts.models import AgentProfile, ModelConfig, Price
from bbx_contracts.providers import PROVIDERS
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request, Response
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from bbx_blackboard.auth import require_service, require_user_or_service
from bbx_blackboard.store.schema import app_settings
from bbx_blackboard.store.schema import platform_configs as table

Kind = Literal["models", "mcp-servers"]


def check_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("地址须为 HTTP(S) URL，凭据请填写到密钥字段")
    if url.query or url.fragment:
        raise ValueError("连接地址不能包含查询参数或片段；认证信息请使用密钥字段")
    return value.rstrip("/")


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=100)
    provider: str
    model: str = Field(min_length=1, max_length=200)
    base_url: str = ""
    reasoning_effort: str = Field(default="none", min_length=1)
    supports_vision: bool | None = None
    price: Price
    provider_options: dict[str, str] = Field(default_factory=dict)
    credentials: dict[str, SecretStr] = Field(default_factory=dict)
    credential_source: Literal["stored", "none"] = "stored"
    api_key: SecretStr | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def check_model(self) -> ModelInput:
        spec = cast(dict[str, Any] | None, PROVIDERS.get(self.provider))
        if spec is None:
            raise ValueError("不支持此 Provider，请从平台目录选择")
        if self.price.currency != "CNY" or not self.price.is_complete():
            raise ValueError("平台模型须填写完整人民币 CNY 价格表，包括 off_peak")
        default_url = str(spec.get("default_base_url") or "")
        if default_url == "provider-default" and not spec.get("base_url_required"):
            if self.base_url not in {"", "provider-default"}:
                raise ValueError("此服务端点由区域和项目确定，不接受自定义 API 地址")
            self.base_url = "provider-default"
        else:
            self.base_url = check_url(self.base_url or default_url)
        if self.price.billing_mode == "deepseek_schedule" and not supports_deepseek_schedule(
            self.base_url, self.model
        ):
            raise ValueError("峰谷计费仅支持官方 DeepSeek 端点和已知模型")
        allowed = {item["name"] for item in spec["options_fields"]}
        if not set(self.provider_options) <= allowed:
            raise ValueError("存在当前 Provider 不支持的连接选项")
        for item in spec["options_fields"]:
            if item.get("required") and not self.provider_options.get(item["name"], "").strip():
                raise ValueError(f"缺少连接选项：{item['name']}")
        allowed_credentials = {item["name"] for item in spec["credential_fields"]}
        if not set(self.credentials) <= allowed_credentials:
            raise ValueError("存在当前 Provider 不支持的凭据字段")
        return self


class McpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=100)
    url: str
    auth_header: str = Field(default="Authorization", pattern=r"^[A-Za-z0-9_-]+$")
    auth_scheme: str = Field(default="Bearer", pattern=r"^[A-Za-z0-9_-]*$")
    secret: SecretStr | None = None
    clear_secret: bool = False
    enabled: bool = True

    _url = field_validator("url")(check_url)


class PlatformStore:
    def __init__(self, engine: AsyncEngine, signing_key: str):
        self.engine = engine
        # Domain separation keeps this key distinct from JWT signing material.
        derived = hashlib.sha256(b"bbx-platform-credentials-v1\0" + signing_key.encode()).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(derived))

    def public(self, row: dict[str, Any]) -> dict[str, Any]:
        common = {key: row[key] for key in ("name", "version", "label", "enabled")}
        common["has_secret"] = bool(row["secret_ciphertext"])
        if row["kind"] == "models":
            return {
                **common,
                "config": {
                    **row["config"],
                    "platform_id": row["name"],
                    "platform_version": row["version"],
                },
                "credential_source": row["credential_source"],
                "configured_credentials": sorted(self._decrypt_model_credentials(row)),
            }
        return {**common, **row["config"]}

    async def get(self, kind: Kind, name: str, version: int | None = None) -> dict[str, Any]:
        query = select(table).where(table.c.kind == kind, table.c.name == name)
        query = (
            query.where(table.c.version == version)
            if version
            else query.order_by(table.c.version.desc()).limit(1)
        )
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        if row is None:
            raise HTTPException(404, "平台配置不存在")
        return dict(row)

    async def list(self, kind: Kind) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(table)
                    .where(table.c.kind == kind)
                    .order_by(table.c.name, table.c.version.desc())
                )
            ).mappings()
            latest = {}
            for row in rows:
                latest.setdefault(row["name"], self.public(dict(row)))
        result = list(latest.values())
        if kind == "models":
            default_name = await self.default_model_name()
            for item in result:
                item["is_default"] = item["name"] == default_name
        return result

    async def save(
        self,
        kind: Kind,
        name: str,
        body: ModelInput | McpInput,
        actor: str,
        *,
        bootstrap: bool = False,
    ) -> dict[str, Any]:
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise HTTPException(
                422, "名称仅允许小写字母开头及字母、数字、下划线、连字符，最长64字符"
            )
        lock = int.from_bytes(
            hashlib.sha256(f"platform:{kind}:{name}".encode()).digest()[:8], "big", signed=True
        )
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            previous = (
                (
                    await conn.execute(
                        select(table)
                        .where(table.c.kind == kind, table.c.name == name)
                        .order_by(table.c.version.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            ciphertext = previous["secret_ciphertext"] if previous else None
            if isinstance(body, ModelInput):
                spec = cast(dict[str, Any], PROVIDERS[body.provider])
                values = (
                    self._decrypt_model_credentials(dict(previous))
                    if previous and previous["config"].get("provider") == body.provider
                    else {}
                )
                values.update(
                    {
                        key: value.get_secret_value()
                        for key, value in body.credentials.items()
                        if value.get_secret_value()
                    }
                )
                if body.api_key and body.api_key.get_secret_value():
                    values["api_key"] = body.api_key.get_secret_value()
                required = [
                    item["name"] for item in spec["credential_fields"] if item.get("required")
                ]
                if (
                    not bootstrap
                    and not spec["allow_no_auth"]
                    and any(not values.get(key) for key in required)
                ):
                    raise HTTPException(
                        422, "请填写所选 Provider 所需的全部平台凭据；已有字段留空可保留"
                    )
                if values and any(not values.get(key) for key in required):
                    raise HTTPException(422, "凭据不完整，请补齐所选 Provider 的必填字段")
                source = "stored" if values or not spec["allow_no_auth"] else "none"
                ciphertext = self._encrypt_credentials(values) if values else None
                config = body.model_dump(
                    mode="json",
                    exclude={"label", "api_key", "credentials", "credential_source", "enabled"},
                )
            else:
                source = "stored"
                secret = body.secret.get_secret_value() if body.secret else None
                config = body.model_dump(
                    mode="json", exclude={"label", "secret", "clear_secret", "enabled"}
                )
                if body.clear_secret:
                    ciphertext, secret = None, None
                if secret:
                    ciphertext = self.cipher.encrypt(secret.encode()).decode()
            row = (
                (
                    await conn.execute(
                        insert(table)
                        .values(
                            kind=kind,
                            name=name,
                            version=(previous["version"] + 1) if previous else 1,
                            label=body.label,
                            config=config,
                            credential_source=source,
                            secret_ciphertext=ciphertext,
                            enabled=body.enabled,
                            created_by=actor,
                        )
                        .returning(table)
                    )
                )
                .mappings()
                .one()
            )
        return self.public(dict(row))

    def _encrypt_credentials(self, values: dict[str, str]) -> str:
        return self.cipher.encrypt(
            json.dumps({"bbx_credentials_v": 1, "values": values}).encode()
        ).decode()

    def _decrypt_model_credentials(self, row: dict[str, Any]) -> dict[str, str]:
        if not row.get("secret_ciphertext"):
            return {}
        plain = self.cipher.decrypt(row["secret_ciphertext"].encode()).decode()
        try:
            value = json.loads(plain)
            if isinstance(value, dict) and value.get("bbx_credentials_v") == 1:
                return value["values"]
        except (ValueError, TypeError):
            pass
        return {"api_key": plain}

    async def credentials(self, kind: Kind, name: str, version: int) -> dict[str, Any]:
        row = await self.get(kind, name, version)
        if kind == "models":
            values = self._decrypt_model_credentials(row)
            return {
                "secret": values.get("api_key"),
                "credentials": values,
                "credential_source": "stored" if values else "none",
            }
        secret = (
            self.cipher.decrypt(row["secret_ciphertext"].encode()).decode()
            if row["secret_ciphertext"]
            else None
        )
        return {"secret": secret, "credential_source": row["credential_source"]}

    async def import_environment_key(self, key: str) -> int:
        if not key:
            return 0
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(table)
                .where(
                    table.c.kind == "models",
                    table.c.secret_ciphertext.is_(None),
                    (table.c.credential_source == "environment")
                    | (table.c.name == "deepseek-default"),
                )
                .values(
                    secret_ciphertext=self._encrypt_credentials({"api_key": key}),
                    credential_source="stored",
                )
            )
            return result.rowcount

    async def default_model_name(self) -> str:
        async with self.engine.connect() as conn:
            value = (
                await conn.execute(
                    select(app_settings.c.value).where(app_settings.c.key == "default_model")
                )
            ).scalar_one_or_none()
        return value["name"] if value else "deepseek-default"

    async def set_default_model(self, name: str) -> None:
        if not (await self.get("models", name))["enabled"]:
            raise HTTPException(422, "默认模型必须启用")
        async with self.engine.begin() as conn:
            await conn.execute(
                pg_insert(app_settings)
                .values(key="default_model", value={"name": name})
                .on_conflict_do_update(index_elements=["key"], set_={"value": {"name": name}})
            )

    async def ensure_default_model(self, model: ModelConfig) -> ModelConfig:
        try:
            row = self.public(await self.get("models", "deepseek-default", 1))
        except HTTPException as error:
            if error.status_code != 404:
                raise
            row = await self.save(
                "models",
                "deepseek-default",
                ModelInput(
                    label="DeepSeek · 默认连接",
                    credential_source="stored",
                    **model.model_dump(exclude={"platform_id", "platform_version"}),
                ),
                "system",
                bootstrap=True,
            )
        return ModelConfig.model_validate(row["config"])

    async def normalize_profile(self, profile: AgentProfile) -> AgentProfile:
        content = profile.model_dump(mode="json")
        for role in ("explore", "derive", "close"):
            model = getattr(profile.models, role)
            if model.platform_id is not None:
                row = await self.get("models", model.platform_id, model.platform_version)
                if not row["enabled"]:
                    raise HTTPException(422, "不能选择已停用的平台模型版本")
                content["models"][role] = self.public(row)["config"]
        for tools in profile.worker_tools.values():
            for binding in tools.mcp_servers:
                row = await self.get("mcp-servers", binding.name, binding.version)
                if not row["enabled"]:
                    raise HTTPException(422, "不能选择已停用的 MCP 服务版本")
        return AgentProfile.model_validate(content)


def store(request: Request) -> PlatformStore:
    return request.app.state.platform_store


router = APIRouter(prefix="/api/platform", tags=["platform"])


@router.get("/models")
async def models(request: Request) -> list[dict[str, Any]]:
    require_user_or_service(request)
    return await store(request).list("models")


@router.get("/providers")
async def providers(request: Request) -> list[dict[str, Any]]:
    require_user_or_service(request)
    return [dict(item) for item in PROVIDERS.values()]


@router.post("/models")
async def new_model(request: Request, body: ModelInput) -> dict[str, Any]:
    identity = require_user_or_service(request)
    return await store(request).save("models", f"model-{uuid4().hex[:16]}", body, identity.name)


@router.post("/models/{name}/default")
async def default_model(request: Request, name: str) -> dict[str, str]:
    require_user_or_service(request)
    await store(request).set_default_model(name)
    return {"default_model_id": name}


class ImportKey(BaseModel):
    api_key: SecretStr


@router.post("/import-environment-key")
async def import_key(request: Request, body: ImportKey) -> dict[str, int]:
    require_service(request)
    return {
        "imported": await store(request).import_environment_key(body.api_key.get_secret_value())
    }


@router.post("/models/{name}")
async def save_model(request: Request, name: str, body: ModelInput) -> dict[str, Any]:
    identity = require_user_or_service(request)
    return await store(request).save("models", name, body, identity.name)


@router.get("/mcp-servers")
async def mcp_servers(request: Request) -> list[dict[str, Any]]:
    require_user_or_service(request)
    return await store(request).list("mcp-servers")


@router.post("/mcp-servers")
async def new_mcp(request: Request, body: McpInput) -> dict[str, Any]:
    identity = require_user_or_service(request)
    return await store(request).save("mcp-servers", f"mcp-{uuid4().hex[:16]}", body, identity.name)


@router.post("/mcp-servers/{name}")
async def save_mcp(request: Request, name: str, body: McpInput) -> dict[str, Any]:
    identity = require_user_or_service(request)
    return await store(request).save("mcp-servers", name, body, identity.name)


@router.get("/mcp-servers/{name}/versions/{version}/tools")
async def list_mcp_tools(request: Request, name: str, version: int) -> dict[str, Any]:
    require_user_or_service(request)
    platform = store(request)
    row = await platform.get("mcp-servers", name, version)
    config = row["config"]
    credential = await platform.credentials("mcp-servers", name, version)
    headers = {}
    if secret := credential["secret"]:
        headers[config["auth_header"]] = f"{config['auth_scheme']} {secret}".strip()
    try:
        async with (
            asyncio.timeout(20),
            httpx.AsyncClient(headers=headers, timeout=15, trust_env=False) as client,
        ):
            async with streamable_http_client(config["url"], http_client=client) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = []
                    cursor = None
                    seen = set()
                    while True:
                        result = await session.list_tools(cursor=cursor)
                        tools.extend(
                            {"name": tool.name, "description": tool.description or ""}
                            for tool in result.tools
                        )
                        cursor = result.nextCursor
                        if not cursor:
                            return {"tools": tools}
                        if cursor in seen:
                            raise ValueError("Repeated MCP cursor")
                        seen.add(cursor)
    except Exception as error:
        raise HTTPException(
            502, f"MCP 连接失败（{type(error).__name__}），请检查地址与认证配置"
        ) from None


@router.get("/{kind}/{name}/versions/{version}/credentials")
async def credentials(
    request: Request, response: Response, kind: Kind, name: str, version: int
) -> dict[str, Any]:
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await store(request).credentials(kind, name, version)


@router.get("/{kind}/{name}/versions/{version}")
async def config_version(request: Request, kind: Kind, name: str, version: int) -> dict[str, Any]:
    require_user_or_service(request)
    return store(request).public(await store(request).get(kind, name, version))
