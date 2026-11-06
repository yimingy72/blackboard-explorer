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
from fastapi import HTTPException, Request

from bbx_blackboard.runner_auth import RunnerIdentity
from bbx_blackboard.runner_control import RunnerControl


@dataclass(frozen=True)
class GatewayIdentity:
    fence: RunnerFence
    binding_id: str
    token_version: str
    audience: Literal["model", "mcp"]
    expires_at: int


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


def issue_gateway_credentials(settings, owner, binding_id):
    expiry = int(min(owner["hard_deadline"], datetime.now(UTC) + timedelta(minutes=5)).timestamp())
    result = {}
    for audience in ("model", "mcp"):
        token = jwt.encode(
            {
                "aud": audience,
                "iss": "bbx-runner-gateway-v1",
                "tid": str(owner["task_id"]),
                "runner_id": owner["runner_id"],
                "pod_uid": owner["pod_uid"],
                "epoch": owner["epoch"],
                "token_version": owner["token_version"],
                "bid": binding_id,
                "exp": expiry,
            },
            _key(settings),
            algorithm="HS256",
        )
        result[audience] = {
            "audience": audience,
            "task_id": str(owner["task_id"]),
            "binding_id": binding_id,
            "expires_at": expiry,
            "token": token,
        }
    return result


def require_gateway_identity(request: Request, task_id: UUID, audience, *, authorization=None):
    scheme, _, token = (authorization or request.headers.get("authorization", "")).partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Gateway capability required")
    try:
        claims = jwt.decode(
            token,
            _key(request.app.state.settings),
            algorithms=["HS256"],
            audience=audience,
            issuer="bbx-runner-gateway-v1",
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
            fence, claims["bid"], claims["token_version"], audience, claims["exp"]
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
    if (
        identity.expires_at <= datetime.now(UTC).timestamp()
        or binding["sdk_session_id"] is None
        or binding["sdk_session_id"] != context.sdk_session_id
        or not await control._confirmed_cursor(conn, tid, binding, context.journal)
    ):
        raise HTTPException(403, "Runtime business context is no longer authorized")
    task = await control._task(conn, tid, live=True)
    if task["ctf_control"].get("failure_hold"):
        raise HTTPException(409, "Task admission is paused")
    return binding
