"""Seed fanout and restart recovery over real board storage with scripted agents."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from agent_framework import BaseChatClient, ChatResponse
from bbx_blackboard.api import create_app
from bbx_blackboard.settings import Settings as BoardSettings
from bbx_objects import ObjectStore
from bbx_runtime.clients import BlackboardClient, EnvdClient
from bbx_runtime.context import CloseMode, TaskType
from bbx_runtime.execenv import ExecEnvHandle
from bbx_runtime.runner import AgentRunner, RunResult
from bbx_runtime.scheduler.supervisor import TaskSupervisor
from bbx_runtime.settings import Settings
from bbx_runtime.testing.fake_envd import FakeEnvd
from bbx_runtime.testing.scripted_client import (
    ScriptedChatClient,
    ScriptStep,
    ScriptToolCall,
    ScriptUsage,
)
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
USAGE = ScriptUsage(2, 3, 4, 1)


class PollBoard(BlackboardClient):
    """ASGITransport buffers endless SSE; poll the real events endpoint for wakeups."""

    async def stream(self, task_id: UUID | str, since: int = 0) -> AsyncIterator[dict[str, Any]]:
        cursor = since
        while True:
            for event in await self.events(task_id, cursor):
                cursor = int(event["version"])
                yield event
            await asyncio.sleep(0.05)


class Barrier:
    def __init__(self, count: int) -> None:
        self.count = count
        self.seen: set[str] = set()
        self.ready = asyncio.Event()
        self.release = asyncio.Event()

    def enter(self, agent_id: str) -> None:
        self.seen.add(agent_id)
        if len(self.seen) >= self.count:
            self.ready.set()


class GatedScriptedClient(ScriptedChatClient):
    def __init__(self, steps: list[ScriptStep], agent_id: str, gate: Barrier) -> None:
        super().__init__(steps)
        self.agent_id = agent_id
        self.gate = gate
        self.entered = False

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        if self.entered or stream:
            return super()._inner_get_response(
                messages=messages, stream=stream, options=options, **kwargs
            )
        self.entered = True
        self.gate.enter(self.agent_id)

        async def held():
            await self.gate.release.wait()
            produced = super(GatedScriptedClient, self)._inner_get_response(
                messages=messages, stream=stream, options=options, **kwargs
            )
            return await cast(Awaitable[ChatResponse], produced)

        return held()


class FakeManager:
    def __init__(self, handle: ExecEnvHandle, envd_http: httpx.AsyncClient) -> None:
        self.handle = handle
        self.envd_http = envd_http

    async def find(self, _task_id: str) -> ExecEnvHandle:
        return self.handle

    async def provision(self, _task_id: str, _profile) -> ExecEnvHandle:
        return self.handle

    async def wait_healthy(self, _handle: ExecEnvHandle) -> None:
        async with EnvdClient(self.handle.base_url, self.handle.token, self.envd_http) as envd:
            assert (await envd.health())["status"] == "ok"

    async def create_user(self, _handle: ExecEnvHandle, agent_id: str) -> dict[str, str]:
        async with EnvdClient(self.handle.base_url, self.handle.token, self.envd_http) as envd:
            return await envd.create_user(agent_id)

    async def archive_to_store(self, _handle: ExecEnvHandle):
        raise AssertionError("The S3a fragment must not archive a running task")

    async def destroy(self, _task_id: str) -> None:
        raise AssertionError("Stopping a supervisor must retain the task environment")


class ScriptedRunner(AgentRunner):
    def __init__(
        self,
        settings: Settings,
        service: BlackboardClient,
        objects: ObjectStore,
        manager: FakeManager,
        envd_http: httpx.AsyncClient,
        fake_envd: FakeEnvd,
        gate: Barrier,
        *,
        seed_intents: int,
    ) -> None:
        super().__init__(settings, service, objects, manager)  # type: ignore[arg-type]
        self.envd_http = envd_http
        self.fake_envd = fake_envd
        self.gate = gate
        self.aux_gate = Barrier(1000)
        self.seed_intents = seed_intents

    async def run_agent(
        self,
        task_id: str,
        agent_id: str,
        task_type: TaskType,
        intent_id: str | None = None,
        mode: CloseMode | None = None,
        *,
        agent_token: str,
        client: BaseChatClient | None = None,
        handle: ExecEnvHandle | None = None,
        envd_http_client: httpx.AsyncClient | None = None,
    ) -> RunResult:
        assert client is None and envd_http_client is None
        if task_type == "explore":
            path = f"/workspace/agents/{agent_id}/evidence.txt"
            self.fake_envd.put_file(path, f"proof from {agent_id}")
            if intent_id is None:
                calls = [
                    ScriptStep(
                        calls=(
                            ScriptToolCall(
                                "post_fact",
                                {
                                    "kind": "observation",
                                    "statement": "Seed evidence",
                                    "evidence": [
                                        {"type": "text", "path": path, "summary": "Seed proof"}
                                    ],
                                },
                            ),
                        ),
                        usage=USAGE,
                    )
                ]
                calls.extend(
                    ScriptStep(
                        calls=(
                            ScriptToolCall(
                                "post_intent",
                                {
                                    "statement": f"Investigate direction {number}",
                                    "based_on": ["F1"],
                                    "expected": "Find a supported result",
                                    "method": f"Inspect case {number}",
                                    "relates_to": ["A1"],
                                },
                            ),
                        ),
                        usage=USAGE,
                    )
                    for number in range(1, self.seed_intents + 1)
                )
                calls.append(
                    ScriptStep(
                        text=(
                            '{"accepted":true,"data":{"intent_result":"none",'
                            '"posted":["F1","I1","I2","I3"],"note":"Seed complete"}}'
                        ),
                        usage=USAGE,
                    )
                )
                model = ScriptedChatClient(calls)
            else:
                model = GatedScriptedClient(
                    [
                        ScriptStep(
                            calls=(
                                ScriptToolCall(
                                    "post_fact",
                                    {
                                        "kind": "observation",
                                        "statement": f"Resolved {intent_id}",
                                        "evidence": [
                                            {
                                                "type": "text",
                                                "path": path,
                                                "summary": "Result proof",
                                            }
                                        ],
                                        "resolves": intent_id,
                                        "result": "confirmed",
                                    },
                                ),
                            ),
                            usage=USAGE,
                        ),
                        ScriptStep(
                            text='{"accepted":true,"data":{"intent_result":"confirmed","posted":[],"note":"Done"}}',
                            usage=USAGE,
                        ),
                    ],
                    agent_id,
                    self.gate,
                )
        elif task_type == "derive":
            model = ScriptedChatClient(
                [
                    ScriptStep(
                        text='{"accepted":true,"data":{"posted":[],"excluded":[]}}', usage=USAGE
                    )
                ]
            )
        else:
            model = GatedScriptedClient(
                [ScriptStep(text='{"accepted":true,"data":{"note":"Idle"}}', usage=USAGE)],
                agent_id,
                self.aux_gate,
            )
        return await super().run_agent(
            task_id,
            agent_id,
            task_type,
            intent_id,
            mode,
            agent_token=agent_token,
            client=model,
            handle=handle,
            envd_http_client=self.envd_http,
        )


@asynccontextmanager
async def scenario(runtime_infrastructure, tmp_path: Path):
    database_url, endpoint = runtime_infrastructure
    parsed = make_url(database_url)
    token = "m3a-service"
    board_settings = BoardSettings(
        postgres_host=parsed.host or "127.0.0.1",
        postgres_port=parsed.port or 5432,
        postgres_user=parsed.username or "test",
        postgres_password=SecretStr(parsed.password or "test"),
        postgres_db=parsed.database or "test",
        minio_root_user="bbxm2buser",
        minio_root_password=SecretStr("bbxm2b-test-password"),
        minio_endpoint=f"http://{endpoint}",
        minio_bucket=f"bbxm3a{uuid4().hex[:12]}",
        service_token=SecretStr(token),
        agent_token_secret=SecretStr("m3a-agent-signing-integration-secret"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    objects = ObjectStore(
        endpoint, "bbxm2buser", "bbxm2b-test-password", board_settings.minio_bucket
    )
    await objects.ensure_bucket()
    engine = create_async_engine(database_url)
    app = create_app(board_settings, engine=engine, objects=objects)
    fake = FakeEnvd(tmp_path)
    try:
        async with app.router.lifespan_context(app), fake:
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), trust_env=False
                ) as board_http,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=fake.app), trust_env=False
                ) as envd_http,
            ):
                service = PollBoard("http://board", token, board_http)
                yield service, objects, fake, envd_http
    finally:
        await engine.dispose()


async def wait_state(board: BlackboardClient, tid: str, predicate, seconds: float = 15) -> dict:
    async with asyncio.timeout(seconds):
        while True:
            state = await board.state(tid)
            if predicate(state):
                return state
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_seed_fanout_and_restart_reassigns_without_attempts(
    runtime_infrastructure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bbx_runtime.scheduler.loop.FALLBACK_SECONDS", 0.25)
    monkeypatch.setattr("bbx_runtime.scheduler.loop.DEBOUNCE_SECONDS", 0.05)
    async with scenario(runtime_infrastructure, tmp_path) as (board, objects, fake, envd_http):
        tid = (
            await board.create_task(
                {
                    "goal": "Resolve three independent directions",
                    "acceptance": [{"id": "A1", "desc": "Record supported results"}],
                    "budget": {"max_concurrent_agents": 3, "max_cost": "100", "max_minutes": 20},
                    "agent_profile": "default",
                }
            )
        )["id"]
        await board.start_task(tid)
        handle = ExecEnvHandle(UUID(tid), "fake-envd", "fake", "http://fake", fake.token)
        manager = FakeManager(handle, envd_http)
        settings = Settings.model_construct(
            deepseek_api_key=SecretStr("fake-model"), max_running_tasks=1
        )

        first_gate = Barrier(3)
        runner = ScriptedRunner(
            settings, board, objects, manager, envd_http, fake, first_gate, seed_intents=3
        )
        first = TaskSupervisor(settings, board, manager, runner)  # type: ignore[arg-type]
        first_task = asyncio.create_task(first.run())
        peak_workers = 0
        observing = True

        async def observe() -> None:
            nonlocal peak_workers
            while observing:
                current = await board.state(tid)
                workers = sum(
                    agent["status"] in {"running", "concluding"}
                    and agent["task_type"] in {"explore", "derive"}
                    for agent in current["agents"].values()
                )
                peak_workers = max(peak_workers, workers)
                await asyncio.sleep(0.02)

        watcher = asyncio.create_task(observe())
        try:
            await asyncio.wait_for(first_gate.ready.wait(), 20)
            held = await board.state(tid)
            active = {
                aid: agent
                for aid, agent in held["agents"].items()
                if agent["status"] == "running" and agent["task_type"] == "explore"
            }
            assert len(active) == 3 and set(active) == first_gate.seen
            assert len(held["intents"]) == 3
            assert {item["holder"] for item in held["intents"].values()} == set(active)
            first_gate.release.set()
            completed = await wait_state(
                board,
                tid,
                lambda state: (
                    all(item["status"] == "closed" for item in state["intents"].values())
                    and all(state["agents"][aid]["status"] == "finished" for aid in first_gate.seen)
                ),
            )
        finally:
            observing = False
            await watcher
            await first.stop()
            await asyncio.wait_for(first_task, 5)

        settled = await board.state(tid)
        assert not [
            aid
            for aid, agent in settled["agents"].items()
            if agent.get("end_reason") == "runtime_error"
        ]
        assert peak_workers == 3
        claims = [event for event in await board.events(tid) if event["type"] == "intent.claimed"]
        assert sorted(event["payload"]["intent_id"] for event in claims) == ["I1", "I2", "I3"]
        assert completed["agents"]["agent-1"]["steps"] == 5
        assert all(completed["agents"][aid]["steps"] == 2 for aid in first_gate.seen)
        assert completed["agents"]["agent-1"]["usage"]["cache_hit_tokens"] == 10
        assert all(
            completed["agents"][aid]["usage"]["cache_hit_tokens"] == 4 for aid in first_gate.seen
        )
        for field in ("cache_hit_tokens", "cache_miss_tokens", "output_tokens"):
            assert completed["task"]["usage"][field] == sum(
                agent["usage"].get(field, 0) for agent in completed["agents"].values()
            )

        derive = await board.register_agent(tid, "derive")
        await board.with_token(derive["token"]).post_intent(
            tid,
            {
                "statement": "Investigate restart handoff",
                "based_on": ["F1"],
                "expected": "New holder can resolve it",
                "method": "Inspect after restart",
                "relates_to": ["A1"],
            },
        )
        await board.finish_agent(
            tid,
            derive["agent_id"],
            {"accepted": True, "data": {"posted": ["I4"], "excluded": []}},
            "normal",
        )

        second_gate = Barrier(1)
        second_runner = ScriptedRunner(
            settings, board, objects, manager, envd_http, fake, second_gate, seed_intents=0
        )
        second = TaskSupervisor(settings, board, manager, second_runner)  # type: ignore[arg-type]
        second_task = asyncio.create_task(second.run())
        try:
            await asyncio.wait_for(second_gate.ready.wait(), 15)
            before_restart = await board.state(tid)
            assert before_restart["intents"]["I4"]["attempts"] == 0
            original_holder = before_restart["intents"]["I4"]["holder"]
            assert original_holder in second_gate.seen
        finally:
            await second.stop()
            await asyncio.wait_for(second_task, 5)
        after_restart = await board.state(tid)
        assert after_restart["intents"]["I4"]["status"] == "open"
        assert after_restart["intents"]["I4"]["attempts"] == 0
        assert after_restart["agents"][original_holder]["end_reason"] == "runtime_restart"

        third_gate = Barrier(1)
        third_runner = ScriptedRunner(
            settings, board, objects, manager, envd_http, fake, third_gate, seed_intents=0
        )
        third = TaskSupervisor(settings, board, manager, third_runner)  # type: ignore[arg-type]
        third_task = asyncio.create_task(third.run())
        try:
            await asyncio.wait_for(third_gate.ready.wait(), 15)
            assert original_holder not in third_gate.seen
            third_gate.release.set()
            recovered = await wait_state(
                board,
                tid,
                lambda state: (
                    state["intents"]["I4"]["status"] == "closed"
                    and all(state["agents"][aid]["status"] == "finished" for aid in third_gate.seen)
                ),
            )
            assert recovered["intents"]["I4"]["attempts"] == 0
            assert recovered["intents"]["I4"]["holder"] is None
        finally:
            await third.stop()
            await asyncio.wait_for(third_task, 5)
