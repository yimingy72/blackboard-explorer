"""Real HTTP, PostgreSQL and MAF team loop; model and drain are explicitly simulated."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from bbx_blackboard.api import create_app
from bbx_blackboard.settings import Settings as BoardSettings
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.pi import ResponseStream
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def ctf_vertical_database():
    container = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg").with_name(
        f"bbx-ctf-vertical-{uuid4().hex[:8]}"
    )
    with container:
        url = container.get_connection_url()
        previous = os.environ.get("BBX_DATABASE_URL")
        os.environ["BBX_DATABASE_URL"] = url
        try:
            command.upgrade(Config(str(ROOT / "services/blackboard/alembic.ini")), "head")
            yield url
        finally:
            if previous is None:
                os.environ.pop("BBX_DATABASE_URL", None)
            else:
                os.environ["BBX_DATABASE_URL"] = previous


class MemoryObjects:
    """Store complete observation payloads without a network object service."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    async def put(self, uri, data, **kwargs):
        self.files[uri] = data if isinstance(data, bytes) else data.read()

    async def get(self, uri):
        return self.files[uri]

    async def exists(self, uri):
        return uri in self.files


class SimulatedDrain:
    """T2 adapter proves coordinator behavior, never real process termination."""

    def __init__(self):
        self.confirmed = False
        self.calls = 0

    async def drain(self, task_id):
        self.calls += 1
        return self.confirmed


class ConcurrentScriptedClient(ScriptedChatClient):
    """Neither teammate may produce its first response before both are running."""

    def __init__(self, steps, member_id, entered, ready):
        super().__init__(steps)
        self.member_id, self.entered, self.ready = member_id, entered, ready

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        assert stream, "The integration scenario must exercise native streaming"
        self.entered.add(self.member_id)
        if self.entered == {"member-1", "member-2"}:
            self.ready.set()

        async def held_response():
            await asyncio.wait_for(self.ready.wait(), 5)
            response = super(ConcurrentScriptedClient, self)._inner_get_response(
                messages=messages, stream=True, options=options, **kwargs
            )
            assert isinstance(response, ResponseStream)
            return response

        return ResponseStream.from_awaitable(held_response())


def call(name, **arguments):
    return ScriptStep(calls=(ScriptToolCall(name, arguments),))


async def run_ready_turns(coordinator, task_id):
    """One scheduling wave; any runner error must fail the test immediately."""
    await coordinator.tick(task_id)
    futures = [future for future, _ in coordinator.active.values() if not future.done()]
    if futures:
        await asyncio.wait_for(asyncio.gather(*futures), 15)


async def test_http_team_sessions_results_and_deferred_close(ctf_vertical_database, monkeypatch):
    settings = BoardSettings(
        postgres_password=SecretStr("isolated-test"),
        minio_root_password=SecretStr("unused-test"),
        service_token=SecretStr("ctf-vertical-service"),
        agent_token_secret=SecretStr("ctf-vertical-isolated-test-signing-key"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    engine = create_async_engine(ctf_vertical_database)
    objects = MemoryObjects()
    app = create_app(settings, engine=engine, objects=objects)
    await app.state.profile_store.ensure_bundled(settings.profiles_dir, app.state.platform_store)
    phase = "initial"
    clients = []
    entered: set[str] = set()
    both_running = asyncio.Event()

    def factory(member, profile):
        nonlocal phase
        mid = member["id"]
        if mid == "lead" and phase == "initial":
            phase = "waiting"
            steps = [
                call("create_teammate", display_name="Alice"),
                call("create_teammate", display_name="Bob"),
                call(
                    "send_message", recipient_id="member-1", body="Inspect alpha", instruction=True
                ),
                call(
                    "send_message", recipient_id="member-2", body="Inspect beta", instruction=True
                ),
                ScriptStep(text="Waiting for the team"),
            ]
        elif mid == "lead" and phase == "finish":
            phase = "closing"
            steps = [
                call(
                    "finish_task",
                    summary="Partial evidence collected",
                    unresolved_items=["Platform validation"],
                    evidence_refs=[],
                    lead_claim=False,
                ),
                ScriptStep(text="Closing requested"),
            ]
        elif mid == "member-1":
            steps = [ScriptStep(text="Alpha evidence saved", expect_contains="Inspect alpha")]
        elif mid == "member-2":
            steps = [ScriptStep(text="Beta evidence saved", expect_contains="Inspect beta")]
        else:
            steps = [ScriptStep(text="Results received")]
        client = (
            ConcurrentScriptedClient(steps, mid, entered, both_running)
            if mid in {"member-1", "member-2"} and mid not in entered
            else ScriptedChatClient(steps)
        )
        clients.append((mid, client))
        return client

    coordinator = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://board", trust_env=False
        ) as http:
            board = BlackboardClient("http://board", "ctf-vertical-service", http)
            tid = (
                await board.create_task(
                    {
                        "mode": "ctf",
                        "goal": "Inspect both fake challenges",
                        "budget": {"max_cost": "100", "max_minutes": 20},
                    }
                )
            )["id"]
            await board.start_task(tid)
            ctf = CtfClient(board)
            await ctf.runtime(tid, "start")
            runtime_settings = Settings.model_construct()
            runner = CtfRunner(ctf, runtime_settings, client_factory=factory)
            drain = SimulatedDrain()
            coordinator = CtfCoordinator(board, runtime_settings, runner=runner, envd=drain)
            await run_ready_turns(coordinator, tid)
            await run_ready_turns(coordinator, tid)
            await run_ready_turns(coordinator, tid)
            assert entered == {"member-1", "member-2"}
            assert both_running.is_set()
            state = await ctf.state(tid)
            assert {m["display_name"] for m in state["members"] if m["role"] != "lead"} == {
                "Alice",
                "Bob",
            }
            results = [
                m for m in state["messages"] if m["notification_purpose"] == "assignment_result"
            ]
            assert len(results) == 2
            result_turns = {m["source_turn_id"] for m in results}
            assert {t["agent_id"] for t in state["turns"] if t["id"] in result_turns} == {
                "member-1",
                "member-2",
            }
            before = await board.get_agent_session(tid, "member-1")
            login = await http.post("/api/login", json={"username": "admin", "password": "test"})
            assert login.status_code == 200
            followup_id = str(uuid4())
            followup = await http.post(
                f"/api/tasks/{tid}/agents/member-1/messages",
                json={"id": followup_id, "content": "Continue alpha using your earlier evidence"},
            )
            assert followup.status_code == 200, followup.text
            await run_ready_turns(coordinator, tid)
            after = await board.get_agent_session(tid, "member-1")
            assert after["session"]["session_id"] == before["session"]["session_id"]
            assert after["revision"] > before["revision"]
            resumed_client = [client for mid, client in clients if mid == "member-1"][-1]
            resumed_messages = resumed_client.received_messages[0]
            assert any(
                message.role == "assistant" and "Alpha evidence saved" in message.text
                for message in resumed_messages
            )
            assert any(
                message.message_id == followup_id
                and "Continue alpha using your earlier evidence" in message.text
                for message in resumed_messages
            )
            clock = [0.0]
            monkeypatch.setattr("bbx_runtime.ctf.coordinator.monotonic", lambda: clock[0])
            phase = "finish"
            response = await http.post(
                f"/api/tasks/{tid}/agents/lead/messages",
                json={"id": str(uuid4()), "content": "Finish with the collected evidence"},
            )
            assert response.status_code == 200, response.text
            await run_ready_turns(coordinator, tid)
            assert await coordinator.tick(tid)
            closing = await ctf.state(tid)
            assert closing["task"]["ctf_control"]["phase"] == "closing"
            late = await http.post(
                f"/api/tasks/{tid}/agents/lead/messages",
                json={"id": str(uuid4()), "content": "Keep this for a later resume"},
            )
            assert late.status_code == 200 and late.json()["deferred"]
            drain.confirmed = True
            clock[0] = coordinator.close_retries[tid].next_retry
            assert not await coordinator.tick(tid)
            final = await ctf.state(tid)
            assert final["task"]["ctf_control"]["phase"] == "closed"
            assert final["task"]["ctf_conclusion"]["summary"] == "Partial evidence collected"
            assert final["task"]["ctf_conclusion"]["unresolved_items"] == ["Platform validation"]
            assert (
                len(
                    [
                        m
                        for m in final["messages"]
                        if m["notification_purpose"] == "assignment_result"
                    ]
                )
                == 2
            )
            assert drain.calls >= 2
            observations = [
                event["payload"]
                for event in await board.events(tid)
                if event["type"] == "agent.trace.recorded"
            ]
            initial_contexts = [item for item in observations if item["kind"] == "initial_context"]
            assert Counter(item["agent_id"] for item in initial_contexts) == {
                "lead": 1,
                "member-1": 1,
                "member-2": 1,
            }
            assert {item["kind"] for item in observations} >= {
                "initial_context",
                "turn_context",
                "tool_call",
                "tool_result",
                "model_output",
            }
            turns = {turn["id"]: turn for turn in final["turns"]}
            for item in observations:
                turn = turns[item["turn_id"]]
                assert item["generation"] == turn["generation"]
                assert item["agent_id"] == turn["agent_id"]
                assert await objects.exists(item["uri"])
                content = await objects.get(item["uri"])
                assert hashlib.sha256(content).hexdigest() == item["sha256"]
                body = json.loads(content)
                assert body
                if item["kind"] == "initial_context":
                    assert body["instructions"]
                    assert ("finish_task" in body["tools"]) == (item["agent_id"] == "lead")
                elif item["kind"] == "tool_result":
                    assert body["tool"] and "result" in body
            model_outputs = [
                json.loads(await objects.get(item["uri"]))["text"]
                for item in observations
                if item["kind"] == "model_output"
            ]
            assert "Alpha evidence saved" in model_outputs
    finally:
        if coordinator:
            await coordinator.close()
        await app.state.workspace_cache.close()
        await engine.dispose()
