"""One scoped ordinary, progress or review model send through AgentGateway."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from bbx_contracts.gateway_models import (
    GATEWAY_EXECUTION_ATTRIBUTE_HEADERS,
    GatewayExecutionGrant,
    GatewayModelRef,
)
from pydantic import ValidationError

from bbx_runtime.clients import BlackboardClient

Purpose = Literal["ordinary", "progress", "review"]
Proof = dict[str, Any] | Callable[[], dict[str, Any]]


def gateway_origin(value: str | None) -> str:
    if value is None:
        raise ValueError("AgentGateway model data origin is unavailable")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port == 0
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Invalid AgentGateway model data origin")
    return value.rstrip("/")


@dataclass(frozen=True)
class GatewayDispatch:
    service: BlackboardClient
    task_id: str
    purpose: Purpose
    ref: GatewayModelRef
    origin: str
    proof: Proof

    @property
    def path(self) -> str:
        return self.service._task(self.task_id) + "/gateway-model"

    @property
    def url(self) -> str:
        return self.origin + "/v1/chat/completions"

    async def claim(self, attempt_id: UUID, body_sha256: str) -> dict:
        proof = self.proof() if callable(self.proof) else self.proof
        try:
            value = GatewayExecutionGrant.model_validate(
                await self.service._json(
                    "POST",
                    f"{self.path}/{self.purpose}/claim",
                    json={
                        **proof,
                        **self.ref.model_dump(mode="json"),
                        "attempt_id": str(attempt_id),
                        "body_sha256": body_sha256,
                    },
                )
            )
        except ValidationError:
            raise ValueError("AgentGateway dispatch grant did not match") from None
        agent = "__progress__" if self.purpose == "progress" else proof["agent_id"]
        conversation = {
            "ordinary": lambda: agent,
            "progress": lambda: proof["job_id"],
            "review": lambda: proof.get("message_id") or proof["turn_id"],
        }[self.purpose]()
        site = value.headers.get(GATEWAY_EXECUTION_ATTRIBUTE_HEADERS["bbx_site_id"])
        attributes = {
            "bbx_site_id": site,
            "bbx_task_id": self.task_id,
            "bbx_agent_id": agent,
            "bbx_attempt_id": str(attempt_id),
            "bbx_purpose": self.purpose,
            "bbx_conversation_id": conversation,
            "bbx_run_number": str(proof["run_number"]),
            "bbx_model_alias": self.ref.alias,
        }
        if self.purpose == "progress":
            attributes["bbx_reservation_id"] = proof["reservation_id"]
        expected = {
            GATEWAY_EXECUTION_ATTRIBUTE_HEADERS[name]: value for name, value in attributes.items()
        }
        if (
            gateway_origin(value.gateway_url) != self.origin
            or value.attempt_id != attempt_id
            or value.body_sha256 != body_sha256
            or not site
            or value.headers != expected
            or not value.key
            or len(value.key) > 1024
        ):
            raise ValueError("AgentGateway dispatch grant did not match")
        return {
            "key": value.key,
            "headers": value.headers,
            "body_sha256": body_sha256,
        }

    async def result(self, attempt_id: UUID, body_sha256: str, status_code: int | None) -> None:
        await self.service._json(
            "POST",
            f"{self.path}/attempts/{attempt_id}/result",
            json={
                "body_sha256": body_sha256,
                "outcome": "complete" if status_code is not None else "unknown",
                **({"status_code": status_code} if status_code is not None else {}),
            },
        )
