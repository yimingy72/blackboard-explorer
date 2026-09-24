"""Database behavior against an isolated pgvector PostgreSQL container."""

import asyncio
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.service import BoardService
from bbx_blackboard.store import schema as s
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


class FakeEmbedder:
    async def embed(self, texts):
        vectors = []
        for value in texts:
            vector = [0.0] * 512
            for offset in range(max(1, len(value) - 2)):
                part = value[offset : offset + 3]
                vector[int.from_bytes(hashlib.sha256(part.encode()).digest()[:2], "big") % 512] += 1
            vectors.append(vector)
        return vectors


class FakeObjects:
    async def exists(self, uri):
        return uri in {"evidence/one", "reports/final"}


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
        yield BoardService(engine, FakeEmbedder(), FakeObjects())
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
    assert preview["similar"][0]["id"] == "I1"
    assert len(await service.events(tid)) == count
    related = await service.get_object(tid, "I1")
    assert "F2" in related["related"]
    await service.post_fact(tid, two, fact(statement="Counter evidence", disputes=["F1"]))
    targeted = await service.events(tid, for_agent=one)
    other = await service.events(tid, for_agent=two)
    assert any(x["type"] == "fact.disputed" for x in targeted)
    assert not any(x["type"] == "fact.disputed" for x in other)


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
