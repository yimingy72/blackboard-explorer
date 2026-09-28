"""Versioned platform connections; plaintext credentials never enter public documents."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from bbx_contracts.models import AgentProfile, ModelConfig, Price
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request, Response
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from bbx_blackboard.auth import require_service, require_user_or_service
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
    provider: Literal["deepseek", "openai_chat", "openai_responses", "openai_compatible"]
    model: str = Field(min_length=1, max_length=200)
    base_url: str
    reasoning_effort: str = Field(default="none", min_length=1)
    price: Price
    credential_source: Literal["environment", "stored", "none"] = "stored"
    api_key: SecretStr | None = None
    enabled: bool = True

    _url = field_validator("base_url")(check_url)

    @model_validator(mode="after")
    def check_model(self) -> ModelInput:
        if self.price.currency != "CNY" or not self.price.is_complete():
            raise ValueError("平台模型须填写完整人民币 CNY 价格表，包括 off_peak")
        if self.credential_source == "environment" and self.provider != "deepseek":
            raise ValueError("环境密钥仅用于现有 DeepSeek 连接")
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

    @staticmethod
    def public(row: dict[str, Any]) -> dict[str, Any]:
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
        return list(latest.values())

    async def save(
        self, kind: Kind, name: str, body: ModelInput | McpInput, actor: str
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
                source = body.credential_source
                secret = body.api_key.get_secret_value() if body.api_key else None
                config = body.model_dump(
                    mode="json", exclude={"label", "api_key", "credential_source", "enabled"}
                )
                if source != "stored":
                    ciphertext = None
                    secret = None
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
            if isinstance(body, ModelInput) and source == "stored" and not ciphertext:
                raise HTTPException(422, "首次保存此模型需要填写 API Key，或选择无需认证")
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

    async def credentials(self, kind: Kind, name: str, version: int) -> dict[str, Any]:
        row = await self.get(kind, name, version)
        secret = (
            self.cipher.decrypt(row["secret_ciphertext"].encode()).decode()
            if row["secret_ciphertext"]
            else None
        )
        return {"secret": secret, "credential_source": row["credential_source"]}

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
                    credential_source="environment",
                    **model.model_dump(exclude={"platform_id", "platform_version"}),
                ),
                "system",
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


@router.post("/models/{name}")
async def save_model(request: Request, name: str, body: ModelInput) -> dict[str, Any]:
    identity = require_user_or_service(request)
    return await store(request).save("models", name, body, identity.name)


@router.get("/mcp-servers")
async def mcp_servers(request: Request) -> list[dict[str, Any]]:
    require_user_or_service(request)
    return await store(request).list("mcp-servers")


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
