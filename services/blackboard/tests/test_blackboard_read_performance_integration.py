"""Ordinary state and message reads against disposable PostgreSQL."""

import json
from datetime import UTC, datetime

import pytest
from bbx_blackboard.conversations import Conversations
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.store import schema as s
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url
from test_store_integration import fact, task

pytestmark = pytest.mark.integration


async def test_state_snapshot_cursor_keeps_concurrent_terminal_event_in_delta(
    board_service, monkeypatch
):
    tid = await task(board_service)
    initial = await board_service.state(tid)
    execute = AsyncConnection.execute
    committed = False

    async def concurrent_close(conn, statement, *args, **kwargs):
        nonlocal committed
        result = await execute(conn, statement, *args, **kwargs)
        if not committed and str(statement).startswith("SELECT tasks.id,"):
            committed = True
            await board_service.transition(tid, "failed", reason="Synthetic concurrent close")
        return result

    monkeypatch.setattr(AsyncConnection, "execute", concurrent_close)
    snapshot = await board_service.state(tid)
    assert committed
    assert snapshot["task"]["status"] == "running"
    assert snapshot["task"]["version"] == initial["task"]["version"]
    delta = await board_service.events(tid, snapshot["task"]["version"])
    assert [item["type"] for item in delta] == ["task.failed"]
    assert (await board_service.state(tid))["task"]["status"] == "failed"


async def test_read_state_omits_large_tool_rows_but_writes_keep_provenance_and_duplicate_checks(
    board_service, monkeypatch
):
    tid = await task(board_service)
    aid = await board_service.register_agent(tid, "explore")
    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            insert(s.tool_calls),
            [
                {
                    "task_id": tid,
                    "id": f"synthetic-{index}",
                    "agent_id": aid,
                    "tool": "inspect",
                    "args": {"command": "x" * 6000},
                    "result_head": "y" * 4096,
                    "created_at": datetime.now(UTC),
                }
                for index in range(1000)
            ],
        )
    statements = []
    execute = AsyncConnection.execute

    async def capture(conn, statement, *args, **kwargs):
        statements.append(str(statement))
        return await execute(conn, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", capture)
    state = await board_service.state(tid)
    assert len(statements) == 6
    assert not any("FROM tool_calls" in sql for sql in statements)
    assert all("FOR UPDATE" not in sql for sql in statements)
    assert len(json.dumps(state, default=str).encode()) < 3000
    async with board_service.repo.engine.connect() as conn:
        full = await board_service.repo.load(conn, tid)
    assert len(full.tool_calls) == 1000
    assert len(json.dumps(full.tool_calls, default=str).encode()) > 10_000_000
    statements.clear()
    posted = await board_service.post_fact(
        tid,
        aid,
        fact(
            evidence=[
                {
                    "type": "text",
                    "uri": "evidence/one",
                    "summary": "Synthetic tool observation",
                    "call_id": "synthetic-0",
                }
            ]
        ),
    )
    assert any("FROM tool_calls" in sql for sql in statements)
    assert (await board_service.state(tid))["facts"][posted["id"]]["provenance"] == "tool_backed"
    with pytest.raises(RuleViolation) as duplicate:
        await board_service.record_tool_call(
            tid, aid, {"id": "synthetic-0", "tool": "inspect", "args": {}}
        )
    assert duplicate.value.code == "duplicate_tool_call"
    foreign = await board_service.register_agent(tid, "explore")
    posted = await board_service.post_fact(
        tid,
        foreign,
        fact(
            evidence=[
                {
                    "type": "text",
                    "uri": "evidence/one",
                    "summary": "Synthetic foreign observation",
                    "call_id": "synthetic-0",
                }
            ]
        ),
    )
    assert (await board_service.state(tid))["facts"][posted["id"]]["provenance"] == "self_reported"


@pytest.mark.parametrize("origin", ["native", "legacy"])
@pytest.mark.parametrize("large_history", [False, True])
async def test_message_metadata_keeps_empty_and_large_sessions_without_fetching_history(
    board_service, monkeypatch, origin, large_history
):
    tid = await task(board_service)
    aid = await board_service.register_agent(tid, "explore")
    store = Conversations(board_service.repo.engine)
    absent = await store.list_messages(tid, aid)
    assert not absent["session_available"] and absent["session_origin"] is None
    session = {"state": {"history": "x" * 6_500_000}} if large_history else {}
    await store.put_session(tid, aid, session, "Synthetic opening", origin, 0, [])
    projections = []
    execute = AsyncConnection.execute

    async def capture(conn, statement, *args, **kwargs):
        result = await execute(conn, statement, *args, **kwargs)
        if getattr(statement, "is_select", False) and "agent_sessions" in str(statement):
            assert list(statement.selected_columns.keys()) == ["origin"]
            frozen = result.freeze()
            projections.extend(dict(row) for row in frozen().mappings())
            return frozen()
        return result

    monkeypatch.setattr(AsyncConnection, "execute", capture)
    metadata = await store.list_messages(tid, aid)
    assert metadata["session_available"] and metadata["session_origin"] == origin
    assert metadata["mode"] == "active"
    assert len(projections) == 1 and len(json.dumps(projections).encode()) < 40
    monkeypatch.setattr(AsyncConnection, "execute", execute)
    assert (await store.get_session(tid, aid))["session"] == session
