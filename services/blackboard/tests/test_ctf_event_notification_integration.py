"""CTF wake notifications become visible only after committed state changes."""

import asyncio
from uuid import uuid4

import asyncpg
import pytest
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_integration import team

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("area", ["mailbox", "board"])
async def test_ctf_notifications_wait_for_commit_and_disappear_on_rollback(ctf, ctf_url, area):
    tid = await team(ctf)
    challenge = await ctf.create_challenge(tid, "user", str(uuid4()), "Synthetic notification")
    connection = await asyncpg.connect(ctf_url.replace("postgresql+asyncpg", "postgresql"))
    notified = asyncio.Event()
    versions = []

    def received(conn, pid, channel, payload):
        versions.append(int(payload))
        notified.set()

    async def change(conn):
        await ctf._task(conn, tid)
        if area == "mailbox":
            await ctf._message(conn, tid, "user", "lead", "Synthetic notice", str(uuid4()))
        else:
            await ctf._board_event(
                conn,
                tid,
                "user",
                "ctf.challenge.updated",
                str(uuid4()),
                "synthetic-hash",
                {**challenge, "revision": challenge["revision"] + 1},
            )

    try:
        await connection.add_listener(f"bbx_task_{tid.hex}", received)
        async with ctf.repo.engine.begin() as conn:
            await change(conn)
            await asyncio.sleep(0.02)
            assert not notified.is_set()
        await asyncio.wait_for(notified.wait(), timeout=2)
        assert versions
        cursor = (await ctf.runtime_state(tid))["event_version"]
        assert versions[-1] <= cursor
        notified.clear()
        count = len(versions)
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            async with ctf.repo.engine.begin() as conn:
                await change(conn)
                raise RuntimeError("synthetic rollback")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(notified.wait(), timeout=0.1)
        assert len(versions) == count
        assert (await ctf.runtime_state(tid))["event_version"] == cursor
    finally:
        await connection.close()
