"""Live worker prompts replace MAF system instructions at the next model call."""

import json
from pathlib import Path
from typing import Any, cast

from agent_framework import Agent, AgentSession, tool
from bbx_objects import ObjectStore
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.context import RunContext
from bbx_runtime.middleware import BoardSyncMiddleware
from bbx_runtime.models import load_runtime_profile
from bbx_runtime.opening import OpeningContextProvider
from bbx_runtime.session import CheckpointHistoryProvider, SessionCheckpoint
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall


class Service:
    def __init__(self, state: dict[str, Any]) -> None:
        self.board = state
        self.revision = 1
        self.prompt = "First system {{ goal }}"
        self.saved: dict[str, Any] | None = None
        self.traces: list[dict[str, Any]] = []

    async def state(self, _task_id: str) -> dict[str, Any]:
        return self.board

    async def get_worker_prompt(self, role: str, since: int = 0) -> dict[str, Any]:
        assert role == "derive"
        return {
            "revision": self.revision,
            "prompt": None if since == self.revision else self.prompt,
        }

    async def put_agent_session(self, _task: str, _agent: str, **body: Any) -> dict[str, Any]:
        current = self.saved["revision"] if self.saved else 0
        assert body["expected_revision"] == current
        self.saved = {
            "session": json.loads(json.dumps(body["session"])),
            "opening_instructions": body["opening_instructions"],
            "origin": body["origin"],
            "revision": current + 1,
        }
        return self.saved

    async def get_agent_session(self, _task: str, _agent: str) -> dict[str, Any]:
        assert self.saved is not None
        return self.saved

    async def agent_messages(self, _task: str, _agent: str, *, status: str) -> dict[str, Any]:
        return {"messages": []}

    async def heartbeat(self, _task: str, _agent: str, **body: Any) -> None:
        self.board["agents"]["agent"]["steps"] += body["steps"]

    async def record_agent_trace(self, _task: str, _agent: str, body: dict[str, Any]) -> None:
        self.traces.append(body)


class Objects:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def put(self, uri: str, data: bytes, **_: Any) -> None:
        self.files[uri] = data


async def test_prompt_replaces_system_once_and_survives_restore() -> None:
    profile = load_runtime_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    state = {
        "task": {
            "goal": "find cause",
            "acceptance": [{"id": "A1", "desc": "show proof"}],
            "acceptance_state": {},
            "params": {},
        },
        "facts": {},
        "intents": {},
        "agents": {"agent": {"status": "running", "last_seen_version": 0, "steps": 0}},
    }
    service, objects = Service(state), Objects()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", AgentSession())
    ctx = RunContext(
        task_id="task",
        agent_id="agent",
        task_type="derive",
        state=state,
        profile=profile,
        service=cast(BlackboardClient, service),
        board=cast(BlackboardClient, service),
        objects=cast(ObjectStore, objects),
        checkpoint=checkpoint,
    )

    @tool
    async def advance() -> str:
        service.revision += 1
        service.prompt = "Second system {{ goal }} {{ acceptance_status }}"
        return "tool result kept"

    @tool
    async def unchanged() -> str:
        service.revision += 1  # A different role or tool changed the global profile.
        return "second result kept"

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("advance"),), expect_contains="First system"),
            ScriptStep(calls=(ScriptToolCall("unchanged"),), expect_contains="Second system"),
            ScriptStep(text="done", expect_contains="second result kept"),
        ]
    )
    async with Agent(
        client=client,
        tools=[advance, unchanged],
        context_providers=[
            OpeningContextProvider(ctx, checkpoint),
            CheckpointHistoryProvider(checkpoint),
        ],
        middleware=[BoardSyncMiddleware(ctx)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        assert (await agent.run("start", session=checkpoint.session)).text == "done"
    assert "First system" in client.received_options[0]["instructions"]
    assert "Second system" in client.received_options[1]["instructions"]
    assert "First system" not in client.received_options[1]["instructions"]
    assert "show proof" in client.received_options[1]["instructions"]
    assert all("First system" not in m.text for m in client.received_messages[1])
    assert any(
        "tool result kept" in str(c.result)
        for m in client.received_messages[1]
        for c in m.contents
        if c.type == "function_result"
    )
    updates = [t for t in service.traces if t["kind"] == "board_update"]
    assert len(updates) == 1
    assert "Second system" in json.loads(objects.files[updates[0]["uri"]])["text"]
    assert checkpoint.prompt_revision == 3

    restored = await SessionCheckpoint.load(cast(BlackboardClient, service), "task", "agent")
    assert restored is not None
    restored_ctx = RunContext(**{**vars(ctx), "checkpoint": restored})
    again = ScriptedChatClient([ScriptStep(text="recovered", expect_contains="done")])
    async with Agent(
        client=again,
        context_providers=[
            OpeningContextProvider(restored_ctx, restored),
            CheckpointHistoryProvider(restored),
        ],
        middleware=[BoardSyncMiddleware(restored_ctx)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        assert (await agent.run("continue", session=restored.session)).text == "recovered"
    assert "Second system" in again.received_options[0]["instructions"]
    assert "First system" not in again.received_options[0]["instructions"]
    assert len([t for t in service.traces if t["kind"] == "board_update"]) == 1
