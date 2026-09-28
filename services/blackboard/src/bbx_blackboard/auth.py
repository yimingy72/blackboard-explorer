"""HTTP identities and task-scoped authorization."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import jwt
from fastapi import HTTPException, Request

from bbx_blackboard.settings import Settings

COOKIE_NAME = "bbx_session"


def _equals(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except UnicodeError:
        return False


@dataclass(frozen=True)
class Principal:
    kind: Literal["service", "agent", "user"]
    name: str
    task_id: UUID | None = None
    agent_id: str | None = None
    derive_round: int | None = None


def issue_agent_token(
    settings: Settings, task_id: UUID, agent_id: str, derive_round: int | None = None
) -> str:
    return jwt.encode(
        {
            "aud": "agent",
            "tid": str(task_id),
            "aid": agent_id,
            **({"derive_round": derive_round} if derive_round is not None else {}),
            "exp": datetime.now(UTC) + timedelta(days=7),
        },
        settings.agent_token_secret.get_secret_value(),
        algorithm="HS256",
    )


def issue_user_token(settings: Settings, username: str) -> str:
    return jwt.encode(
        {
            "aud": "user",
            "sub": username,
            "exp": datetime.now(UTC) + timedelta(hours=12),
        },
        settings.agent_token_secret.get_secret_value(),
        algorithm="HS256",
    )


def check_user_password(settings: Settings, username: str, password: str) -> bool:
    for entry in settings.admin_users.get_secret_value().split(","):
        name, separator, secret = entry.partition(":")
        if separator and _equals(name, username) and _equals(secret, password):
            return True
    return False


def principal(request: Request) -> Principal:
    settings: Settings = request.app.state.settings
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "Invalid bearer token")
        if _equals(token, settings.service_token.get_secret_value()):
            return Principal("service", "scheduler")
    else:
        token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        raise HTTPException(401, "Authentication required")
    try:
        claims = jwt.decode(
            token,
            settings.agent_token_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=["agent", "user"],
            options={"require": ["aud", "exp"]},
        )
        if claims["aud"] == "agent":
            return Principal(
                "agent",
                claims["aid"],
                task_id=UUID(claims["tid"]),
                agent_id=claims["aid"],
                derive_round=int(claims["derive_round"]) if "derive_round" in claims else None,
            )
        if claims["aud"] == "user":
            return Principal("user", claims["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError, UnicodeError) as exc:
        raise HTTPException(401, "Invalid or expired token") from exc
    raise HTTPException(401, "Invalid token audience")


def require_task_reader(request: Request, task_id: UUID) -> Principal:
    identity = principal(request)
    if identity.kind == "agent" and identity.task_id != task_id:
        raise HTTPException(403, "Task access denied")
    return identity


def require_agent_writer(request: Request, task_id: UUID) -> Principal:
    identity = require_task_reader(request, task_id)
    if identity.kind != "agent":
        raise HTTPException(403, "Agent token required")
    return identity


def require_service(request: Request) -> Principal:
    identity = principal(request)
    if identity.kind != "service":
        raise HTTPException(403, "Service token required")
    return identity


def require_user_or_service(request: Request) -> Principal:
    identity = principal(request)
    if identity.kind == "agent":
        raise HTTPException(403, "User or service token required")
    return identity
