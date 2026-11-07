"""Per-binding model/MCP capabilities, separate from control and service identity."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Literal
from uuid import UUID

import jwt
from bbx_contracts.runner import RunnerFence
from bbx_contracts.runner_bootstrap import native_pause_state
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request
from jwt.algorithms import RSAAlgorithm

from bbx_blackboard.runner_auth import RunnerIdentity
from bbx_blackboard.runner_control import RunnerControl


@dataclass(frozen=True)
class GatewayIdentity:
    fence: RunnerFence
    binding_id: str
    token_version: str
    audience: Literal["model", "mcp"]
    expires_at: int
    role: Literal["lead", "teammate"] | None = None


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


@lru_cache(maxsize=2)
def _mcp_material(raw):
    if not raw:
        raise HTTPException(503, "MCP signing identity is unavailable")
    try:
        key = serialization.load_pem_private_key(raw.encode(), password=None)
    except (TypeError, ValueError) as error:
        raise HTTPException(503, "MCP signing identity is invalid") from error
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise HTTPException(503, "MCP signing identity requires an RSA key of at least 2048 bits")
    public = key.public_key()
    encoded = public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    jwk = {
        **json.loads(RSAAlgorithm.to_jwk(public)),
        "kid": hashlib.sha256(encoded).hexdigest(),
        "alg": "RS256",
        "use": "sig",
    }
    return key, public, jwk


def mcp_public_jwks(settings):
    return {"keys": [dict(_mcp_material(settings.runner_mcp_signing_key.get_secret_value())[2])]}


def issue_gateway_credentials(settings, owner, binding_id, role):
    if role not in {"lead", "teammate"}:
        raise HTTPException(403, "Runner role is not ready")
    mcp_key, _, jwk = _mcp_material(settings.runner_mcp_signing_key.get_secret_value())
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
                **({"role": role} if audience == "mcp" else {}),
            },
            mcp_key if audience == "mcp" else _key(settings),
            algorithm="RS256" if audience == "mcp" else "HS256",
            headers={"kid": jwk["kid"]} if audience == "mcp" else None,
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
        key = _key(request.app.state.settings)
        algorithm = "HS256"
        if audience == "mcp":
            algorithm = "RS256"
            _, key, jwk = _mcp_material(
                request.app.state.settings.runner_mcp_signing_key.get_secret_value()
            )
            if jwt.get_unverified_header(token).get("kid") != jwk["kid"]:
                raise ValueError("Invalid MCP key identity")
        claims = jwt.decode(
            token,
            key,
            algorithms=[algorithm],
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
            or (audience == "mcp" and claims.get("role") not in {"lead", "teammate"})
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
    if task["ctf_control"].get("failure_hold") or native_pause_state(task) == "resuming":
        raise HTTPException(409, "Task admission is paused")
    return binding


async def validate_gateway_role(conn, tid, identity, binding):
    if identity.audience == "mcp":
        from bbx_blackboard.runner_control import row
        from bbx_blackboard.store import schema as s

        member = await row(conn, s.ctf_members, tid, binding["binding"]["agent_id"])
        if identity.role != member["role"]:
            raise HTTPException(403, "MCP role scope changed")
