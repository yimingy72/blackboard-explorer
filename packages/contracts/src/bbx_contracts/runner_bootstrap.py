"""Trusted Task resource bootstrap requests, separate from Runner capabilities."""

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from .models import ContractModel
from .native_gateway import FrozenExchangeRate
from .runner import RunnerId

RUNTIME_LOCK_KEY = 0x42425852554E
RUNTIME_FENCE_SEQUENCE = "native_runtime_controller_fence"


class NativeRunnerLaunch(ContractModel):
    request_id: UUID
    model_alias: str = Field(min_length=1, max_length=512)
    exchange_rate: FrozenExchangeRate


class NativeRunnerClaim(ContractModel):
    controller_id: RunnerId
    controller_fence: int = Field(ge=1)
    controller_pid: int = Field(ge=1)
    controller_started_at: AwareDatetime
    resource_config: dict[str, Any] = Field(max_length=32)


class NativeRunnerAction(ContractModel):
    operation_id: UUID
    controller_id: RunnerId
    controller_generation: int = Field(ge=1)
    action: Literal[
        "sandbox_create", "observation", "material", "activate", "renew", "stop", "cleanup", "error"
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
