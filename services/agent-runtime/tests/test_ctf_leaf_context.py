"""Compact only confirmed history at the native MAF provider boundary."""

from copy import deepcopy
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agent_framework import Content, Message, tool
from bbx_runtime.ctf.context import estimate_messages_tokens, message_signature
from bbx_runtime.ctf.middleware import CtfHistoryProvider, TurnBoundary
from bbx_runtime.ctf.session import CtfCheckpoint, load_checkpoint
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_context import result, text
from test_ctf_provider_boundary import DispatchService, native
from test_ctf_runtime import TURN


async def saved_provider(messages, *, threshold=1000):
    service = DispatchService()
    service.runtime_state = AsyncMock(wraps=service.runtime_state)
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    checkpoint.session.state["in_memory"] = {"messages": messages}
    await checkpoint.save()
    value = CtfHistoryProvider(
        cast(Any, service), checkpoint, TURN, service.profile.model, threshold
    )
    return service, value


async def loaded(value):
    return await value.get_messages(
        value.checkpoint.session.session_id, state=value.checkpoint.session.state["in_memory"]
    )


def almost_full_history():
    return [
        Message(role="assistant", contents=[Content.from_text("x" * 1000)]),
        Message(role="assistant", contents=[Content.from_text("y" * 1000)]),
        Message(role="assistant", contents=[Content.from_function_call("call", "inspect")]),
    ]


async def test_leaf_eviction_maps_distinct_none_ids_keeps_new_inputs_and_restores_prefix():
    original = almost_full_history()
    before = [message.to_dict() for message in original]
    service, value = await saved_provider(original)
    view = await loaded(value)
    fresh = text("fresh", "!" * 100)
    outgoing = [*view, result("call", "x" * 180), fresh]
    assert estimate_messages_tokens(view) < value.input_limit
    assert estimate_messages_tokens(outgoing) > value.input_limit
    projected = value.prepare_provider_messages(outgoing)
    assert fresh in projected
    assert any(content.call_id == "call" for message in projected for content in message.contents)
    assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == 2
    assert value.checkpoint.session.state["ctf_history_view"]["epoch"] == 1
    assert [message.to_dict() for message in original] == before
    service.lose_response = True
    await value.checkpoint.save()
    restored = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    restarted = CtfHistoryProvider(cast(Any, service), restored, TURN, service.profile.model, 1000)
    again = await loaded(restarted)
    assert again[0].to_dict() == projected[0].to_dict()
    assert restored.session.state["ctf_history_view"]["epoch"] == 1
    cast(AsyncMock, service.runtime_state).assert_awaited_once()


@pytest.mark.parametrize("mismatch", ["reordered", "copied_content", "changed", "unconfirmed"])
async def test_uncertain_history_mapping_never_evicts_a_new_or_changed_message(mismatch):
    original = almost_full_history()
    _, value = await saved_provider(original)
    view = await loaded(value)
    if mismatch == "reordered":
        view[0], view[1] = view[1], view[0]
    elif mismatch == "copied_content":
        view = deepcopy(view)
    elif mismatch == "changed":
        view[0].contents[0].text = "z" * 1900
    else:
        cast(CtfCheckpoint, value.checkpoint).confirmed_history = ()
    with pytest.raises(TurnBoundary, match="context_blocked"):
        value.prepare_provider_messages([*view, text("fresh", "!" * 200)])
    assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == 0


async def test_oversized_fresh_input_and_confirmed_current_turn_instruction_stay_protected():
    original = almost_full_history()
    _, value = await saved_provider(original)
    view = await loaded(value)
    with pytest.raises(TurnBoundary, match="context_blocked"):
        value.prepare_provider_messages([*view, text("fresh", "!" * 2000)])
    assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == 0
    original[0].message_id = "current-instruction"
    original[0].role = "user"
    await value.checkpoint.save()
    value.checkpoint.session.state["ctf_active_input_ids"] = {
        "turn_id": TURN["id"],
        "ids": ["current-instruction"],
    }
    view = await loaded(value)
    projected = value.prepare_provider_messages([*view, text("fresh", "!" * 200)])
    assert any(
        message.message_id == "current-instruction" and message.text == original[0].text
        for message in projected
    )
    assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == 2
    assert (
        value.checkpoint.session.state["ctf_context_recovery"]["protected_inputs"][0]["message_id"]
        == "current-instruction"
    )


@pytest.mark.parametrize("input_kind", ["unconfirmed", "current_turn"])
async def test_history_loading_cannot_bypass_confirmation_or_current_input_protection(input_kind):
    original = [text("old", "previous work", "assistant")]
    _, value = await saved_provider(original)
    fresh = text("fresh", "!" * 1600, "assistant")
    original.append(fresh)
    if input_kind == "current_turn":
        fresh.role = "user"
        value.checkpoint.session.state["ctf_active_input_ids"] = {
            "turn_id": TURN["id"],
            "ids": ["fresh"],
        }
        await value.checkpoint.save()
    with pytest.raises(TurnBoundary, match="context_blocked"):
        await loaded(value)
    assert value.checkpoint.session.state.get("ctf_history_view", {}).get("excluded_before", 0) == 0
    assert original[-1] is fresh and fresh.text == "!" * 1600


async def test_leaf_compaction_keeps_parallel_batches_and_reasoning_atomic_with_stable_preview():
    reasoning = Message(
        role="assistant",
        contents=[Content.from_text_reasoning(text="summary", id="rs", protected_data="opaque")],
    )
    first = Message(
        role="assistant",
        contents=[
            Content.from_function_call("a", "inspect", arguments={"q": "x" * 900}),
            Content.from_function_call("b", "inspect"),
        ],
    )
    a = Message(role="tool", contents=[Content.from_function_result("a", result="x" * 20000)])
    b = Message(role="tool", contents=[Content.from_function_result("b", result="done")])
    last = Message(
        role="assistant",
        contents=[
            Content.from_function_call("c", "inspect"),
            Content.from_function_call("d", "inspect"),
        ],
    )
    original = [reasoning, first, a, b, last]
    before = [message.to_dict() for message in original]
    _, value = await saved_provider(original)
    view = await loaded(value)
    preview = view[2].contents[0].result
    assert str(preview).startswith("[Truncated model preview:")
    padding = value.input_limit - estimate_messages_tokens(view) + 20
    fresh = text("fresh", "!" * padding)
    projected = value.prepare_provider_messages(
        [*view, result("c", "done"), result("d", "done"), fresh]
    )
    assert fresh in projected
    assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == 4
    call_ids = {content.call_id for message in projected for content in message.contents}
    assert {"c", "d"} <= call_ids and not {"a", "b"} & call_ids
    assert not any(content.id == "rs" for message in projected for content in message.contents)
    assert [message.to_dict() for message in original] == before
    assert value.checkpoint.session.state["ctf_tool_previews"]["None:a:0"] == preview


async def test_leaf_cannot_discard_a_group_with_an_unknown_parallel_tool_outcome():
    call = Message(
        role="assistant",
        contents=[
            Content.from_function_call("a", "inspect", arguments={"q": "x" * 1300}),
            Content.from_function_call("b", "inspect"),
        ],
    )
    _, value = await saved_provider([call, result("a", "done")])
    view = await loaded(value)
    with pytest.raises(TurnBoundary, match="context_blocked"):
        value.prepare_provider_messages([*view, text("fresh", "!" * 300)])
    assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == 0


@pytest.mark.parametrize("stream", [False, True])
async def test_native_leaf_loop_compacts_once_preserves_inputs_and_commits_before_dispatch(stream):
    original = [
        Message(role="assistant", contents=[Content.from_text("x" * 5700)]),
        Message(role="assistant", contents=[Content.from_text("done")]),
    ]
    before = [message.to_dict() for message in original]
    service, value = await saved_provider(original, threshold=3000)
    checkpoint = value.checkpoint
    service.post("initial", "INITIAL instruction must remain visible")

    @tool
    def inspect_first() -> str:
        service.post("late", "LATE instruction must remain visible")
        return "!" * 280

    @tool
    def inspect_second() -> str:
        return "?" * 250

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("inspect_first", call_id="first"),)),
            ScriptStep(calls=(ScriptToolCall("inspect_second", call_id="second"),)),
            ScriptStep(text="done"),
        ]
    )
    dispatched = []
    original_leaf = client._inner_get_response

    def inspect_dispatch(**kwargs):
        dispatched.append((set(service.delivered), deepcopy(service.saved)))
        return original_leaf(**kwargs)

    client._inner_get_response = inspect_dispatch
    await native(service, checkpoint, client, stream=stream, tools=[inspect_first, inspect_second])
    assert len(client.received_messages) == 3
    assert len(dispatched) == 3
    assert dispatched[1][0] == {"initial"}
    assert dispatched[1][1]["session"]["state"]["ctf_history_view"]["epoch"] == 1
    assert dispatched[1][1]["session"]["state"]["ctf_history_view"]["excluded_before"] == 1
    assert service.delivered == {"initial", "late"}
    for messages in client.received_messages[1:]:
        joined = "\n".join(message.text for message in messages)
        assert "INITIAL instruction" in joined and "LATE instruction" in joined
        assert "Context recovery" in messages[0].text
    assert checkpoint.session.state["ctf_history_view"]["epoch"] == 1
    assert [message.to_dict() for message in original] == before
    cast(AsyncMock, service.runtime_state).assert_awaited_once()


async def test_confirmed_history_proof_comes_from_committed_payload_not_mutated_live_session():
    service, value = await saved_provider([text("saved", "committed", "assistant")])
    checkpoint = value.checkpoint
    original_runtime = service.runtime

    async def mutate_after_commit(task_id, operation, **body):
        response = await original_runtime(task_id, operation, **body)
        if operation == "checkpoint":
            checkpoint.session.state["in_memory"]["messages"].append(
                text("unconfirmed", "new", "assistant")
            )
        return response

    service.runtime = mutate_after_commit
    await checkpoint.save()
    raw = checkpoint.session.state["in_memory"]["messages"]
    assert len(raw) == 2
    assert cast(CtfCheckpoint, checkpoint).confirmed_history == (message_signature(raw[0]),)
