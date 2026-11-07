"""Trusted Task resource bootstrap requests, separate from Runner capabilities."""

import hashlib
import json
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from .models import ContractModel
from .native_gateway import FrozenExchangeRate
from .runner import JournalCursor, RunnerId

RUNTIME_LOCK_KEY = 0x42425852554E
RUNTIME_FENCE_SEQUENCE = "native_runtime_controller_fence"


def retirement_manifest_digest(manifest: dict) -> str:
    """Hash management state semantically; Runner invocation digests still cover original bytes."""
    return hashlib.sha256(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class NativeRunnerLaunch(ContractModel):
    request_id: UUID
    model_alias: str = Field(min_length=1, max_length=512)
    exchange_rate: FrozenExchangeRate


def native_pause_state(task):
    return ((task.get("ctf_control") or {}).get("native_pause") or {}).get("state")


def native_recovery_frozen(task):
    return bool((task.get("ctf_control") or {}).get("native_recovery"))


class NativePauseRequest(ContractModel):
    request_id: UUID


class NativeResumeRequest(ContractModel):
    request_id: UUID
    pause_id: UUID
    instruction: str | None = Field(default=None, min_length=1, max_length=1500)


class NativeBudgetIncreaseRequest(ContractModel):
    request_id: UUID
    pause_id: UUID
    additional_cny: Decimal = Field(gt=0, max_digits=16, decimal_places=9)


class NativeBudgetIncreaseStatus(ContractModel):
    request_id: UUID
    pause_id: UUID
    threshold_cny: Decimal
    threshold_usd: Decimal


class NativePauseStatus(ContractModel):
    pause_id: UUID | None = None
    state: Literal["running", "draining", "paused", "resuming", "resumed", "stopping", "stopped"]
    blocker: str | None = None


class NativeRecoveryBinding(ContractModel):
    binding_id: RunnerId
    agent_id: RunnerId
    state: str
    assignment_id: RunnerId | None = None
    sdk_session_id: RunnerId | None = None
    confirmed_cursor: JournalCursor | None = Field(
        default=None, description="Last database confirmation; journal bytes were not read."
    )
    sdk_start_unconfirmed: bool


class NativeRecoveryStatus(ContractModel):
    """Read-only original evidence, without proof of volume writer exit or recovery readiness."""

    state: Literal["not_frozen", "frozen", "stopping", "stopped"]
    incident_id: UUID | None = None
    operation_id: UUID | None = None
    reason: str | None = None
    captured_at: AwareDatetime | None = None
    original_epoch: int | None = None
    controller_generation: int | None = None
    sandbox_create_claimed: bool = False
    resources: dict[str, str] = Field(default_factory=dict)
    bindings: list[NativeRecoveryBinding] = Field(default_factory=list, max_length=32)
    bindings_truncated: bool = False
    ledger: dict[str, dict[str, int]] = Field(default_factory=dict)


class NativeRunnerClaim(ContractModel):
    controller_id: RunnerId
    controller_fence: int = Field(ge=1)
    controller_pid: int = Field(ge=1)
    controller_started_at: AwareDatetime
    resource_config: dict[str, Any] = Field(max_length=32)
    retirement_single_apiserver: dict[str, str] = Field(default_factory=dict, max_length=2)


class NativeRunnerAction(ContractModel):
    operation_id: UUID
    controller_id: RunnerId
    controller_generation: int = Field(ge=1)
    action: Literal[
        "sandbox_create",
        "observation",
        "material",
        "activate",
        "renew",
        "stop",
        "cleanup",
        "error",
        "retire_prepare",
        "retire_plan",
        "retire_admit",
        "retire_complete",
        "sandbox_host_material",
        "sandbox_host_ready",
    ]
    observation: dict[str, Any] = Field(default_factory=dict, max_length=16)
    proof: dict[str, Any] = Field(default_factory=dict, max_length=8)
    error_code: (
        Literal[
            "native_exit_unconfirmed",
            "native_resource_unknown",
            "native_control_unavailable",
            "native_bootstrap_unavailable",
        ]
        | None
    ) = None
