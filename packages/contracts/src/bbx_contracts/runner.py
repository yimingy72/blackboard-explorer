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


RunnerBusinessContract = Literal["challenge-board-v1", "exploration-board-v1"]


class RunnerBinding(ContractModel):
    business_contract: RunnerBusinessContract
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

    @model_validator(mode="after")
    def no_exploration_assignment(self):
        if self.business_contract == "exploration-board-v1" and self.assignment_ref is not None:
            raise ValueError("Exploration bindings cannot carry a CTF assignment")
        return self


ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class EffectiveRunnerModel(ContractModel):
    id: str = Field(min_length=1, max_length=512)
    model_revision: int | None = Field(default=None, ge=1, exclude_if=lambda value: value is None)
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


BACKGROUND_HELPER_TOOLS = (
    "delegate_helper_background",
    "background_helper_status",
    "cancel_helper",
)

McpTarget = Literal["business", "sandbox"]


class RunnerMcpRoute(ContractModel):
    target: McpTarget
    wire_name: RunnerId


class HelperChild(ContractModel):
    child_id: RunnerId
    parent_binding_id: RunnerId
    deadline_ms: int = Field(ge=1, le=9007199254740991)
    mode: Literal["background"] | None = None


class SessionSpec(ContractModel):
    binding: RunnerBinding
    model: EffectiveRunnerModel
    model_credential_ref: RunnerId
    mcp_target_credential_refs: dict[McpTarget, RunnerId]
    allowed_tools: list[RunnerId] = Field(max_length=128)
    mcp_tool_routes: dict[RunnerId, RunnerMcpRoute] = Field(max_length=128)
    instructions: str = Field(max_length=131072)
    profile_revision: int = Field(ge=1)
    prompt_revision: int = Field(ge=1)
    helper_foreground: bool = False
    helper_background: bool = False
    helper_child: HelperChild | None = None

    @model_validator(mode="after")
    def exact_tool_aliases(self):
        if self.binding.business_contract == "exploration-board-v1" and (
            self.helper_foreground
            or self.helper_background
            or self.helper_child
            or set(self.allowed_tools) & {"delegate_helper", *BACKGROUND_HELPER_TOOLS}
        ):
            raise ValueError("Ordinary Blackboard bindings cannot delegate CTF helpers")
        if self.helper_child and (self.helper_foreground or self.helper_background):
            raise ValueError("Helpers cannot delegate")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("Declared tools must be distinct")
        refs = self.mcp_target_credential_refs
        routes = [(route.target, route.wire_name) for route in self.mcp_tool_routes.values()]
        if (
            not refs
            or len(set(refs.values())) != len(refs)
            or set(self.mcp_tool_routes) != set(self.allowed_tools)
            or len(set(routes)) != len(routes)
            or any(target not in refs for target, _ in routes)
            or self.helper_child
            and set(refs) != {"sandbox"}
        ):
            raise ValueError("Invalid direct MCP target binding")
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


class BoardContext(ContractModel):
    version: int = Field(ge=0, le=9007199254740991)
    content: str = Field(max_length=131072)
    source_digest: Digest


class PublishedPrompt(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    fence: RunnerFence
    binding_id: RunnerId
    prompt_revision: int = Field(ge=1)
    instructions: str = Field(max_length=131072)
    board_context: BoardContext | None = Field(default=None, exclude_if=lambda value: value is None)


class OperationAck(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    confirmed: Literal[True]
    request_id: RunnerId
    status: str
    receipt_digest: Digest


class BoardTurnTerminal(ContractModel):
    outcome: Literal["completed", "aborted", "failed"]
    assistant_entry_id: RunnerId | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class CommandReceipt(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    request_id: RunnerId
    payload_digest: Digest
    fence: RunnerFence
    status: Literal["prepared", "accepted", "applied", "completed", "aborted", "closed", "rejected"]
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
    supported_business_contracts: list[RunnerBusinessContract] = Field(default_factory=list)
    board_terminal: BoardTurnTerminal | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
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

    @model_validator(mode="after")
    def board_terminal_requires_confirmed_idle_input(self):
        if self.status != "completed":
            if self.board_terminal is not None:
                raise ValueError("Only a completed input has terminal board evidence")
            return self
        if (
            self.board_terminal is None
            or not self.input_id
            or not self.binding_id
            or not self.sdk_session_id
            or self.cursor is None
            or self.sdk_idle is not True
            or self.journal_confirmed is not True
            or self.pending_confirmation is not None
            or self.unresolved_attempts
            or self.unresolved_invocations
        ):
            raise ValueError("A completed board input requires confirmed idle journal evidence")
        return self


class RunnerBootstrap(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    fence: RunnerFence
    control_url: str
    model_proxy_url: str
    mcp_targets: dict[McpTarget, str] = Field(min_length=1)
    control_credential_ref: RunnerId
    credential_file: str
    journal_root: str
    mcp_metadata_timeout_ms: int = Field(default=15000, ge=50, le=30000)
    helper_foreground: bool = False
    helper_background: bool = False


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
    published_prompt: PublishedPrompt
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
