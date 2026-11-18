"""CTF task contracts and immutable role prompt snapshots."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from uuid import UUID

import yaml
from pydantic import (
    AfterValidator,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from .mcp_catalog import NativeMcpGrants
from .model_limits import effective_max_output_tokens
from .models import ContractModel, ExecResources, ModelConfig, TaskStatus, Usage, WorkerTools
from .pi_models import PiModelRef
from .providers import reasoning_efforts
from .runner_bootstrap import NativeCtfEnvironmentRef

CtfRole = Literal["lead", "teammate"]
CTF_BOARD_RESULT_MAX_BYTES = 16000
CTF_COMMON_TOOLS = frozenset(
    {
        "list_members",
        "send_message",
        "execute_command",
        "start_command",
        "cancel_command",
        "read_command_result",
        "list_challenges",
        "get_challenge",
        "get_record",
        "create_challenge",
        "update_challenge",
        "list_records",
        "append_record",
        "register_artifact",
        "request_help",
        "record_candidate",
        "read_turn_result",
        "read_saved_tool_result",
        "view_image",
    }
)
CTF_LEAD_TOOLS = CTF_COMMON_TOOLS | {
    "set_team_plan",
    "read_task_input",
    "create_challenges",
    "assign_challenges",
    "create_teammate",
    "stop_teammate",
    "resume_teammate",
    "remove_teammate",
    "finish_task",
    "set_verification_required",
    "confirm_messages",
    "list_deferred_messages",
}
CTF_TOOLS = {"lead": CTF_LEAD_TOOLS, "teammate": CTF_COMMON_TOOLS | {"read_task_input"}}
# Provider billing is settled after a response. Keep a conservative allowance for
# already-admitted requests instead of treating the configured max as a hard cap.
CTF_BUDGET_RESERVE_RATIO = Decimal("0.20")


def ctf_spend_limit(max_cost: Decimal | str) -> Decimal:
    return Decimal(str(max_cost)) * (Decimal(1) - CTF_BUDGET_RESERVE_RATIO)


PROMPT_FIELDS = frozenset(
    {
        "member_name",
        "member_id",
        "task_id",
        "agent_workspace",
        "shared_workspace",
        "allowed_tool_names",
    }
)
_PLACEHOLDER = re.compile(r"{{\s*([a-z_]+)\s*}}")


class CtfBudget(ContractModel):
    currency: Literal["USD", "CNY"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    max_cost: Decimal = Field(gt=0)
    max_minutes: int = Field(gt=0)


class CtfOptions(ContractModel):
    max_teammates: int = Field(default=4, ge=0)
    max_steps: int = Field(default=60, ge=1)
    context_threshold: int = Field(default=128000, ge=1)
    dispatch_mode: Literal["manual", "automatic"] = "manual"
    first_plan_max_calls: int = Field(default=3, ge=1, le=10)
    first_plan_timeout_seconds: int = Field(default=60, ge=10, le=300)

    @model_validator(mode="after")
    def automatic_capacity(self) -> CtfOptions:
        if self.dispatch_mode == "automatic" and self.max_teammates == 0:
            raise ValueError("Automatic mode requires at least one teammate")
        return self

    @model_serializer(mode="wrap")
    def preserve_legacy_snapshot(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        result = handler(self)
        if self.dispatch_mode == "manual":
            for key in ("dispatch_mode", "first_plan_max_calls", "first_plan_timeout_seconds"):
                if key not in self.model_fields_set:
                    result.pop(key, None)
        return result


class CtfWorkSource(ContractModel):
    namespace: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
    external_id: str | None = Field(default=None, min_length=1, max_length=200)


class CtfWorkScope(ContractModel):
    objective: str = Field(min_length=1, max_length=2000)
    boundary: str = Field(min_length=1, max_length=2000)
    input_refs: list[str] = Field(default_factory=list, max_length=32)
    resource_refs: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("input_refs", "resource_refs")
    @classmethod
    def bounded_refs(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value) for value in values
        ):
            raise ValueError("References must be distinct nonempty bounded identifiers")
        return values


class CtfPrerequisite(ContractModel):
    kind: Literal["gate", "work_completed"]
    ref: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def work_reference(self) -> CtfPrerequisite:
        if self.kind == "work_completed":
            self.ref = str(UUID(self.ref))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", self.ref):
            raise ValueError("Invalid prerequisite reference")
        return self


class CtfWorkDeliverable(ContractModel):
    criteria: str = Field(min_length=1, max_length=2000)
    evidence_required: bool = True
    validator_ref: str | None = Field(default=None, min_length=1, max_length=200)


class CtfSolveTemplate(ContractModel):
    permission_profile: Literal["solver"] = "solver"
    deliverable: CtfWorkDeliverable
    priority: Literal[0, 1, 2, 3] = 1
    boundary: str = Field(min_length=1, max_length=2000)
    input_refs: list[str] = Field(default_factory=list, max_length=32)
    resource_refs: list[str] = Field(default_factory=list, max_length=16)
    requires: list[CtfPrerequisite] = Field(default_factory=list, max_length=16)
    max_auto_solve_items: int = Field(default=20, ge=1, le=200, strict=True)
    source_namespace: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")

    @field_validator("input_refs", "resource_refs")
    @classmethod
    def bounded_refs(cls, values: list[str]) -> list[str]:
        return CtfWorkScope.bounded_refs(values)


class CtfWorkItem(ContractModel):
    work_id: UUID
    kind: Literal["prepare", "discover", "solve"]
    source: CtfWorkSource
    scope: CtfWorkScope
    requires: list[CtfPrerequisite] = Field(default_factory=list, max_length=16)
    priority: Literal[0, 1, 2, 3] = 1
    deliverable: CtfWorkDeliverable
    permission_profile: Literal["solver", "env_prepare"] = "solver"
    solve_template: CtfSolveTemplate | None = None

    @model_validator(mode="after")
    def bounded_delegation(self) -> CtfWorkItem:
        template = self.solve_template
        if self.kind == "solve" and self.permission_profile != "solver":
            raise ValueError("Solve work requires solver permissions")
        if any(p.kind == "work_completed" and p.ref == str(self.work_id) for p in self.requires):
            raise ValueError("Work cannot depend on itself")
        if template is not None:
            if self.kind != "discover" or template.source_namespace != self.source.namespace:
                raise ValueError("Solve templates require a discover item in the same namespace")
            if not set(template.resource_refs) <= set(self.scope.resource_refs):
                raise ValueError("Solve template cannot widen discovery resource scope")
            if any(
                p.kind == "work_completed" and p.ref == str(self.work_id) for p in template.requires
            ):
                raise ValueError("Streaming solve items cannot wait for their discover parent")
        if len({(p.kind, p.ref) for p in self.requires}) != len(self.requires):
            raise ValueError("Duplicate prerequisite")
        return self


class CtfTeamPlanRequest(ContractModel):
    request_id: UUID
    expected_plan_revision: int = Field(ge=0, strict=True)
    dispatch_enabled: bool = True
    upsert: list[CtfWorkItem] = Field(default_factory=list, max_length=20)
    cancel_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def distinct_work(self) -> CtfTeamPlanRequest:
        identifiers = [item.work_id for item in self.upsert]
        if len(identifiers) != len(set(identifiers)) or len(self.cancel_ids) != len(
            set(self.cancel_ids)
        ):
            raise ValueError("Work identifiers must be unique")
        if set(identifiers) & set(self.cancel_ids):
            raise ValueError("Cannot update and cancel the same work")
        return self


class CtfTaskInputReadRequest(ContractModel):
    file_id: UUID | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=2000, ge=1, le=8000)


class CtfTaskSpec(ContractModel):
    mode: Literal["ctf"]
    name: str | None = Field(default=None, min_length=1, max_length=100)
    goal: str = Field(min_length=1)
    domain_context: str | None = None
    completion_requirements: str | None = None
    budget: CtfBudget
    ctf_options: CtfOptions = Field(default_factory=CtfOptions)
    agent_profile: str = Field(default="ctf", min_length=1)
    egress_allowlist: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_name(self) -> CtfTaskSpec:
        if self.name is not None and "\x00" in self.name:
            raise ValueError("Task name cannot contain NUL")
        return self


class CtfTaskCreate(CtfTaskSpec):
    native_mcp: NativeMcpGrants | None = Field(default=None, exclude_if=lambda value: value is None)
    pi_model: PiModelRef | None = Field(default=None, exclude_if=lambda value: value is None)
    native_environment: NativeCtfEnvironmentRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    native_vpn_source_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    input_group_id: UUID | None = None
    input_file_ids: list[UUID] | None = Field(default=None, max_length=20)
    public_input_ids: list[UUID] | None = Field(default=None, max_length=20)
    model_id: str | None = Field(default=None, min_length=1)
    model_version: int | None = Field(default=None, ge=1)
    profile_version: int | None = Field(default=None, ge=1)
    reasoning_effort: Literal["off", "none", "minimal", "low", "medium", "high"] | None = Field(
        default=None, description="任务思考强度；省略或 null 沿用已发布配置"
    )

    @model_validator(mode="after")
    def validate_selection(self) -> CtfTaskCreate:
        if self.native_vpn_source_id is not None and self.input_group_id is None:
            raise ValueError("A private VPN configuration requires an owned input group")
        if self.model_version is not None and self.model_id is None:
            raise ValueError("Model version requires a model ID")
        if self.input_file_ids is not None and self.input_group_id is None:
            raise ValueError("Input files require an input group")
        if self.input_file_ids is not None and len(set(self.input_file_ids)) != len(
            self.input_file_ids
        ):
            raise ValueError("Input file IDs must be unique")
        if self.public_input_ids is not None:
            if self.input_group_id is None or len(set(self.public_input_ids)) != len(
                self.public_input_ids
            ):
                raise ValueError("Public inputs require a group and unique file IDs")
            if self.input_file_ids is not None and not set(self.public_input_ids) <= set(
                self.input_file_ids
            ):
                raise ValueError("Public inputs must belong to the selected inputs")
        return self


class CtfPromptTemplates(ContractModel):
    lead: str = Field(min_length=1)
    teammate: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_templates(self) -> CtfPromptTemplates:
        for source in (self.lead, self.teammate):
            fields = set(_PLACEHOLDER.findall(source))
            remaining = _PLACEHOLDER.sub("", source)
            if fields != PROMPT_FIELDS or any(
                token in remaining for token in ("{{", "}}", "{%", "{#")
            ):
                raise ValueError("CTF prompts require exactly the trusted identity placeholders")
        return self


class CtfPlatformToolBinding(ContractModel):
    """Purpose metadata for one exact tool in the existing MCP registry."""

    server_name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    server_version: int = Field(ge=1)
    tool_name: str = Field(min_length=1)
    purpose: Literal["management", "connect", "submit", "status", "unknown"] = "unknown"
    result_adapter: Literal["none", "fake_ctf_v1", "flag_receipt_v1"] = "none"
    read_only: bool = False

    @model_validator(mode="after")
    def effect_is_consistent(self) -> CtfPlatformToolBinding:
        if self.read_only and self.purpose not in {"connect", "status"}:
            raise ValueError("Only connection/status tools can be declared read-only")
        return self


class CtfRoleRunOptions(ContractModel):
    reasoning_effort: str | None = Field(default=None, min_length=1)
    max_output_tokens: int | None = Field(default=None, strict=True, ge=1)


class CtfAgentProfile(ContractModel):
    mode: Literal["ctf"] = "ctf"
    model: ModelConfig
    role_run_options: dict[CtfRole, CtfRoleRunOptions] = Field(default_factory=dict)
    options: CtfOptions = Field(default_factory=CtfOptions)
    prompt_templates: CtfPromptTemplates
    worker_tools: dict[CtfRole, WorkerTools]
    platform_tools: list[CtfPlatformToolBinding] = Field(default_factory=list)
    exec_image: str = Field(min_length=1)
    exec_resources: ExecResources
    privileged_allowlist: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_role_run_options(self) -> CtfAgentProfile:
        self.role_run_options = {
            role: options
            for role, options in self.role_run_options.items()
            if options.model_dump(exclude_none=True)
        }
        for role in self.role_run_options:
            resolve_ctf_model(self, role)
        return self

    @model_validator(mode="after")
    def validate_tools(self) -> CtfAgentProfile:
        if set(self.worker_tools) != {"lead", "teammate"}:
            raise ValueError("Both CTF roles must have explicit tools")
        for role, tools in self.worker_tools.items():
            selected = set(tools.builtin)
            required = {"list_members", "send_message"}
            if role == "lead":
                required |= {"create_teammate", "finish_task"}
            if self.options.dispatch_mode == "automatic":
                required.add("read_task_input")
                if role == "lead":
                    required.add("set_team_plan")
                    if (
                        selected
                        & {
                            "execute_command",
                            "start_command",
                            "cancel_command",
                            "read_command_result",
                            "view_image",
                        }
                        or tools.mcp_servers
                    ):
                        raise ValueError("Automatic Lead cannot execute commands or platform tools")
            if not required <= selected or not selected <= CTF_TOOLS[role]:
                raise ValueError(f"Invalid {role} tools")
            if len(selected) != len(tools.builtin):
                raise ValueError("Duplicate builtin tool")
            if len({binding.name for binding in tools.mcp_servers}) != len(tools.mcp_servers):
                raise ValueError("Duplicate MCP binding")
            if role == "teammate" and any(
                binding.allowed_tools is None for binding in tools.mcp_servers
            ):
                raise ValueError("Teammate MCP bindings require an explicit allowlist")
        bindings = {
            (binding.server_name, binding.server_version, binding.tool_name): binding
            for binding in self.platform_tools
        }
        if len(bindings) != len(self.platform_tools):
            raise ValueError("Duplicate platform tool binding")
        assigned = set()
        for role, tools in self.worker_tools.items():
            for server in tools.mcp_servers:
                if server.allowed_tools is None:
                    raise ValueError("CTF MCP tools require an exact allowlist")
                for name in server.allowed_tools:
                    key = (server.name, server.version, name)
                    binding = bindings.get(key)
                    if binding is None or binding.purpose == "unknown":
                        raise ValueError("Unclassified platform tools cannot be exposed")
                    if role == "teammate" and binding.purpose not in {"submit", "status"}:
                        raise ValueError("Platform management and connection require Lead")
                    assigned.add(key)
        if set(bindings) - assigned:
            raise ValueError("Platform bindings must reference an assigned MCP tool/version")
        return self


def resolve_ctf_model(profile: CtfAgentProfile, role: CtfRole) -> ModelConfig:
    """Derive one validated role model without changing the frozen base snapshot."""
    if role not in {"lead", "teammate"}:
        raise ValueError("Unknown CTF role")
    overrides = profile.role_run_options.get(role)
    updates = overrides.model_dump(exclude_none=True) if overrides is not None else {}
    effort = updates.get("reasoning_effort")
    if effort is not None and effort not in reasoning_efforts(
        profile.model.provider, profile.model.model
    ):
        raise ValueError(f"{role} reasoning_effort is unsupported by the selected model")
    output = updates.get("max_output_tokens")
    if output is not None and output > effective_max_output_tokens(profile.model):
        raise ValueError(f"{role} max_output_tokens exceeds the base model output limit")
    return ModelConfig.model_validate({**profile.model.model_dump(), **updates})


def resolve_ctf_options(
    profile: CtfAgentProfile,
    overrides: Mapping[str, Any] | None = None,
    *,
    inherit_model_window: bool = False,
) -> CtfOptions:
    """Resolve task overrides without applying the runtime's input safety margin.

    Creation inherits the selected model window when no threshold was requested.
    Existing tasks retain their saved threshold; runtime input sizing bounds it
    against the model window and output reserve.
    """
    requested = CtfOptions.model_validate(overrides or {}).model_dump(exclude_unset=True)
    options = profile.options.model_dump()
    window = profile.model.context_window
    if inherit_model_window and window is not None:
        if requested.get("context_threshold", window) > window:
            raise ValueError("CTF context_threshold exceeds the selected model context_window")
        options["context_threshold"] = window
    return CtfOptions.model_validate({**options, **requested})


def execution_agent_id(member_id: str) -> str:
    """Map durable team identities to the existing execution directory namespace."""
    if member_id == "lead":
        return "agent-1"
    match = re.fullmatch(r"member-([1-9][0-9]*)", member_id)
    if match is None:
        raise ValueError("Invalid CTF member identity")
    return f"agent-{int(match[1]) + 1}"


class CtfMemberOperationRequest(ContractModel):
    request_id: UUID


class CtfExecutionIdentity(ContractModel):
    boot_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    drained: bool = False


class CtfDrainProof(ContractModel):
    boot_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    drained: Literal[True]
    task_id: UUID | None = None
    agent_id: str | None = None
    member_id: str | None = None
    state: Literal["drained"] | None = None


class CtfMemberOperation(ContractModel):
    request_id: UUID
    kind: Literal["stop", "resume", "remove"]
    target_generation: int | None = Field(default=None, ge=0)
    boot_id: str | None = None
    phase: str | None = None


class CtfMember(ContractModel):
    task_id: UUID
    agent_id: str = Field(min_length=1)
    role: CtfRole
    display_name: str = Field(min_length=1, max_length=100)
    lifecycle: Literal["active", "stopped", "removed"] = "active"
    run_state: Literal["provisioning", "idle", "running", "stopping", "interrupted", "failed"] = (
        "idle"
    )
    generation: int = Field(default=0, ge=0)
    current_turn_id: UUID | None = None
    create_request_id: UUID | None = None
    usage: Usage = Field(default_factory=Usage)
    pending_operation: CtfMemberOperation | None = None
    execution: CtfExecutionIdentity | None = None
    operation_history: list[CtfMemberOperation] = Field(default_factory=list)
    removed_at: datetime | None = None


class CtfTurn(ContractModel):
    task_id: UUID
    id: UUID
    agent_id: str
    generation: int = Field(ge=1)
    purpose: Literal["execution", "review"] = "execution"
    status: Literal["running", "finished", "failed", "interrupted", "stopped"] = "running"
    assignment_message_ids: list[UUID] = Field(default_factory=list)
    notification_only: bool = False
    end_reason: str | None = None
    final_answer: str | None = None
    checkpoint_revision: int = Field(default=0, ge=0)
    runtime_instance: str | None = None
    usage: Usage = Field(default_factory=Usage)
    explicit_reply_ids: list[UUID] = Field(default_factory=list)


class CtfMessage(ContractModel):
    task_id: UUID
    id: UUID
    sender_kind: Literal["user", "agent", "system"]
    sender_id: str
    recipient_id: str
    purpose: Literal["execution", "review"] = "execution"
    kind: Literal[
        "instruction",
        "message",
        "assignment_result",
        "notification",
        "help_request",
        "turn_finished",
        "review",
        "review_result",
    ] = "message"
    body: str = Field(min_length=1)
    status: Literal["queued", "leased", "delivered", "cancelled", "failed"] = "queued"
    reply_to: UUID | None = None
    assignment_id: UUID | None = None
    challenge_id: UUID | None = None
    source_turn_id: UUID | None = None
    recipient_sequence: int = Field(default=0, ge=0)
    deferred: bool = False
    claim_token: UUID | None = None
    claim_turn_id: UUID | None = None
    claim_generation: int | None = Field(default=None, ge=1)
    lease_until: datetime | None = None
    delivered_turn_id: UUID | None = None
    session_revision: int | None = Field(default=None, ge=0)
    notification_purpose: str | None = None


class CtfFailureModelError(ContractModel):
    """Bounded provider diagnostics copied from a failed model call."""

    category: str = Field(pattern=r"^[a-z_]{1,64}$")
    scope: Literal["provider", "request", "infrastructure"] | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    failure_phase: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    exception_type: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,99}$")
    transport_type: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,99}$")
    incomplete_reason: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
    )
    transient: bool | None = None


class CtfFailureDiagnostic(ContractModel):
    """Safe coordinator failure metadata; never contains request or tool payloads."""

    error_type: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,99}$")
    leaf_error_types: list[Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,99}$")]] = (
        Field(default_factory=list, max_length=8)
    )
    phase: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    occurred_at: datetime
    correlation_id: UUID
    summary: str = Field(min_length=1, max_length=240)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    member_id: str | None = Field(default=None, min_length=1, max_length=128)
    turn_id: str | None = Field(default=None, min_length=1, max_length=128)
    generation: int | None = Field(default=None, ge=0)
    model_error: CtfFailureModelError | None = None


class CtfConclusion(ContractModel):
    end_reason: Literal[
        "goal_claimed", "user_stop", "budget_exhausted", "system_failure", "partial"
    ]
    summary: str
    unresolved_items: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    lead_claim: bool = False
    verification_refs: list[str] = Field(default_factory=list)
    run_number: int = Field(default=1, ge=1)
    requested_by: str | None = None
    requested_at: datetime | None = None
    finalized_at: datetime | None = None
    failure: CtfFailureDiagnostic | None = None


class CtfTaskView(CtfTaskSpec):
    id: UUID
    status: TaskStatus
    version: int = Field(default=0, ge=0)
    members: list[CtfMember] = Field(default_factory=list)
    conclusion: CtfConclusion | None = None


def render_ctf_prompt(profile: CtfAgentProfile, role: CtfRole, **context: str) -> str:
    """Render only trusted fields; never recursively interpret substituted text."""
    if set(context) != PROMPT_FIELDS:
        raise ValueError("Prompt context must contain exactly the trusted identity fields")
    source = getattr(profile.prompt_templates, role)
    return _PLACEHOLDER.sub(lambda match: context[match.group(1)], source)


def load_ctf_profile(directory: Path) -> CtfAgentProfile:
    """Load a CTF snapshot without accepting blackboard profiles."""
    data = yaml.safe_load((directory / "profile.yaml").read_text(encoding="utf-8"))
    paths = data.pop("prompts")
    if set(paths) != {"lead", "teammate"}:
        raise ValueError("CTF profile requires lead and teammate prompts")
    data["prompt_templates"] = {
        role: (directory / path).read_text(encoding="utf-8") for role, path in paths.items()
    }
    return CtfAgentProfile.model_validate(data)


class CtfEvent(ContractModel):
    """CTF event envelope, including version-only redacted cursor events."""

    version: int = Field(ge=1)
    task_id: UUID
    type: Literal[
        "ctf.member.created",
        "ctf.member.state_changed",
        "ctf.member.removed",
        "ctf.started",
        "ctf.challenge.created",
        "ctf.challenge.updated",
        "ctf.record.appended",
        "ctf.artifact.registered",
        "ctf.verification.updated",
        "ctf.target.updated",
        "ctf.platform.dispatched",
        "ctf.platform.result_captured",
        "ctf.message.posted",
        "ctf.message.delivered",
        "ctf.native_worker.assigned",
        "ctf.native_worker.released",
        "ctf.native_worker.stopped",
        "ctf.native_worker.cleanup_changed",
        "ctf.turn.started",
        "ctf.turn.finished",
        "ctf.conclusion.requested",
        "ctf.conclusion.finalized",
        "ctf.coordination.reminded",
        "ctf.plan.updated",
        "ctf.dispatch.assigned",
        "ctf.work.discovered",
        "ctf.work.notice",
        "ctf.cursor",
    ]
    actor: str = Field(min_length=1)
    object_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    addressed_to: list[str] | None = None
    created_at: datetime


CtfWorkStatus = Literal["pending", "in_progress", "blocked", "completed", "cancelled"]


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Text must not be blank")
    return value


CtfProgressStage = Literal["approach", "attempt", "breakthrough", "result"]
CtfShortTitle = Annotated[str, Field(min_length=1, max_length=40), AfterValidator(_nonblank)]
CtfShortSummary = Annotated[str, Field(min_length=1, max_length=160), AfterValidator(_nonblank)]
CtfSolutionText = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_nonblank)]


class CtfProgress(ContractModel):
    stage: CtfProgressStage
    title: CtfShortTitle
    summary: CtfShortSummary


class CtfSolutionStep(ContractModel):
    kind: CtfProgressStage
    title: CtfShortTitle
    result: Literal["supported", "ruled_out", "inconclusive"]
    summary: CtfShortSummary
    record_ids: list[UUID] = Field(default_factory=list, max_length=8)
    artifact_ids: list[UUID] = Field(default_factory=list, max_length=8)


class CtfSolution(ContractModel):
    conclusion: CtfSolutionText
    approach: CtfSolutionText
    steps: list[CtfSolutionStep] = Field(min_length=1, max_length=6)
    remaining: list[CtfSolutionText] = Field(default_factory=list, max_length=3)
    reproduction: Annotated[str, Field(min_length=1, max_length=300), AfterValidator(_nonblank)]

    @model_validator(mode="after")
    def bounded_text(self) -> CtfSolution:
        texts = [self.conclusion, self.approach, self.reproduction, *self.remaining]
        texts.extend(text for step in self.steps for text in (step.title, step.summary))
        if sum(map(len, texts)) > 1600:
            raise ValueError("Solution text must not exceed 1600 characters")
        return self


def validate_ctf_delivery(completed: bool, solution: CtfSolution | None) -> None:
    if completed != (solution is not None):
        raise ValueError("Solution is required only when completing challenge work")


class CtfChallenge(ContractModel):
    id: UUID
    task_id: UUID
    title: str = Field(min_length=1)
    description: str = ""
    connection: str | None = None
    external_id: str | None = None
    requirements: str | None = None
    owner_id: str | None = None
    collaborator_ids: list[str] = Field(default_factory=list)
    work_status: CtfWorkStatus = "pending"
    revision: int = Field(ge=1)
    verification: dict[str, object] = Field(default_factory=dict)
    target: dict[str, object] = Field(default_factory=dict)
    tombstone: bool = False
    solution_record_id: UUID | None = None
    work_item: CtfWorkItem | None = None
    scope_revision: int = Field(default=1, ge=1)
    origin_work_id: UUID | None = None
    auto_solve_count: int = Field(default=0, ge=0)


class CtfArtifactUploadRequest(ContractModel):
    request_id: UUID
    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def workspace_artifact_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            "\x00" in value
            or ".." in path.parts
            or str(path) != value
            or not any(
                path.is_relative_to(root) and path != root
                for root in (PurePosixPath("/workspace/agents"), PurePosixPath("/workspace/shared"))
            )
        ):
            raise ValueError("Artifact requires a canonical workspace file path")
        return value


class CtfArtifactRegisterRequest(CtfArtifactUploadRequest):
    """Trusted service output; never an Agent-authored artifact reference."""

    uri: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0, le=50 * 1024 * 1024)
    filename: str = Field(min_length=1)


class CtfArtifactRef(ContractModel):
    id: UUID
    task_id: UUID
    path: str
    uri: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    filename: str
    created_version: int = Field(ge=1)


class CtfRecord(ContractModel):
    id: UUID
    task_id: UUID
    challenge_id: UUID
    author_id: str = Field(min_length=1)
    kind: Literal[
        "note", "correction", "help_request", "artifact", "verification", "target", "solution"
    ]
    body: str = Field(min_length=1)
    created_version: int = Field(ge=1)
    created_at: datetime
    artifact_refs: list[CtfArtifactRef] = Field(default_factory=list)
    attempted_routes: str | None = None
    observations_and_basis: str | None = None
    failure_conditions: str | None = None
    current_blocker: str | None = None
    help_needed: str | None = None
    no_artifacts_reason: str | None = None
    verification: dict[str, object] | None = None
    work_status: CtfWorkStatus | None = None
    target: dict[str, object] | None = None
    platform_call: dict[str, object] | None = None
    summary_applied: bool = True
    progress: CtfProgress | None = None
    solution: CtfSolution | None = None
    solution_generation: int | None = Field(default=None, ge=1)
    solution_turn_id: UUID | None = None
    solution_run_number: int | None = Field(default=None, ge=1)
    solution_challenge_revision: int | None = Field(default=None, ge=1)


class CtfChallengeCreateRequest(ContractModel):
    request_id: UUID
    title: str = Field(min_length=1)
    description: str = ""
    connection: str | None = None
    external_id: str | None = None
    requirements: str | None = None


class CtfChallengeClaimRequest(ContractModel):
    request_id: UUID
    expected_revision: int = Field(ge=1)


class CtfAssignmentInstruction(ContractModel):
    recipient_id: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=20000)


class CtfChallengeUpdateRequest(ContractModel):
    request_id: UUID
    expected_revision: int = Field(ge=1)
    action: Literal["claim", "assign", "set_status", "release", "reopen", "collaborators", "delete"]
    owner_id: str | None = Field(default=None, min_length=1)
    collaborator_ids: list[str] | None = None
    work_status: Literal["in_progress", "blocked", "completed", "cancelled"] | None = None
    solution: CtfSolution | None = None
    instructions: list[CtfAssignmentInstruction] | None = Field(
        default=None, min_length=1, max_length=32
    )

    @model_validator(mode="after")
    def action_fields(self) -> CtfChallengeUpdateRequest:
        validate_ctf_delivery(
            self.action == "set_status" and self.work_status == "completed", self.solution
        )
        for field, action in (
            ("owner_id", "assign"),
            ("collaborator_ids", "collaborators"),
            ("work_status", "set_status"),
        ):
            value = getattr(self, field)
            if (value is not None) != (self.action == action):
                raise ValueError(f"{field} is required only for action {action}")
        if self.collaborator_ids is not None and (
            any(not member.strip() for member in self.collaborator_ids)
            or len(set(self.collaborator_ids)) != len(self.collaborator_ids)
        ):
            raise ValueError("Collaborators must be nonempty unique member IDs")
        if self.instructions is not None:
            recipients = [item.recipient_id for item in self.instructions]
            if self.action not in {"assign", "collaborators"}:
                raise ValueError("Instructions require assignment or collaborator update")
            if any(not value.strip() for value in recipients) or len(set(recipients)) != len(
                recipients
            ):
                raise ValueError("Instruction recipients must be nonempty unique member IDs")
        return self


class CtfChallengeBatchCreateRequest(ContractModel):
    items: list[CtfChallengeCreateRequest] = Field(min_length=1, max_length=20)


class CtfChallengeAssignItem(ContractModel):
    request_id: UUID
    challenge_id: UUID
    expected_revision: int = Field(ge=1)
    owner_id: str = Field(min_length=1)
    instructions: list[CtfAssignmentInstruction] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def assignment_fields(self) -> CtfChallengeAssignItem:
        CtfChallengeUpdateRequest(
            request_id=self.request_id,
            expected_revision=self.expected_revision,
            action="assign",
            owner_id=self.owner_id,
            instructions=self.instructions,
        )
        return self


class CtfChallengeBatchAssignRequest(ContractModel):
    items: list[CtfChallengeAssignItem] = Field(min_length=1, max_length=20)


def ctf_tool_capability_issues(
    profile: CtfAgentProfile, roles: tuple[CtfRole, ...] = ("lead", "teammate")
) -> dict[str, list[str]]:
    """Validate newly selected tools without invalidating frozen legacy profiles."""
    issues: dict[str, list[str]] = {}
    for role in roles:
        tools = profile.worker_tools[role]
        selected = set(tools.builtin)
        missing = set()
        if "list_challenges" in selected and "get_challenge" not in selected:
            missing.add("get_challenge")
        if (
            selected
            & {"execute_command", "get_challenge", "get_record", "list_records", "read_turn_result"}
            or tools.mcp_servers
        ) and "read_saved_tool_result" not in selected:
            missing.add("read_saved_tool_result")
        if "start_command" in selected:
            missing.update({"cancel_command", "read_command_result"} - selected)
        if missing:
            issues[role] = sorted(missing)
    return issues


class CtfRecordAppendRequest(ContractModel):
    progress: CtfProgress | None = None
    request_id: UUID
    body: str = Field(min_length=1)
    kind: Literal["note", "correction"] = "note"
    artifact_ids: list[UUID] = Field(default_factory=list)


class CtfHelpRequest(ContractModel):
    request_id: UUID
    expected_revision: int = Field(ge=1)
    body: str = Field(min_length=1)
    artifact_ids: list[UUID] = Field(default_factory=list)
    attempted_routes: str = Field(min_length=1)
    observations_and_basis: str = Field(min_length=1)
    failure_conditions: str = Field(min_length=1)
    current_blocker: str = Field(min_length=1)
    help_needed: str = Field(min_length=1)
    no_artifacts_reason: str | None = None

    @field_validator(
        "attempted_routes",
        "observations_and_basis",
        "failure_conditions",
        "current_blocker",
        "help_needed",
    )
    @classmethod
    def explicit_help_field(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Help fields must explicitly describe the current work")
        return value

    @model_validator(mode="after")
    def explain_missing_artifacts(self) -> CtfHelpRequest:
        if not self.artifact_ids and not (self.no_artifacts_reason or "").strip():
            raise ValueError("Help without artifacts requires an explicit no_artifacts_reason")
        return self


class CtfVerificationCandidateRequest(ContractModel):
    request_id: UUID
    expected_revision: int = Field(ge=1)
    status: Literal["candidate", "unknown"]
    summary: str = Field(min_length=1)
    evidence_refs: list[UUID] = Field(default_factory=list)
    work_status: Literal["in_progress", "blocked", "completed"] | None = None

    solution: CtfSolution | None = None

    @model_validator(mode="after")
    def delivery_fields(self) -> CtfVerificationCandidateRequest:
        validate_ctf_delivery(self.work_status == "completed", self.solution)
        return self


class CtfVerificationRequiredRequest(ContractModel):
    request_id: UUID
    expected_revision: int = Field(ge=1)
    required: bool
    basis: str = Field(min_length=1)

    @field_validator("basis")
    @classmethod
    def explicit_basis(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Verification requirement needs an explicit basis")
        return value


class CtfManualVerificationRequest(ContractModel):
    request_id: UUID
    expected_revision: int = Field(ge=1)
    status: Literal["accepted", "rejected", "unknown"]
    summary: str = Field(min_length=1)
    evidence_refs: list[UUID] = Field(default_factory=list)


class CtfFakePlatformResult(ContractModel):
    """Structured test-adapter result; never evidence of a real platform call."""

    status: Literal["accepted", "rejected", "unknown"]
    target_id: str = Field(min_length=1)
    submission_id: str | None = None
    target_status: (
        Literal["unknown", "starting", "running", "stopping", "stopped", "error"] | None
    ) = None
    connection: str | None = None


class CtfVerification(ContractModel):
    required: bool = False
    basis: str | None = None
    submission_id: str | None = None
    status: Literal["pending", "candidate", "unknown", "accepted", "rejected"] = "pending"
    source: Literal["agent", "platform", "user"] | None = None
    external_ref: str | None = None
    response_uri: str | None = None
    summary: str | None = None
    updated_at: datetime | None = None
    test_only: bool = False
    user_id: str | None = None
    call_id: str | None = None


class CtfPlatformAuthorizeRequest(ContractModel):
    expected_revision: int = Field(ge=1)
    server_name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    server_version: int = Field(ge=1)
    tool_name: str = Field(min_length=1)


class CtfPlatformBeginRequest(CtfPlatformAuthorizeRequest):
    call_id: str = Field(min_length=1)


class CtfPlatformResultRequest(CtfPlatformAuthorizeRequest):
    """Service evidence pointer; the backend reads and verifies the saved result."""

    request_id: UUID
    call_id: str = Field(min_length=1)
    response_uri: str = Field(min_length=1)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CtfConfirmMessagesRequest(ContractModel):
    message_ids: list[UUID] = Field(min_length=1, max_length=100)


class CtfRecoveryInputProofRequest(ContractModel):
    agent_id: str = Field(min_length=1, max_length=100)
    turn_id: UUID
    generation: int = Field(ge=0)
    message_ids: list[UUID] = Field(min_length=1, max_length=100)


class CtfTurnResultReadRequest(ContractModel):
    turn_id: UUID
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=2000, ge=1, le=16000)


class CtfSavedToolResultReadRequest(ContractModel):
    call_id: str = Field(min_length=1, max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=2000, ge=1, le=16000)


class CtfDeferredMessagesReadRequest(ContractModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=100)
