"""Trusted registration requests for the persistent Runner control plane."""

from uuid import UUID

from pydantic import AwareDatetime, Field

from .models import ContractModel
from .runner import Digest, EffectiveRunnerModel, RunnerId


class RunnerOwnerRegistration(ContractModel):
    request_id: UUID
    runner_id: RunnerId
    pod_uid: RunnerId
    expected_epoch: int = Field(ge=0)
    hard_deadline: AwareDatetime | None = None


class RunnerOwnerUpdate(ContractModel):
    expected_epoch: int = Field(ge=1)


class RunnerBindingRegistration(ContractModel):
    request_id: UUID
    expected_epoch: int = Field(ge=1)
    agent_id: RunnerId
    challenge_id: UUID | None = None
    model: EffectiveRunnerModel
    model_credential_ref: RunnerId
    mcp_credential_ref: RunnerId
    allowed_tools: list[RunnerId] = Field(default_factory=list, max_length=128)
    instructions: str = Field(max_length=131072)
    profile_revision: int = Field(ge=1)
    prompt_revision: int = Field(ge=1)


class RunnerCommandSubmission(ContractModel):
    request_id: RunnerId
    expected_epoch: int = Field(ge=1)
    payload_json: str = Field(min_length=1, max_length=2097152)
    payload_digest: Digest


class RunnerPromptPublication(ContractModel):
    expected_epoch: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    instructions: str = Field(max_length=131072)
