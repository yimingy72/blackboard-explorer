"""Context budgets and prefix stability without a provider or execution environment."""

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agent_framework import AgentSession, Content, Message, annotate_message_groups
from agent_framework.exceptions import ChatClientInvalidRequestException
from agent_framework.openai import OpenAIChatClient
from bbx_contracts.ctf import load_ctf_profile
from bbx_contracts.storage import storage_safe
from bbx_runtime.ctf.context import (
    align_history_start,
    estimate_messages_tokens,
    estimate_text_tokens,
    input_token_limit,
    message_signature,
    safe_history_boundaries,
    serialized_message_signature,
    stable_tool_previews,
)
from bbx_runtime.ctf.middleware import CtfHistoryProvider, TurnBoundary
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.ctf.session import CtfCheckpoint
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
from test_ctf_provider_boundary import DispatchService
from test_ctf_runtime import TURN


def model():
    return load_ctf_profile(Path("profiles/ctf")).model


def text(identifier: str, value: str, role="user") -> Message:
    return Message(role=role, message_id=identifier, contents=[Content.from_text(value)])


def result(identifier: str, value: str) -> Message:
    return Message(
        role="tool",
        message_id=identifier,
        contents=[Content.from_function_result(identifier, result=value)],
    )


def provider(messages, *, turn="turn", state=None, threshold=1000, fixed=0, configured_model=None):
    session = AgentSession()
    session.state.update(state or {})
    session.state["in_memory"] = {"messages": messages}
    service = AsyncMock()
    service.runtime_state.return_value = {
        "task": {"goal": "Synthetic task"},
        "members": [{"id": "member", "role": "lead", "lifecycle": "active"}],
    }
    checkpoint = CtfCheckpoint(cast(Any, service), "task", "member", session)
    checkpoint.confirmed_history = tuple(message_signature(message) for message in messages)
    return CtfHistoryProvider(
        cast(Any, service),
        checkpoint,
        {"id": turn},
        configured_model or model(),
        threshold,
        fixed_input_tokens=fixed,
    )


async def history(value):
    return await value.get_messages(
        value.checkpoint.session.session_id,
        state=value.checkpoint.session.state["in_memory"],
    )


def test_budget_uses_tokens_model_window_output_reserve_and_one_margin():
    configured = model().model_copy(update={"context_window": 1_000_000})
    assert input_token_limit(configured, 400_000) == 320_000
    assert input_token_limit(configured, 1_000_000) == int((1_000_000 - 393216) * 0.8)
    assert input_token_limit(model(), 128000) == 102400
    assert estimate_text_tokens("abc" * 1000) == 1000
    assert estimate_text_tokens("汉" * 1000) == 3000
    assert estimate_text_tokens("{}" * 1000) == 2000


def test_zero_input_capacity_blocks_dispatch_without_dividing_by_the_limit():
    configured = model().model_copy(update={"context_window": 1})
    view = provider([], configured_model=configured)
    assert view.input_limit == 0
    with pytest.raises(TurnBoundary):
        view.prepare_provider_messages([text("new", "A real model input")])
    assert view.checkpoint.session.state["ctf_context_blocked"]["limit_tokens"] == 0


@pytest.mark.parametrize(
    "value",
    [
        "",
        "".join(chr(code) for code in range(128)),
        "".join(chr(code) for code in range(128, 4096)),
        "上下文 résumé κόσμος नमस्ते 👩🏽‍💻\n{}[]\t" * 1000,
    ],
)
def test_fast_token_estimate_preserves_ascii_whitespace_and_unicode_budget(value):
    ascii_words = sum(char.isascii() and (char.isalnum() or char.isspace()) for char in value)
    other_bytes = sum(
        len(char.encode("utf-8"))
        for char in value
        if not (char.isascii() and (char.isalnum() or char.isspace()))
    )
    assert estimate_text_tokens(value) == (ascii_words + 2) // 3 + other_bytes


def test_native_checkpoint_signatures_match_maf_roundtrip_for_rich_contents():
    messages = [
        Message(
            role="assistant",
            message_id="assistant",
            author_name="worker",
            contents=[
                Content.from_text("result\x00with storage escaping"),
                Content.from_text_reasoning(text="reasoning", protected_data='{"opaque":true}'),
                Content.from_function_call("call", "inspect", arguments={"nested": [1, "汉"]}),
            ],
            additional_properties={"_group": {"id": "group"}, "trace_only": "ignored"},
        ),
        Message(role="tool", contents=[Content.from_function_result("call", result={"a": [1, 2]})]),
        Message(role="user", contents=[Content.from_data(data=b"image", media_type="image/png")]),
    ]
    session = AgentSession()
    session.state["in_memory"] = {"messages": messages}
    payload = storage_safe(session.to_dict())
    for saved in payload["state"]["in_memory"]["messages"]:
        assert serialized_message_signature(saved) == message_signature(Message.from_dict(saved))


def test_trace_metadata_cannot_trigger_history_eviction():
    message = text("message", "visible")
    before = estimate_messages_tokens([message])
    message.contents[0].additional_properties["deepseek_reasoning"] = "trace" * 100000
    message.additional_properties["internal"] = "state" * 100000
    assert estimate_messages_tokens([message]) == before


def test_responses_estimate_omits_plain_trace_but_includes_encrypted_replay():
    message = Message(
        role="assistant", contents=[Content.from_text_reasoning(text="trace" * 10000)]
    )
    assert estimate_messages_tokens([message], provider="openai_responses") < 100
    message.contents[0].id = "reasoning-item"
    message.contents[0].protected_data = "encrypted" * 10000
    assert estimate_messages_tokens([message], provider="openai_responses") > 10000


def test_preview_stays_identical_after_new_results_budget_change_and_session_restore():
    first = result("first", "HEAD" + "!" * 50000 + "TAIL")
    session = AgentSession()
    view = stable_tool_previews([first], session.state, 10000)
    preview = view[0].contents[0].result
    assert str(preview).startswith("[Truncated model preview:")
    assert "HEAD" in str(preview) and "TAIL" in str(preview)
    restored = AgentSession.from_dict(session.to_dict())
    second = result("second", "?" * 50000)
    later = stable_tool_previews([first, second], restored.state, 2000)
    assert later[0].contents[0].result == preview
    assert first.contents[0].result != preview


def test_short_result_is_not_retroactively_shortened():
    session = AgentSession()
    first = result("short", "abc" * 30)
    assert stable_tool_previews([first], session.state, 10000)[0] is first
    assert stable_tool_previews([first], session.state, 1)[0] is first


async def test_batch_eviction_leaves_headroom_and_next_calls_keep_identical_prefix():
    messages = [
        text("old", "x" * 1800),
        text("done", "complete", "assistant"),
        text("middle", "y" * 600),
        text("done2", "complete", "assistant"),
        text("latest", "continue"),
    ]
    value = provider(messages)
    view = await history(value)
    assert [message.message_id for message in view][1:] == ["middle", "done2", "latest"]
    assert "Synthetic task" in view[0].text
    state = value.checkpoint.session.state
    assert state["ctf_history_view"]["epoch"] == 1
    messages.extend([text("reply", "next", "assistant"), text("more", "continue")])
    again = await history(value)
    assert [m.to_dict() for m in again[: len(view)]] == [m.to_dict() for m in view]
    assert state["ctf_history_view"]["epoch"] == 1
    assert len(state["in_memory"]["messages"]) == 7


def test_parallel_tool_batch_boundary_waits_for_every_result():
    call = Message(
        role="assistant",
        contents=[
            Content.from_function_call("a", "inspect"),
            Content.from_function_call("b", "inspect"),
        ],
    )
    a, b = result("a", "done"), result("b", "done")
    assert safe_history_boundaries([call, a, b, text("next", "continue")], 0) == [3]
    assert safe_history_boundaries([call, a], 0) == []


def test_responses_reasoning_and_annotated_tool_group_remain_atomic():
    reasoning = Message(
        role="assistant",
        contents=[
            Content.from_text_reasoning(text="summary", id="reasoning", protected_data="opaque")
        ],
    )
    call = Message(role="assistant", contents=[Content.from_function_call("call", "inspect")])
    output = result("call", "done")
    final = text("answer", "done", "assistant")
    for message in [reasoning, call, output, final]:
        message.additional_properties["_group"] = {"id": "provider-group", "has_reasoning": True}
    assert safe_history_boundaries(
        [reasoning, call, output, final, text("next", "continue")], 0
    ) == [4]
    # Even unannotated SDK streams must not cut a standalone reasoning item
    # away from the following calls/results.
    for message in [reasoning, call, output, final]:
        message.additional_properties.pop("_group")
    assert safe_history_boundaries([reasoning, call, output, text("next", "continue")], 0) == [3]


def test_responses_counts_grouped_clear_summary_and_preserves_unmetered_opaque_payload():
    summary = Message(
        role="assistant", contents=[Content.from_text_reasoning(text="summary" * 1000, id="group")]
    )
    encrypted = Message(
        role="assistant",
        contents=[Content.from_text_reasoning(id="group", protected_data="opaque" * 1000)],
    )
    assert estimate_messages_tokens([summary], provider="openai_responses") < 100
    before = estimate_messages_tokens([summary, encrypted], provider="openai_responses")
    assert before > 2300
    assert before < 2400
    encrypted.contents[0].protected_data = "opaque" * 1000000
    assert estimate_messages_tokens([summary, encrypted], provider="openai_responses") == before
    assert encrypted.contents[0].protected_data == "opaque" * 1000000


def test_structured_protected_reasoning_counts_clear_text_but_not_opaque_members():
    reasoning = Content.from_text_reasoning(
        protected_data=('{"text":"clear summary", "nested":{"encrypted_content":"opaque"}}')
    )
    message = Message(role="assistant", contents=[reasoning])
    before = estimate_messages_tokens([message], provider="openai_chat")
    assert reasoning.protected_data is not None
    reasoning.protected_data = reasoning.protected_data.replace("opaque", "encoded" * 10000)
    assert estimate_messages_tokens([message], provider="openai_chat") == before
    reasoning.protected_data = '{"text":"' + "clear" * 10000 + '"}'
    assert estimate_messages_tokens([message], provider="openai_chat") > before + 10000


def test_native_noncontiguous_call_result_group_and_late_parallel_result():
    call = Message(
        role="assistant",
        contents=[
            Content.from_function_call("a", "inspect"),
            Content.from_function_call("b", "inspect"),
        ],
    )
    unrelated = text("intervening", "notification")
    a, b = result("a", "first"), result("b", "late")
    final = text("completed", "done", "assistant")
    assert safe_history_boundaries([call, unrelated, a, final], 0) == []
    assert safe_history_boundaries([call, unrelated, a, final, b], 0) == [5]


def test_native_annotation_does_not_change_durable_message_ids_properties_or_content():
    reasoning = Message(
        role="assistant",
        contents=[
            Content.from_text_reasoning(text="summary", id="reasoning", protected_data="opaque")
        ],
    )
    call = Message(role="assistant", contents=[Content.from_function_call("call", "inspect")])
    output = result("call", "done")
    messages = [reasoning, call, text("intervening", "notification"), output]
    before = [message.to_dict() for message in messages]
    assert safe_history_boundaries(messages, 0) == [4]
    assert [message.to_dict() for message in messages] == before
    assert reasoning.message_id is None and call.message_id is None


async def test_batch_epoch_and_recovery_prefix_survive_native_session_restore():
    messages = [
        text("old", "x" * 3000),
        text("done", "complete", "assistant"),
        text("latest", "continue"),
    ]
    original = provider(messages)
    first = await history(original)
    restored = AgentSession.from_dict(original.checkpoint.session.to_dict())
    second = provider(
        restored.state["in_memory"]["messages"], turn="next-turn", state=restored.state
    )
    later = await history(second)
    assert [message.to_dict() for message in later] == [message.to_dict() for message in first]
    assert second.checkpoint.session.state["ctf_history_view"]["epoch"] == 1
    cast(Any, second.service).runtime_state.assert_not_awaited()


async def test_legacy_checkpoint_cutoff_moves_forward_then_responses_replay_is_valid():
    reasoning = Message(
        role="assistant",
        contents=[
            Content.from_text_reasoning(text="summary", id="rs_old", protected_data="opaque")
        ],
    )
    call = Message(role="assistant", contents=[Content.from_function_call("call", "inspect")])
    messages = [reasoning, call, result("call", "done"), text("latest", "continue")]
    annotate_message_groups(messages)
    original = provider(messages)
    original.checkpoint.session.state["ctf_history_view"] = {"excluded_before": 1, "epoch": 4}
    restored = AgentSession.from_dict(original.checkpoint.session.to_dict())
    durable = restored.state["in_memory"]["messages"]
    # SDK serialization only: no provider request or credential resolution.
    client = OpenAIChatClient(model="synthetic", api_key="not-used")
    with pytest.raises(ChatClientInvalidRequestException, match="encrypted reasoning"):
        client._prepare_messages_for_openai(durable[1:], request_uses_service_side_storage=False)
    value = provider(durable, state=restored.state)
    before = [message.to_dict() for message in durable]
    view = await history(value)
    assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == 3
    assert value.checkpoint.session.state["ctf_history_view"]["epoch"] == 5
    assert "Synthetic task" in view[0].text and view[-1].message_id == "latest"
    replay = client._prepare_messages_for_openai(view, request_uses_service_side_storage=False)
    assert all(item.get("type") not in {"function_call", "function_call_output"} for item in replay)
    assert [message.to_dict() for message in durable] == before
    assert [message.to_dict() for message in await history(value)] == [
        message.to_dict() for message in view
    ]
    cast(Any, value.service).runtime_state.assert_awaited_once()


async def test_legacy_cutoff_inside_unfinished_batch_blocks_until_new_recovery_turn():
    call = Message(
        role="assistant",
        contents=[
            Content.from_function_call("a", "inspect"),
            Content.from_function_call("b", "inspect"),
        ],
    )
    messages = [call, result("a", "done"), text("latest", "continue")]
    annotate_message_groups(messages)
    original = provider(messages)
    original.checkpoint.session.state["ctf_history_view"] = {"excluded_before": 1, "epoch": 2}
    restored = AgentSession.from_dict(original.checkpoint.session.to_dict())
    value = provider(restored.state["in_memory"]["messages"], state=restored.state)
    assert align_history_start(messages, 1) is None
    with pytest.raises(TurnBoundary):
        await history(value)
    with pytest.raises(TurnBoundary):
        await history(value)
    assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == 1
    cast(Any, value.service).runtime_state.assert_not_awaited()
    resumed = provider(messages, turn="resumed", state=value.checkpoint.session.state)
    view = await history(resumed)
    assert len(view) == 1 and "list_challenges" in view[0].text
    assert resumed.checkpoint.session.state["ctf_history_view"]["excluded_before"] == len(messages)
    assert resumed.checkpoint.session.state["ctf_history_view"]["epoch"] == 3
    assert resumed.checkpoint.session.state["in_memory"]["messages"] == messages
    assert not any(
        content.type == "function_call" for message in view for content in message.contents
    )
    # A late result becomes a completed atomic group; it is skipped forward,
    # never reintroduced together with its original executable declaration.
    late = [*messages, result("b", "late")]
    assert align_history_start(late, 1) == 4


async def test_new_turn_recovers_blocked_history_once_using_current_blackboard():
    original = text("oversized", "!" * 10000)
    value = provider([original])
    with pytest.raises(TurnBoundary):
        await history(value)
    with pytest.raises(TurnBoundary):
        await history(value)
    cast(Any, value.checkpoint.service).runtime_state.assert_not_awaited()
    recovered = provider([original], turn="resumed", state=value.checkpoint.session.state)
    view = await history(recovered)
    assert len(view) == 1
    assert "Synthetic task" in view[0].text and "list_challenges" in view[0].text
    assert recovered.checkpoint.session.state["in_memory"]["messages"] == [original]
    assert [message.to_dict() for message in await history(recovered)] == [
        message.to_dict() for message in view
    ]
    cast(Any, recovered.checkpoint.service).runtime_state.assert_awaited_once()
    # Recovery never bypasses a newly appended unshrinkable instruction.
    with pytest.raises(TurnBoundary):
        recovered.prepare_provider_messages([*view, text("fresh", "!" * 10000)])


async def test_fixed_prompt_overhead_and_oversized_recovery_snapshot_still_block():
    value = provider([], fixed=900)
    with pytest.raises(TurnBoundary):
        value.prepare_provider_messages([])
    value.checkpoint.session.state["ctf_context_blocked"] = {"turn_id": "previous"}
    cast(Any, value.service).runtime_state.return_value = {"task": {"goal": "!" * 10000}}
    view = await history(value)
    with pytest.raises(TurnBoundary):
        value.prepare_provider_messages(view)


def test_context_block_records_only_present_ids_for_mailbox_recovery():
    value = provider([])
    value.block_context(
        [
            text("input", "continue"),
            Message(role="assistant", contents=[Content.from_text("model output")]),
        ],
        100,
        0,
    )
    assert value.checkpoint.session.state["ctf_context_blocked"]["message_ids"] == ["input"]


async def test_runner_uses_saved_task_options_for_steps_context_and_fixed_prompt_budget():
    class Service(DispatchService):
        async def runtime_state(self, _):
            state = await super().runtime_state(_)
            state["task"]["ctf_options"] = {"max_steps": 4000, "context_threshold": 400000}
            return state

    service = Service()
    service.post("input", "Synthetic work")
    client = ScriptedChatClient([ScriptStep(text="done")])
    profiles = []

    def factory(_, profile):
        profiles.append(profile)
        return client

    runner = CtfRunner(cast(Any, service), cast(Any, None), client_factory=factory)
    await runner.run(
        "task",
        {"id": "member-1", "display_name": "Test", "role": "teammate"},
        {**TURN, "token": "fake"},
    )
    assert profiles[0].options.max_steps == 4000
    assert profiles[0].options.context_threshold == 400000
    outputs = [row["body"] for row in service.observations if row["kind"] == "model_output"]
    assert outputs[0]["context_view"]["input_limit_tokens"] == 320000
    assert outputs[0]["context_view"]["fixed_input_tokens"] > 0
