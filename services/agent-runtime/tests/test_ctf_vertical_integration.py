"""Real HTTP, PostgreSQL and MAF team loop; model and drain are explicitly simulated."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from agent_framework import ResponseStream
from alembic import command
from alembic.config import Config
from bbx_blackboard.api import create_app
from bbx_blackboard.settings import Settings as BoardSettings
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.execenv import ExecEnvHandle
from bbx_runtime.execenv.manager import ArchiveResult
from bbx_runtime.scheduler.supervisor import TaskSupervisor
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


async def test_http_team_sessions_results_and_deferred_close(ctf_vertical_database):
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
            runtime_settings = Settings.model_construct(
                deepseek_api_key=SecretStr("unused-scripted")
            )
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


async def test_replacement_uses_persisted_execution_boot_over_restarted_daemon(
    ctf_vertical_database, monkeypatch
):
    """Combine real DB binding/CAS routes with supervisor replacement orchestration."""
    settings = BoardSettings(
        postgres_password=SecretStr("isolated-test"),
        minio_root_password=SecretStr("unused-test"),
        service_token=SecretStr("ctf-vertical-service"),
        agent_token_secret=SecretStr("ctf-vertical-isolated-test-signing-key"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    engine = create_async_engine(ctf_vertical_database)
    app = create_app(settings, engine=engine, objects=MemoryObjects())
    await app.state.profile_store.ensure_bundled(settings.profiles_dir, app.state.platform_store)
    original_boot, unknown_boot, fresh_boot = [str(uuid4()) for _ in range(3)]
    phase_checks = []
    returned = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://board", trust_env=False
        ) as http:
            board = BlackboardClient("http://board", "ctf-vertical-service", http)
            tid = (
                await board.create_task(
                    {
                        "mode": "ctf",
                        "goal": "Restore registered work after a daemon crash",
                        "budget": {"max_cost": "100", "max_minutes": 20},
                    }
                )
            )["id"]
            await board.start_task(tid)
            ctf = CtfClient(board)
            await ctf.runtime(tid, "start")
            turn = await ctf.runtime(
                tid, "claim_turn", agent_id="lead", runtime_instance="before-crash"
            )
            await ctf.runtime(
                tid,
                "record_execution",
                agent_id="lead",
                turn_id=turn["id"],
                generation=turn["generation"],
                boot_id=original_boot,
            )
            old = ExecEnvHandle(UUID(tid), "old-container", "old", "http://old-envd", "test-token")
            fresh = ExecEnvHandle(
                UUID(tid), "new-container", "new", "http://new-envd", "test-token"
            )
            archive_uri = f"workspace/{tid}/replacement-old-container.tar.zst"

            async def phase(expected):
                state = await ctf.state(tid)
                replacement = state["task"]["ctf_control"]["replacement"]
                assert replacement["phase"] == expected
                assert replacement["old_boot_id"] == original_boot
                assert replacement["old_boot_id"] != unknown_boot
                member = next(m for m in state["members"] if m["id"] == "lead")
                assert member["execution"]["boot_id"] == original_boot
                assert not member["execution"]["drained"]
                assert member["generation"] == turn["generation"]
                phase_checks.append(expected)

            async def archive(handle, *, uri):
                assert handle == old and uri == archive_uri
                await phase("begin")
                return ArchiveResult(uri, 123, "none")

            async def destroy(task_id, container_id):
                assert task_id == tid and container_id == old.container_id
                await phase("archive_saved")

            async def provision(task_id, profile, uri):
                assert task_id == tid and uri == archive_uri
                assert profile.mode == "ctf"
                await phase("destroyed")
                return fresh

            class DaemonStatus:
                def __init__(self, base_url, token):
                    self.base_url = base_url
                    self.closed = False
                    assert token == "test-token"

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    await self.close()

                async def close(self):
                    self.closed = True

                async def ctf_status(self):
                    return {
                        "task_id": tid,
                        "boot_id": unknown_boot if self.base_url == old.base_url else fresh_boot,
                        "state": "unknown" if self.base_url == old.base_url else "ready",
                        "drained": False,
                    }

            monkeypatch.setattr("bbx_runtime.scheduler.supervisor.EnvdClient", DaemonStatus)
            manager = Mock()
            manager.find = AsyncMock(return_value=old)
            manager.archive_to_store = AsyncMock(side_effect=archive)
            manager.destroy_confirmed = AsyncMock(side_effect=destroy)
            manager.provision = AsyncMock(side_effect=provision)
            supervisor = TaskSupervisor(
                Settings.model_construct(deepseek_api_key=SecretStr("unused-scripted")),
                board,
                manager,
                Mock(),
            )
            returned = await supervisor._replace_ctf_envd(tid)
            assert returned.base_url == fresh.base_url
            assert phase_checks == ["begin", "archive_saved", "destroyed"]
            final = await ctf.state(tid)
            replacement = final["task"]["ctf_control"]["replacement"]
            assert replacement == {
                "old_container_id": old.container_id,
                "old_boot_id": original_boot,
                "phase": "ready",
                "archive_uri": archive_uri,
                "new_boot_id": fresh_boot,
            }
            lead = next(m for m in final["members"] if m["id"] == "lead")
            assert lead["generation"] == turn["generation"] + 1
            assert lead["execution"]["drained"] is True
            assert lead["execution"]["container_destroyed"] == old.container_id
            assert lead["execution"]["replacement_boot_id"] == fresh_boot
            with pytest.raises(RemoteError) as stale:
                await ctf.runtime(
                    tid,
                    "authorize_member",
                    agent_id="lead",
                    turn_id=turn["id"],
                    generation=turn["generation"],
                )
            assert stale.value.status in {403, 409}
            manager.destroy_confirmed.assert_awaited_once_with(tid, old.container_id)
            manager.archive_to_store.assert_awaited_once()
            manager.provision.assert_awaited_once()
    finally:
        if returned is not None:
            await returned.close()
        await app.state.workspace_cache.close()
        await engine.dispose()
