"""Versioned MCP discovery evidence, distinct from explicit publication grants."""

import hashlib
import ipaddress
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .models import ContractModel
from .runner import Digest, RunnerId

McpRole = Literal["explore", "lead", "teammate"]
McpServerName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
McpToolName = Annotated[str, Field(min_length=1, max_length=256)]

TRANSLATION_RANGES = tuple(
    ipaddress.IPv6Network(value) for value in ("64:ff9b::/96", "64:ff9b:1::/48")
)


_EXTERNAL_SDK = ContextVar("bbx_external_mcp_sdk", default=False)


def _safe_sdk_log(_record: logging.LogRecord) -> bool:
    return not _EXTERNAL_SDK.get()


for _sdk_logger in (
    "mcp.client.streamable_http",
    "client",  # Pinned MCP SDK client session logger.
    "httpcore.http11",
    "httpcore.http2",
):
    logging.getLogger(_sdk_logger).addFilter(_safe_sdk_log)


@contextmanager
def quiet_external_sdk():
    """Keep untrusted raw wire diagnostics out of logs for this external session."""
    token = _EXTERNAL_SDK.set(True)
    try:
        yield
    finally:
        _EXTERNAL_SDK.reset(token)


def public_mcp_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return (
        address.is_global
        and not address.is_multicast
        and not (
            isinstance(address, ipaddress.IPv6Address)
            and (
                address.ipv4_mapped is not None
                or address.sixtofour is not None
                or address.teredo is not None
                or any(address in network for network in TRANSLATION_RANGES)
            )
        )
    )


def safe_tool_json(value: Any, depth: int = 0) -> bool:
    if depth > 24:
        return False
    if isinstance(value, str):
        return "\x00" not in value
    if isinstance(value, list):
        return all(safe_tool_json(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return all(
            "\x00" not in key
            and (
                key not in {"$ref", "$dynamicRef", "$recursiveRef"}
                or isinstance(item, str)
                and item.startswith("#")
            )
            and safe_tool_json(item, depth + 1)
            for key, item in value.items()
        )
    return True


def canonical_digest(value: Any) -> str:
    data = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def tool_schema_digest(input_schema: dict, output_schema: dict | None) -> str:
    return canonical_digest({"input_schema": input_schema, "output_schema": output_schema})


class McpToolDetail(ContractModel):
    name: McpToolName
    description: str = Field(default="", max_length=12000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    schema_digest: Digest

    @model_validator(mode="after")
    def safe_schema(self):
        if not safe_tool_json(self.input_schema) or not safe_tool_json(self.output_schema):
            raise ValueError("MCP schemas require bounded local references")
        if self.schema_digest != tool_schema_digest(self.input_schema, self.output_schema):
            raise ValueError("MCP schema digest changed")
        return self


class McpDiscovery(ContractModel):
    status: Literal["untested", "ready", "error"]
    version: int = Field(ge=1)
    published: bool = False
    roles: list[McpRole] = Field(default_factory=list, max_length=3)
    discovery_digest: Digest | None = None
    checked_at: datetime | None = None
    tools: list[McpToolDetail] = Field(default_factory=list, max_length=128)
    error: str | None = None


class McpPublish(ContractModel):
    discovery_digest: Digest
    tools: list[McpToolName] = Field(min_length=1, max_length=128)
    roles: list[McpRole] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_tools(self):
        if len(set(self.tools)) != len(self.tools) or len(set(self.roles)) != len(self.roles):
            raise ValueError("MCP tool and role grants must be distinct")
        return self


class NativeMcpGrant(ContractModel):
    name: McpServerName
    version: int = Field(ge=1, strict=True)
    allowed_tools: list[McpToolName] = Field(min_length=1, max_length=32)


class NativeMcpGrants(ContractModel):
    lead: list[NativeMcpGrant] = Field(default_factory=list, max_length=8)
    teammate: list[NativeMcpGrant] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def distinct_grants(self):
        for group in (self.lead, self.teammate):
            if (
                len({grant.name for grant in group}) != len(group)
                or sum(len(grant.allowed_tools) for grant in group) > 32
            ):
                raise ValueError("A Native role can grant at most 32 tools on distinct Servers")
            if any(len(set(grant.allowed_tools)) != len(grant.allowed_tools) for grant in group):
                raise ValueError("Native role tool grants must be distinct")
        return self


class FrozenNativeMcpTool(ContractModel):
    authority: str
    name: McpServerName
    version: int = Field(ge=1, strict=True)
    tool: McpToolDetail


class ExternalMcpCheckpoint(ContractModel):
    attempt_id: UUID
    call_id: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")


class SandboxMcpAuthorize(ContractModel):
    method: Literal[
        "initialize",
        "notifications/initialized",
        "notifications/cancelled",
        "tools/list",
        "tools/call",
        "ping",
    ]
    sdk_session_id: RunnerId
