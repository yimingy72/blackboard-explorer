"""Ordinary tool refusals and uncertain effects through the real MAF pipeline."""

import asyncio
import json
from typing import cast

import httpx
import pytest
from agent_framework import (
    Agent,
    AgentSession,
    FunctionInvocationContext,
    MiddlewareFailure,
    MiddlewareTermination,
    tool,
)
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.middleware import (
    GraceGateMiddleware,
    ToolArgumentFeedbackMiddleware,
    ToolLogMiddleware,
)
from bbx_runtime.session import SessionCheckpoint
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from bbx_runtime.tool_failures import propagate_tool_failure
from bbx_runtime.tools import make_board_tools
from test_middleware import FakeBoard, FakeObjects, FakeService, run_context
from test_session_checkpoint import MemorySessionService

ARGUMENTS = dict(
    statement="Synthetic direction",
    based_on=["F1"],
    expected="Result",
    method="Check",
    relates_to=["A1"],
)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadError("synthetic private committed response loss"),
        RemoteError(503, "synthetic private unavailable response"),
        RemoteError(403, "synthetic private authorization"),
        RemoteError(422, "synthetic private old round", "stale_agent_round"),
    ],
)
async def test_committed_intent_unknown_response_aborts_native_before_second_effect(failure):
    class Board(FakeBoard):
        def __init__(self):
            super().__init__()
            self.effects = []

        async def post_intent(self, task, request):
            self.effects.append(request.statement)
            raise failure

    board, service = Board(), FakeService()
    context = run_context(service, board=board)
    item = next(item for item in make_board_tools(context) if item.name == "post_intent")
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("post_intent", ARGUMENTS),)),
            ScriptStep(calls=(ScriptToolCall("post_intent", ARGUMENTS),)),
            ScriptStep(text="must not repeat"),
        ]
    )
    async with Agent(
        client=client,
        tools=[item],
        middleware=[
            ToolLogMiddleware(context),
            GraceGateMiddleware(context),
            ToolArgumentFeedbackMiddleware(),
        ],
    ) as agent:
        with pytest.raises(type(failure)) as caught, propagate_tool_failure():
            await agent.run("Synthetic publication")
    assert caught.value is failure
    assert len(board.effects) == len(client.received_messages) == 1
    assert len(service.logged) == 1
    assert "synthetic private" not in service.logged[0]["result_head"]


async def test_known_domain_refusal_corrects_locally_with_explicit_failure_marker():
    class Board(FakeBoard):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def post_intent(self, task, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise RemoteError(422, "synthetic private field", "invalid_reference")
            return {"id": "I1"}

    board, service = Board(), FakeService()
    context = run_context(service, board=board)
    item = next(item for item in make_board_tools(context) if item.name == "post_intent")
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("post_intent", ARGUMENTS),)),
            ScriptStep(
                calls=(ScriptToolCall("post_intent", {**ARGUMENTS, "based_on": ["F2"]}),),
                expect_contains="invalid_reference",
            ),
            ScriptStep(text="done", expect_contains="已提交意图 I1"),
        ]
    )
    async with Agent(
        client=client,
        tools=[item],
        middleware=[
            ToolLogMiddleware(context),
            GraceGateMiddleware(context),
            ToolArgumentFeedbackMiddleware(),
        ],
    ) as agent:
        assert (await agent.run("Synthetic correction")).text == "done"
    recorded = json.loads(service.logged[0]["result_head"])[0]
    assert recorded["additional_properties"]["bbx_tool_error"] == "invalid_reference"
    assert "synthetic private" not in recorded["text"]
    assert len(board.requests) == 2


@pytest.mark.parametrize("stage", ["objects", "record", "checkpoint"])
async def test_completed_tool_result_survives_audit_failure_without_native_reexecution(stage):
    original = httpx.ReadError("synthetic private audit failure")
    service, objects = FakeService(), FakeObjects()
    session_service = MemorySessionService()
    checkpoint = SessionCheckpoint(
        cast(BlackboardClient, session_service), "task", "agent", AgentSession()
    )
    context = run_context(service, objects=objects)
    context.checkpoint = checkpoint
    effects = []

    @tool
    async def effect() -> str:
        effects.append("done")
        return "known completed output"

    if stage == "objects":

        async def failed_put(*args, **kwargs):
            raise original

        objects.put = failed_put
    elif stage == "record":

        async def failed_record(*args, **kwargs):
            raise original

        service.record_tool_call = failed_record
    else:

        async def failed_save():
            raise original

        checkpoint.save = failed_save
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("effect", call_id="completed"),)),
            ScriptStep(calls=(ScriptToolCall("effect"),)),
            ScriptStep(text="must not repeat"),
        ]
    )
    async with Agent(
        client=client, tools=[effect], middleware=[ToolLogMiddleware(context)]
    ) as agent:
        with pytest.raises(httpx.ReadError) as caught, propagate_tool_failure():
            await agent.run("Synthetic effect", session=checkpoint.session)
    assert caught.value is original
    assert len(effects) == len(client.received_messages) == 1
    saved = checkpoint.session.state["bbx_tool_results"]["completed"]
    assert "known completed output" in str(saved)
    checkpoint.save = SessionCheckpoint.save.__get__(checkpoint)
    await checkpoint.save()
    restored = await SessionCheckpoint.load(
        cast(BlackboardClient, session_service), "task", "agent"
    )
    assert restored is not None
    assert "known completed output" in str(restored.session.state["bbx_tool_results"]["completed"])


async def test_tool_result_checkpoint_lost_ack_is_confirmed_without_reexecuting_successful_tool():
    service = FakeService()
    session_service = MemorySessionService()
    session_service.lose_next = True
    checkpoint = SessionCheckpoint(
        cast(BlackboardClient, session_service), "task", "agent", AgentSession()
    )
    context = run_context(service)
    context.checkpoint = checkpoint
    effects = []

    @tool
    async def effect() -> str:
        effects.append("done")
        return "known completed output"

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("effect", call_id="completed"),)),
            ScriptStep(text="done", expect_contains="known completed output"),
        ]
    )
    async with Agent(
        client=client, tools=[effect], middleware=[ToolLogMiddleware(context)]
    ) as agent:
        result = await agent.run("Synthetic lost checkpoint ACK", session=checkpoint.session)
    assert result.text == "done"
    assert effects == ["done"]
    assert len(service.logged) == checkpoint.revision == 1
    assert "known completed output" in str(
        checkpoint.session.state["bbx_tool_results"]["completed"]
    )


@pytest.mark.parametrize(
    "original", [MiddlewareFailure("synthetic abort"), asyncio.CancelledError("synthetic cancel")]
)
async def test_tool_log_does_not_swallow_public_middleware_failure_or_cancellation(original):
    context = run_context(FakeService())

    @tool
    async def stop() -> str:
        raise original

    client = ScriptedChatClient(
        [ScriptStep(calls=(ScriptToolCall("stop"),)), ScriptStep(text="must not run")]
    )
    async with Agent(client=client, tools=[stop], middleware=[ToolLogMiddleware(context)]) as agent:
        with pytest.raises(type(original)):
            await agent.run("Synthetic abort")
    assert len(client.received_messages) == 1


async def test_tool_log_preserves_public_graceful_termination_identity():
    context = run_context(FakeService())

    @tool
    async def stopped() -> str:
        return "must not execute"

    original = MiddlewareTermination("synthetic graceful guard")

    async def terminate():
        raise original

    invocation = FunctionInvocationContext(function=stopped, arguments={})
    with pytest.raises(MiddlewareTermination) as caught:
        await ToolLogMiddleware(context).process(invocation, terminate)
    assert caught.value is original
