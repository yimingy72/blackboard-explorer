"""Operator discovery and explicitly published MCP tools, using the existing SDK."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from bbx_contracts.mcp_catalog import (
    FrozenNativeMcpTool,
    McpDiscovery,
    McpPublish,
    McpServerName,
    McpToolDetail,
    NativeMcpGrants,
    canonical_digest,
    public_mcp_ip,
    quiet_external_sdk,
    safe_tool_json,
    tool_schema_digest,
)
from fastapi import APIRouter, HTTPException, Request, Response
from jsonschema.validators import validator_for
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from pydantic import Field
from referencing import Registry
from sqlalchemy import case, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bbx_blackboard.auth import principal, require_user_or_service
from bbx_blackboard.pi_registry import RUNTIME_HEADER, normalized_url, trusted_header
from bbx_blackboard.platform import config_lock, store
from bbx_blackboard.store.schema import app_settings
from bbx_blackboard.store.schema import platform_configs as table

MCP = "/api/platform/mcp-servers"
MATERIAL = "/api/runtime/mcp-servers"
KIND = "mcp-servers"
AUTH_HEADERS = {"authorization", "x-api-key", "api-key"}
MAX_RESPONSE = 2 * 1024 * 1024
MAX_CATALOG = 512 * 1024
router = APIRouter(tags=["platform-mcp"])
ServerName = McpServerName
ServerVersion = Annotated[int, Field(ge=1)]


async def public_address(raw: str) -> str:
    target = urlsplit(raw)
    if target.scheme != "https" or not target.hostname:
        raise ValueError("Public HTTPS is required")
    async with asyncio.timeout(5):
        resolved = await asyncio.get_running_loop().getaddrinfo(
            target.hostname, target.port or 443, type=socket.SOCK_STREAM
        )
    addresses = list(dict.fromkeys(ipaddress.ip_address(item[4][0]) for item in resolved))
    if (
        not addresses
        or len(addresses) > 32
        or any(not public_mcp_ip(str(addr)) for addr in addresses)
    ):
        raise ValueError("The target is outside the supported public network")
    return str(addresses[0])


class BoundedResponse(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self):
        remaining = MAX_RESPONSE
        async for chunk in self.stream:
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("MCP response exceeds the byte bound")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class PinnedMcpTransport(httpx.AsyncHTTPTransport):
    """Delegate HTTP/TLS to HTTPX; pin one approved URL for this SDK client."""

    def __init__(self, url: str, address: str):
        super().__init__(retries=0, trust_env=False)
        self.original = httpx.URL(url)
        self.address = address

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url != self.original or request.method not in {"POST", "GET", "DELETE"}:
            raise ValueError("MCP transport left its approved endpoint")
        request.url = request.url.copy_with(host=self.address)
        request.headers["Host"] = self.original.netloc.decode("ascii")
        request.headers["Accept-Encoding"] = "identity"
        request.extensions["sni_hostname"] = self.original.host
        response = await super().handle_async_request(request)
        if (
            response.is_redirect
            or response.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            await response.aclose()
            raise ValueError("MCP server used a redirect or unsupported response encoding")
        assert isinstance(response.stream, httpx.AsyncByteStream)
        response.stream = BoundedResponse(response.stream)
        return response


def contains_secret(value: Any, secret: str | None) -> bool:
    if not secret:
        return False
    if isinstance(value, str):
        return secret in value
    if isinstance(value, list):
        return any(contains_secret(item, secret) for item in value)
    if isinstance(value, dict):
        return any(
            contains_secret(k, secret) or contains_secret(v, secret) for k, v in value.items()
        )
    return False


def operator(request: Request) -> bool:
    registry = getattr(request.app.state, "pi_registry", None)
    return bool(registry is not None and registry.operator(request))


def require_operator(request: Request):
    if not operator(request):
        raise HTTPException(403, "只有主站指定操作者可以管理或测试 MCP Server")


def preview_key(name: ServerName, version: ServerVersion) -> str:
    return f"mcp-discovery:{name}:{version}"


def view(
    row: dict, published_version: int | None = None, published_roles: list | None = None
) -> dict:
    config = row["config"]
    return {
        **{key: row[key] for key in ("name", "version", "label", "enabled")},
        **{key: config[key] for key in ("url", "auth_header", "auth_scheme")},
        "has_secret": bool(row["secret_ciphertext"]),
        "published": bool(config.get("published")),
        "published_roles": (
            published_roles
            if published_roles is not None
            else config.get("published", {}).get("roles", [])
        ),
        "catalog_version": config.get("catalog_version"),
        "published_version": published_version
        if published_version is not None
        else (row["version"] if config.get("published") else None),
    }


async def local_catalog(request: Request, *, include_drafts: bool = False) -> list[dict]:
    latest = (
        select(
            table.c.name,
            table.c.version,
            table.c.enabled,
            table.c.deleted,
            table.c.config["catalog_version"].astext.label("catalog_version"),
        )
        .where(table.c.kind == KIND)
        .distinct(table.c.name)
        .order_by(table.c.name, table.c.version.desc())
        .subquery()
    )
    published = (
        select(table.c.name, table.c.version, table.c.config["published"]["roles"].label("roles"))
        .where(
            table.c.kind == KIND,
            table.c.enabled.is_(True),
            table.c.deleted.is_(False),
            table.c.config.has_key("published"),
        )
        .distinct(table.c.name)
        .order_by(table.c.name, table.c.version.desc())
        .subquery()
    )
    async with store(request).engine.connect() as conn:
        result = await conn.execute(
            select(table, published.c.version.label("published_version"), published.c.roles)
            .join(latest, latest.c.name == table.c.name)
            .outerjoin(published, published.c.name == table.c.name)
            .where(
                table.c.kind == KIND,
                latest.c.deleted.is_(False),
                (
                    (table.c.version == latest.c.version)
                    if include_drafts
                    else table.c.version
                    == case(
                        (latest.c.catalog_version.is_distinct_from("p6b"), latest.c.version),
                        (latest.c.enabled.is_(True), published.c.version),
                        else_=None,
                    )
                ),
            )
            .order_by(table.c.name)
        )
    return [
        view(
            dict(row),
            row["published_version"] if row["enabled"] else None,
            row["roles"] if row["enabled"] and row["roles"] else [],
        )
        for row in result.mappings()
    ]


async def catalog(request: Request) -> list[dict]:
    return await local_catalog(request, include_drafts=operator(request))


async def local_evidence(
    request: Request, name: ServerName, version: ServerVersion
) -> McpDiscovery:
    row = await store(request).get(KIND, name, version)
    async with store(request).engine.connect() as conn:
        found = await conn.scalar(
            select(app_settings.c.value).where(app_settings.c.key == preview_key(name, version))
        )
    if published := row["config"].get("published"):
        return McpDiscovery.model_validate(
            {
                **{
                    key: published[key]
                    for key in ("tools", "discovery_digest", "checked_at", "roles")
                },
                "published": True,
                "version": version,
                "status": found["status"] if found else "ready",
                **(
                    {"checked_at": found["checked_at"], "error": found.get("error")}
                    if found
                    else {}
                ),
            }
        )
    return (
        McpDiscovery.model_validate(found)
        if found
        else McpDiscovery(status="untested", version=version)
    )


async def evidence(request: Request, name: ServerName, version: ServerVersion) -> McpDiscovery:
    return await local_evidence(request, name, version)


@asynccontextmanager
async def approved_session(url: str, headers: dict[str, str]):
    address = await public_address(url)
    with quiet_external_sdk():
        async with (
            httpx.AsyncClient(
                headers=headers,
                transport=PinnedMcpTransport(url, address),
                follow_redirects=False,
                trust_env=False,
                timeout=12,
            ) as http,
            streamable_http_client(url, http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session


async def list_approved_tools(session: ClientSession, secret: str | None) -> list[McpToolDetail]:
    tools: list[McpToolDetail] = []
    names: set[str] = set()
    cursors: set[str] = set()
    size = 0
    cursor = None
    for _ in range(16):
        page = await session.list_tools(cursor=cursor)
        for tool in page.tools:
            if tool.name in names or len(tools) >= 128:
                raise ValueError("MCP catalog exceeds its bound or has duplicate tool names")
            names.add(tool.name)
            value = {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
                "output_schema": tool.outputSchema,
                "annotations": tool.annotations.model_dump(mode="json", exclude_none=True)
                if tool.annotations
                else None,
            }
            if not safe_tool_json(value) or contains_secret(value, secret):
                raise ValueError("MCP catalog metadata is unsafe")
            size += len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
            if size > MAX_CATALOG:
                raise ValueError("MCP catalog exceeds its byte bound")
            tools.append(
                McpToolDetail.model_validate(
                    {
                        **value,
                        "schema_digest": tool_schema_digest(tool.inputSchema, tool.outputSchema),
                    }
                )
            )
        cursor = page.nextCursor
        if not cursor:
            return sorted(tools, key=lambda item: item.name)
        if cursor in cursors or len(cursor) > 1024:
            raise ValueError("Invalid MCP tool cursor")
        cursors.add(cursor)
    raise ValueError("MCP pagination exceeds its bound")


async def discover_tools(
    url: str, headers: dict[str, str], secret: str | None
) -> list[McpToolDetail]:
    async with asyncio.timeout(20), approved_session(url, headers) as session:
        return await list_approved_tools(session, secret)


@router.get(MCP + "/capabilities")
async def capabilities(request: Request) -> dict:
    require_user_or_service(request)
    return {
        "can_manage": operator(request),
        "external_roles": ["explore", "lead", "teammate"],
        "discovery_network": "platform",
        "execution_network": "trusted-runtime",
        "native_execution_network": "blackboard-business-mcp",
        "private_supported": False,
    }


@router.post(MCP + "/{name}/versions/{version}/discover", response_model=McpDiscovery)
async def discover(request: Request, name: ServerName, version: ServerVersion) -> McpDiscovery:
    require_operator(request)
    platform = store(request)
    row = await platform.get(KIND, name, version)
    config = row["config"]
    credential = await platform.credentials(KIND, name, version)
    secret = credential["secret"]
    headers: dict[str, str] = {}
    if secret:
        headers[config["auth_header"]] = f"{config['auth_scheme']} {secret}".strip()
    try:
        if config["auth_header"].lower() not in AUTH_HEADERS:
            raise ValueError("Unsupported authentication header")
        url, _ = normalized_url(config["url"])
        tools = await discover_tools(url, headers, secret)
        output = McpDiscovery(
            status="ready",
            version=version,
            checked_at=datetime.now(UTC),
            tools=tools,
            discovery_digest=canonical_digest([item.model_dump(mode="json") for item in tools]),
        )
    except Exception:
        output = McpDiscovery(
            status="error",
            version=version,
            checked_at=datetime.now(UTC),
            error="连接或读取工具失败，请检查公开 HTTPS 地址、认证和工具定义。",
        )
    async with platform.engine.begin() as conn:
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": config_lock(KIND, name)}
        )
        await conn.execute(
            pg_insert(app_settings)
            .values(key=preview_key(name, version), value=output.model_dump(mode="json"))
            .on_conflict_do_update(
                index_elements=[app_settings.c.key], set_={"value": output.model_dump(mode="json")}
            )
        )
    return output


@router.post(MCP + "/{name}/versions/{version}/publish")
async def publish(
    request: Request, name: ServerName, version: ServerVersion, body: McpPublish
) -> dict:
    require_operator(request)
    platform = store(request)
    async with platform.engine.begin() as conn:
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": config_lock(KIND, name)}
        )
        previous = (
            (
                await conn.execute(
                    select(table)
                    .where(table.c.kind == KIND, table.c.name == name)
                    .order_by(table.c.version.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        if (
            previous is None
            or previous["version"] != version
            or previous["deleted"]
            or not previous["enabled"]
            or previous["config"].get("catalog_version") != "p6b"
        ):
            raise HTTPException(409, "请选择当前已启用版本")
        found = await conn.scalar(
            select(app_settings.c.value).where(app_settings.c.key == preview_key(name, version))
        )
        if (
            not found
            or found["status"] != "ready"
            or found["discovery_digest"] != body.discovery_digest
        ):
            raise HTTPException(409, "连接测试结果已变化，请重新测试")
        discovered = {tool["name"]: tool for tool in found["tools"]}
        if not set(body.tools) <= discovered.keys():
            raise HTTPException(422, "只能发布明确发现的工具")
        config = {
            **previous["config"],
            "published": {
                "roles": body.roles,
                "tools": [discovered[name] for name in body.tools],
                "discovery_digest": body.discovery_digest,
                "checked_at": found["checked_at"],
                "source_version": version,
            },
        }
        inserted = (
            (
                await conn.execute(
                    insert(table)
                    .values(
                        kind=KIND,
                        name=name,
                        version=version + 1,
                        label=previous["label"],
                        config=config,
                        credential_source=previous["credential_source"],
                        secret_ciphertext=previous["secret_ciphertext"],
                        enabled=True,
                        created_by=principal(request).name,
                    )
                    .returning(table)
                )
            )
            .mappings()
            .one()
        )
        return view(dict(inserted))


async def local_material(platform, name: ServerName, version: ServerVersion) -> dict:
    row = await platform.get(KIND, name, version)
    config = row["config"]
    if not row["enabled"] or config.get("catalog_version") != "p6b" or not config.get("published"):
        raise HTTPException(403, "MCP Server version is not published")
    credential = await platform.credentials(KIND, name, version)
    return {**view(row), "published": config["published"], "secret": credential["secret"]}


@router.get(MATERIAL + "/{name}/versions/{version}/material", include_in_schema=False)
async def material(request: Request, response: Response, name: ServerName, version: ServerVersion):
    settings = request.app.state.settings
    trusted_header(request, RUNTIME_HEADER, settings.pi_model_runtime_token.get_secret_value())
    response.headers["Cache-Control"] = "no-store"
    return await local_material(store(request), name, version)


@router.get(MCP + "/{name}/versions/{version}/tools", response_model=McpDiscovery)
async def tool_details(request: Request, name: ServerName, version: ServerVersion) -> McpDiscovery:
    require_user_or_service(request)
    result = await evidence(request, name, version)
    if not result.published and not operator(request):
        raise HTTPException(403, "MCP discovery drafts are operator-only")
    return result


async def validate_profile_tools(request: Request, profile, *, previous=None) -> None:
    registry = getattr(request.app.state, "pi_registry", None)
    if registry is None or not registry.enabled:
        await store(request).validate_profile_tools(profile)
        return
    if not any(tools.mcp_servers for tools in profile.worker_tools.values()):
        return
    available = {entry["name"]: entry for entry in await catalog(request)}
    for role, tools in profile.worker_tools.items():
        for binding in tools.mcp_servers:
            old = previous.worker_tools.get(role) if previous else None
            if old and binding in old.mcp_servers:
                continue
            if (
                role != "explore"
                or binding.catalog_version != "p6b"
                or not binding.allowed_tools
                or len(set(binding.allowed_tools)) != len(binding.allowed_tools)
            ):
                raise HTTPException(422, "外部 MCP 仅用于 Explore，须明确选择工具")
            latest = available.get(binding.name)
            if not latest or not latest["enabled"] or not latest.get("published_version"):
                raise HTTPException(422, "MCP Server 尚未发布或已停用")
            published = await evidence(request, binding.name, binding.version)
            if (
                not published.published
                or "explore" not in published.roles
                or not set(binding.allowed_tools) <= {tool.name for tool in published.tools}
            ):
                raise HTTPException(422, "只能绑定已发布版本的明确工具")


def native_tool_alias(server: str, wire_name: str) -> str:
    display = re.sub(r"[^a-zA-Z0-9_]+", "_", server + "_" + wire_name)[:44]
    return f"mcp_{display}_{canonical_digest([server, wire_name])[:12]}"


async def _freeze_role_grants(request: Request, grants, *, latest_only: bool) -> dict:
    registry = request.app.state.pi_registry
    settings = request.app.state.settings
    if not registry.enabled or not settings.pi_model_authority:
        raise HTTPException(422, "当前原生 CTF 环境没有启用外部 MCP")
    available = {entry["name"]: entry for entry in await catalog(request)}
    output = {}
    for role, bindings in grants.items():
        selected: dict[str, dict] = {}
        for grant in bindings:
            current = available.get(grant.name)
            if (
                not current
                or not current["enabled"]
                or not current.get("published_version")
                or (
                    latest_only
                    and (
                        current["published_version"] != grant.version
                        or role not in current.get("published_roles", [])
                    )
                )
            ):
                raise HTTPException(422, "所选 MCP Server 版本未对该原生角色发布或已停用")
            published = await evidence(request, grant.name, grant.version)
            if not published.published or role not in published.roles:
                raise HTTPException(422, "所选 MCP Server 版本未对该原生角色发布")
            tools = {tool.name: tool for tool in published.tools}
            for name in grant.allowed_tools or ():
                alias = native_tool_alias(grant.name, name)
                if alias in selected or name not in tools:
                    raise HTTPException(422, "请选择该版本已发布的明确 MCP 工具")
                selected[alias] = FrozenNativeMcpTool(
                    authority=settings.pi_model_authority,
                    name=grant.name,
                    version=grant.version,
                    tool=tools[name],
                ).model_dump(mode="json")
        if selected:
            output[role] = selected
    if len(json.dumps(output, ensure_ascii=False).encode()) > MAX_CATALOG:
        raise HTTPException(422, "所选 MCP 工具定义超过原生任务限制")
    return output


async def freeze_native_grants(request: Request, grants: NativeMcpGrants) -> dict:
    return await _freeze_role_grants(
        request, {role: getattr(grants, role) for role in ("lead", "teammate")}, latest_only=True
    )


async def freeze_board_grants(request: Request, profile) -> dict:
    await validate_profile_tools(request, profile)
    explore = profile.worker_tools.get("explore")
    if explore is None or not explore.mcp_servers:
        return {}
    return await _freeze_role_grants(request, {"explore": explore.mcp_servers}, latest_only=False)


async def frozen_native_material(application, grant: FrozenNativeMcpTool) -> dict:
    registry = application.state.pi_registry
    if not registry.enabled or grant.authority != registry.settings.pi_model_authority:
        raise ValueError("The frozen MCP authority changed")
    return await local_material(application.state.platform_store, grant.name, grant.version)


async def invoke_native_tool(
    application, grant: FrozenNativeMcpTool, role: str, arguments: dict, before_send
) -> dict:
    material = await frozen_native_material(application, grant)
    approved = material["published"]
    if (
        material["name"] != grant.name
        or material["version"] != grant.version
        or role not in approved["roles"]
        or grant.tool.model_dump(mode="json") not in approved["tools"]
        or material["auth_header"].lower() not in AUTH_HEADERS
    ):
        raise ValueError("Frozen MCP material changed")
    schema = grant.tool.input_schema
    validator_for(schema)(schema, registry=Registry()).validate(arguments)
    secret = material["secret"]
    headers = (
        {material["auth_header"]: f"{material['auth_scheme']} {secret}".strip()} if secret else {}
    )
    url, _ = normalized_url(material["url"])
    async with approved_session(url, headers) as session:
        tools = await list_approved_tools(session, secret)
        if not any(
            tool.name == grant.tool.name and tool.schema_digest == grant.tool.schema_digest
            for tool in tools
        ):
            raise ValueError("Published MCP tool schema changed")
        await before_send()
        result = await session.call_tool(grant.tool.name, arguments)
        if result.isError:
            raise ValueError("External MCP did not confirm a successful result")
        payload = types.CallToolResult(
            content=result.content, structuredContent=result.structuredContent, isError=False
        ).model_dump(mode="json", by_alias=True, exclude_none=True)
        if contains_secret(payload, secret):
            raise ValueError("External MCP returned protected credential data")
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > MAX_CATALOG:
            raise ValueError("External MCP tool result exceeds its byte bound")
        return payload
