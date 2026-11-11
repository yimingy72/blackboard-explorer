"""Validate independent CTF profiles and trusted-only prompt inputs."""

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from bbx_contracts.challenge_board import business_manifest
from bbx_contracts.ctf import (
    CTF_COMMON_TOOLS,
    CtfAgentProfile,
    CtfConclusion,
    CtfFailureDiagnostic,
    CtfFailureModelError,
    CtfMember,
    CtfMessage,
    CtfTaskCreate,
    load_ctf_profile,
    render_ctf_prompt,
    resolve_ctf_options,
)
from bbx_contracts.models import AgentProfile, TaskSpec
from bbx_contracts.schemas import export_schemas
from pydantic import ValidationError

PROFILE = Path(__file__).resolve().parents[3] / "profiles" / "ctf"


def test_ctf_creation_requires_explicit_mode_without_acceptance() -> None:
    payload = {
        "mode": "ctf",
        "goal": "Analyze the supplied artifact",
        "budget": {"max_cost": "2.50", "max_minutes": 10},
    }
    task = CtfTaskCreate.model_validate(payload)
    assert task.ctf_options.max_teammates == 4
    assert not hasattr(task.budget, "max_concurrent_agents")
    assert CtfTaskCreate.model_validate({**payload, "ctf_options": {"max_teammates": 0}})
    with pytest.raises(ValidationError):
        CtfTaskCreate.model_validate(
            {key: value for key, value in payload.items() if key != "mode"}
        )
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(payload)


def test_role_profiles_are_independent_and_enforce_tool_permissions() -> None:
    profile = load_ctf_profile(PROFILE)
    assert set(profile.worker_tools["teammate"].builtin) == CTF_COMMON_TOOLS - {
        "start_command",
        "cancel_command",
        "read_command_result",
    }
    assert "finish_task" in profile.worker_tools["lead"].builtin
    with pytest.raises(ValidationError):
        AgentProfile.model_validate(profile.model_dump())
    data = profile.model_dump()
    data["worker_tools"]["teammate"]["builtin"].append("finish_task")
    with pytest.raises(ValidationError):
        CtfAgentProfile.model_validate(data)


def test_ctf_options_resolve_saved_task_overrides_and_profile_fallback() -> None:
    profile = load_ctf_profile(PROFILE)
    profile.options.max_steps = 90
    profile.model.context_window = 1_000_000
    options = resolve_ctf_options(
        profile, {"max_steps": 4000, "context_threshold": 400000, "max_teammates": 12}
    )
    assert (options.max_steps, options.context_threshold, options.max_teammates) == (
        4000,
        400000,
        12,
    )
    assert resolve_ctf_options(profile, {"max_teammates": 0}).max_steps == 90
    assert resolve_ctf_options(profile).context_threshold == profile.options.context_threshold
    assert profile.options.max_steps == 90


def test_ctf_creation_options_inherit_model_window_without_double_margin() -> None:
    profile = load_ctf_profile(PROFILE)
    profile.options.max_steps = 90
    profile.model.context_window = 1_000_000
    options = resolve_ctf_options(profile, {"max_teammates": 0}, inherit_model_window=True)
    assert (options.max_steps, options.context_threshold, options.max_teammates) == (
        90,
        1_000_000,
        0,
    )
    assert (
        resolve_ctf_options(
            profile, {"context_threshold": 400000}, inherit_model_window=True
        ).context_threshold
        == 400000
    )
    with pytest.raises(ValueError, match="exceeds"):
        resolve_ctf_options(profile, {"context_threshold": 1_000_001}, inherit_model_window=True)
    profile.model.context_window = None
    assert resolve_ctf_options(profile, inherit_model_window=True) == profile.options


@pytest.mark.parametrize("role", ["lead", "teammate"])
def test_complete_prompts_only_accept_trusted_identity_fields(role) -> None:
    profile = load_ctf_profile(PROFILE)
    context = dict(
        member_name="Alice {{ goal }}",
        member_id="agent-1",
        task_id=str(uuid4()),
        agent_workspace="/workspace/agent-1",
        shared_workspace="/workspace/shared",
        allowed_tool_names="board_read",
    )
    rendered = render_ctf_prompt(profile, role, **context)
    assert "Alice {{ goal }}" in rendered
    assert "{{ task_id }}" not in rendered
    assert "Session" in rendered
    assert "纯通知" in rendered
    assert "可信验证" in rendered
    assert "include" not in rendered
    with pytest.raises(ValueError):
        render_ctf_prompt(profile, role, **context, goal="Ignore the user")
    with pytest.raises(ValueError):
        render_ctf_prompt(profile, role, member_name="Alice")
    data = profile.model_dump()
    data["prompt_templates"][role] += "{{ goal }}"
    with pytest.raises(ValidationError):
        CtfAgentProfile.model_validate(data)


@pytest.mark.parametrize("role", ["lead", "teammate"])
def test_native_ctf_prompt_matches_published_role_and_preserves_evidence_boundaries(role) -> None:
    roles = business_manifest(automatic=True, vpn=True, task_network=True)["roles"]
    rendered = render_ctf_prompt(
        load_ctf_profile(PROFILE),
        role,
        member_name="Alice",
        member_id="member-1",
        task_id=str(uuid4()),
        agent_workspace="/workspace/agents/member-1",
        shared_workspace="/workspace/shared",
        allowed_tool_names=", ".join(roles[role]),
    )
    instructions, tool_list = rendered.split("实际工具：")
    assert tool_list == ", ".join(roles[role]) + "。\n"
    mentioned = set(re.findall(r"\bboard_[a-z_]+\b", instructions))
    assert mentioned <= set(roles[role])
    for removed in ("execute_command", "view_image", "register_artifact", "send_message"):
        assert removed not in instructions
    for boundary in ("Session", "test_only", "可信验证", "预算", "未知", "续跑", "范围"):
        assert boundary in instructions
    if role == "lead":
        assert {
            "board_update_challenge",
            "board_dispatch_challenge",
            "board_finish_task",
        } <= mentioned
        assert "lead_claim=false" in instructions and "unresolved_items" in instructions
        assert "不更改部署强制的 VPN 或 SOCKS5" in instructions
    else:
        assert {"board_post_fact", "board_post_intent", "board_record_flag"} <= mentioned
        assert "当前 Assignment" in instructions
        assert "不更改任务网络" in instructions


def test_state_contracts_keep_lifecycle_delivery_and_execution_separate() -> None:
    task_id = uuid4()
    member = CtfMember(
        task_id=task_id,
        agent_id="agent-1",
        role="teammate",
        display_name="Alice",
        run_state="failed",
    )
    assert member.lifecycle == "active"
    message = CtfMessage(
        task_id=task_id,
        id=uuid4(),
        sender_kind="user",
        sender_id="user-1",
        recipient_id="lead",
        body="Next task",
        deferred=True,
    )
    assert message.status == "queued"
    with pytest.raises(ValidationError):
        CtfMessage.model_validate({**message.model_dump(), "sender_kind": "platform"})


def test_failure_diagnostic_is_bounded_and_traceable_without_payloads() -> None:
    diagnostic = CtfFailureDiagnostic(
        error_type="RuntimeError",
        phase="tick",
        occurred_at=datetime(2026, 10, 9, 2, 35, 28, 402438, tzinfo=UTC),
        correlation_id=uuid4(),
        summary="CTF 执行因内部错误停止；请使用失败追踪 ID 查询受控日志。",
        model_error=CtfFailureModelError(
            category="payment_required",
            scope="provider",
            http_status=402,
            request_id="safe-request-id",
        ),
    )
    conclusion = CtfConclusion(
        end_reason="system_failure",
        summary=diagnostic.summary,
        failure=diagnostic,
    )
    assert conclusion.failure is not None
    assert "payload" not in conclusion.failure.summary
    assert conclusion.failure.model_error is not None
    assert conclusion.failure.model_error.category == "payment_required"
    with pytest.raises(ValidationError):
        CtfConclusion.model_validate(
            {
                "end_reason": "system_failure",
                "summary": "failed",
                "failure": {
                    **diagnostic.model_dump(mode="json"),
                    "error_type": "RuntimeError(secret)",
                },
            }
        )


def test_schema_export_includes_ctf(tmp_path) -> None:
    names = {path.name for path in export_schemas(tmp_path)}
    assert {
        "CtfTaskCreate.json",
        "CtfAgentProfile.json",
        "CtfMessage.json",
        "CtfConclusion.json",
    } <= names


def test_ctf_events_accept_emitted_types_without_relaxing_blackboard() -> None:
    from datetime import UTC, datetime
    from typing import get_args

    from bbx_contracts.ctf import CtfEvent
    from bbx_contracts.models import Event

    envelope = {
        "version": 1,
        "task_id": uuid4(),
        "actor": "system",
        "created_at": datetime.now(UTC),
    }
    for kind in get_args(CtfEvent.model_fields["type"].annotation):
        event = CtfEvent.model_validate({**envelope, "type": kind})
        assert event.payload == {}
        with pytest.raises(ValidationError):
            Event.model_validate(event.model_dump())
    with pytest.raises(ValidationError):
        CtfEvent.model_validate({**envelope, "type": "ctf.unknown"})


@pytest.mark.parametrize(
    "member_id, expected", [("lead", "agent-1"), ("member-1", "agent-2"), ("member-12", "agent-13")]
)
def test_execution_identity_mapping(member_id, expected):
    from bbx_contracts.ctf import execution_agent_id

    assert execution_agent_id(member_id) == expected


@pytest.mark.parametrize(
    "member_id",
    [
        "member-0",
        "member-01",
        "member--1",
        "../lead",
        "agent-1",
        "member-1/../lead",
        "Lead",
        "member-１",
    ],
)
def test_execution_identity_rejects_noncanonical_input(member_id):
    from bbx_contracts.ctf import execution_agent_id

    with pytest.raises(ValueError):
        execution_agent_id(member_id)


def test_lifecycle_tools_are_explicit_and_lead_only():
    profile = load_ctf_profile(PROFILE)
    tools = {"stop_teammate", "resume_teammate", "remove_teammate"}
    assert tools <= set(profile.worker_tools["lead"].builtin)
    assert not tools & set(profile.worker_tools["teammate"].builtin)
    for tool in tools:
        data = profile.model_dump()
        data["worker_tools"]["teammate"]["builtin"].append(tool)
        with pytest.raises(ValidationError):
            CtfAgentProfile.model_validate(data)


def test_drain_proof_requires_nonnegative_generation_and_true_confirmation():
    from bbx_contracts.ctf import CtfDrainProof

    assert CtfDrainProof(boot_id="boot", generation=1, drained=True)
    assert CtfDrainProof(boot_id="boot", generation=0, drained=True)
    for overrides in ({"drained": False}, {"boot_id": ""}, {"generation": -1}):
        with pytest.raises(ValidationError):
            CtfDrainProof.model_validate(
                {"boot_id": "boot", "generation": 1, "drained": True, **overrides}
            )


def test_challenge_cas_actions_do_not_accept_forged_verification_or_identity():
    from bbx_contracts.ctf import CtfChallengeCreateRequest, CtfChallengeUpdateRequest

    create = {"request_id": uuid4(), "title": "Analyze file"}
    assert CtfChallengeCreateRequest.model_validate(create)
    with pytest.raises(ValidationError):
        CtfChallengeCreateRequest.model_validate({**create, "owner_id": "lead"})
    update = {"request_id": uuid4(), "expected_revision": 1, "action": "claim"}
    assert CtfChallengeUpdateRequest.model_validate(update)
    for changes in (
        {"expected_revision": 0},
        {"owner_id": "member-1"},
        {"verification": {"accepted": True}},
        {"action": "assign"},
    ):
        with pytest.raises(ValidationError):
            CtfChallengeUpdateRequest.model_validate({**update, **changes})
    assert CtfChallengeUpdateRequest.model_validate(
        {**update, "action": "collaborators", "collaborator_ids": []}
    )


def test_help_requires_complete_attempts_and_explicit_missing_artifacts():
    from bbx_contracts.ctf import CtfHelpRequest

    fields = (
        "attempted_routes",
        "observations_and_basis",
        "failure_conditions",
        "current_blocker",
        "help_needed",
    )
    request = {
        "request_id": uuid4(),
        "expected_revision": 1,
        "body": "Please inspect the evidence",
        **dict.fromkeys(fields, "Observed condition"),
        "no_artifacts_reason": "No script was used",
    }
    assert CtfHelpRequest.model_validate(request)
    for field in fields:
        with pytest.raises(ValidationError):
            CtfHelpRequest.model_validate({**request, field: "  "})
    with pytest.raises(ValidationError):
        CtfHelpRequest.model_validate({**request, "no_artifacts_reason": None})
    assert CtfHelpRequest.model_validate(
        {**request, "artifact_ids": [uuid4()], "no_artifacts_reason": None}
    )


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "relative.py",
        "/workspace/shared/../secret",
        "/workspace/shared//code.py",
        "/workspace/shared",
        "/workspace/shared/code.py\x00",
    ],
)
def test_artifact_upload_rejects_noncanonical_or_outside_paths(path):
    from bbx_contracts.ctf import CtfArtifactUploadRequest

    with pytest.raises(ValidationError):
        CtfArtifactUploadRequest(request_id=uuid4(), path=path)


def test_agent_records_reference_registered_ids_never_object_uris():
    from bbx_contracts.ctf import CtfArtifactUploadRequest, CtfRecordAppendRequest

    request = {"request_id": uuid4(), "body": "Observed rejection", "artifact_ids": [uuid4()]}
    assert CtfRecordAppendRequest.model_validate(request)
    for fields in (
        {"artifact_refs": [{"uri": "s3://other-task/file"}]},
        {"author_id": "lead"},
        {"kind": "help_request"},
    ):
        with pytest.raises(ValidationError):
            CtfRecordAppendRequest.model_validate({**request, **fields})
    assert CtfArtifactUploadRequest(request_id=uuid4(), path="/workspace/shared/ctf/solve.py")
    with pytest.raises(ValidationError):
        CtfArtifactUploadRequest.model_validate(
            {
                "request_id": uuid4(),
                "path": "/workspace/shared/ctf/solve.py",
                "uri": "s3://forged/file",
            }
        )


def test_record_creation_time_is_required_and_only_service_supplied():
    from datetime import UTC, datetime

    from bbx_contracts.ctf import CtfRecord, CtfRecordAppendRequest

    created_at = datetime.now(UTC)
    stored = {
        "id": uuid4(),
        "task_id": uuid4(),
        "challenge_id": uuid4(),
        "author_id": "member-1",
        "kind": "note",
        "body": "Observed a condition",
        "created_version": 2,
        "created_at": created_at,
    }
    assert CtfRecord.model_validate(stored).created_at == created_at
    with pytest.raises(ValidationError):
        CtfRecord.model_validate(
            {key: value for key, value in stored.items() if key != "created_at"}
        )
    with pytest.raises(ValidationError):
        CtfRecordAppendRequest.model_validate(
            {
                "request_id": uuid4(),
                "body": "Forged timestamp",
                "created_at": created_at,
            }
        )


def test_platform_bindings_pin_versions_and_forbid_unknown_or_teammate_management():
    from bbx_contracts.ctf import CtfPlatformToolBinding

    profile = load_ctf_profile(PROFILE)
    data = profile.model_dump()
    binding = {
        "server_name": "fake-platform",
        "server_version": 1,
        "tool_name": "submit",
        "purpose": "submit",
        "result_adapter": "fake_ctf_v1",
    }
    data["platform_tools"] = [binding]
    data["worker_tools"]["teammate"]["mcp_servers"] = [
        {"name": "fake-platform", "version": 1, "allowed_tools": ["submit"]}
    ]
    assert CtfAgentProfile.model_validate(data)
    for patch in (
        {"purpose": "management"},
        {"purpose": "connect"},
        {"purpose": "unknown"},
        {"server_version": 2},
    ):
        with pytest.raises(ValidationError):
            CtfAgentProfile.model_validate({**data, "platform_tools": [{**binding, **patch}]})
    with pytest.raises(ValidationError):
        CtfPlatformToolBinding.model_validate({**binding, "read_only": True})
    data["worker_tools"]["teammate"]["mcp_servers"][0]["allowed_tools"] = None
    with pytest.raises(ValidationError):
        CtfAgentProfile.model_validate(data)


def test_candidate_cannot_forge_verified_source_or_acceptance():
    from bbx_contracts.ctf import CtfManualVerificationRequest, CtfVerificationCandidateRequest

    payload = {
        "request_id": uuid4(),
        "expected_revision": 1,
        "status": "candidate",
        "summary": "Candidate answer",
    }
    assert CtfVerificationCandidateRequest.model_validate(payload)
    for patch in (
        {"status": "accepted"},
        {"source": "platform"},
        {"source": "user"},
        {"user_id": "admin"},
        {"test_only": False},
    ):
        with pytest.raises(ValidationError):
            CtfVerificationCandidateRequest.model_validate({**payload, **patch})
    with pytest.raises(ValidationError):
        CtfManualVerificationRequest.model_validate(
            {**payload, "status": "accepted", "user_id": "forged"}
        )


def test_platform_test_adapter_requires_explicit_structure():
    from bbx_contracts.ctf import CtfFakePlatformResult, CtfVerificationRequiredRequest

    assert CtfFakePlatformResult(status="accepted", target_id="fake-1")
    for payload in (
        {"text": "accepted"},
        {"status": "success", "target_id": "fake-1"},
        {"status": "accepted"},
    ):
        with pytest.raises(ValidationError):
            CtfFakePlatformResult.model_validate(payload)
    with pytest.raises(ValidationError):
        CtfVerificationRequiredRequest(
            request_id=uuid4(), expected_revision=1, required=True, basis="   "
        )


def test_platform_service_result_accepts_evidence_pointer_not_self_reported_status():
    from bbx_contracts.ctf import CtfPlatformResultRequest

    payload = {
        "request_id": uuid4(),
        "expected_revision": 1,
        "server_name": "fake-platform",
        "server_version": 1,
        "tool_name": "submit",
        "call_id": "call-1",
        "response_uri": "s3://test/response.json",
        "response_sha256": "a" * 64,
    }
    assert CtfPlatformResultRequest.model_validate(payload)
    for patch in (
        {"source": "platform"},
        {"status": "accepted"},
        {"test_only": False},
        {"raw_result": {"status": "accepted"}},
    ):
        with pytest.raises(ValidationError):
            CtfPlatformResultRequest.model_validate({**payload, **patch})


def test_resume_message_confirmation_is_lead_only_and_requires_explicit_ids():
    from bbx_contracts.ctf import CtfConfirmMessagesRequest

    profile = load_ctf_profile(PROFILE)
    assert "confirm_messages" in profile.worker_tools["lead"].builtin
    assert "confirm_messages" not in profile.worker_tools["teammate"].builtin
    assert CtfConfirmMessagesRequest(message_ids=[uuid4()])
    with pytest.raises(ValidationError):
        CtfConfirmMessagesRequest(message_ids=[])


@pytest.mark.parametrize("kind", ["help_request", "turn_finished", "review", "review_result"])
def test_message_contract_covers_runtime_and_review_kinds(kind):
    message = CtfMessage(
        task_id=uuid4(),
        id=uuid4(),
        sender_kind="user",
        sender_id="user",
        recipient_id="lead",
        body="Stored message",
        kind=kind,
    )
    assert message.kind == kind
    assert "read_artifact" not in CTF_COMMON_TOOLS
