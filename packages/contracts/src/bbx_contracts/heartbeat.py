"""Confirmed ordinary model usage and backward-compatible heartbeat request fields."""

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from bbx_contracts.models import AgentTaskType, ContractModel, Usage

USAGE_RECEIPT_PROTOCOL = 1
UNKNOWN_USAGE_WARNING = "模型服务未报告完整用量；当前费用只包含已报告的 token，费用不完整。"


def usage_receipt_support(session: dict[str, Any]) -> dict[str, int]:
    return (
        {"usage_receipt_protocol": USAGE_RECEIPT_PROTOCOL}
        if session.get("state", {}).get("bbx_usage_outbox")
        else {}
    )


def canonical_task_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        return value


class HeartbeatRequest(ContractModel):
    requested_at: AwareDatetime | None = None
    expected_derive_round: int | None = Field(default=None, ge=1)
    expected_started_at: AwareDatetime | None = None
    steps: int = Field(ge=0)
    context_tokens: int = Field(ge=0)
    usage: Usage
    usage_known: bool = True
    last_seen_version: int = Field(ge=0)
    request_id: UUID | None = None
    expected_run_number: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def receipt_scope(self):
        if self.request_id is not None and (
            self.expected_run_number is None or self.requested_at is None
        ):
            raise ValueError("Usage receipts require the original run and request time")
        return self


class OrdinaryUsageReceipt(ContractModel):
    request: HeartbeatRequest
    task_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    task_type: AgentTaskType
    profile_name: str = Field(min_length=1)
    profile_version: int = Field(ge=1)
    billing_mode: Literal["fixed", "deepseek_schedule"] | None = None

    @field_validator("task_id")
    @classmethod
    def task_identity(cls, value: str) -> str:
        return canonical_task_id(value)

    @model_validator(mode="after")
    def confirmed_call(self):
        if self.request.request_id is None or self.request.steps != 1:
            raise ValueError("A model usage receipt requires one identified successful call")
        if self.task_type == AgentTaskType.DERIVE and self.request.expected_derive_round is None:
            raise ValueError("A derive usage receipt requires its original round")
        return self


class ReceiptHeartbeatRequest(ContractModel):
    receipt: HeartbeatRequest

    @model_validator(mode="after")
    def identified_receipt(self):
        if self.receipt.request_id is None:
            raise ValueError("Receipt heartbeat requires an identified call")
        return self


def heartbeat_digest(task_id: str, agent_id: str, payload: dict[str, Any]) -> str:
    task_id = canonical_task_id(task_id)
    request = HeartbeatRequest.model_validate(payload).model_dump(mode="json")
    if request["expected_started_at"] is None:
        request.pop("expected_started_at")
    return hashlib.sha256(
        json.dumps(
            {"task_id": task_id, "agent_id": agent_id, "request": request},
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
