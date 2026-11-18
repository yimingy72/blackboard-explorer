"""Versioned platform connections; plaintext credentials never enter public documents."""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from bbx_contracts.models import AgentProfile
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from bbx_blackboard.auth import require_service, require_user_or_service
from bbx_blackboard.store.schema import platform_configs as table

Kind = Literal["mcp-servers"]
StoredKind = Literal["mcp-servers", "pi-models"]


def config_lock(kind: str, name: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"platform:{kind}:{name}".encode()).digest()[:8], "big", signed=True
    )


def check_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("地址须为 HTTP(S) URL，凭据请填写到密钥字段")
    if url.query or url.fragment:
        raise ValueError("连接地址不能包含查询参数或片段；认证信息请使用密钥字段")
    return value.rstrip("/")


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
        return {**common, **row["config"]}

    async def get(
        self,
        kind: StoredKind,
        name: str,
        version: int | None = None,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        query = select(table).where(table.c.kind == kind, table.c.name == name)
        if not include_deleted:
            query = query.where(table.c.deleted.is_(False))
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
                    .where(table.c.kind == kind, table.c.deleted.is_(False))
                    .distinct(table.c.name)
                    .order_by(table.c.name, table.c.version.desc())
                )
            ).mappings()
            result = [self.public(dict(row)) for row in rows]
        return result

    async def save(
        self,
        kind: Kind,
        name: str,
        body: McpInput,
        actor: str,
        *,
        p6b: bool = False,
    ) -> dict[str, Any]:
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise HTTPException(
                422, "名称仅允许小写字母开头及字母、数字、下划线、连字符，最长64字符"
            )
        async with self.engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": config_lock(kind, name)}
            )
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
            if previous and previous["deleted"]:
                raise HTTPException(409, "此 MCP 服务已删除，请刷新列表或新增服务")
            ciphertext = previous["secret_ciphertext"] if previous else None
            source = "stored"
            secret = body.secret.get_secret_value() if body.secret else None
            config = body.model_dump(
                mode="json", exclude={"label", "secret", "clear_secret", "enabled"}
            )
            if p6b:
                config["catalog_version"] = "p6b"
            if body.clear_secret:
                ciphertext, secret = None, None
            if ciphertext and not secret and previous:
                from bbx_blackboard.pi_registry import normalized_url

                old = previous["config"]
                if (
                    normalized_url(old["url"])[1] != normalized_url(body.url)[1]
                    or old["auth_header"].lower() != body.auth_header.lower()
                    or old["auth_scheme"] != body.auth_scheme
                ):
                    raise HTTPException(422, "更换上游或认证方式时，请重新填写或清除旧密钥")
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

    async def credentials(self, kind: StoredKind, name: str, version: int) -> dict[str, Any]:
        # Existing tasks and resumed conversations still use their pinned credentials.
        row = await self.get(kind, name, version, include_deleted=True)
        secret = (
            self.cipher.decrypt(row["secret_ciphertext"].encode()).decode()
            if row["secret_ciphertext"]
            else None
        )
        return {"secret": secret, "credential_source": row["credential_source"]}

    async def validate_profile_tools(self, profile: AgentProfile) -> None:
        for tools in profile.worker_tools.values():
            for binding in tools.mcp_servers:
                row = await self.get("mcp-servers", binding.name, binding.version)
                if not row["enabled"]:
                    raise HTTPException(422, "不能选择已停用的 MCP 服务版本")


def store(request: Request) -> PlatformStore:
    return request.app.state.platform_store


router = APIRouter(prefix="/api/platform", tags=["platform"])


@router.get("/mcp-servers")
async def mcp_servers(request: Request) -> list[dict[str, Any]]:
    from bbx_blackboard.mcp_catalog import catalog

    require_user_or_service(request)
    return await catalog(request)


async def checked_save(request: Request, name: str, body: McpInput) -> dict[str, Any]:
    from bbx_blackboard.mcp_catalog import AUTH_HEADERS, public_address, require_operator
    from bbx_blackboard.pi_registry import normalized_url

    require_operator(request)
    if body.auth_header.lower() not in AUTH_HEADERS or (body.secret and body.clear_secret):
        raise HTTPException(422, "请选择受支持的认证方式和密钥操作")
    if body.secret and len(body.secret.get_secret_value()) > 8192:
        raise HTTPException(422, "MCP 密钥超过长度上限")
    try:
        url, _ = normalized_url(body.url)
        await public_address(url)
    except (ValueError, OSError):
        raise HTTPException(422, "MCP Server 须为可解析到公网的 HTTPS 地址") from None
    body = body.model_copy(update={"url": url})
    return await store(request).save(
        "mcp-servers", name, body, require_user_or_service(request).name, p6b=True
    )


@router.post("/mcp-servers")
async def new_mcp(request: Request, body: McpInput) -> dict[str, Any]:
    return await checked_save(request, f"mcp-{uuid4().hex[:16]}", body)


@router.post("/mcp-servers/{name}")
async def save_mcp(request: Request, name: str, body: McpInput) -> dict[str, Any]:
    return await checked_save(request, name, body)


@router.get("/{kind}/{name}/versions/{version}/credentials")
async def credentials(
    request: Request, response: Response, kind: Kind, name: str, version: int
) -> dict[str, Any]:
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    platform = store(request)
    config = (await platform.get(kind, name, version, include_deleted=True))["config"]
    if config.get("catalog_version") == "p6b" or config.get("published"):
        raise HTTPException(403, "Managed MCP credentials require a dedicated Runtime identity")
    return await platform.credentials(kind, name, version)


@router.get("/{kind}/{name}/versions/{version}")
async def config_version(request: Request, kind: Kind, name: str, version: int) -> dict[str, Any]:
    require_user_or_service(request)
    import re

    from bbx_blackboard.mcp_catalog import operator, view

    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or version < 1:
        raise HTTPException(404, "平台配置不存在")
    row = await store(request).get(kind, name, version)
    if (
        row["config"].get("catalog_version") == "p6b"
        and not row["config"].get("published")
        and not operator(request)
    ):
        raise HTTPException(403, "MCP discovery drafts are operator-only")
    return view(row)
