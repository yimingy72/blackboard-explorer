"""Per-binding model/MCP capabilities, separate from control and service identity."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import jwt
from bbx_contracts.runner import RunnerFence
from bbx_contracts.runner_bootstrap import native_resume_pending
from fastapi import HTTPException, Request

from bbx_blackboard.runner_auth import RunnerIdentity
from bbx_blackboard.runner_control import RunnerControl

DIRECT_MCP = {
    "business": ("business-mcp", "bbx-runner-business-v2"),
    "sandbox": ("sandbox-mcp", "bbx-runner-sandbox-v2"),
}
DIRECT_ISSUERS = {audience: issuer for audience, issuer in DIRECT_MCP.values()}
MCP_AUDIENCES = frozenset(DIRECT_ISSUERS)


@dataclass(frozen=True)
class GatewayIdentity:
    fence: RunnerFence
    binding_id: str
    token_version: str
    audience: Literal["model", "business-mcp", "sandbox-mcp"]
    expires_at: int
    role: Literal["lead", "teammate", "helper"] | None = None


@dataclass(frozen=True)
class RuntimeBusinessContext:
    identity: GatewayIdentity
    sdk_session_id: str
    journal: dict


def _key(settings):
    return hmac.new(
        settings.agent_token_secret.get_secret_value().encode(),
        b"bbx-runner-gateway-v1",
        hashlib.sha256,
    ).digest()


def issue_gateway_credentials(settings, owner, binding_id, role, *, targets, deadline_ms=None):
    if role not in {"lead", "teammate", "helper"}:
        raise HTTPException(403, "Runner role is not ready")
    expiry = int(min(owner["hard_deadline"], datetime.now(UTC) + timedelta(minutes=5)).timestamp())
    if deadline_ms is not None:
        expiry = min(expiry, deadline_ms // 1000)
    result = {}
    issued = {"model": ("model", "bbx-runner-gateway-v1")}
    if not targets or not set(targets) <= DIRECT_MCP.keys():
        raise HTTPException(403, "Runner needs a frozen MCP target")
    if role == "helper" and set(targets) != {"sandbox"}:
        raise HTTPException(403, "Helper is limited to its Sandbox target")
    issued.update({target: DIRECT_MCP[target] for target in targets})
    for name, (audience, issuer) in issued.items():
        token = jwt.encode(
            {
                "aud": audience,
                "iss": issuer,
                "tid": str(owner["task_id"]),
                "runner_id": owner["runner_id"],
                "pod_uid": owner["pod_uid"],
                "epoch": owner["epoch"],
                "token_version": owner["token_version"],
                "bid": binding_id,
                "exp": expiry,
                **({"role": role} if audience in MCP_AUDIENCES else {}),
            },
            scoped_key(settings, audience),
            algorithm="HS256",
        )
        result[name] = {
            "audience": audience,
            "task_id": str(owner["task_id"]),
            "binding_id": binding_id,
            "expires_at": expiry,
            "token": token,
        }
    return result


def scoped_key(settings, audience: str) -> bytes:
    key = _key(settings)
    return hmac.digest(key, audience.encode(), "sha256") if audience in DIRECT_ISSUERS else key


async def task_direct_mcp(conn, tid: UUID) -> bool:
    from sqlalchemy import select

    from bbx_blackboard.store import schema as s

    config = await conn.scalar(
        select(s.task_runner_resources.c.data).where(s.task_runner_resources.c.task_id == tid)
    )
    return isinstance(config, dict) and config.get("mcp_transport") == "direct-v2"


async def require_mcp_transport(conn, tid: UUID, identity: GatewayIdentity) -> None:
    if identity.audience in MCP_AUDIENCES and not await task_direct_mcp(conn, tid):
        raise HTTPException(403, "MCP capability does not match the frozen transport")


def mcp_identity(request: Request, task_id: UUID, *, parent_only: bool = False):
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise HTTPException(401, "One MCP capability is required")
    try:
        scheme, _, token = values[0].partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ValueError("Missing capability")
        audience = jwt.decode(token, options={"verify_signature": False}).get("aud")
        allowed = {"business-mcp"} if parent_only else MCP_AUDIENCES
        if not isinstance(audience, str) or audience not in allowed:
            raise ValueError("Unexpected MCP audience")
    except (jwt.InvalidTokenError, ValueError) as error:
        raise HTTPException(401, "MCP capability audience denied") from error
    return require_gateway_identity(request, task_id, audience)


def require_gateway_identity(request: Request, task_id: UUID, audience, *, authorization=None):
    values = request.headers.getlist("authorization")
    if authorization is None and len(values) != 1:
        raise HTTPException(401, "One Runner capability is required")
    scheme, _, token = (authorization if authorization is not None else values[0]).partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Gateway capability required")
    try:
        if audience not in {"model", *MCP_AUDIENCES}:
            raise ValueError("Unexpected Runner capability")
        claims = jwt.decode(
            token,
            scoped_key(request.app.state.settings, audience),
            algorithms=["HS256"],
            audience=audience,
            issuer=DIRECT_ISSUERS.get(audience, "bbx-runner-gateway-v1"),
            options={
                "require": [
                    "aud",
                    "iss",
                    "exp",
                    "tid",
                    "runner_id",
                    "pod_uid",
                    "epoch",
                    "token_version",
                    "bid",
                ]
            },
        )
        if (
            claims["aud"] != audience
            or not isinstance(claims["bid"], str)
            or not isinstance(claims["token_version"], str)
            or (
                audience in MCP_AUDIENCES
                and claims.get("role") not in {"lead", "teammate", "helper"}
            )
        ):
            raise ValueError("Invalid capability scope")
        fence = RunnerFence(
            task_id=claims["tid"],
            runner_id=claims["runner_id"],
            pod_uid=claims["pod_uid"],
            epoch=claims["epoch"],
        )
        if UUID(fence.task_id) != task_id:
            raise HTTPException(403, "Gateway task scope denied")
        return GatewayIdentity(
            fence,
            claims["bid"],
            claims["token_version"],
            audience,
            claims["exp"],
            claims.get("role"),
        )
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(401, "Invalid or expired gateway capability") from error


async def validate_runtime_context(conn, tid, context):
    """Run inside the real domain transaction, after taking its Task lock."""
    control = RunnerControl(conn.engine)
    identity = context.identity
    owner = await control._identity(
        conn, tid, RunnerIdentity(identity.fence, identity.token_version), live=True
    )
    binding = await control._binding(conn, tid, identity.binding_id, owner, live=True, permit=True)
    await validate_gateway_role(conn, tid, identity, binding)
    if (
        identity.expires_at <= datetime.now(UTC).timestamp()
        or binding["sdk_session_id"] is None
        or binding["sdk_session_id"] != context.sdk_session_id
        or not await control._confirmed_cursor(conn, tid, binding, context.journal)
    ):
        raise HTTPException(403, "Runtime business context is no longer authorized")
    task = await control._task(conn, tid, live=True)
    if task["ctf_control"].get("failure_hold") or native_resume_pending(task):
        raise HTTPException(409, "Task admission is paused")
    return binding


async def validate_gateway_role(conn, tid, identity, binding):
    if identity.audience in MCP_AUDIENCES:
        await require_mcp_transport(conn, tid, identity)
        from bbx_blackboard.runner_control import row
        from bbx_blackboard.store import schema as s

        member = await row(conn, s.ctf_members, tid, binding["binding"]["agent_id"])
        expected = "helper" if binding["business"].get("native_child_id") else member["role"]
        if identity.role != expected:
            raise HTTPException(403, "MCP role scope changed")
