"""Versioned, task-scoped control and journal receipts for the Pi Task Runner."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal

from pydantic import Field, RootModel, field_validator, model_validator

from bbx_contracts.models import ContractModel

RunnerId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
PROTOCOL = "bbx.task-runner.v1"
MAX_INPUT_JSON_BYTES = 131072
MAX_MODEL_BODY_BYTES = 8 * 1024 * 1024
MAX_MODEL_METADATA_BYTES = 17 * 1024 * 1024


class RunnerFence(ContractModel):
    task_id: RunnerId
    runner_id: RunnerId
    pod_uid: RunnerId
    epoch: int = Field(ge=1, le=9007199254740991)


class JournalCursor(ContractModel):
    offset: int = Field(ge=0, le=9007199254740991)
    seq: int = Field(ge=0, le=9007199254740991)
    hash: Digest
    leaf_id: str | None = None
    anchor_offset: int = Field(ge=0, le=9007199254740991)
    anchor_size: int = Field(ge=0, le=2097152)
    anchor_hash: Digest


class RunnerBinding(ContractModel):
    business_contract: Literal["challenge-board-v1"]
    binding_id: RunnerId
    platform_session_id: RunnerId
    agent_id: RunnerId
    generation: int = Field(
        ge=1,
        description=(
            "Platform binding/assignment identity version, "
            "not legacy CtfMember claim_turn generation"
        ),
    )
    journal_generation: RunnerId
    assignment_ref: str | None


ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class EffectiveRunnerModel(ContractModel):
    id: str = Field(min_length=1, max_length=512)
    api: Literal["openai-completions"]
    input: list[Literal["text", "image"]] = Field(min_length=1, max_length=2)
    reasoning: bool
    thinking_level: ThinkingLevel
    thinking_level_map: dict[ThinkingLevel, str | None] | None = None
    thinking_budgets: dict[str, int] | None = None
    context_window: int = Field(ge=1024)
    max_output_tokens: int = Field(ge=1)

    @model_validator(mode="after")
    def supported_settings(self):
        if "text" not in self.input or len(set(self.input)) != len(self.input):
            raise ValueError("The current route requires distinct modalities including text")
        if not self.reasoning and self.thinking_level != "off":
            raise ValueError("A non-reasoning model cannot select a thinking level")
        if self.thinking_budgets:
            raise ValueError("The current route does not support explicit thinking budgets")
        if self.thinking_level_map and self.thinking_level_map.get(self.thinking_level, "") is None:
            raise ValueError("The selected thinking level is unsupported")
        return self


class HelperChild(ContractModel):
    child_id: RunnerId
    parent_binding_id: RunnerId
    deadline_ms: int = Field(ge=1, le=9007199254740991)


class SessionSpec(ContractModel):
    binding: RunnerBinding
    model: EffectiveRunnerModel
    model_credential_ref: RunnerId
    mcp_credential_ref: RunnerId
    allowed_tools: list[RunnerId] = Field(max_length=128)
    mcp_tool_aliases: dict[RunnerId, RunnerId] = Field(max_length=128)
    instructions: str = Field(max_length=131072)
    profile_revision: int = Field(ge=1)
    prompt_revision: int = Field(ge=1)
    helper_foreground: bool = False
    helper_child: HelperChild | None = None

    @model_validator(mode="after")
    def exact_tool_aliases(self):
        if self.helper_child and self.helper_foreground:
            raise ValueError("Helpers cannot delegate")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("Declared tools must be distinct")
        if set(self.mcp_tool_aliases) != set(self.allowed_tools) or len(
            set(self.mcp_tool_aliases.values())
        ) != len(self.mcp_tool_aliases):
            raise ValueError("MCP aliases must map every declared tool to a distinct wire name")
        return self


class PreparePayload(ContractModel):
    kind: Literal["prepare"]
    spec: SessionSpec


class InputPayload(ContractModel):
    kind: Literal["input"]
    binding_id: RunnerId
    input_id: RunnerId
    content: str = Field(min_length=1, max_length=20000)
    mode: Literal["steer", "follow_up"] = "follow_up"

    @field_validator("content")
    @classmethod
    def encoded_input_bound(cls, value: str) -> str:
        value.encode("utf-8")
        if len(runner_json_text(value).encode("utf-8")) > MAX_INPUT_JSON_BYTES:
            raise ValueError("Input JSON exceeds the encoded byte bound")
        return value


class AbortPayload(ContractModel):
    kind: Literal["abort"]
    binding_id: RunnerId


class ClosePayload(ContractModel):
    kind: Literal["close"]
    binding_id: RunnerId


class ShutdownPayload(ContractModel):
    kind: Literal["shutdown"]


class RunnerPayload(
    RootModel[
        Annotated[
            PreparePayload | InputPayload | AbortPayload | ClosePayload | ShutdownPayload,
            Field(discriminator="kind"),
        ]
    ]
):
    pass


class RunnerCommand(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    request_id: RunnerId
    command_seq: int = Field(ge=1, le=9007199254740991)
    fence: RunnerFence
    payload_digest: Digest
    payload_json: str = Field(min_length=1, max_length=2097152)

    @model_validator(mode="after")
    def exact_payload(self):
        if runner_payload_digest(self.payload_json) != self.payload_digest:
            raise ValueError("Control payload bytes do not match their digest")
        RunnerPayload.model_validate_json(self.payload_json)
        return self

    @property
    def decoded_payload(
        self,
    ) -> PreparePayload | InputPayload | AbortPayload | ClosePayload | ShutdownPayload:
        return RunnerPayload.model_validate_json(self.payload_json).root


class RunnerPoll(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    commands: list[RunnerCommand] = Field(max_length=100)
    lease_renewals: list[SessionLeaseReceipt] = Field(default_factory=list, max_length=100)


class InputApplication(ContractModel):
    input_id: RunnerId
    entry_id: str
    seq: int = Field(ge=1)


class JournalConfirmation(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    request_id: Digest
    fence: RunnerFence
    binding: RunnerBinding
    sdk_session_id: RunnerId
    previous: JournalCursor
    cursor: JournalCursor
    applied_inputs: list[InputApplication]


class JournalReceipt(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    confirmed: Literal[True]
    request_id: Digest
    binding_id: RunnerId
    sdk_session_id: RunnerId
    cursor: JournalCursor


class JournalState(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    binding_id: RunnerId
    sdk_session_id: RunnerId
    cursor: JournalCursor


class SessionLeaseRequest(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    request_id: RunnerId
    payload_digest: Digest
    fence: RunnerFence
    binding: RunnerBinding


class SessionLeaseReceipt(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    confirmed: Literal[True]
    request_id: RunnerId
    fence: RunnerFence
    binding_id: RunnerId
    binding: RunnerBinding
    lease_expires_at: int = Field(ge=1)
    lease_revision: int = Field(ge=1)
    sdk_session_id: RunnerId | None = None
    cursor: JournalCursor | None = None


class ToolInvocation(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    fence: RunnerFence
    binding: RunnerBinding
    sdk_session_id: RunnerId
    invocation_id: Digest
    tool_call_id: str = Field(min_length=1)
    assistant_entry_id: str = Field(min_length=1)
    tool_name: RunnerId
    arguments_digest: Digest
    arguments_json: str
    journal: JournalCursor

    @model_validator(mode="after")
    def exact_arguments(self):
        validate_runner_json_object(self.arguments_json, self.arguments_digest)
        return self


class ToolPermit(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    confirmed: Literal[True]
    invocation_id: Digest
    disposition: Literal["admitted", "completed", "unknown"]
    arguments_digest: Digest
    result_digest: Digest | None = None
    result_json: str | None = None
    authorized_wait_until_ms: int | None = Field(default=None, ge=1, le=9007199254740991)

    @model_validator(mode="after")
    def saved_result(self):
        if self.disposition == "admitted" and self.authorized_wait_until_ms is None:
            raise ValueError("An admitted tool requires a trusted wait deadline")
        if self.disposition == "completed" and self.result_json is None:
            raise ValueError("Completed tools require their preserved result JSON")
        if self.result_json is not None:
            validate_runner_json_object(self.result_json, self.result_digest)
        return self


class ToolResultReceipt(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    confirmed: Literal[True]
    invocation_id: Digest
    result_digest: Digest
    result_json: str

    @model_validator(mode="after")
    def exact_result(self):
        validate_runner_json_object(self.result_json, self.result_digest)
        return self


class ToolResultConfirmation(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    invocation: ToolInvocation


class ToolUnknownSubmission(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    invocation: ToolInvocation


class ModelAttemptRequest(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    fence: RunnerFence
    binding: RunnerBinding
    sdk_session_id: RunnerId
    call_id: RunnerId
    attempt_id: RunnerId
    journal: JournalCursor
    purpose: Literal["turn", "summary"]
    profile_revision: int = Field(ge=1)
    prompt_revision: int = Field(ge=1)
    model_snapshot: EffectiveRunnerModel
    body_json: str = Field(min_length=1, max_length=MAX_MODEL_BODY_BYTES)
    body_digest: Digest

    @model_validator(mode="after")
    def exact_body(self):
        if len(self.body_json.encode("utf-8")) > MAX_MODEL_BODY_BYTES:
            raise ValueError("Model body exceeds its UTF-8 byte bound")
        validate_runner_json_object(self.body_json, self.body_digest)
        return self


class PublishedPrompt(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    fence: RunnerFence
    binding_id: RunnerId
    prompt_revision: int = Field(ge=1)
    instructions: str = Field(max_length=131072)


class ModelAdmissionReceipt(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    confirmed: Literal[True]
    call_id: RunnerId
    attempt_id: RunnerId
    request_digest: Digest


class ModelOutcomeSubmission(ContractModel):
    """SDK observations only; native gateway evidence owns fees and accounting outcomes."""

    protocol: Literal["bbx.task-runner.v1"]
    fence: RunnerFence
    binding_id: RunnerId
    sdk_session_id: RunnerId
    call_id: RunnerId
    attempt_id: RunnerId
    outcome: Literal["completed", "unknown", "not_sent"]
    model_outcome: Literal["completed", "error", "aborted"]
    usage: dict | None
    http_status: int | None


class ModelOutcomeReceipt(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    confirmed: Literal[True]
    attempt_id: RunnerId
    request_digest: Digest


class OperationAck(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    confirmed: Literal[True]
    request_id: RunnerId
    status: str
    receipt_digest: Digest


class CommandReceipt(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    request_id: RunnerId
    payload_digest: Digest
    fence: RunnerFence
    status: Literal["prepared", "accepted", "applied", "aborted", "closed", "rejected"]
    binding_id: RunnerId | None = None
    sdk_session_id: RunnerId | None = None
    cursor: JournalCursor | None = None
    input_id: RunnerId | None = None
    unresolved_attempts: list[str] = Field(default_factory=list)
    unresolved_invocations: list[str] = Field(default_factory=list)
    sdk_idle: bool | None = None
    helper_run_id: RunnerId | None = None
    journal_confirmed: bool | None = None
    pending_confirmation: JournalConfirmation | None = None
    supported_business_contracts: list[Literal["challenge-board-v1"]] = Field(default_factory=list)
    error_code: (
        Literal[
            "stale_fence",
            "request_conflict",
            "binding_unavailable",
            "boundary_frozen",
            "unsupported_model",
        ]
        | None
    ) = None


class RunnerBootstrap(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    fence: RunnerFence
    control_url: str
    model_proxy_url: str
    mcp_gateway_url: str
    control_credential_ref: RunnerId
    credential_file: str
    journal_root: str
    mcp_metadata_timeout_ms: int = Field(default=15000, ge=50, le=30000)
    helper_foreground: bool = False


class RunnerSchemaBundle(ContractModel):
    bootstrap: RunnerBootstrap
    poll: RunnerPoll
    command: RunnerCommand
    payload: RunnerPayload
    receipt: CommandReceipt
    confirmation: JournalConfirmation
    journal_receipt: JournalReceipt
    journal_state: JournalState
    lease_request: SessionLeaseRequest
    lease_receipt: SessionLeaseReceipt
    tool_invocation: ToolInvocation
    tool_permit: ToolPermit
    tool_result_receipt: ToolResultReceipt
    tool_result_confirmation: ToolResultConfirmation
    tool_unknown_submission: ToolUnknownSubmission
    model_attempt_request: ModelAttemptRequest
    published_prompt: PublishedPrompt
    model_admission_receipt: ModelAdmissionReceipt
    model_outcome_submission: ModelOutcomeSubmission
    model_outcome_receipt: ModelOutcomeReceipt
    operation_ack: OperationAck


def runner_json_text(value: object) -> str:
    """Serialize once; persist/send this exact text instead of canonicalizing it again."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def runner_payload_digest(payload_json: str) -> str:
    """Digest exact UTF-8 JSON text; equivalent reserializations may have different digests."""
    if not isinstance(payload_json, str):
        raise TypeError("Runner digests require the preserved JSON text")
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def validate_runner_json_object(text: str, expected_digest: str | None) -> dict:
    if runner_payload_digest(text) != expected_digest:
        raise ValueError("Preserved JSON bytes do not match their digest")

    def invalid_number(value: str):
        raise ValueError(f"Non-finite JSON number: {value}")

    def finite_float(value: str):
        parsed = float(value)
        if not math.isfinite(parsed):
            invalid_number(value)
        return parsed

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Repeated JSON object key")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            parse_constant=invalid_number,
            parse_float=finite_float,
            object_pairs_hook=unique_object,
        )
    except RecursionError as error:
        raise ValueError("JSON object nesting exceeds its bound") from error
    if not isinstance(value, dict):
        raise ValueError("Runner JSON requires an object")
    return value


if __name__ == "__main__":
    print(json.dumps(RunnerSchemaBundle.model_json_schema(), ensure_ascii=False, indent=2))
