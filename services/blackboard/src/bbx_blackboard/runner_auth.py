"""Runner control credentials never become a platform Principal."""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from bbx_contracts.runner import RunnerFence
from fastapi import HTTPException, Request

from bbx_blackboard.settings import Settings


@dataclass(frozen=True)
class RunnerIdentity:
    fence: RunnerFence
    token_version: str


def _key(settings: Settings) -> bytes:
    return hmac.new(
        settings.agent_token_secret.get_secret_value().encode(),
        b"bbx-runner-control-v1",
        hashlib.sha256,
    ).digest()


def runner_credential_expiry(owner: dict) -> int:
    return int(min(owner["hard_deadline"], datetime.now(UTC) + timedelta(minutes=5)).timestamp())


def issue_runner_control_token(
    settings: Settings, owner: dict, *, expires_at: int | None = None
) -> str:
    return jwt.encode(
        {
            "aud": "runner-control",
            "iss": "bbx-runner-control-v1",
            "tid": str(owner["task_id"]),
            "runner_id": owner["runner_id"],
            "pod_uid": owner["pod_uid"],
            "epoch": owner["epoch"],
            "token_version": owner["token_version"],
            "exp": expires_at if expires_at is not None else runner_credential_expiry(owner),
        },
        _key(settings),
        algorithm="HS256",
    )


def require_runner_control(request: Request, task_id: UUID) -> RunnerIdentity:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Runner control token required")
    try:
        claims = jwt.decode(
            token,
            _key(request.app.state.settings),
            algorithms=["HS256"],
            audience="runner-control",
            issuer="bbx-runner-control-v1",
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
                ]
            },
        )
        if claims["aud"] != "runner-control" or not isinstance(claims["token_version"], str):
            raise ValueError("Invalid Runner credential")
        fence = RunnerFence(
            task_id=claims["tid"],
            runner_id=claims["runner_id"],
            pod_uid=claims["pod_uid"],
            epoch=claims["epoch"],
        )
        if UUID(fence.task_id) != task_id:
            raise HTTPException(403, "Runner task access denied")
        return RunnerIdentity(fence, claims["token_version"])
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError) as error:
        raise HTTPException(401, "Invalid or expired Runner credential") from error
