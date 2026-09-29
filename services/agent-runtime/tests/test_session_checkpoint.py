"""MAF's real history survives model calls, tool loops, and a cold restore."""

import asyncio
import json
from pathlib import Path
from typing import cast

import httpx
import pytest
from agent_framework import Agent, AgentSession, Content, Message, tool
from bbx_objects import ObjectStore
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.context import RunContext
from bbx_runtime.middleware import ToolLogMiddleware
from bbx_runtime.models import load_runtime_profile
from bbx_runtime.session import (
    CheckpointHistoryProvider,
    SessionCheckpoint,
    _unpaired_calls,
    repair_unpaired_tool_calls,
)
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall


class MemorySessionService:
    def __init__(self):
        self.saved = None
        self.snapshots = []
        self.deliveries = []
        self.lose_next = False
        self.server_error_next = False

    async def get_agent_session(self, _task_id, _agent_id):
        if self.saved is None:
            raise RemoteError(404, "missing")
        return self.saved

    async def put_agent_session(
        self,
        _task_id,
        _agent_id,
        *,
        session,
        opening_instructions,
        origin,
        expected_revision,
        deliveries,
        review_claim,
    ):
        current_revision = self.saved["revision"] if self.saved is not None else 0
        if expected_revision != current_revision:
            raise RemoteError(409, "revision conflict")
        self.saved = {
            "session": json.loads(json.dumps(session)),
            "opening_instructions": opening_instructions,
            "origin": origin,
            "revision": current_revision + 1,
        }
        self.snapshots.append(self.saved)
        self.deliveries.extend(deliveries)
        if self.lose_next:
            self.lose_next = False
            raise httpx.ReadError("response lost")
        if self.server_error_next:
            self.server_error_next = False
            raise RemoteError(503, "post-commit notification failed")
        return self.saved


async def test_checkpoint_sends_storage_safe_snapshot_without_mutating_live_history() -> None:
    service = MemorySessionService()
    raw_result = "before" + chr(0) + "after"
    session = AgentSession()
    session.state["in_memory"] = {
        "messages": [
            Message(
                role="tool",
                contents=[Content.from_function_result("call-1", result=raw_result)],
            )
        ]
    }
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", session)
    await checkpoint.save()
    assert service.saved is not None
    stored = service.saved["session"]["state"]["in_memory"]["messages"][0]["contents"][0]
    assert stored["result"] == r"before\u0000after"
    assert stored["items"][0]["text"] == r"before\u0000after"
    live = session.state["in_memory"]["messages"][0].contents[0].result
    assert live == raw_result


async def test_tool_history_and_duplicate_text_survive_cold_restore():
    service = MemorySessionService()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", AgentSession())
    checkpoint.opening_instructions = "original instructions"

    @tool
    async def get() -> str:
        return "full tool result"

    first = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("get"),)),
            ScriptStep(text="first answer", expect_contains="full tool result"),
        ]
    )
    async with Agent(
        client=first,
        tools=[get],
        context_providers=[CheckpointHistoryProvider(checkpoint)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        response = await agent.run(
            Message(role="user", contents=[Content.from_text("same words")], message_id="one"),
            session=checkpoint.session,
        )
    assert response.text == "first answer"
    assert len(service.snapshots) == 2
    restored = await SessionCheckpoint.load(cast(BlackboardClient, service), "task", "agent")
    assert restored is not None
    assert restored.opening_instructions == "original instructions"
    history = restored.session.state["in_memory"]["messages"]
    assert any(
        "full tool result" in str(content.result)
        for msg in history
        for content in msg.contents
        if content.type == "function_result"
    )
    before = restored.session.to_dict()
    assert repair_unpaired_tool_calls(restored.session, include_interrupted=True) == 0
    assert restored.session.to_dict() == before

    second = ScriptedChatClient([ScriptStep(text="second answer", expect_contains="first answer")])
    async with Agent(
        client=second,
        context_providers=[CheckpointHistoryProvider(restored)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        response = await agent.run(
            Message(role="user", contents=[Content.from_text("same words")], message_id="two"),
            session=restored.session,
        )
    assert response.text == "second answer"
    ids = [msg.message_id for msg in restored.session.state["in_memory"]["messages"]]
    assert ids.count("one") == 1 and ids.count("two") == 1
    replayed = second.received_messages[0]
    assert any(message.message_id == "one" and message.text == "same words" for message in replayed)
    assert any(
        "full tool result" in str(content.result)
        for message in replayed
        for content in message.contents
        if content.type == "function_result"
    )


async def test_unfinished_model_call_cannot_ack_claimed_message():
    service = MemorySessionService()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", AgentSession())
    checkpoint.stage_delivery("message-1", "claim-1")
    await checkpoint.save()  # Runner cancellation/finally before MAF persisted the input.
    assert service.deliveries == []
    assert checkpoint.deliveries == [{"id": "message-1", "claim_token": "claim-1"}]
    checkpoint.session.state["in_memory"] = {
        "messages": [
            Message(role="user", message_id="message-1", contents=[Content.from_text("hello")])
        ]
    }
    await checkpoint.save()
    assert service.deliveries == [{"id": "message-1", "claim_token": "claim-1"}]


async def test_lost_checkpoint_response_reconciles_committed_revision_and_delivery():
    service = MemorySessionService()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", AgentSession())
    checkpoint.session.state["in_memory"] = {
        "messages": [
            Message(role="user", message_id="message-1", contents=[Content.from_text("hello")])
        ]
    }
    checkpoint.stage_delivery("message-1", "claim-1")
    service.lose_next = True
    await checkpoint.save()
    assert checkpoint.revision == 1
    assert checkpoint.deliveries == []
    assert service.deliveries == [{"id": "message-1", "claim_token": "claim-1"}]
    await checkpoint.save()
    assert checkpoint.revision == 2


@pytest.mark.parametrize("lose_response", [False, True])
@pytest.mark.parametrize("cancel_reason", ["runtime_restart", "heartbeat"])
async def test_cancelled_committed_put_finishes_before_next_save(
    lose_response: bool, cancel_reason: str
):
    class HeldResponseService(MemorySessionService):
        def __init__(self):
            super().__init__()
            self.committed = asyncio.Event()
            self.release = asyncio.Event()
            self.in_flight = False

        async def put_agent_session(self, *args, **kwargs):
            self.in_flight = True
            try:
                saved = await super().put_agent_session(*args, **kwargs)
                self.committed.set()
                await self.release.wait()
                if lose_response:
                    raise httpx.ReadError("response lost after commit")
                return saved
            finally:
                self.in_flight = False

    service = HeldResponseService()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", AgentSession())
    checkpoint.session.state["in_memory"] = {
        "messages": [
            Message(role="user", message_id="message-1", contents=[Content.from_text("hello")])
        ]
    }
    checkpoint.stage_delivery("message-1", "claim-1")
    pending = asyncio.create_task(checkpoint.save())
    await asyncio.wait_for(service.committed.wait(), 2)
    pending.cancel(cancel_reason)
    await asyncio.sleep(0)
    assert not pending.done()
    pending.cancel("grace_timeout")
    await asyncio.sleep(0)
    assert not pending.done()
    assert service.in_flight
    service.release.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await asyncio.wait_for(pending, 2)
    assert cancelled.value.args == (cancel_reason,)
    assert not service.in_flight
    assert checkpoint.revision == 1
    assert checkpoint.deliveries == []
    assert service.deliveries == [{"id": "message-1", "claim_token": "claim-1"}]
    assert service.saved is not None
    assert (
        service.saved["session"]["state"]["in_memory"]["messages"][0]["message_id"] == "message-1"
    )
    await checkpoint.save()
    assert checkpoint.revision == 2
    assert service.deliveries == [{"id": "message-1", "claim_token": "claim-1"}]


@pytest.mark.parametrize("conflict", [False, True])
async def test_cancelled_checkpoint_still_exposes_write_failure(conflict: bool):
    class HeldFailureService(MemorySessionService):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.in_flight = False

        async def put_agent_session(self, *args, **kwargs):
            self.in_flight = True
            try:
                self.started.set()
                await self.release.wait()
                if not conflict:
                    raise RuntimeError("write failed")
                return await super().put_agent_session(*args, **kwargs)
            finally:
                self.in_flight = False

    service = HeldFailureService()
    if conflict:
        service.saved = {
            "session": {"state": {"other_writer": True}},
            "opening_instructions": "other",
            "origin": "native",
            "revision": 1,
        }
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", AgentSession())
    pending = asyncio.create_task(checkpoint.save())
    await asyncio.wait_for(service.started.wait(), 2)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    service.release.set()
    with pytest.raises(RemoteError if conflict else RuntimeError) as error:
        await asyncio.wait_for(pending, 2)
    if conflict:
        assert isinstance(error.value, RemoteError)
        assert error.value.status == 409
        assert service.saved is not None
        assert service.saved["session"] == {"state": {"other_writer": True}}
    assert checkpoint.revision == 0
    assert not service.in_flight


async def test_post_commit_server_error_reconciles_but_claim_conflict_does_not():
    service = MemorySessionService()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", AgentSession())
    service.server_error_next = True
    await checkpoint.save()
    assert checkpoint.revision == 1
    checkpoint.revision = 0
    with pytest.raises(RemoteError) as error:
        await checkpoint.save()
    assert error.value.status == 409


async def test_cancelled_tool_call_is_repaired_before_read_only_resume():
    service = MemorySessionService()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", AgentSession())
    started = asyncio.Event()
    never = asyncio.Event()
    slow_calls = 0

    @tool
    async def slow() -> str:
        nonlocal slow_calls
        slow_calls += 1
        started.set()
        await never.wait()
        return "unreachable real result"

    first = ScriptedChatClient([ScriptStep(calls=(ScriptToolCall("slow"),))])
    async with Agent(
        client=first,
        tools=[slow],
        context_providers=[CheckpointHistoryProvider(checkpoint)],
        require_per_service_call_history_persistence=True,
    ) as agent:

        async def invoke():
            return await agent.run("start", session=checkpoint.session)

        running = asyncio.create_task(invoke())
        await asyncio.wait_for(started.wait(), 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    restored = await SessionCheckpoint.load(cast(BlackboardClient, service), "task", "agent")
    assert restored is not None
    assert len(_unpaired_calls(restored.session.state["in_memory"]["messages"])) == 1
    assert repair_unpaired_tool_calls(restored.session, include_interrupted=True) == 1
    assert _unpaired_calls(restored.session.state["in_memory"]["messages"]) == []
    await restored.save()

    @tool
    async def get() -> str:
        return "read only"

    review = ScriptedChatClient(
        [ScriptStep(text="The prior command did not finish.", expect_contains="结果未保存")]
    )
    async with Agent(
        client=review,
        tools=[get],
        context_providers=[CheckpointHistoryProvider(restored)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        response = await agent.run(
            Message(
                role="user", message_id="review-question", contents=[Content.from_text("status?")]
            ),
            session=restored.session,
        )
    assert response.text == "The prior command did not finish."
    assert slow_calls == 1
    assert _unpaired_calls(review.received_messages[0]) == []
    assert restored.session.state["bbx_recovery_repairs"] == [
        {"call_id": "script-1-1", "kind": "interrupted"}
    ]


def test_saved_real_tool_result_is_preferred_over_interruption_marker():
    session = AgentSession()
    session.state["in_memory"] = {
        "messages": [
            Message(
                role="assistant",
                contents=[Content.from_function_call("call-1", "get", arguments={})],
            )
        ]
    }
    session.state["bbx_tool_results"] = {
        "call-1": Content.from_function_result("call-1", result="actual output").to_dict()
    }
    assert repair_unpaired_tool_calls(session, include_interrupted=False) == 1
    result = session.state["in_memory"]["messages"][-1].contents[0]
    assert result.result == "actual output"
    assert result.additional_properties["bbx_recovery_repair"] == "saved_tool_result"


async def test_parallel_tools_keep_completed_real_result_when_other_tool_is_cancelled():
    class ToolService(MemorySessionService):
        async def record_tool_call(self, *_args):
            return []

    class Objects:
        async def put(self, *_args, **_kwargs):
            return None

    service = ToolService()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", AgentSession())
    ctx = RunContext(
        task_id="task",
        agent_id="agent",
        task_type="derive",
        state={"task": {"params": {}}},
        profile=load_runtime_profile(Path(__file__).resolve().parents[3] / "profiles/default"),
        service=cast(BlackboardClient, service),
        board=cast(BlackboardClient, service),
        objects=cast(ObjectStore, Objects()),
        checkpoint=checkpoint,
    )
    started = asyncio.Event()
    never = asyncio.Event()

    @tool
    async def fast() -> str:
        return "actual fast output"

    @tool
    async def slow() -> str:
        started.set()
        await never.wait()
        return "unreachable"

    client = ScriptedChatClient(
        [ScriptStep(calls=(ScriptToolCall("fast"), ScriptToolCall("slow")))]
    )
    async with Agent(
        client=client,
        tools=[fast, slow],
        middleware=[ToolLogMiddleware(ctx)],
        context_providers=[CheckpointHistoryProvider(checkpoint)],
        require_per_service_call_history_persistence=True,
    ) as agent:

        async def invoke():
            return await agent.run("start", session=checkpoint.session)

        running = asyncio.create_task(invoke())
        await asyncio.wait_for(started.wait(), 2)

        async def fast_saved() -> bool:
            return bool(
                service.saved
                and "script-1-1" in service.saved["session"]["state"].get("bbx_tool_results", {})
            )

        async with asyncio.timeout(2):
            while not await fast_saved():
                await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    restored = await SessionCheckpoint.load(cast(BlackboardClient, service), "task", "agent")
    assert restored is not None
    assert len(_unpaired_calls(restored.session.state["in_memory"]["messages"])) == 2
    assert repair_unpaired_tool_calls(restored.session, include_interrupted=False) == 1
    assert len(_unpaired_calls(restored.session.state["in_memory"]["messages"])) == 1
    assert repair_unpaired_tool_calls(restored.session, include_interrupted=True) == 1
    assert _unpaired_calls(restored.session.state["in_memory"]["messages"]) == []
    repairs = restored.session.state["bbx_recovery_repairs"]
    assert repairs == [
        {"call_id": "script-1-1", "kind": "saved_tool_result"},
        {"call_id": "script-1-2", "kind": "interrupted"},
    ]
    await restored.save()
    review = ScriptedChatClient([ScriptStep(text="The second result was unavailable.")])
    async with Agent(
        client=review,
        context_providers=[CheckpointHistoryProvider(restored)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        await agent.run("What happened?", session=restored.session)
    outgoing = review.received_messages[0]
    assert _unpaired_calls(outgoing) == []
    results = [
        str(content.result)
        for message in outgoing
        for content in message.contents
        if content.type == "function_result"
    ]
    assert any("actual fast output" in result for result in results)
    assert any("结果未保存" in result for result in results)
