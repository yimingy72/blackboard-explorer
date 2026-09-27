"""Database behavior against an isolated pgvector PostgreSQL container."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from bbx_blackboard.conversations import Conversations
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.service import BoardService
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


class FakeObjects:
    async def exists(self, uri):
        return uri in {"evidence/one", "reports/final"} or uri.startswith("traces/")


async def test_agent_numbers_are_task_local_under_concurrent_registration(board_service):
    first, second = await asyncio.gather(task(board_service), task(board_service))
    initial = await asyncio.gather(
        board_service.register_agent(first, "explore"),
        board_service.register_agent(second, "explore"),
    )
    assert initial == ["agent-1", "agent-1"]
    same_task = await asyncio.gather(
        *(board_service.register_agent(first, "explore") for _ in range(4))
    )
    assert sorted(same_task) == ["agent-2", "agent-3", "agent-4", "agent-5"]
    assert await board_service.register_agent(second, "explore") == "agent-2"
    assert (await board_service.state(first))["counters"]["agent"] == 5
    assert (await board_service.state(second))["counters"]["agent"] == 2


async def test_trace_events_are_isolated_and_do_not_change_board_projection(board_service):
    first, second = await asyncio.gather(task(board_service), task(board_service))
    assert await board_service.register_agent(first, "explore") == "agent-1"
    assert await board_service.register_agent(second, "explore") == "agent-1"
    before = await board_service.state(first)
    uri = f"traces/{first}/agent-1/000000-initial_context-abcd.json"
    body = {"kind": "initial_context", "step": 0, "uri": uri, "summary": "start"}
    events = await board_service.record_agent_trace(first, "agent-1", body)
    assert events[0]["type"] == "agent.trace.recorded"
    assert events[0]["payload"] == {"agent_id": "agent-1", **body}
    after = await board_service.state(first)
    assert after["last_change_version"] == before["last_change_version"]
    assert after["facts"] == before["facts"] == {}
    assert after["intents"] == before["intents"] == {}
    assert not any(e["type"] == "agent.trace.recorded" for e in await board_service.events(second))
    for invalid in (
        body,
        {**body, "uri": f"traces/{second}/agent-1/other.json"},
        {**body, "uri": f"traces/{first}/agent-2/other.json"},
    ):
        with pytest.raises(RuleViolation):
            await board_service.record_agent_trace(first, "agent-1", invalid)
    with pytest.raises(RuleViolation) as missing:
        await board_service.record_agent_trace(
            first, "agent-2", {**body, "uri": f"traces/{first}/agent-2/other.json"}
        )
    assert missing.value.code == "agent_inactive"
    await board_service.replay(first)
    assert (await board_service.state(first))["last_change_version"] == before[
        "last_change_version"
    ]


@pytest.fixture(scope="module")
def database_url():
    container = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg")
    container.with_name(f"bbx-m1a-test-{uuid4().hex[:8]}")
    with container:
        url = container.get_connection_url()
        config = Config(str(ROOT / "services/blackboard/alembic.ini"))
        old = os.environ.get("BBX_DATABASE_URL")
        os.environ["BBX_DATABASE_URL"] = url
        try:
            command.upgrade(config, "head")
            yield url
            command.downgrade(config, "base")
        finally:
            if old is None:
                os.environ.pop("BBX_DATABASE_URL", None)
            else:
                os.environ["BBX_DATABASE_URL"] = old


@pytest.fixture
async def board_service(database_url):
    engine = create_async_engine(database_url)
    try:
        yield BoardService(engine, FakeObjects())
    finally:
        await engine.dispose()


async def task(service):
    tid = await service.create_task(
        {
            "goal": "Find cause",
            "acceptance": [{"id": "A1", "desc": "Show cause"}],
            "budget": {"max_cost": "10", "max_minutes": 60},
            "agent_profile": "default",
        }
    )
    await service.transition(tid, "provisioning")
    await service.transition(tid, "running")
    return tid


def fact(**changes):
    return {
        "kind": "observation",
        "statement": "The system returned a verifiable result",
        "evidence": [{"type": "text", "uri": "evidence/one", "summary": "result"}],
        **changes,
    }


def intent(**changes):
    return {
        "statement": "Find the source",
        "based_on": ["F1"],
        "expected": "A trace",
        "method": "Read logs",
        "relates_to": ["A1"],
        **changes,
    }


async def projections(service, tid):
    async with service.repo.engine.connect() as conn:
        result = {}
        for table in (s.tasks, s.task_counters, s.facts, s.intents, s.agent_runs, s.tool_calls):
            key = "id" if table is s.tasks else "task_id"
            rows = (await conn.execute(select(table).where(table.c[key] == tid))).mappings().all()
            result[table.name] = sorted((dict(x) for x in rows), key=lambda x: str(x))
        return result


async def test_conversation_cas_claim_and_reply_are_atomic(board_service):
    first, second = await asyncio.gather(task(board_service), task(board_service))
    aid = await board_service.register_agent(first, "explore")
    other_aid = await board_service.register_agent(second, "explore")
    store = Conversations(board_service.repo.engine)
    session = {"type": "session", "session_id": "s1", "state": {}}
    with pytest.raises(HTTPException) as missing:
        await store.get_session(first, aid)
    assert missing.value.status_code == 404
    results = await asyncio.gather(
        *(store.put_session(first, aid, session, "opening", "native", 0, []) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(item, dict) for item in results) == 1
    conflicts = [item for item in results if isinstance(item, HTTPException)]
    assert len(conflicts) == 1 and conflicts[0].status_code == 409
    assert (await store.get_session(first, aid))["revision"] == 1

    mid = uuid4()
    posted = await store.post_message(first, aid, mid, "question")
    assert posted["status"] == "queued"
    assert await store.post_message(first, aid, mid, "question") == posted
    assert (
        len(
            [
                event
                for event in await board_service.events(first)
                if event["type"] == "agent.message.posted"
            ]
        )
        == 1
    )
    for tid, agent_id, content in ((first, aid, "different"), (second, other_aid, "question")):
        with pytest.raises(HTTPException) as conflict:
            await store.post_message(tid, agent_id, mid, content)
        assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as cross_task:
        await store.claim(second, other_aid, mid, "active")
    assert cross_task.value.status_code == 404

    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.agent_runs)
            .where(s.agent_runs.c.task_id == first, s.agent_runs.c.id == aid)
            .values(status="finished")
        )
    assert (await store.pending())[0]["id"] == mid
    claimed = await store.claim(first, aid, mid, "review")
    with pytest.raises(HTTPException) as stale:
        await store.complete(first, aid, mid, uuid4(), session, "opening", "native", 1, "reply", {})
    assert stale.value.status_code == 409
    reply = await store.complete(
        first,
        aid,
        mid,
        claimed["claim_token"],
        session,
        "opening",
        "native",
        1,
        "reply",
        {"cost": 1},
    )
    assert reply["reply_to"] == mid
    repeated = await store.complete(
        first,
        aid,
        mid,
        claimed["claim_token"],
        session,
        "opening",
        "native",
        1,
        "reply",
        {"cost": 1},
    )
    assert repeated["id"] == reply["id"]
    assert len((await store.list_messages(first, aid))["messages"]) == 2
    await board_service.replay(first)
    assert (await store.get_session(first, aid))["revision"] == 2
    assert len((await store.list_messages(first, aid))["messages"]) == 2


async def test_delete_purge_retries_and_preserves_other_tasks(board_service):
    first, second = await asyncio.gather(task(board_service), task(board_service))
    aid = await board_service.register_agent(first, "explore")
    store = Conversations(board_service.repo.engine)
    await store.put_session(first, aid, {"type": "session"}, "opening", "native", 0, [])
    await store.post_message(first, aid, uuid4(), "queued")
    with pytest.raises(HTTPException) as active:
        await store.request_delete(first)
    assert active.value.status_code == 409
    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.agent_runs).where(s.agent_runs.c.task_id == first).values(status="finished")
        )
        await conn.execute(update(s.tasks).where(s.tasks.c.id == first).values(status="finished"))
    assert (await store.request_delete(first))["deleting"]
    assert (await store.request_delete(first))["deleting"]
    assert first in await store.deletions()
    with pytest.raises(HTTPException) as blocked:
        await store.post_message(first, aid, uuid4(), "late")
    assert blocked.value.status_code == 409

    class Objects:
        def __init__(self):
            self.keys = {
                f"evidence/{first}/agent-1/a",
                f"traces/{first}/agent-1/a.json",
                f"reports/{first}.md",
                f"workspace/{first}.tar.zst",
                f"evidence/{second}/agent-1/keep",
            }
            self.fail_once = True

        async def list(self, prefix):
            return [key for key in self.keys if key.startswith(prefix)]

        async def remove(self, key):
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("object store unavailable")
            self.keys.discard(key)

    objects = Objects()
    with pytest.raises(RuntimeError):
        await store.purge(first, objects)
    assert first in await store.deletions()
    assert (await store.purge(first, objects))["purged"]
    assert (await store.purge(first, objects))["purged"]
    assert await store.deletions() == []
    assert objects.keys == {f"evidence/{second}/agent-1/keep"}
    assert (await board_service.state(second))["task"]["id"] == second


async def test_message_and_delete_share_task_lock(board_service):
    tid = await task(board_service)
    aid = await board_service.register_agent(tid, "explore")
    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.agent_runs).where(s.agent_runs.c.task_id == tid).values(status="finished")
        )
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(status="finished"))
    store = Conversations(board_service.repo.engine)
    results = await asyncio.gather(
        store.post_message(tid, aid, uuid4(), "racing message"),
        store.request_delete(tid),
        return_exceptions=True,
    )
    assert results[1] == {"task_id": tid, "deleting": True}
    if isinstance(results[0], Exception):
        assert isinstance(results[0], HTTPException) and results[0].status_code == 409
    else:
        assert (await store.list_messages(tid, aid))["messages"][0]["status"] == "failed"


async def test_parallel_derive_registration_and_empty_result(board_service):
    tid = await task(board_service)
    explore = await board_service.register_agent(tid, "explore")
    posted = await board_service.post_fact(tid, explore, fact())
    derive = await board_service.register_agent(tid, "derive")
    agent = (await board_service.state(tid))["agents"][derive]
    assert agent["derive_from_version"] == posted["events"][0]["version"]
    assert agent["derive_parallel"] is True
    async with board_service.repo.engine.begin() as conn:
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(derive_empty_streak=2))
    await board_service.finish_agent(
        tid,
        derive,
        {"accepted": True, "data": {"posted": [], "excluded": []}},
        "normal",
    )
    assert (await board_service.state(tid))["task"]["derive_empty_streak"] == 2


async def test_expired_claim_replaces_token_and_recover_requeues(board_service):
    tid = await task(board_service)
    aid = await board_service.register_agent(tid, "explore")
    store = Conversations(board_service.repo.engine)
    mid = uuid4()
    await store.post_message(tid, aid, mid, "question")
    first = await store.claim(tid, aid, mid, "active")
    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.agent_messages)
            .where(s.agent_messages.c.id == mid)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    second = await store.claim(tid, aid, mid, "active")
    assert second["claim_token"] != first["claim_token"]
    with pytest.raises(HTTPException) as stale:
        await store.put_session(
            tid,
            aid,
            {"type": "session"},
            "opening",
            "native",
            0,
            [{"id": mid, "claim_token": first["claim_token"]}],
        )
    assert stale.value.status_code == 409
    assert await store.recover() == {"requeued": 1}
    assert (await store.list_messages(tid, aid, "queued"))["messages"][0]["id"] == mid
    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.agent_runs)
            .where(s.agent_runs.c.task_id == tid, s.agent_runs.c.id == aid)
            .values(status="finished")
        )
    old_review = await store.claim(tid, aid, mid, "review")
    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.agent_messages)
            .where(s.agent_messages.c.id == mid)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    new_review = await store.claim(tid, aid, mid, "review")
    with pytest.raises(HTTPException) as fenced:
        await store.put_session(
            tid,
            aid,
            {"type": "session"},
            "opening",
            "native",
            0,
            [],
            {"id": mid, "claim_token": old_review["claim_token"]},
        )
    assert fenced.value.status_code == 409
    saved = await store.put_session(
        tid,
        aid,
        {"type": "session"},
        "opening",
        "native",
        0,
        [],
        {"id": mid, "claim_token": new_review["claim_token"]},
    )
    assert saved["revision"] == 1


async def test_finish_requeues_only_unconsumed_active_messages(board_service):
    tid = await task(board_service)
    aid = await board_service.register_agent(tid, "explore")
    store = Conversations(board_service.repo.engine)
    waiting, consumed = uuid4(), uuid4()
    await store.post_message(tid, aid, waiting, "unconsumed")
    await store.post_message(tid, aid, consumed, "consumed")
    waiting_claim = await store.claim(tid, aid, waiting, "active")
    consumed_claim = await store.claim(tid, aid, consumed, "active")
    await store.put_session(
        tid,
        aid,
        {"type": "session"},
        "opening",
        "native",
        0,
        [{"id": consumed, "claim_token": consumed_claim["claim_token"]}],
    )
    await board_service.finish_agent(tid, aid, {"accepted": True, "data": {}}, "normal")
    rows = {row["id"]: row for row in (await store.list_messages(tid, aid))["messages"]}
    assert rows[waiting]["status"] == "queued"
    assert rows[consumed]["status"] == "delivered"
    assert [row["id"] for row in await store.pending()] == [waiting]
    with pytest.raises(HTTPException) as old_claim:
        await store.put_session(
            tid,
            aid,
            {"type": "session"},
            "opening",
            "native",
            1,
            [{"id": waiting, "claim_token": waiting_claim["claim_token"]}],
        )
    assert old_claim.value.status_code == 409


async def test_derive_registration_rejects_stale_parallel_phase(board_service):
    tid = await task(board_service)
    explore = await board_service.register_agent(tid, "explore")
    await board_service.post_fact(tid, explore, fact())
    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.agent_runs)
            .where(s.agent_runs.c.task_id == tid, s.agent_runs.c.id == explore)
            .values(status="finished")
        )
    with pytest.raises(RuleViolation) as stale_parallel:
        await board_service.register_agent(tid, "derive", derive_parallel=True)
    assert stale_parallel.value.code == "stale_derive"
    assert len((await board_service.state(tid))["agents"]) == 1
    with pytest.raises(RuleViolation) as stale_quiescent:
        await board_service.register_agent(tid, "derive", derive_parallel=False)
    assert stale_quiescent.value.code == "stale_derive"
    async with board_service.repo.engine.begin() as conn:
        board = await board_service.state(tid)
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(last_judgment_version=board["last_change_version"])
        )
    derive = await board_service.register_agent(tid, "derive", derive_parallel=False)
    agent = (await board_service.state(tid))["agents"][derive]
    assert agent["derive_parallel"] is False


async def test_replay_and_lifecycle(board_service):
    service = board_service
    tid = await task(service)
    aid = await service.register_agent(tid, "explore", is_seed=True)
    await service.record_tool_call(
        tid, aid, {"id": "c1", "tool": "execute", "args": {}, "result_head": "ok"}
    )
    first = await service.post_fact(tid, aid, fact())
    assert first["id"] == "F1"
    assert (await service.state(tid))["facts"]["F1"]["provenance"] == "self_reported"
    second = await service.post_fact(
        tid,
        aid,
        fact(
            statement="Tool observed 502",
            evidence=[{"type": "text", "uri": "evidence/one", "summary": "tool", "call_id": "c1"}],
        ),
    )
    assert (await service.state(tid))["facts"][second["id"]]["provenance"] == "tool_backed"
    posted = await service.post_intent(tid, aid, intent(claim=True))
    iid = posted["id"]
    assert (await service.state(tid))["intents"][iid]["holder"] == aid
    await service.heartbeat(
        tid, aid, steps=1, context_tokens=100, usage={"cache_miss_tokens": 100}, last_seen_version=3
    )
    await service.heartbeat(
        tid, aid, steps=1, context_tokens=120, usage={"cache_miss_tokens": 50}, last_seen_version=2
    )
    progress = (await service.state(tid))["agents"][aid]
    assert progress["steps"] == 2
    assert progress["usage"]["cache_miss_tokens"] == 150
    assert progress["last_seen_version"] == 3
    await service.post_fact(
        tid,
        aid,
        fact(
            statement="The trace confirms the source",
            resolves=iid,
            result="confirmed",
            satisfies=["A1"],
        ),
    )
    assert (await service.state(tid))["pending_claims"]
    close = await service.register_agent(tid, "close", close_mode="judge")
    await service.submit_close(
        tid,
        close,
        {
            "verdicts": [
                {"id": "A1", "verdict": "met", "reason": "confirmed", "evidence_facts": ["F3"]}
            ]
        },
    )
    await service.finish_agent(tid, close, {"accepted": True}, "normal")
    assert not (await service.state(tid))["pending_claims"]
    await service.post_fact(tid, aid, fact(statement="That trace is invalid", disputes=["F3"]))
    assert (await service.state(tid))["task"]["acceptance_state"]["A1"]["status"] == "unmet"
    await service.conclude(tid, aid, "limit")
    assert await service.take_grace(tid, aid) == 2
    await service.finish_agent(tid, aid, {"accepted": True}, "limit")
    derive = await service.register_agent(tid, "derive")
    await service.finish_agent(
        tid, derive, {"accepted": True, "data": {"posted": [], "excluded": ["x"]}}, "normal"
    )
    assert (await service.state(tid))["task"]["derive_empty_streak"] == 1
    followup = await service.register_agent(tid, "explore")
    await service.post_intent(tid, followup, intent(statement="Check the alternative source"))
    assert (await service.state(tid))["task"]["derive_empty_streak"] == 0
    await service.post_fact(
        tid, followup, fact(statement="The counter evidence was outdated", disputes=["F4"])
    )
    assert any(x["type"] == "fact.undisputed" for x in await service.events(tid))
    for _ in range(3):
        await service.claim(tid, followup, "I2")
        await service.release(tid, followup, "I2", "Needs another attempt")
    assert (await service.state(tid))["intents"]["I2"]["result"] == "inconclusive"
    await service.transition(tid, "closing")
    final = await service.register_agent(tid, "close", close_mode="final")
    await service.submit_close(
        tid,
        final,
        {
            "verdicts": [
                {"id": "A1", "verdict": "unmet", "reason": "disputed", "missing": "new trace"}
            ]
        },
        report_uri="reports/final",
    )
    before = await projections(service, tid)
    await service.replay(tid)
    assert await projections(service, tid) == before
    assert (await service.state(tid))["task"]["status"] == "finished"


async def test_concurrent_claim_and_numbering(board_service):
    service = board_service
    tid = await task(service)
    agent = await service.register_agent(tid, "explore")
    await service.post_fact(tid, agent, fact())
    iid = (await service.post_intent(tid, agent, intent()))["id"]
    others = [await service.register_agent(tid, "explore") for _ in range(50)]
    outcomes = await asyncio.gather(
        *(service.claim(tid, x, iid) for x in others), return_exceptions=True
    )
    assert sum(isinstance(x, list) for x in outcomes) == 1
    assert sum(isinstance(x, RuleViolation) for x in outcomes) == 49
    posted = await asyncio.gather(
        *(service.post_intent(tid, agent, intent(statement=f"Direction {n}")) for n in range(30))
    )
    assert sorted(int(x["id"][1:]) for x in posted) == list(range(2, 32))


async def test_claim_race_and_notify(board_service, database_url):
    service = board_service
    tid = await task(service)
    agent = await service.register_agent(tid, "explore")
    claimant = await service.register_agent(tid, "explore")
    await service.post_fact(tid, agent, fact())
    iid = (await service.post_intent(tid, agent, intent()))["id"]
    conn = await asyncpg.connect(database_url.replace("postgresql+asyncpg://", "postgresql://"))
    queue = asyncio.Queue()
    channel = f"bbx_task_{tid.hex}"

    def on_notify(_conn, _pid, _channel, payload):
        queue.put_nowait(payload)

    await conn.add_listener(channel, on_notify)
    try:
        await asyncio.gather(
            service.claim_for(tid, iid, claimant),
            service.post_intent(tid, agent, intent(statement="Independent direction", claim=True)),
        )
        state = await service.state(tid)
        assert state["intents"][iid]["holder"] == claimant
        assert state["intents"]["I2"]["holder"] == agent
        notified = int(await asyncio.wait_for(queue.get(), 3))
        assert notified <= state["task"]["version"]
    finally:
        await conn.remove_listener(channel, on_notify)
        await conn.close()


async def test_archive_event_projects_and_replays(board_service):
    service = board_service
    tid = await task(service)
    await service.transition(tid, "closing")
    await service.transition(tid, "finished")
    uri = f"workspace/{tid}.tar.zst"

    class ArchiveObjects:
        async def exists(self, value):
            return value == uri

    service.objects = ArchiveObjects()
    written = await service.record_archive(tid, uri, 1234, "agents-only")
    assert len(written) == 1
    assert written[0]["type"] == "task.archived"
    assert written[0]["payload"] == {"uri": uri, "size": 1234, "fallback": "agents-only"}
    assert (await service.state(tid))["task"]["workspace_uri"] == uri
    assert await service.record_archive(tid, uri, 1234, "agents-only") == []
    assert sum(event["type"] == "task.archived" for event in await service.events(tid)) == 1
    await service.replay(tid)
    assert (await service.state(tid))["task"]["workspace_uri"] == uri


async def test_runtime_restart_preserves_counters_and_attempts_on_replay(board_service):
    service = board_service
    tid = await task(service)
    first_seed = await service.register_agent(tid, "explore", is_seed=True)
    await service.finish_agent(tid, first_seed, {"accepted": True}, "normal")
    failing = await service.register_agent(tid, "explore")
    await service.finish_agent(tid, failing, {"accepted": False}, "runtime_error")
    interrupted_seed = await service.register_agent(tid, "explore", is_seed=True)
    await service.conclude(tid, interrupted_seed, "limit")
    await service.finish_agent(tid, interrupted_seed, {"accepted": False}, "runtime_restart")
    state = await service.state(tid)
    assert state["task"]["status"] == "running"
    assert state["task"]["failure_streak"] == 1
    assert state["task"]["seed_empty_count"] == 1
    before = await projections(service, tid)
    await service.replay(tid)
    assert await projections(service, tid) == before

    with_intent = await task(service)
    holder = await service.register_agent(with_intent, "explore")
    await service.post_fact(with_intent, holder, fact())
    await service.post_intent(with_intent, holder, intent(claim=True))
    await service.conclude(with_intent, holder, "limit")
    await service.finish_agent(with_intent, holder, {"accepted": False}, "runtime_restart")
    state = await service.state(with_intent)
    assert state["intents"]["I1"]["attempts"] == 0
    assert state["intents"]["I1"]["status"] == "open"
    before = await projections(service, with_intent)
    await service.replay(with_intent)
    assert await projections(service, with_intent) == before


async def test_dry_run_objects_and_event_filter(board_service):
    service = board_service
    tid = await task(service)
    one = await service.register_agent(tid, "explore")
    two = await service.register_agent(tid, "explore")
    with pytest.raises(RuleViolation) as missing:
        await service.post_fact(
            tid, one, fact(evidence=[{"type": "text", "uri": "missing", "summary": "gone"}])
        )
    assert missing.value.code == "evidence_missing"
    await service.post_fact(tid, one, fact())
    await service.post_intent(tid, one, intent())
    await service.claim(tid, one, "I1")
    await service.post_fact(
        tid, one, fact(statement="Resolution", resolves="I1", result="inconclusive")
    )
    count = len(await service.events(tid))
    preview = await service.post_intent(
        tid,
        two,
        intent(statement="Find the source again", method="Inspect traces", retry_of="I1"),
        dry_run=True,
    )
    assert preview == {"valid": True}
    assert len(await service.events(tid)) == count
    related = await service.get_object(tid, "I1")
    assert "F2" in related["related"]
    await service.post_fact(tid, two, fact(statement="Counter evidence", disputes=["F1"]))
    targeted = await service.events(tid, for_agent=one)
    other = await service.events(tid, for_agent=two)
    assert any(x["type"] == "fact.disputed" for x in targeted)
    assert not any(x["type"] == "fact.disputed" for x in other)


async def test_agent_owned_duplicate_judgment_and_legacy_replay(board_service):
    service = board_service
    tid = await task(service)
    aid = await service.register_agent(tid, "explore")
    before = await service.events(tid)
    assert await service.post_fact(tid, aid, fact(), dry_run=True) == {"valid": True}
    assert await service.events(tid) == before
    first = await service.post_fact(tid, aid, fact())
    second = await service.post_fact(tid, aid, fact())
    assert (first["id"], second["id"]) == ("F1", "F2")
    assert "similar" not in first
    event = first["events"][0]
    # Simulate an immutable event recorded by the former vector-based version.
    legacy_payload = {**event["payload"], "embedding": [0.0] * 512}
    async with service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.events)
            .where(s.events.c.version == event["version"])
            .values(payload=legacy_payload)
        )
        assert not (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name IN ('facts', 'intents') AND column_name = 'embedding'"
                )
            )
        ).all()
    await service.replay(tid)
    restored = await service.state(tid)
    assert restored["facts"]["F1"]["statement"] == fact()["statement"]
    assert "embedding" not in restored["facts"]["F1"]


async def test_same_agent_claim_race(board_service):
    service = board_service
    tid = await task(service)
    agent = await service.register_agent(tid, "explore")
    await service.post_fact(tid, agent, fact())
    await service.post_intent(tid, agent, intent())
    outcomes = await asyncio.gather(
        service.claim_for(tid, "I1", agent),
        service.post_intent(tid, agent, intent(statement="Second direction", claim=True)),
        return_exceptions=True,
    )
    assert sum(isinstance(x, RuleViolation) for x in outcomes) == 1
    state = await service.state(tid)
    assert sum(x["holder"] == agent for x in state["intents"].values()) == 1


async def test_terminal_replay(board_service):
    service = board_service
    stopped = await task(service)
    await service.transition(stopped, "stopped", reason="user request")
    before_stop = await projections(service, stopped)
    await service.replay(stopped)
    assert await projections(service, stopped) == before_stop
    failed = await task(service)
    for _ in range(3):
        agent = await service.register_agent(failed, "explore")
        await service.finish_agent(
            failed, agent, {"accepted": False, "reason": "error"}, "runtime_error"
        )
    assert (await service.state(failed))["task"]["status"] == "failed"
    before_fail = await projections(service, failed)
    await service.replay(failed)
    assert await projections(service, failed) == before_fail
