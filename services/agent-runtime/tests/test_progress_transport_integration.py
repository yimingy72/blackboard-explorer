"""Real progress HTTP, PostgreSQL transactions and native scripted MAF sessions."""

import asyncio
import html
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from agent_framework import AgentSession
from bbx_blackboard.api import create_app
from bbx_blackboard.progress import AGENT_ID, SOURCE_TYPES
from bbx_blackboard.settings import Settings as BoardSettings
from bbx_blackboard.store import schema as s
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.progress.client import ProgressClient
from bbx_runtime.progress.worker import ProgressWorker
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import (
    ScriptedChatClient,
    ScriptStep,
    ScriptToolCall,
    ScriptUsage,
)
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from services.blackboard.tests.test_store_integration import FakeObjects
from services.blackboard.tests.test_store_integration import database_url as database_url

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
USAGE = ScriptUsage(7, 23, 11, 2)


class ProcessLostAfterCommit(BaseException):
    """Simulate process loss without running the worker's graceful failure handler."""


class ProgressObjects(FakeObjects):
    async def exists(self, uri):
        return uri.startswith("evidence/") or await super().exists(uri)


class ProgressTransport(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.operations: list[tuple[str, str, int]] = []
        self.drop_completed_checkpoint = False
        self.crash_completed_checkpoint = False
        self.crashed = False
        self.dropped = False
        self.cancel_dispatch_checkpoint = False
        self.cancelled = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        self.operations.append((request.method, request.url.path, response.status_code))
        if request.url.path.endswith("/checkpoint") and request.method == "POST":
            body = json.loads(request.content)
            saved = body["session"]["state"]
            if (
                self.cancel_dispatch_checkpoint
                and not self.cancelled
                and saved.get("bbx_progress_active_request", {}).get("status") == "dispatching"
            ):
                assert response.status_code == 200
                await response.aread()
                self.cancelled = True
                raise asyncio.CancelledError()
            if len(saved.get("bbx_progress_receipts", {})) >= 2 and not saved.get(
                "bbx_progress_active_request"
            ):
                assert response.status_code == 200
                await response.aread()
                if self.crash_completed_checkpoint and not self.crashed:
                    self.crashed = True
                    raise ProcessLostAfterCommit()
                if self.drop_completed_checkpoint and not self.dropped:
                    self.dropped = True
                    raise httpx.ReadError("Synthetic committed checkpoint response loss")
        return response

    async def aclose(self):
        await self.inner.aclose()


@pytest.fixture
async def progress_http(database_url):
    engine = create_async_engine(database_url)
    config = BoardSettings.model_construct(
        postgres_password=SecretStr("test-only"),
        minio_root_password=SecretStr("test-only"),
        service_token=SecretStr("progress-transport-service"),
        agent_token_secret=SecretStr("progress-transport-signing-secret-at-least-32"),
        admin_users=SecretStr("admin:test-only"),
        profiles_dir=ROOT / "profiles/default",
    )
    app = create_app(config, engine=engine, objects=ProgressObjects())
    await app.state.profile_store.ensure_bundled(config.profiles_dir, app.state.platform_store)
    transport = ProgressTransport(app)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as http:
        board = BlackboardClient("http://board", config.service_token.get_secret_value(), http)
        try:
            yield board, app, transport
        finally:
            # No task runtime, Ubuntu or fixed application port is started by these tests.
            async with engine.begin() as conn:
                rows = (await conn.execute(select(s.task_progress))).mappings().all()
                for row in rows:
                    value = dict(row["data"])
                    value["settings"] = {**value["settings"], "enabled": False}
                    await conn.execute(
                        update(s.task_progress)
                        .where(s.task_progress.c.task_id == row["task_id"])
                        .values(data=value)
                    )
            await app.state.workspace_cache.close()
            await engine.dispose()


async def create_progress_task(board: BlackboardClient, *, facts: int = 2, intents: int = 1):
    tid = (
        await board.create_task(
            {
                "goal": "解释旧编码样例触发校验失败的条件与范围",
                "acceptance": [{"id": "A1", "desc": "识别失败条件和未证边界"}],
                "budget": {"max_cost": "10", "max_minutes": 60},
                "agent_profile": "default",
            }
        )
    )["id"]
    await board.start_task(tid)
    await board.transition(tid, "running")
    registration = await board.register_agent(tid, "explore", is_seed=True)
    writer = board.with_token(registration["token"])
    for index in range(facts):
        await writer.post_fact(
            tid,
            {
                "kind": "observation",
                "statement": f"旧编码样例{index + 1}在长度阈值16处触发校验失败，其他条件未验证。",
                "evidence": [
                    {
                        "type": "text",
                        "uri": f"evidence/{tid}/{registration['agent_id']}/baseline.txt",
                        "summary": "阈值16的样例输出",
                    }
                ],
            },
        )
    for index in range(intents):
        await writer.post_intent(
            tid,
            {
                "statement": f"核对第{index + 1}组旧编码样例的失败边界",
                "based_on": ["F1"],
                "expected": "长度阈值16能够区分失败输入",
                "method": "对比阈值两侧输入并保留反例",
                "relates_to": ["A1"],
            },
        )
    await board._json(
        "PUT",
        f"/tasks/{tid}/progress/settings",
        json={"enabled": True, "max_cost": "20", "merge_seconds": 0, "min_interval_seconds": 3600},
    )
    events = await board.events(tid)
    sources = [evt for evt in events if evt["type"] in SOURCE_TYPES]
    return tid, registration["agent_id"], sources


def finding(refs):
    return {
        "id": "encoding-boundary",
        "parent_id": "goal",
        "kind": "finding",
        "title": "旧编码样例在长度阈值16处触发校验失败",
        "question": "失败是否由旧编码样例的长度条件触发？",
        "summary": "阈值16处已有失败样例，跨输入族的适用范围尚未证实。",
        "significance": "将输入族和长度条件关联，缩小下一步补证范围。",
        "next_step": "按相关意图对比阈值两侧输入。",
        "limitations": "现有观察不能证明唯一成因，也未覆盖全部输入族。",
        "source_refs": refs,
        "key_steps": [{"text": "阈值16处观察到校验失败。", "source_refs": refs[:1]}],
    }


def refs_for(sources, through):
    selected = [evt for evt in sources if evt["version"] <= through]
    first = next(evt for evt in selected if evt["type"] == "fact.posted")
    last = selected[-1]
    refs = [{"kind": "fact", "id": first["payload"]["id"], "version": first["version"]}]
    if last["type"] in {"fact.posted", "intent.posted"}:
        refs.append(
            {
                "kind": last["type"].split(".")[0],
                "id": last["payload"]["id"],
                "version": last["version"],
            }
        )
    return refs


def script_for(job, sources, *, read_tools=False):
    selected = [
        evt for evt in sources if job["source_version"] < evt["version"] <= job["through_version"]
    ]
    batches = max(1, (len(selected) + 29) // 30)
    steps = []
    for index in range(batches):
        if index == 0 and read_tools:
            steps.append(
                ScriptStep(
                    calls=(
                        ScriptToolCall("get", {"object_id": "F1"}),
                        ScriptToolCall("search", {"q": "旧编码", "limit": 3}),
                    ),
                    usage=USAGE,
                )
            )
        steps.extend(
            [
                ScriptStep(
                    calls=(
                        ScriptToolCall(
                            "update_progress_tree",
                            {
                                "upserts": [finding(refs_for(sources, job["through_version"]))],
                                "removed_ids": [],
                                "overview": (
                                    "旧编码样例在阈值16处已有失败依据；"
                                    "关键子问题是适用范围和替代解释。"
                                ),
                            },
                        ),
                    ),
                    usage=USAGE,
                ),
                ScriptStep(
                    text=(f"阈值16处触发失败 [F1](fact:F1)，整理截至 v{job['through_version']}。"),
                    usage=USAGE,
                ),
            ]
        )
    return ScriptedChatClient(steps)


def install_script(monkeypatch, script):
    def client_factory(_model, **kwargs):
        assert kwargs["max_retries"] == 0
        return script

    monkeypatch.setattr("bbx_runtime.progress.worker.make_client", client_factory)


def observed_sources(scripts):
    versions = set()
    for script in scripts:
        for request in script.received_messages:
            for message in request:
                if message.role != "user" or "<progress_source_data>" not in message.text:
                    continue
                encoded = message.text.split("<progress_source_data>\n", 1)[1].split(
                    "\n</progress_source_data>", 1
                )[0]
                value = json.loads(html.unescape(encoded))
                if isinstance(value, dict) and "source_batch" in value:
                    versions.update(evt["version"] for evt in value["source_batch"]["events"])
    return versions


async def test_two_job_backfill_real_http_session_graph_question_and_independent_usage(
    progress_http, monkeypatch
):
    board, app, transport = progress_http
    tid, aid, sources = await create_progress_task(board, facts=105, intents=3)
    assert len(sources) > 100
    before = await board.state(tid)
    worker = ProgressWorker(
        Settings.model_construct(deepseek_api_key=SecretStr("test-only")), board
    )
    first = await worker.control.claim()
    assert first is not None and first["task_id"] == tid
    assert first["through_version"] == sources[99]["version"]
    with pytest.raises(RemoteError) as future:
        await worker.control.bound(first).object("F105")
    assert future.value.status == 404
    scripts = []
    view: dict[str, Any] = {}
    for index in range(2):
        job = first if index == 0 else await worker.control.claim()
        assert job is not None and job["task_id"] == tid
        script = script_for(job, sources, read_tools=index == 0)
        scripts.append(script)
        install_script(monkeypatch, script)
        await worker.process(job)
        assert len(script.received_messages) == len(script.steps)
        view = await board._json("GET", f"/tasks/{tid}/progress")
        assert view["status"] == "idle", view["error"]
        assert view["source_version"] == job["through_version"]
        assert view["revision"] == index + 1
    assert observed_sources(scripts) == {evt["version"] for evt in sources}
    assert (
        scripts[0].received_options[0]["instructions"]
        == scripts[1].received_options[0]["instructions"]
    )
    assert view["source_version"] == view["desired_source_version"]
    assert {n["id"] for n in view["nodes"]} == {"goal", "encoding-boundary"}
    assert view["nodes"][1]["key_steps"][0]["source_refs"]
    assert Decimal(view["budget"]["spent"]) > 0
    assert Decimal(view["budget"]["reserved"]) == Decimal(view["budget"]["unknown"]) == 0
    after = await board.state(tid)
    assert after["task"]["usage"] == before["task"]["usage"]
    assert after["task"]["status"] == before["task"]["status"] == "running"
    assert after["last_change_version"] == before["last_change_version"]
    assert after["agents"][aid] == before["agents"][aid]
    assert await worker.control.claim() is None

    mid = str(uuid4())
    await board._json(
        "POST",
        f"/tasks/{tid}/progress/messages",
        json={"id": mid, "content": "阈值16意味着什么？", "node_id": "encoding-boundary"},
    )
    question = await worker.control.claim()
    assert question is not None and question["kind"] == "question"
    transport.drop_completed_checkpoint = True
    script = script_for(question, sources, read_tools=True)
    install_script(monkeypatch, script)
    await worker.process(question)
    assert transport.dropped
    messages = await board._json("GET", f"/tasks/{tid}/progress/messages")
    replies = [m for m in messages["messages"] if m["role"] == "assistant"]
    assert len(replies) == 1 and replies[0]["reply_to"] == mid
    assert f"v{question['through_version']}" in replies[0]["content"]
    async with app.state.engine.connect() as conn:
        session = (
            (
                await conn.execute(
                    select(s.agent_sessions).where(
                        s.agent_sessions.c.task_id == UUID(tid),
                        s.agent_sessions.c.agent_id == AGENT_ID,
                    )
                )
            )
            .mappings()
            .one()
        )
    native = AgentSession.from_dict(session["session"])
    receipts = native.state["bbx_progress_receipts"]
    calls = sum(len(s.received_messages) for s in [*scripts, script])
    assert len(receipts) == calls
    assert all(receipt["settled"] for receipt in receipts.values())
    assert all(
        receipt["usage"]["output_tokens"] == USAGE.completion_tokens
        and receipt["usage"]["cache_miss_tokens"] == USAGE.prompt_cache_miss_tokens
        for receipt in receipts.values()
    )
    assert any(
        path.endswith("/session") and method == "GET" and status == 200
        for method, path, status in transport.operations
    )
    events = await board.events(tid)
    assert sum(e["type"] == "progress.usage.settled" for e in events) == calls
    assert all(
        status < 400
        for method, path, status in transport.operations
        if method == "POST"
        and any(
            path.endswith("/" + op)
            for op in ("candidate", "checkpoint", "reserve", "settle", "complete")
        )
    )
    final_state = await board.state(tid)
    assert final_state["task"]["usage"] == before["task"]["usage"]
    assert final_state["last_change_version"] == before["last_change_version"]
    final_view = await board._json("GET", f"/tasks/{tid}/progress")
    assert Decimal(final_view["budget"]["spent"]) == sum(
        Decimal(evt["payload"]["cost"]) for evt in events if evt["type"] == "progress.usage.settled"
    )


async def test_restart_after_committed_response_checkpoint_recovers_without_model_replay(
    progress_http, monkeypatch
):
    board, app, transport = progress_http
    tid, _, sources = await create_progress_task(board)
    mid = str(uuid4())
    await board._json(
        "POST", f"/tasks/{tid}/progress/messages", json={"id": mid, "content": "目前确定了什么？"}
    )
    worker = ProgressWorker(
        Settings.model_construct(deepseek_api_key=SecretStr("test-only")), board
    )
    job = await worker.control.claim()
    assert job is not None and job["kind"] == "question"
    script = script_for(job, sources)
    install_script(monkeypatch, script)
    transport.crash_completed_checkpoint = True
    with pytest.raises(ProcessLostAfterCommit):
        await worker.process(job)
    assert transport.crashed and len(script.received_messages) == 2
    async with app.state.engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    select(s.progress_jobs).where(s.progress_jobs.c.id == UUID(job["job_id"]))
                )
            )
            .mappings()
            .one()
        )
        data = dict(row["data"])
        assert [r["status"] for r in data["receipts"].values()].count("reserved") == 1
        data["lease_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await conn.execute(
            update(s.progress_jobs)
            .where(s.progress_jobs.c.id == UUID(job["job_id"]))
            .values(data=data)
        )
    resumed = await ProgressClient(board).claim()
    assert resumed is not None and resumed["job_id"] == job["job_id"]
    untouched = ScriptedChatClient([])
    install_script(monkeypatch, untouched)
    restarted = ProgressWorker(Settings.model_construct(), board)
    await restarted.process(resumed)
    assert untouched.received_messages == []
    view = await board._json("GET", f"/tasks/{tid}/progress")
    assert view["status"] == "idle" and view["revision"] == 1, view["error"]
    assert len(view["nodes"]) == 2
    assert Decimal(view["budget"]["reserved"]) == Decimal(view["budget"]["unknown"]) == 0
    messages = await board._json("GET", f"/tasks/{tid}/progress/messages")
    assert len([m for m in messages["messages"] if m["role"] == "assistant"]) == 1
    events = await board.events(tid)
    assert sum(evt["type"] == "progress.usage.settled" for evt in events) == 2
    assert sum(evt["type"] == "progress.tree.updated" for evt in events) == 1


async def expire_job(app, job):
    async with app.state.engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    select(s.progress_jobs).where(s.progress_jobs.c.id == UUID(job["job_id"]))
                )
            )
            .mappings()
            .one()
        )
        data = {
            **row["data"],
            "lease_until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        await conn.execute(
            update(s.progress_jobs)
            .where(s.progress_jobs.c.id == UUID(job["job_id"]))
            .values(data=data)
        )


async def test_session_known_ceiling_survives_later_object_reads_and_partial_scan_restart(
    progress_http, monkeypatch
):
    board, app, transport = progress_http
    tid, _, sources = await create_progress_task(board, facts=105, intents=0)
    worker = ProgressWorker(
        Settings.model_construct(deepseek_api_key=SecretStr("test-only")), board
    )
    job = await worker.control.claim()
    assert job is not None and job["through_version"] == sources[99]["version"]
    far = next(
        evt for evt in sources if evt["type"] == "fact.posted" and evt["payload"]["id"] == "F89"
    )
    original = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall("get", {"object_id": "F89"}),
                    ScriptToolCall("search", {"q": "样例89", "limit": 3}),
                ),
                usage=USAGE,
            ),
            ScriptStep(
                text=f"第89个样例也在阈值16处失败 [F89](fact:F89)，资料版本{far['version']}。",
                usage=USAGE,
            ),
        ]
    )
    install_script(monkeypatch, original)
    transport.crash_completed_checkpoint = True
    with pytest.raises(ProcessLostAfterCommit):
        await worker.process(job)
    async with app.state.engine.connect() as conn:
        progress = (
            await conn.execute(
                select(s.task_progress.c.data).where(s.task_progress.c.task_id == UUID(tid))
            )
        ).scalar_one()
        job_data = (
            await conn.execute(
                select(s.progress_jobs.c.data).where(s.progress_jobs.c.id == UUID(job["job_id"]))
            )
        ).scalar_one()
    assert job_data["scanned_version"] < far["version"] <= progress["session_knowledge_version"]
    assert progress["session_knowledge_version"] == job["through_version"]
    assert progress["session_source_version"] == job_data["scanned_version"]
    await expire_job(app, job)
    resumed = await worker.control.claim()
    assert resumed is not None and resumed["through_version"] == job["through_version"]
    # The first batch's durable model response is reused; remaining pages still run.
    retry = ScriptedChatClient(script_for(resumed, sources).steps[2:])
    install_script(monkeypatch, retry)
    await worker.process(resumed)
    assert len(original.received_messages) == 2 and len(retry.received_messages) == 6
    assert observed_sources([original, retry]) == {
        evt["version"] for evt in sources if evt["version"] <= job["through_version"]
    }
    view = await board._json("GET", f"/tasks/{tid}/progress")
    assert view["status"] == "idle" and view["source_version"] == job["through_version"]


@pytest.mark.parametrize(
    "cancel_after_reserve", [False, True], ids=["reserve-402", "cancel-unsent"]
)
async def test_unsent_request_failure_is_terminal_and_can_be_explicitly_resumed(
    progress_http, monkeypatch, cancel_after_reserve
):
    board, app, transport = progress_http
    tid, _, sources = await create_progress_task(board)
    mid = str(uuid4())
    await board._json(
        "POST", f"/tasks/{tid}/progress/messages", json={"id": mid, "content": "目前知道什么？"}
    )
    if cancel_after_reserve:
        transport.cancel_dispatch_checkpoint = True
    else:
        await board._json(
            "PUT", f"/tasks/{tid}/progress/settings", json={"enabled": True, "max_cost": "0.000001"}
        )
    worker = ProgressWorker(
        Settings.model_construct(deepseek_api_key=SecretStr("test-only")), board
    )
    job = await worker.control.claim()
    assert job is not None and job["kind"] == "question"
    unused = ScriptedChatClient([])
    install_script(monkeypatch, unused)
    if cancel_after_reserve:
        with pytest.raises(asyncio.CancelledError):
            await worker.process(job)
    else:
        await worker.process(job)
    assert unused.received_messages == []
    assert not any(
        path.endswith("/checkpoint") and status == 409 for _, path, status in transport.operations
    )
    if not cancel_after_reserve:
        assert not any(path.endswith("/settle") for _, path, _ in transport.operations)
    view = await board._json("GET", f"/tasks/{tid}/progress")
    assert view["status"] == "paused", view
    assert all(Decimal(view["budget"][field]) == 0 for field in ("reserved", "unknown", "spent"))
    async with app.state.engine.connect() as conn:
        saved_job = (
            (
                await conn.execute(
                    select(s.progress_jobs).where(s.progress_jobs.c.id == UUID(job["job_id"]))
                )
            )
            .mappings()
            .one()
        )
        saved_session = (
            await conn.execute(
                select(s.agent_sessions.c.session).where(
                    s.agent_sessions.c.task_id == UUID(tid), s.agent_sessions.c.agent_id == AGENT_ID
                )
            )
        ).scalar_one()
    assert saved_job["status"] == "failed"
    assert "bbx_progress_active_request" not in saved_session["state"]
    assert len(saved_job["data"]["receipts"]) == int(cancel_after_reserve)
    if cancel_after_reserve:
        assert all(r["status"] == "settled" for r in saved_job["data"]["receipts"].values())
    messages = await board._json("GET", f"/tasks/{tid}/progress/messages")
    assert next(m for m in messages["messages"] if m["id"] == mid)["status"] == "failed"
    assert await worker.control.claim() is None
    await board._json(
        "PUT", f"/tasks/{tid}/progress/settings", json={"enabled": True, "max_cost": "20"}
    )
    next_mid = str(uuid4())
    await board._json(
        "POST",
        f"/tasks/{tid}/progress/messages",
        json={"id": next_mid, "content": "恢复整理后解释现有依据。"},
    )
    next_job = await worker.control.claim()
    assert next_job is not None and next_job["job_id"] == next_mid
    script = script_for(next_job, sources)
    install_script(monkeypatch, script)
    await worker.process(next_job)
    assert (await board._json("GET", f"/tasks/{tid}/progress"))["status"] == "idle"
