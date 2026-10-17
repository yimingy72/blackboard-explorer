"""A concurrent task change cannot disappear behind a CTF snapshot cursor."""

from typing import cast
from uuid import uuid4

import pytest
from bbx_blackboard.service import BoardService, ObjectStore
from sqlalchemy.ext.asyncio import AsyncConnection
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_ctf_integration import team

pytestmark = pytest.mark.integration


async def test_ctf_snapshot_cursor_keeps_concurrent_task_update_in_delta(ctf, monkeypatch):
    tid = await team(ctf)
    initial = await ctf.state(tid)
    execute = AsyncConnection.execute
    committed = False

    async def concurrent_change(conn, statement, *args, **kwargs):
        nonlocal committed
        result = await execute(conn, statement, *args, **kwargs)
        if not committed and str(statement).startswith("SELECT tasks.id,"):
            committed = True
            await ctf.request_finish(
                tid,
                actor="user",
                request_id=uuid4(),
                conclusion={
                    "end_reason": "user_stop",
                    "summary": "Synthetic concurrent stop",
                },
            )
        return result

    monkeypatch.setattr(AsyncConnection, "execute", concurrent_change)
    snapshot = await ctf.state(tid)
    assert committed
    assert snapshot["task"]["status"] == "running"
    assert snapshot["event_version"] == initial["event_version"]
    delta = await BoardService(ctf.repo.engine, cast(ObjectStore, None)).events(
        tid, snapshot["event_version"]
    )
    assert any(item["type"] == "ctf.conclusion.requested" for item in delta)
    assert (await ctf.state(tid))["task"]["ctf_control"]["phase"] == "closing"
