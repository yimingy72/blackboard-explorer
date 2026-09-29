"""Small real-board harness for the M3b scripted lifecycle scenarios."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from agent_framework import BaseChatClient, ChatResponse, ResponseStream
from bbx_blackboard.api import create_app
from bbx_blackboard.settings import Settings as BoardSettings
from bbx_objects import ObjectStore
from bbx_runtime.clients import BlackboardClient, EnvdClient
from bbx_runtime.context import CloseMode, TaskType
from bbx_runtime.execenv import ArchiveResult, ExecEnvHandle, ExecEnvManager
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

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_USAGE = ScriptUsage(2, 3, 4, 1)
ScriptFactory = Callable[
    [TaskType, str | None, CloseMode | None, str, dict[str, Any]], ScriptedChatClient
]


class PollBoard(BlackboardClient):
    """Use real events reads because ASGITransport buffers endless SSE responses."""

    def __init__(self, base_url: str, token: str, http_client: httpx.AsyncClient) -> None:
        super().__init__(base_url, token, http_client)
        self.only_task_id: str | None = None

    async def list_tasks(self) -> list[dict[str, Any]]:
        tasks = await super().list_tasks()
        return [task for task in tasks if str(task["id"]) == self.only_task_id]

    async def stream(self, task_id: UUID | str, since: int = 0) -> AsyncIterator[dict[str, Any]]:
        cursor = since
        while True:
            for event in await self.events(task_id, cursor):
                cursor = int(event["version"])
                yield event
            await asyncio.sleep(0.05)


class Gate:
    """Hold the first model response for one or more agents."""

    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.seen: set[str] = set()
        self.ready = asyncio.Event()
        self.release = asyncio.Event()

    def enter(self, agent_id: str) -> None:
        self.seen.add(agent_id)
        if len(self.seen) >= self.count:
            self.ready.set()


class GateClient(ScriptedChatClient):
    def __init__(
        self,
        steps: Sequence[ScriptStep],
        agent_id: str,
        gate: Gate,
        *,
        at_call: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(steps, **kwargs)
        self.agent_id = agent_id
        self.gate = gate
        self.at_call = at_call
        self.calls = 0

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.calls += 1
        if self.calls != self.at_call:
            return super()._inner_get_response(
                messages=messages, stream=stream, options=options, **kwargs
            )
        self.gate.enter(self.agent_id)

        if stream:

            async def held_stream():
                await self.gate.release.wait()
                produced = super(GateClient, self)._inner_get_response(
                    messages=messages, stream=True, options=options, **kwargs
                )
                assert isinstance(produced, ResponseStream)
                return produced

            return ResponseStream.from_awaitable(held_stream())

        async def held():
            await self.gate.release.wait()
            produced = super(GateClient, self)._inner_get_response(
                messages=messages, stream=stream, options=options, **kwargs
            )
            return await cast(Awaitable[ChatResponse], produced)

        return held()


def fact_step(
    aid: str,
    statement: str,
    *,
    path: str | None = None,
    kind: str = "observation",
    resolves: str | None = None,
    result: str | None = None,
    satisfies: Sequence[str] = (),
    disputes: Sequence[str] = (),
    derived_from: Sequence[str] = (),
    usage: ScriptUsage = DEFAULT_USAGE,
) -> ScriptStep:
    arguments: dict[str, Any] = {
        "kind": kind,
        "statement": statement,
        "evidence": [
            {
                "type": "text",
                "path": path or f"/workspace/agents/{aid}/evidence.txt",
                "summary": "Scripted evidence",
            }
        ],
    }
    if resolves is not None:
        arguments.update(resolves=resolves, result=result)
    if satisfies:
        arguments["satisfies"] = list(satisfies)
    if disputes:
        arguments["disputes"] = list(disputes)
    if derived_from:
        arguments["derived_from"] = list(derived_from)
    return ScriptStep(calls=(ScriptToolCall("post_fact", arguments),), usage=usage)


def intent_step(
    statement: str,
    *,
    based_on: Sequence[str] = ("F1",),
    relates_to: Sequence[str] = ("A1",),
    expected: str = "Find a supported result",
    method: str = "Inspect the evidence",
    claim: bool = False,
    retry_of: str | None = None,
    usage: ScriptUsage = DEFAULT_USAGE,
) -> ScriptStep:
    arguments: dict[str, Any] = {
        "statement": statement,
        "based_on": list(based_on),
        "expected": expected,
        "method": method,
        "relates_to": list(relates_to),
        "claim": claim,
    }
    if retry_of is not None:
        arguments["retry_of"] = retry_of
    return ScriptStep(calls=(ScriptToolCall("post_intent", arguments),), usage=usage)


def close_step(
    verdicts: Sequence[dict[str, Any]],
    *,
    report: str | None = None,
    usage: ScriptUsage = DEFAULT_USAGE,
) -> ScriptStep:
    args: dict[str, Any] = {"verdicts": list(verdicts)}
    if report is not None:
        args["report"] = report
    return ScriptStep(calls=(ScriptToolCall("submit_close", args),), usage=usage)


def receipt_step(
    task_type: TaskType,
    *,
    note: str = "Done",
    posted: Sequence[str] = (),
    excluded: Sequence[str] = (),
    intent_result: str = "none",
    accepted: bool = True,
    usage: ScriptUsage = DEFAULT_USAGE,
) -> ScriptStep:
    if not accepted:
        value: dict[str, Any] = {"accepted": False, "reason": note}
    elif task_type == "explore":
        value = {
            "accepted": True,
            "data": {"intent_result": intent_result, "posted": list(posted), "note": note},
        }
    elif task_type == "derive":
        value = {"accepted": True, "data": {"posted": list(posted), "excluded": list(excluded)}}
    else:
        value = {"accepted": True, "data": {"note": note}}
    return ScriptStep(text=json.dumps(value, ensure_ascii=False), usage=usage)


class FakeManager:
    def __init__(
        self, handle: ExecEnvHandle, envd_http: httpx.AsyncClient, objects: ObjectStore
    ) -> None:
        self.handle: ExecEnvHandle | None = handle
        self.envd_http = envd_http
        self.objects = objects
        self.archives: list[ArchiveResult] = []
        self.destroyed: list[str] = []

    async def find(self, _task_id: str) -> ExecEnvHandle | None:
        return self.handle

    async def provision(self, _task_id: str, _profile) -> ExecEnvHandle:
        assert self.handle is not None
        return self.handle

    async def wait_healthy(self, _handle: ExecEnvHandle) -> None:
        assert self.handle is not None
        async with EnvdClient(self.handle.base_url, self.handle.token, self.envd_http) as envd:
            assert (await envd.health())["status"] == "ok"

    async def create_user(self, _handle: ExecEnvHandle, agent_id: str) -> dict[str, str]:
        assert self.handle is not None
        async with EnvdClient(self.handle.base_url, self.handle.token, self.envd_http) as envd:
            return await envd.create_user(agent_id)

    async def archive_to_store(self, handle: ExecEnvHandle) -> ArchiveResult:
        uri = f"workspace/{handle.task_id}.tar.zst"
        async with EnvdClient(handle.base_url, handle.token, self.envd_http) as envd:
            async with envd.archive_stream() as response:
                with tempfile.TemporaryFile() as output:
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        await asyncio.to_thread(output.write, chunk)
                    await asyncio.to_thread(output.seek, 0)
                    await self.objects.put(
                        uri, output, length=size, content_type="application/zstd"
                    )
                    result = ArchiveResult(
                        uri, size, response.headers.get("X-Archive-Fallback", "none")
                    )
                    self.archives.append(result)
                    return result

    async def destroy(self, task_id: str) -> None:
        self.destroyed.append(task_id)
        self.handle = None


class ScenarioRunner(AgentRunner):
    def __init__(
        self,
        settings: Settings,
        service: BlackboardClient,
        objects: ObjectStore,
        manager: FakeManager,
        envd_http: httpx.AsyncClient,
        fake: FakeEnvd,
        factory: ScriptFactory,
    ) -> None:
        super().__init__(settings, service, objects, manager)  # type: ignore[arg-type]
        self.envd_http = envd_http
        self.fake = fake
        self.factory = factory
        self.clients: dict[str, ScriptedChatClient] = {}

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
        expected_derive_round: int | None = None,
    ) -> RunResult:
        assert client is None and envd_http_client is None
        if task_type == "explore":
            self.fake.put_file(
                f"/workspace/agents/{agent_id}/evidence.txt", f"proof from {agent_id}"
            )
        state = await self.service.state(task_id)
        scripted = self.factory(task_type, intent_id, mode, agent_id, state)
        self.clients[agent_id] = scripted
        return await super().run_agent(
            task_id,
            agent_id,
            task_type,
            intent_id,
            mode,
            agent_token=agent_token,
            client=scripted,
            handle=handle,
            envd_http_client=self.envd_http,
            expected_derive_round=expected_derive_round,
        )


@dataclass
class Scenario:
    board: PollBoard
    objects: ObjectStore
    fake: FakeEnvd
    envd_http: httpx.AsyncClient
    manager: FakeManager
    runner: ScenarioRunner
    task_id: str
    settings: Settings
    supervisor: TaskSupervisor
    controller: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.controller is not None:
            raise RuntimeError("Scenario supervisor is already running")
        self.controller = asyncio.create_task(self.supervisor.run())

    async def stop(self) -> None:
        if self.controller is not None:
            await self.supervisor.stop()
            await asyncio.wait_for(self.controller, 10)
            self.controller = None

    async def state(self) -> dict[str, Any]:
        return await self.board.state(self.task_id)

    async def events(self, since: int = 0) -> list[dict[str, Any]]:
        return await self.board.events(self.task_id, since)

    async def wait(
        self, predicate: Callable[[dict[str, Any]], bool], seconds: float = 20
    ) -> dict[str, Any]:
        async with asyncio.timeout(seconds):
            while True:
                state = await self.state()
                if predicate(state):
                    return state
                if self.controller is not None and self.controller.done():
                    await self.controller
                    raise AssertionError("Scenario supervisor ended before the expected state")
                await asyncio.sleep(0.05)

    def new_supervisor(self, factory: ScriptFactory | None = None) -> None:
        if self.controller is not None:
            raise RuntimeError("Stop the current supervisor before recovery")
        runner = ScenarioRunner(
            self.settings,
            self.board,
            self.objects,
            self.manager,
            self.envd_http,
            self.fake,
            factory or self.runner.factory,
        )
        self.runner = runner
        self.supervisor = TaskSupervisor(
            self.settings,
            self.board,
            cast(ExecEnvManager, self.manager),
            runner,
        )


@asynccontextmanager
async def scenario(
    runtime_infrastructure,
    tmp_path: Path,
    factory: ScriptFactory,
    spec_overrides: Mapping[str, Any] | None = None,
) -> AsyncIterator[Scenario]:
    database_url, endpoint = runtime_infrastructure
    parsed = make_url(database_url)
    suffix = uuid4().hex[:12]
    token = f"m3b-service-{suffix}"
    board_settings = BoardSettings(
        postgres_host=parsed.host or "127.0.0.1",
        postgres_port=parsed.port or 5432,
        postgres_user=parsed.username or "test",
        postgres_password=SecretStr(parsed.password or "test"),
        postgres_db=parsed.database or "test",
        minio_root_user="bbxm2buser",
        minio_root_password=SecretStr("bbxm2b-test-password"),
        minio_endpoint=f"http://{endpoint}",
        minio_bucket=f"bbxm3b{suffix}",
        service_token=SecretStr(token),
        agent_token_secret=SecretStr(f"m3b-agent-signing-{suffix}-integration-secret"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    objects = ObjectStore(
        endpoint, "bbxm2buser", "bbxm2b-test-password", board_settings.minio_bucket
    )
    await objects.ensure_bucket()
    engine = create_async_engine(database_url, pool_pre_ping=True)
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
                board = PollBoard("http://board", token, board_http)
                spec: dict[str, Any] = {
                    "goal": "Scripted M3b scenario",
                    "acceptance": [{"id": "A1", "desc": "Verify a supported finding"}],
                    "budget": {"max_concurrent_agents": 3, "max_cost": "100", "max_minutes": 20},
                    "agent_profile": "default",
                }
                for key, value in (spec_overrides or {}).items():
                    if isinstance(value, dict) and isinstance(spec.get(key), dict):
                        spec[key] = {**spec[key], **value}
                    else:
                        spec[key] = value
                tid = (await board.create_task(spec))["id"]
                board.only_task_id = tid
                await board.start_task(tid)
                handle = ExecEnvHandle(UUID(tid), "fake-envd", "fake", "http://fake", fake.token)
                manager = FakeManager(handle, envd_http, objects)
                settings = Settings.model_construct(
                    deepseek_api_key=SecretStr("scripted-model"), max_running_tasks=1
                )
                runner = ScenarioRunner(settings, board, objects, manager, envd_http, fake, factory)
                owner = TaskSupervisor(
                    settings,
                    board,
                    cast(ExecEnvManager, manager),
                    runner,
                )
                session = Scenario(
                    board, objects, fake, envd_http, manager, runner, tid, settings, owner
                )
                try:
                    yield session
                finally:
                    await session.stop()
    finally:
        await engine.dispose()
