"""Offline SQL and transaction checks; PostgreSQL lock/planner checks remain integration work."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import ARRAY, JSON, Column, Integer, MetaData, Table, create_engine, insert, select
from sqlalchemy.dialects.postgresql import JSONB, dialect
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateIndex


@pytest.fixture
def storage(monkeypatch):
    """Run actual SQL through stdlib SQLite with a synchronous-to-async test adapter."""
    metadata = MetaData()
    tables = {}
    for name in ("tasks", "ctf_members", "ctf_turns", "ctf_messages", "agent_sessions", "events"):
        original = getattr(s, name)
        columns = [
            Column(
                col.name,
                JSON() if isinstance(col.type, JSONB | ARRAY) else col.type,
                primary_key=col.primary_key,
                nullable=True,
            )
            for col in original.columns
        ]
        if name == "events":
            columns[0] = Column("version", Integer, primary_key=True)
        tables[name] = Table(name, metadata, *columns)
        # Keep the original JSONB comparator for the initial_context lookup.
        if name != "events":
            monkeypatch.setattr(s, name, tables[name])
    database = create_engine("sqlite://")
    metadata.create_all(database)
    queries = []

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        async def execute(self, query):
            queries.append(query)
            return self.connection.execute(query)

    class Engine:
        @asynccontextmanager
        async def begin(self):
            with database.begin() as connection:
                yield Connection(connection)

        @asynccontextmanager
        async def connect(self):
            with database.connect() as connection:
                yield Connection(connection)

    service = CtfService(cast(AsyncEngine, Engine()))
    service._emit = AsyncMock(return_value=[])
    service.repo.notify = AsyncMock()

    async def append(connection, tid, entries):
        result = []
        for entry in entries:
            row = {"task_id": tid, **entry}
            written = await connection.execute(insert(tables["events"]).values(**row))
            result.append({**row, "version": written.inserted_primary_key[0]})
        return result

    service.repo.append = append
    objects = SimpleNamespace(put=AsyncMock())
    service.objects = objects
    tid, current_id, old_id = uuid4(), str(uuid4()), str(uuid4())
    with database.begin() as connection:
        connection.execute(
            insert(s.tasks).values(
                id=tid,
                mode="ctf",
                deleting=False,
                status="running",
                ctf_control={"phase": "running", "unresolved_checkpoints": [current_id]},
            )
        )
        connection.execute(
            insert(s.ctf_members).values(
                task_id=tid,
                id="lead",
                generation=2,
                current_turn_id=current_id,
                lifecycle="active",
            )
        )
        connection.execute(
            insert(s.ctf_turns),
            [
                {
                    "task_id": tid,
                    "id": current_id,
                    "agent_id": "lead",
                    "generation": 2,
                    "purpose": "execution",
                    "status": "running",
                    "end_reason": None,
                    "assignment_message_ids": [],
                },
                {
                    "task_id": tid,
                    "id": old_id,
                    "agent_id": "lead",
                    "generation": 1,
                    "purpose": "execution",
                    "status": "finished",
                    "end_reason": "completed",
                    "assignment_message_ids": [],
                },
            ],
        )
    result = SimpleNamespace(
        service=service,
        database=database,
        tables=tables,
        queries=queries,
        objects=objects,
        tid=tid,
        current_id=current_id,
        old_id=old_id,
    )
    try:
        yield result
    finally:
        database.dispose()


def message(storage, **changes):
    return {
        "task_id": storage.tid,
        "id": str(uuid4()),
        "recipient_id": "lead",
        "recipient_sequence": 1,
        "sender_kind": "agent",
        "sender_id": "member",
        "kind": "instruction",
        "body": "Synthetic private instruction",
        "purpose": "execution",
        "status": "delivered",
        "session_revision": 1,
        "delivered_turn_id": storage.old_id,
        "claim_turn_id": storage.old_id,
        "claim_generation": 1,
        "claim_token": str(uuid4()),
        **changes,
    }


async def test_session_narrow_revision_query_retains_cas_storage_safety_and_default_response(
    storage,
):
    service = storage.service
    payload = {"state": {"value": "large\x00body" * 1000}}
    async with service.repo.engine.begin() as connection:
        saved = await service.sessions._save_session(
            connection, storage.tid, "lead", payload, "open\x00now", "native", 0
        )
    assert saved["session"] != payload
    assert "\x00" not in json.dumps(saved)
    assert saved == await service.sessions.get_session(storage.tid, "lead")
    async with service.repo.engine.begin() as connection:
        compact = await service.sessions._save_session(
            connection,
            storage.tid,
            "lead",
            payload,
            "open\x00now",
            "native",
            1,
            return_session=False,
        )
    assert compact == {"revision": 2}
    for revision in (0, 1, 3):
        with pytest.raises(HTTPException, match="revision conflict"):
            async with service.repo.engine.begin() as connection:
                await service.sessions._save_session(
                    connection, storage.tid, "lead", {}, "wrong", "native", revision
                )
    assert (await service.sessions.get_session(storage.tid, "lead"))["session"] == saved["session"]
    queries = [query for query in storage.queries if getattr(query, "is_select", False)]
    narrow = [query for query in queries if list(query.selected_columns.keys()) == ["revision"]]
    assert len(narrow) == 5


async def test_checkpoint_small_receipt_preserves_delivery_and_turn_barriers(storage):
    mid = str(uuid4())
    claimed = message(
        storage,
        id=mid,
        status="leased",
        claim_turn_id=storage.current_id,
        claim_generation=2,
        delivered_turn_id=None,
        session_revision=None,
    )
    with storage.database.begin() as connection:
        connection.execute(insert(s.ctf_messages).values(**claimed))
    payload = {
        "state": {
            "bbx_checkpoint_id": str(uuid4()),
            "in_memory": {"messages": [{"role": "user", "message_id": mid, "text": "x" * 200000}]},
        }
    }
    result = await storage.service.checkpoint(
        storage.tid,
        "lead",
        storage.current_id,
        2,
        payload,
        "synthetic opening",
        0,
        [{"message_id": mid, "claim_token": claimed["claim_token"]}],
    )
    assert result == {
        "revision": 1,
        "checkpoint_id": payload["state"]["bbx_checkpoint_id"],
        "task_id": str(storage.tid),
        "agent_id": "lead",
        "turn_id": storage.current_id,
        "generation": 2,
        "purpose": "execution",
        "delivered_message_ids": [mid],
    }
    assert len(json.dumps(result).encode()) < 500
    saved = await storage.service.sessions.get_session(storage.tid, "lead")
    assert saved["session"] == payload
    with storage.database.connect() as connection:
        delivered = connection.execute(select(s.ctf_messages)).mappings().one()
        assert delivered["status"] == "delivered"
        assert delivered["session_revision"] == result["revision"]
        current = (
            connection.execute(select(s.ctf_turns).where(s.ctf_turns.c.id == storage.current_id))
            .mappings()
            .one()
        )
        assert current["checkpoint_revision"] == result["revision"]
        assert current["assignment_message_ids"] == [mid]
        assert (
            connection.execute(select(s.tasks.c.ctf_control)).scalar_one()["unresolved_checkpoints"]
            == []
        )
    with pytest.raises(HTTPException, match="revision conflict"):
        await storage.service.checkpoint(
            storage.tid, "lead", storage.current_id, 2, {}, "wrong", 0, []
        )
    with pytest.raises(HTTPException, match="Stale CTF generation"):
        await storage.service.checkpoint(
            storage.tid, "lead", storage.current_id, 1, {}, "wrong", 1, []
        )
    with pytest.raises(HTTPException, match="absent from persisted history"):
        await storage.service.checkpoint(
            storage.tid,
            "lead",
            storage.current_id,
            2,
            {},
            "wrong",
            1,
            [{"message_id": mid, "claim_token": claimed["claim_token"]}],
        )
    with pytest.raises(HTTPException, match="Stale mailbox claim"):
        await storage.service.checkpoint(
            storage.tid,
            "lead",
            storage.current_id,
            2,
            payload,
            "wrong",
            1,
            [{"message_id": mid, "claim_token": str(uuid4())}],
        )
    assert await storage.service.sessions.get_session(storage.tid, "lead") == saved


@pytest.mark.parametrize("kind", ["initial_context", "tool_result", "model_output"])
async def test_observation_lookup_deduplicates_without_losing_conflicts_or_fences(storage, kind):
    args = (storage.tid, "lead", storage.current_id, 2, kind)
    body = {"step": 1, "tool": "synthetic", "value": "offline"}
    first = await storage.service.record_observation(*args, body, "request")
    assert await storage.service.record_observation(*args, body, "request") == first
    assert storage.objects.put.await_count == 1
    if kind == "initial_context":
        assert await storage.service.record_observation(*args, {}, "other-request") == first
    else:
        with pytest.raises(HTTPException, match="Observation request ID conflict"):
            await storage.service.record_observation(*args, {}, "request")
        lookup = [
            query
            for query in storage.queries
            if getattr(query, "is_select", False) and query.get_final_froms()[0].name == "events"
        ]
        assert len(lookup) == 4
        for query in lookup:
            compiled = str(query.compile(dialect=dialect()))
            assert "events.object_id =" in compiled
            assert "events.type = 'agent.trace.recorded'" in compiled
            assert "->>" not in compiled
    with pytest.raises(HTTPException, match="Stale CTF generation"):
        await storage.service.record_observation(
            storage.tid, "lead", storage.current_id, 1, kind, body, "late"
        )
    assert storage.objects.put.await_count == 1


async def test_observation_rechecks_after_upload_and_rejects_new_generation(storage):
    async def invalidate(*args, **kwargs):
        with storage.database.begin() as connection:
            connection.execute(
                s.ctf_members.update().values(generation=3).where(s.ctf_members.c.id == "lead")
            )

    storage.objects.put.side_effect = invalidate
    with pytest.raises(HTTPException, match="Stale CTF generation"):
        await storage.service.record_observation(
            storage.tid, "lead", storage.current_id, 2, "model_output", {"step": 1}, "late"
        )
    with storage.database.connect() as connection:
        assert connection.execute(select(storage.tables["events"])).first() is None


async def test_observation_rechecks_after_upload_and_accepts_concurrent_identical_write(storage):
    observation_args = (
        storage.tid,
        "lead",
        storage.current_id,
        2,
        "model_output",
        {"step": 1},
        "same",
    )
    concurrent = {}

    async def duplicate(*args, **kwargs):
        storage.objects.put.side_effect = None
        concurrent.update(await storage.service.record_observation(*observation_args))

    storage.objects.put.side_effect = duplicate
    result = await storage.service.record_observation(*observation_args)
    assert result == concurrent
    with storage.database.connect() as connection:
        events = connection.execute(select(storage.tables["events"])).all()
        assert len(events) == 1
    assert storage.objects.put.await_count == 2


def test_observation_index_targets_the_exact_lookup():
    index = next(index for index in s.events.indexes if index.name == "ix_events_ctf_observation")
    ddl = str(CreateIndex(index).compile(dialect=dialect()))
    assert "(task_id, object_id) WHERE type = 'agent.trace.recorded'" in ddl


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {
            "sender_kind": "system",
            "kind": "turn_finished",
            "notification_purpose": "assignment_result",
        },
        {"sender_kind": "system", "notification_purpose": "turn_recovery"},
        {"sender_kind": "system", "notification_purpose": "context_continuation"},
    ],
)
async def test_recovery_proofs_only_return_bounded_metadata_from_completed_old_turn(
    storage, changes
):
    entry = message(storage, **changes)
    with storage.database.begin() as connection:
        connection.execute(insert(s.ctf_messages).values(**entry))
    result = await storage.service.recovery_input_proofs(
        storage.tid, "lead", storage.current_id, 2, [entry["id"]]
    )
    proof = result["proofs"][0]
    assert proof["id"] == entry["id"]
    assert proof["turn_status"] == "finished"
    assert proof["end_reason"] == "completed"
    assert proof["turn_generation"] == proof["claim_generation"] == 1
    assert "body" not in proof
    assert "session" not in proof
    assert len(proof) == 14


@pytest.mark.parametrize(
    "changes",
    [
        {"recipient_id": "other"},
        {"task_id": uuid4()},
        {"sender_kind": "user"},
        {"sender_kind": "system", "notification_purpose": "unknown"},
        {"sender_kind": "system", "kind": "turn_finished", "notification_purpose": None},
        {"kind": "message"},
        {"purpose": "review"},
        {"status": "leased"},
        {"session_revision": 0},
        {"session_revision": None},
        {"claim_generation": 9},
        {"claim_turn_id": str(uuid4())},
        {"delivered_turn_id": str(uuid4())},
        {"delivered_turn_id": None},
    ],
)
async def test_recovery_proofs_filter_unconfirmed_foreign_and_unknown_messages(storage, changes):
    entry = message(storage, **changes)
    with storage.database.begin() as connection:
        connection.execute(insert(s.ctf_messages).values(**entry))
    assert await storage.service.recovery_input_proofs(
        storage.tid, "lead", storage.current_id, 2, [entry["id"]]
    ) == {"proofs": []}


@pytest.mark.parametrize(
    "changes",
    [
        {"end_reason": "interrupted"},
        {"end_reason": "step_limit"},
        {"end_reason": "context_blocked"},
        {"end_reason": "provider_transient"},
        {"end_reason": None},
        {"status": "running"},
        {"purpose": "review"},
        {"agent_id": "other"},
        {"task_id": uuid4()},
        {"generation": 3},
    ],
)
async def test_recovery_proofs_never_assume_a_noncompleted_or_foreign_turn_finished(
    storage, changes
):
    entry = message(storage)
    with storage.database.begin() as connection:
        connection.execute(insert(s.ctf_messages).values(**entry))
        connection.execute(
            s.ctf_turns.update().where(s.ctf_turns.c.id == storage.old_id).values(**changes)
        )
    assert await storage.service.recovery_input_proofs(
        storage.tid, "lead", storage.current_id, 2, [entry["id"]]
    ) == {"proofs": []}


@pytest.mark.parametrize(
    "identifiers", [["invalid"], [1], [str(uuid4()).upper()], [str(uuid4())] * 101, "invalid"]
)
async def test_recovery_proofs_reject_malformed_or_unbounded_identifiers(storage, identifiers):
    with pytest.raises(HTTPException) as caught:
        await storage.service.recovery_input_proofs(
            storage.tid, "lead", storage.current_id, 2, identifiers
        )
    assert caught.value.status_code == 422


async def test_recovery_proofs_fence_even_empty_requests_and_disallow_review(storage):
    assert await storage.service.recovery_input_proofs(
        storage.tid, "lead", storage.current_id, 2, []
    ) == {"proofs": []}
    with pytest.raises(HTTPException, match="Stale CTF generation"):
        await storage.service.recovery_input_proofs(storage.tid, "lead", storage.current_id, 1, [])
    with pytest.raises(HTTPException, match="Stale CTF turn"):
        await storage.service.recovery_input_proofs(storage.tid, "lead", str(uuid4()), 2, [])
    with storage.database.begin() as connection:
        connection.execute(s.tasks.update().values(status="finished"))
        connection.execute(s.ctf_turns.update().values(purpose="review"))
    with pytest.raises(HTTPException, match="require execution"):
        await storage.service.recovery_input_proofs(storage.tid, "lead", storage.current_id, 2, [])
