"""Native MAF dispatch and model-only result previews use no provider or environment."""

import copy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agent_framework import (
    Agent,
    Content,
    Message,
    MessageInjectionMiddleware,
    ResponseStream,
    tool,
)
from bbx_contracts.ctf import load_ctf_profile
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.context import visible_message_text
from bbx_runtime.ctf.middleware import (
    CtfHistoryProvider,
    MailboxMiddleware,
    TurnBoundary,
    flush_usage_outbox,
)
from bbx_runtime.ctf.provider_boundary import provider_call_boundary
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_runtime import TURN, MemoryCtf


class DispatchService(MemoryCtf):
    def __init__(self):
        super().__init__()
        self.client = self
        self.profile = load_ctf_profile(Path("profiles/ctf"))
        self.profile.options.context_threshold = 3000
        self.calls = []
        self.finished = []
        self.reservations = {}
        self.unknown = {}

    async def runtime_state(self, _):
        return {
            "task": {
                "agent_profile": "ctf",
                "agent_profile_version": 1,
                "budget": {"max_minutes": 10},
                "ctf_control": {"budget_reservations": self.reservations},
            }
        }

    async def get_profile(self, *_):
        return {"profile": self.profile.model_dump(mode="json")}

    async def runtime(self, task_id, operation, **body):
        self.calls.append((operation, copy.deepcopy(body)))
        if operation == "authorize_member":
            if "reservation_id" in body:
                self.reservations.setdefault(body["reservation_id"], {"state": "admitted"})
            return {}
        if operation == "mark_reservation_sent":
            self.reservations[body["reservation_id"]]["state"] = "sent"
            return {}
        if operation == "bill_usage":
            self.reservations.pop(body["reservation_id"], None)
            return {}
        if operation == "finish_turn":
            self.finished.append(body)
            self.unknown.update(
                {key: value for key, value in self.reservations.items() if value["state"] == "sent"}
            )
            self.reservations.clear()
            return {}
        return await super().runtime(task_id, operation, **body)


async def native(service, checkpoint, client, *, stream=True, tools=()):
    mailbox = MailboxMiddleware(cast(Any, service), checkpoint, TURN, 10, Decimal("0.6"))
    history = CtfHistoryProvider(cast(Any, service), checkpoint, TURN, service.profile.model, 3000)
    with provider_call_boundary(client, mailbox.enter_provider, history.prepare_provider_messages):
        async with Agent(
            client=client,
            instructions="Synthetic task",
            tools=tools,
            context_providers=[history],
            middleware=[mailbox, MessageInjectionMiddleware()],
            require_per_service_call_history_persistence=True,
        ) as agent:
            response = agent.run([], session=checkpoint.session, stream=stream)
            if stream:
                assert isinstance(response, ResponseStream)
                async for _ in response:
                    pass
                return await response.get_final_response()
            return await response


async def test_unshrinkable_history_never_dispatches_or_consumes_steps_or_continues():
    service = DispatchService()
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    original = Message(
        role="user", message_id="large-input", contents=[Content.from_text("x" * 20000)]
    )
    checkpoint.session.state["in_memory"] = {"messages": [original]}
    await checkpoint.save()
    client = ScriptedChatClient([ScriptStep(text="must not run")])
    runner = CtfRunner(
        cast(CtfClient, service), cast(Any, SimpleNamespace()), client_factory=lambda *_: client
    )
    await runner.run(
        "task",
        {"id": "member-1", "display_name": "Test", "role": "teammate"},
        {**TURN, "token": "fake"},
    )
    assert not client.received_messages
    assert not service.unknown and not service.reservations
    assert service.finished[0]["end_reason"] == "context_blocked"
    assert "Lead action is required" in service.finished[0]["answer"]
    assert not any(
        operation in {"mark_reservation_sent", "enqueue_continuation"}
        for operation, _ in service.calls
    )
    assert service.saved is not None
    state = service.saved["session"]["state"]
    assert not state.get("ctf_steps")
    assert "ctf_model_call" not in state
    assert not any(observation["kind"] == "model_output" for observation in service.observations)
    assert state["ctf_context_blocked"]["message_ids"] == ["large-input"]
    assert len(state["ctf_context_blocked"]["fingerprint"]) == 64
    assert "x" * 20000 in str(state["in_memory"])


@pytest.mark.parametrize("stream", [True, False])
async def test_native_tool_loop_previews_new_result_and_keeps_full_session_and_pairs(
    stream, monkeypatch
):
    ticks = iter([10.0, 10.75, 11.0, 12.5])
    monkeypatch.setattr("bbx_runtime.ctf.middleware.monotonic", lambda: next(ticks))
    service = DispatchService()
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    result = "HEAD" + "x" * 20000 + "TAIL"

    @tool
    async def inspect() -> str:
        """Return a synthetic large observation."""
        return result

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("inspect", call_id="inspect-call"),)),
            ScriptStep(text="finished", expect_contains="Truncated model preview"),
        ]
    )
    original_method = client._inner_get_response
    await native(service, checkpoint, client, stream=stream, tools=[inspect])
    assert client._inner_get_response == original_method
    assert len(client.received_messages) == 2
    outgoing = client.received_messages[1]
    calls = [
        content.call_id
        for message in outgoing
        for content in message.contents
        if content.type == "function_call"
    ]
    results = [
        content
        for message in outgoing
        for content in message.contents
        if content.type == "function_result"
    ]
    assert calls == ["inspect-call"] and [content.call_id for content in results] == calls
    assert "HEAD" in str(results[0].result) and "TAIL" in str(results[0].result)
    assert result not in str(results[0].result)
    durable = checkpoint.session.state["in_memory"]["messages"]
    assert any(
        content.result == result
        for message in durable
        for content in message.contents
        if content.type == "function_result"
    )
    sent = [
        body["reservation_id"]
        for operation, body in service.calls
        if operation == "mark_reservation_sent"
    ]
    assert sent == [f"{TURN['id']}:1", f"{TURN['id']}:2"]
    assert checkpoint.session.state["ctf_steps"][TURN["id"]] == 2
    outputs = [row["body"] for row in service.observations if row["kind"] == "model_output"]
    assert [row["elapsed_ms"] for row in outputs] == [750, 1500]
    assert outputs[0]["context_view"]["message_count"] == 0
    assert outputs[1]["context_view"]["message_count"] == len(outgoing)
    assert outputs[1]["context_view"]["estimated_chars"] == sum(
        len(visible_message_text(message, provider=service.profile.model.provider))
        for message in outgoing
    )
    assert outputs[1]["context_view"]["truncated_tool_result_count"] == 1
    assert outputs[1]["context_view"]["excluded_message_count"] == 0
    bills = [body["usage"] for operation, body in service.calls if operation == "bill_usage"]
    assert [row["usage"] for row in outputs] == bills
    assert all(row["started_at"] for row in outputs)
    for messages, options in zip(client.received_messages, client.received_options, strict=True):
        prompt = str([message.to_dict() for message in messages]) + str(options.get("instructions"))
        assert "ctf_model_call" not in prompt and "started_at" not in prompt
        assert "_started_monotonic" not in prompt


async def test_oversized_new_instruction_is_not_silently_previewed_or_dispatched():
    service = DispatchService()
    service.post("huge-instruction", "x" * 20000)
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    client = ScriptedChatClient([ScriptStep(text="must not run")])
    with pytest.raises(TurnBoundary, match="context_blocked"):
        await native(service, checkpoint, client)
    assert not client.received_messages
    assert "huge-instruction" in checkpoint.session.state["ctf_context_blocked"]["message_ids"]
    assert not checkpoint.session.state.get("ctf_steps")
    assert not any(operation == "mark_reservation_sent" for operation, _ in service.calls)


async def test_history_exclusion_boundary_is_not_reset_by_next_turn():
    service = DispatchService()
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    checkpoint.session.state["in_memory"] = {
        "messages": [
            Message(role="user", message_id="old", contents=[Content.from_text("x" * 20000)]),
            Message(role="assistant", message_id="answer", contents=[Content.from_text("done")]),
            Message(role="user", message_id="new", contents=[Content.from_text("continue")]),
        ]
    }
    await checkpoint.save()
    history = CtfHistoryProvider(cast(Any, service), checkpoint, TURN, service.profile.model, 3000)
    for _ in range(2):
        view = await history.get_messages(
            checkpoint.session.session_id, state=checkpoint.session.state["in_memory"]
        )
        assert [message.message_id for message in view][-1:] == ["new"]
        assert "list_challenges" in view[0].text
    assert checkpoint.session.state["ctf_history_view"]["excluded_before"] == 2
    assert len(checkpoint.session.state["in_memory"]["messages"]) == 3


@pytest.mark.parametrize("legacy", [False, True])
async def test_historical_usage_flush_uses_confirmed_original_identity_and_not_current_turn(legacy):
    entry = {
        "request_id": "receipt",
        "reservation_id": "old-turn:1",
        "usage": {"cost": "0.1"},
        "agent_id": "member-1",
        "turn_id": "old-turn",
        "generation": 2,
    }
    if legacy:
        for field in ("agent_id", "turn_id", "generation"):
            entry.pop(field)
    service = SimpleNamespace(runtime=AsyncMock(return_value={"request_ids": ["receipt"]}))
    checkpoint = SimpleNamespace(
        task_id="task",
        agent_id="member-1",
        save=AsyncMock(),
        session=SimpleNamespace(state={"ctf_usage_outbox": [entry]}),
    )
    await flush_usage_outbox(cast(Any, service), cast(Any, checkpoint), TURN)
    service.runtime.assert_awaited_once_with("task", "reconcile_usage_outbox", agent_id="member-1")
    assert not checkpoint.session.state["ctf_usage_outbox"]
    checkpoint.save.assert_awaited_once()


async def test_unconfirmed_historical_usage_is_never_removed_from_outbox():
    entry = {"request_id": "receipt", "reservation_id": "old-turn:1", "usage": {"cost": "0.1"}}
    service = SimpleNamespace(runtime=AsyncMock(return_value={"request_ids": []}))
    checkpoint = SimpleNamespace(
        task_id="task",
        agent_id="member-1",
        save=AsyncMock(),
        session=SimpleNamespace(state={"ctf_usage_outbox": [entry]}),
    )
    with pytest.raises(RuntimeError, match="absent from the confirmed Session"):
        await flush_usage_outbox(cast(Any, service), cast(Any, checkpoint), TURN)
    assert checkpoint.session.state["ctf_usage_outbox"] == [entry]
    checkpoint.save.assert_not_awaited()


async def test_history_trimming_drops_entire_parallel_tool_result_batch():
    service = DispatchService()
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    checkpoint.session.state["in_memory"] = {
        "messages": [
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call("first", "inspect", arguments={"q": "x" * 20000}),
                    Content.from_function_call("second", "inspect"),
                ],
            ),
            Message(
                role="tool", contents=[Content.from_function_result("first", result="x" * 20000)]
            ),
            Message(role="tool", contents=[Content.from_function_result("second", result="done")]),
            Message(role="user", message_id="new", contents=[Content.from_text("continue")]),
        ]
    }
    await checkpoint.save()
    history = CtfHistoryProvider(cast(Any, service), checkpoint, TURN, service.profile.model, 3000)
    view = await history.get_messages(
        checkpoint.session.session_id, state=checkpoint.session.state["in_memory"]
    )
    assert [message.message_id for message in view][-1:] == ["new"]
    assert "list_challenges" in view[0].text
    assert checkpoint.session.state["ctf_history_view"]["excluded_before"] == 3
