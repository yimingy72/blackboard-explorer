"""Trusted registration requests for the persistent Runner control plane."""

from datetime import UTC
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from .models import ContractModel
from .runner import Digest, EffectiveRunnerModel, JournalCursor, RunnerId


class RunnerOwnerRegistration(ContractModel):
    request_id: UUID
    runner_id: RunnerId
    pod_uid: RunnerId
    expected_epoch: int = Field(ge=0)
    hard_deadline: AwareDatetime | None = None


class RunnerOwnerUpdate(ContractModel):
    expected_epoch: int = Field(ge=1)


class BoardRunIdentity(ContractModel):
    run_number: int = Field(ge=1)
    started_at: AwareDatetime
    derive_round: int | None = Field(default=None, ge=1)

    @field_validator("started_at")
    @classmethod
    def canonical_start(cls, value: AwareDatetime):
        return value.astimezone(UTC)


class BoardTraceProof(ContractModel):
    binding_id: RunnerId
    sdk_session_id: RunnerId
    cursor: JournalCursor
    entry_id: RunnerId
    run_number: int = Field(ge=1)


class BoardJournalProjection(ContractModel):
    format: Literal["pi-journal-v1"]
    binding_id: RunnerId
    sdk_session_id: RunnerId
    cursor: JournalCursor
    run_number: int = Field(ge=1)
    transcript_uri: str = Field(min_length=1, max_length=512)
    sha256: Digest
    size: int = Field(ge=1, le=64 * 1024 * 1024)


class BoardDispatchProof(BoardRunIdentity):
    agent_id: RunnerId
    instructions: str = Field(min_length=1, max_length=131072)
    input: str = Field(min_length=1, max_length=20000)


class BoardAbortProof(BoardRunIdentity):
    agent_id: RunnerId
    reason: str = Field(min_length=1, max_length=128)


class RunnerBindingRegistration(ContractModel):
    request_id: UUID
    expected_epoch: int = Field(ge=1)
    agent_id: RunnerId
    challenge_id: UUID | None = None
    board_run: BoardRunIdentity | None = Field(default=None, exclude_if=lambda value: value is None)
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
