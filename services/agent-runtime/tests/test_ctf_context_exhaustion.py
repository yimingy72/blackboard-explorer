"""Safe proactive CTF view compaction and retained blocked diagnostics."""

from copy import deepcopy
from typing import Any, cast

import pytest
from agent_framework import Content, Message
from bbx_runtime.ctf.context import estimate_messages_tokens, input_token_limit
from bbx_runtime.ctf.middleware import CtfHistoryProvider, TurnBoundary
from bbx_runtime.ctf.session import load_checkpoint
from test_ctf_context import result, text
from test_ctf_leaf_context import almost_full_history, loaded, saved_provider
from test_ctf_runtime import TURN


async def test_proactive_leaf_compaction_preserves_original_history_before_hard_limit():
    original = almost_full_history()
    before = [message.to_dict() for message in original]
    _, value = await saved_provider(original)
    view = await loaded(value)
    estimated = estimate_messages_tokens(view, provider=value.model.provider)
    assert value.input_limit * 0.9 < estimated < value.input_limit
    projected = value.prepare_provider_messages(view)
    assert estimate_messages_tokens(projected, provider=value.model.provider) < estimated
    assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == 2
    assert [message.to_dict() for message in original] == before


async def test_protected_first_assignment_moves_whole_to_recovery_and_survives_restart():
    original = almost_full_history()
    original[0] = text("assignment", "Keep this exact directive unchanged", "user")
    original[1] = text("large-old-output", "x" * 1800, "assistant")
    before = [message.to_dict() for message in original]
    service, value = await saved_provider(original)
    value.turn = {**TURN, "assignment_message_ids": ["assignment", None]}
    view = await loaded(value)
    fresh = text("fresh", "!" * 400)
    projected = value.prepare_provider_messages([*view, fresh])
    assert fresh in projected
    directive = next(message for message in projected if message.message_id == "assignment")
    assert directive.to_dict() == original[0].to_dict()
    assert [message.to_dict() for message in original] == before
    assert value.checkpoint.session.state["ctf_context_recovery"]["protected_inputs"] == [before[0]]
    assert value.checkpoint.session.state["ctf_context_compaction"]["protected_hits"] == [0]
    await value.checkpoint.save()
    restored = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    restarted = CtfHistoryProvider(cast(Any, service), restored, value.turn, value.model, 1000)
    again = await loaded(restarted)
    assert [message.to_dict() for message in again[:2]] == [
        message.to_dict() for message in projected[:2]
    ]
    assert len([message for message in again if message.message_id == "assignment"]) == 1


async def test_hard_overflow_loading_can_relocate_confirmed_text_assignment_without_losing_it():
    messages = [
        text("assignment", "Retain exact instruction"),
        text("old", "x" * 3000, "assistant"),
        text("recent", "done", "assistant"),
    ]
    before = [message.to_dict() for message in messages]
    _, value = await saved_provider(messages)
    value.turn = {**TURN, "assignment_message_ids": ["assignment"]}
    projected = await loaded(value)
    assert (
        next(message for message in projected if message.message_id == "assignment").to_dict()
        == before[0]
    )
    assert estimate_messages_tokens(projected, provider=value.model.provider) <= value.input_limit
    assert [message.to_dict() for message in messages] == before


async def test_partial_compaction_below_limit_is_valid_without_reaching_low_watermark():
    original = almost_full_history()
    original[0] = text("assignment", "x" * 1000)
    _, value = await saved_provider(original)
    value.turn = {**TURN, "assignment_message_ids": ["assignment"]}
    view = await loaded(value)
    projected = value.prepare_provider_messages([*view, text("fresh", "!" * 200)])
    estimate = estimate_messages_tokens(projected, provider=value.model.provider)
    assert value.input_limit * 0.6 < estimate <= value.input_limit
    assert value.checkpoint.session.state["ctf_context_compaction"]["branch"] == "compacted"


@pytest.mark.parametrize(
    "branch",
    [
        "no_loaded_prefix",
        "loaded_prefix_mismatch",
        "unconfirmed_prefix",
        "missing_recovery_seed",
        "no_safe_boundary",
        "remaining_over_limit",
    ],
)
async def test_blocked_leaf_explains_compaction_branch_and_records_safe_ids(branch, caplog):
    original = almost_full_history()
    _, value = await saved_provider(original)
    view = await loaded(value)
    outgoing = [*view, text("fresh", "!" * 200)]
    if branch == "no_loaded_prefix":
        value._loaded_messages = []
    elif branch == "loaded_prefix_mismatch":
        outgoing[0] = deepcopy(outgoing[0])
    elif branch == "unconfirmed_prefix":
        cast(Any, value.checkpoint).confirmed_history = ()
    elif branch == "missing_recovery_seed":
        value.checkpoint.session.state.pop("ctf_context_recovery_seed")
    elif branch == "no_safe_boundary":
        value.turn = {**TURN, "assignment_message_ids": [None]}
        original[0].message_id = "protected-group"
        await value.checkpoint.save()
        value.turn["assignment_message_ids"] = ["protected-group", None]
        # A provider replay group cannot be detached into the recovery prefix.
        original[0].additional_properties["_group"] = {"id": "opaque-group"}
        await value.checkpoint.save()
        view = await loaded(value)
        outgoing = [*view, text("fresh", "!" * 200)]
    elif branch == "remaining_over_limit":
        outgoing[-1] = text("fresh", "!" * 2000)
    value.checkpoint.session.state["ctf_last_provider_usage"] = {
        "turn_id": TURN["id"],
        "step": 4,
        "input_tokens": 333,
        "output_tokens": 12,
    }
    with pytest.raises(TurnBoundary, match="context_blocked"):
        value.prepare_provider_messages(outgoing)
    blocked = value.checkpoint.session.state["ctf_context_blocked"]
    assert blocked["branch"] == branch
    assert isinstance(blocked["eligible"], int)
    assert blocked["last_provider_usage"]["input_tokens"] == 333
    assert None not in blocked["input_message_ids"]
    assert None not in blocked["message_ids"]
    assert branch in caplog.text
    assert "x" * 40 not in caplog.text


async def test_unknown_parallel_tool_group_remains_uncut_with_exact_failure_diagnostics():
    messages = [
        Message(
            role="assistant",
            contents=[
                Content.from_function_call("a", "inspect", arguments={"q": "x" * 1300}),
                Content.from_function_call("b", "inspect"),
            ],
        ),
        result("a", "done"),
    ]
    before = [message.to_dict() for message in messages]
    _, value = await saved_provider(messages)
    view = await loaded(value)
    with pytest.raises(TurnBoundary):
        value.prepare_provider_messages([*view, text("fresh", "!" * 300)])
    blocked = value.checkpoint.session.state["ctf_context_blocked"]
    assert blocked["branch"] == "missing_recovery_seed"
    assert blocked["cut"] == 0
    assert [message.to_dict() for message in messages] == before


def test_threshold_output_reserve_and_safety_margin_remain_hard_budget():
    from pathlib import Path

    from bbx_contracts.ctf import load_ctf_profile

    model = load_ctf_profile(Path("profiles/ctf")).model.model_copy(
        update={
            "provider": "openai_responses",
            "model": "deepseek-flash",
            "context_window": 1_000_000,
        }
    )
    assert input_token_limit(model, 400_000) == 320_000
    assert input_token_limit(model, 1_000_000) == 485_427
    # A threshold above the input/output split cannot enlarge the hard input gate.
    assert input_token_limit(model, 606_784) == 485_427


async def test_block_retains_latest_single_provider_usage_instead_of_turn_average():
    from agent_framework import tool
    from bbx_runtime.testing.scripted_client import (
        ScriptedChatClient,
        ScriptStep,
        ScriptToolCall,
        ScriptUsage,
    )
    from test_ctf_provider_boundary import DispatchService, native

    service = DispatchService()
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")

    @tool
    def inspect() -> str:
        return "synthetic result"

    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(ScriptToolCall("inspect"),), usage=ScriptUsage(prompt_cache_miss_tokens=1000)
            ),
            ScriptStep(
                text="done", usage=ScriptUsage(prompt_cache_miss_tokens=42, completion_tokens=12)
            ),
        ]
    )
    await native(service, checkpoint, client, tools=[inspect])
    value = CtfHistoryProvider(cast(Any, service), checkpoint, TURN, service.profile.model, 3000)
    value.block_context([text("new", "!" * 3000)], value.input_limit, 0)
    blocked = checkpoint.session.state["ctf_context_blocked"]
    assert blocked["last_provider_usage"]["input_tokens"] == 42
    assert blocked["last_provider_usage"]["output_tokens"] == 12
    assert blocked["last_provider_usage"]["step"] == 2
    assert service.saved is not None
    assert service.saved["session"]["state"]["ctf_last_provider_usage"]["input_tokens"] == 42


@pytest.mark.parametrize("overflow", [770, 1206, 1438, 2402, 2913, 3131, 4110, 4150])
async def test_small_overflow_with_safe_boundary_accepts_suffix_above_low_watermark(overflow):
    messages = [
        text("old", "x" * 18000, "assistant"),
        Message(role="assistant", contents=[Content.from_function_call("unknown", "inspect")]),
    ]
    _, value = await saved_provider(messages, threshold=400000)
    value.input_limit = 320000
    view = await loaded(value)
    padding = (
        value.input_limit
        + overflow
        - estimate_messages_tokens([*view, text("fresh", "")], provider=value.model.provider)
    )
    fresh = text("fresh", "!" * padding)
    assert (
        value.input_limit
        < estimate_messages_tokens([*view, fresh], provider=value.model.provider)
        <= value.input_limit * 1.013
    )
    projected = value.prepare_provider_messages([*view, fresh])
    assert fresh in projected
    assert (
        value.input_limit * 0.6
        < estimate_messages_tokens(projected, provider=value.model.provider)
        <= value.input_limit
    )


async def test_native_dispatch_persists_relocated_assignment_without_history_loss():
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
    from test_ctf_provider_boundary import DispatchService, native

    service = DispatchService()
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    original = [
        text("assignment", "Exact assigned objective remains required"),
        text("old-output", "x" * 6900, "assistant"),
    ]
    before = [message.to_dict() for message in original]
    checkpoint.session.state["in_memory"] = {"messages": original}
    await checkpoint.save()
    checkpoint.delivered_message_ids.add("assignment")
    client = ScriptedChatClient(
        [ScriptStep(text="done", expect_contains="Exact assigned objective")]
    )
    snapshots = []
    original_leaf = client._inner_get_response

    def record_dispatch(**kwargs):
        snapshots.append(deepcopy(service.saved))
        return original_leaf(**kwargs)

    client._inner_get_response = record_dispatch
    await native(service, checkpoint, client)
    assert len(client.received_messages) == 1
    assert snapshots[0] is not None
    prefix_inputs = snapshots[0]["session"]["state"]["ctf_context_recovery"]["protected_inputs"]
    assert prefix_inputs == [before[0]]
    assert [
        message.to_dict() for message in checkpoint.session.state["in_memory"]["messages"][:2]
    ] == before
    assert any(message.message_id == "assignment" for message in client.received_messages[0])
    assert not any(message.message_id == "old-output" for message in client.received_messages[0])


async def test_blocked_runner_keeps_local_checkpoint_after_save_and_marker_failure():
    from types import SimpleNamespace

    import httpx
    from bbx_runtime.clients.blackboard import RemoteError
    from bbx_runtime.ctf.runner import CtfRunner
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
    from test_ctf_provider_boundary import DispatchService

    original_error = httpx.ReadError("synthetic snapshot response failure")

    class FailingService(DispatchService):
        fail_checkpoint = True

        async def runtime(self, task_id, operation, **payload):
            if (
                operation == "checkpoint"
                and self.fail_checkpoint
                and payload["session"].get("state", {}).get("ctf_context_blocked")
            ):
                raise original_error
            if operation == "checkpoint_failed":
                raise RemoteError(409, "synthetic marker rejection")
            return await super().runtime(task_id, operation, **payload)

    service = FailingService()
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    checkpoint.session.state["in_memory"] = {"messages": [text("oversized-input", "!" * 10000)]}
    await checkpoint.save()
    client = ScriptedChatClient([ScriptStep(text="must not dispatch")])
    runner = CtfRunner(
        cast(Any, service), cast(Any, SimpleNamespace()), client_factory=lambda *_: client
    )
    member = {"id": "member-1", "display_name": "Alice", "role": "teammate"}
    with pytest.raises(httpx.ReadError) as raised:
        await runner.run("task", member, {**TURN, "token": "fake"})
    assert raised.value is original_error
    pending, _, _, answer = runner.pending_checkpoints[("task", "member-1")]
    blocked = pending.session.state["ctf_context_blocked"]
    assert blocked["branch"] in answer
    assert f"eligible {blocked['eligible']}, cut {blocked['cut']}" in answer
    assert "splitting tool pairs" not in answer
    assert pending.failed
    assert not client.received_messages
    assert not service.finished
    observations = [
        body
        for operation, body in service.calls
        if operation == "record_observation"
        and body["kind"] == "turn_context"
        and body["body"].get("boundary") == "context_blocked"
    ]
    assert observations[0]["body"]["diagnostics"]["branch"] == blocked["branch"]
    service.fail_checkpoint = False
    await runner.retry_checkpoints("task")
    assert not runner.pending_checkpoints
    assert service.finished[0]["end_reason"] == "context_blocked"
    assert not client.received_messages


@pytest.mark.parametrize(
    "provider,missing_input,expected",
    [
        ("anthropic", False, 60),
        ("bedrock", False, 60),
        ("openai_responses", False, 10),
        ("anthropic", True, None),
    ],
)
async def test_native_latest_provider_usage_matches_billing_cache_input(
    provider, missing_input, expected
):
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptUsage
    from test_ctf_provider_boundary import DispatchService, native

    class ReportedUsage(ScriptUsage):
        def as_maf(self) -> Any:
            details = {
                "cache_creation_input_token_count": 20,
                "cache_read_input_token_count": 30,
                "output_token_count": 7,
            }
            if not missing_input:
                details["input_token_count"] = 10
            return details

    service = DispatchService()
    service.profile.model = service.profile.model.model_copy(update={"provider": provider})
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    client = ScriptedChatClient([ScriptStep(text="done", usage=ReportedUsage())])
    await native(service, checkpoint, client)
    actual = checkpoint.session.state["ctf_last_provider_usage"]
    assert actual["input_tokens"] == expected
    assert actual["provider"] == provider
    assert actual["output_tokens"] == 7
    assert service.saved is not None
    assert service.saved["session"]["state"]["ctf_last_provider_usage"]["input_tokens"] == expected


async def test_step_limit_pending_checkpoint_keeps_original_reason_after_save_recovers():
    from types import SimpleNamespace

    import httpx
    from bbx_runtime.clients.blackboard import RemoteError
    from bbx_runtime.ctf.runner import CtfRunner
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
    from test_ctf_provider_boundary import DispatchService

    class Service(DispatchService):
        fail_checkpoint = False

        def __init__(self):
            super().__init__()
            self.profile.options.max_steps = 1
            self.profile.options.context_threshold = 100000

        async def runtime(self, task_id, operation, **payload):
            if operation == "checkpoint" and self.fail_checkpoint:
                raise httpx.ReadError("synthetic boundary save failure")
            if operation == "checkpoint_failed":
                raise RemoteError(409, "synthetic marker rejection")
            saved = await super().runtime(task_id, operation, **payload)
            if operation == "record_observation" and payload["kind"] == "tool_result":
                self.fail_checkpoint = True
            return saved

    class Envd:
        calls = []

        async def execute(self, task_id, member_id, command):
            self.calls.append(command)
            return "synthetic tool result"

    service, envd = Service(), Envd()
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "synthetic"}),)),
            ScriptStep(text="must not dispatch"),
        ]
    )
    runner = CtfRunner(
        cast(Any, service),
        cast(Any, SimpleNamespace()),
        client_factory=lambda *_: client,
        envd=envd,
    )
    with pytest.raises(httpx.ReadError):
        await runner.run(
            "task",
            {"id": "member-1", "display_name": "Alice", "role": "teammate"},
            {**TURN, "token": "fake"},
        )
    checkpoint = runner.pending_checkpoints[("task", "member-1")][0]
    assert checkpoint.pending_end_reason == "step_limit"
    assert len(client.received_options) == 1
    assert envd.calls == ["synthetic"]
    assert not service.finished
    service.fail_checkpoint = False
    await runner.retry_checkpoints("task")
    assert not runner.pending_checkpoints
    assert service.finished[0]["end_reason"] == "step_limit"
    assert len(client.received_options) == 1
    assert envd.calls == ["synthetic"]
