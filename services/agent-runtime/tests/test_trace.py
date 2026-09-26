"""Trace text comes from actual public model content and actual context injection."""

import json
from pathlib import Path
from typing import Any, cast

from agent_framework import Agent, ChatResponse, Content, Message, tool
from bbx_objects import ObjectStore
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.context import RunContext
from bbx_runtime.middleware import BoardSyncMiddleware
from bbx_runtime.models import load_runtime_profile
from bbx_runtime.opening import OpeningContextProvider
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from bbx_runtime.trace import model_content

PROFILE_DIR = Path(__file__).resolve().parents[3] / "profiles" / "default"


class Service:
    def __init__(self) -> None:
        self.traces: list[dict[str, Any]] = []
        self.steps = 0
        self.board_events: list[dict[str, Any]] = []

    async def state(self, _task_id: str) -> dict[str, Any]:
        return {
            "agents": {
                "agent-1": {
                    "status": "running",
                    "steps": self.steps,
                    "last_seen_version": 0,
                    "conclude_requested_at": None,
                }
            }
        }

    async def events(self, _task_id: str, since: int = 0, for_agent: str | None = None):
        return [event for event in self.board_events if event["version"] > since]

    async def heartbeat(self, _task_id: str, _agent_id: str, **_kwargs: Any):
        self.steps += 1
        return {}

    async def record_agent_trace(self, _task_id: str, agent_id: str, trace: dict[str, Any]):
        self.traces.append({"agent_id": agent_id, **trace})
        return []


class Objects:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, uri: str, data: bytes, **_kwargs: Any) -> None:
        self.data[uri] = data


def context(service: Service, objects: Objects) -> RunContext:
    return RunContext(
        task_id="00000000-0000-0000-0000-000000000001",
        agent_id="agent-1",
        task_type="explore",
        state={
            "task": {
                "goal": "Find the cause",
                "domain_context": "test",
                "acceptance": [{"id": "A1", "desc": "Explain"}],
                "acceptance_state": {},
                "params": {},
            }
        },
        profile=load_runtime_profile(PROFILE_DIR),
        service=cast(BlackboardClient, service),
        board=cast(BlackboardClient, service),
        objects=cast(ObjectStore, objects),
    )


async def test_trace_order_matches_opening_injection_and_each_model_round() -> None:
    service, objects = Service(), Objects()
    service.board_events = [
        {
            "version": 2,
            "type": "fact.posted",
            "actor": "agent-2",
            "object_id": "F1",
            "payload": {"statement": "Observed result"},
        }
    ]
    ctx = context(service, objects)

    @tool
    async def inspect() -> str:
        return "seen"

    client = ScriptedChatClient(
        [ScriptStep(calls=(ScriptToolCall("inspect"),)), ScriptStep(text="Final answer")]
    )
    provider = OpeningContextProvider(ctx)
    result = await Agent(
        client=client,
        tools=[inspect],
        context_providers=[provider],
        middleware=[BoardSyncMiddleware(ctx)],
    ).run("开始。")
    assert result.text == "Final answer"
    assert [(t["kind"], t["step"]) for t in service.traces] == [
        ("initial_context", 0),
        ("board_update", 1),
        ("model_output", 1),
        ("model_output", 2),
    ]
    texts = [json.loads(objects.data[t["uri"]]) for t in service.traces]
    assert "Find the cause" in texts[0]["text"]
    assert "Observed result" in texts[1]["text"]
    assert texts[1]["text"] in str(client.received_messages[0][0].text)
    assert texts[2] == {"text": "调用工具：inspect"}
    assert texts[3] == {"text": "Final answer"}
    assert all(len(t["summary"]) <= 240 for t in service.traces)
    assert all("reasoning" not in item for item in texts)


async def test_trace_storage_failure_does_not_lose_model_usage_or_result() -> None:
    class FailingObjects(Objects):
        async def put(self, uri: str, data: bytes, **_kwargs: Any) -> None:
            raise OSError("storage unavailable")

    service = Service()
    ctx = context(service, FailingObjects())
    result = await Agent(
        client=ScriptedChatClient([ScriptStep(text="Still complete")]),
        context_providers=[OpeningContextProvider(ctx)],
        middleware=[BoardSyncMiddleware(ctx)],
    ).run("开始。")
    assert result.text == "Still complete"
    assert service.steps == 1
    assert service.traces == []


def test_reasoning_requires_text_content_even_when_usage_reports_reasoning_tokens() -> None:
    response = ChatResponse(
        messages=[Message(role="assistant", contents=[Content.from_text("Visible")])],
        usage_details=cast(Any, {"reasoning_output_token_count": 50}),
    )
    assert model_content(response) == ("Visible", None)
    response = ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_text_reasoning(text="Returned reasoning"),
                    Content.from_text("Visible"),
                ],
            )
        ]
    )
    assert model_content(response) == ("Visible", "Returned reasoning")
