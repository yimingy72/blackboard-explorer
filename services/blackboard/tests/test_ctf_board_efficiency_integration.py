"""Real PostgreSQL isolation, stop ordering and additive mailbox migration."""

import asyncio
import runpy
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import select, text
from test_ctf_integration import board_team, team
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def assigned(ctf):
    tid, owner, helper = await board_team(ctf)
    challenge = await ctf.create_challenge(tid, "user", str(uuid4()), "Synthetic isolated target")
    challenge = await ctf.update_challenge(
        tid,
        "user",
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=1,
        action="assign",
        owner_id=owner["actor"],
    )
    challenge = await ctf.update_challenge(
        tid,
        "user",
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=2,
        action="collaborators",
        collaborator_ids=[helper["actor"]],
    )
    return tid, owner, helper, challenge


async def notices(ctf, tid):
    async with ctf.repo.engine.connect() as conn:
        return [
            dict(row)
            for row in (
                await conn.execute(
                    select(s.ctf_messages).where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.notification_purpose == "challenge_progress",
                    )
                )
            ).mappings()
        ]


def progress(owner, challenge):
    return {
        **owner,
        "challenge_id": challenge["id"],
        "request_id": str(uuid4()),
        "body": "Synthetic reproducible observation",
        "progress": {"stage": "result", "title": "Result", "summary": "Evidence is available"},
    }


async def test_progress_mail_commit_visibility_unique_slot_and_rollback(ctf, ctf_url, monkeypatch):
    tid, owner, helper, challenge = await assigned(ctf)
    listener = await asyncpg.connect(ctf_url.replace("postgresql+asyncpg", "postgresql"))
    notified = asyncio.Event()

    def received(*args):
        notified.set()

    original = ctf._message

    async def observe(*args, **kwargs):
        result = await original(*args, **kwargs)
        # A separate PostgreSQL connection sees neither uncommitted mail nor NOTIFY.
        assert await notices(ctf, tid) == []
        assert not notified.is_set()
        return result

    try:
        await listener.add_listener(f"bbx_task_{tid.hex}", received)
        monkeypatch.setattr(ctf, "_message", observe)
        first = progress(owner, challenge)
        record = await ctf.append_record(tid, **first)
        await asyncio.wait_for(notified.wait(), timeout=2)
        monkeypatch.setattr(ctf, "_message", original)
        assert await ctf.append_record(tid, **first) == record
        await ctf.append_record(tid, **progress(owner, challenge))
        saved = await notices(ctf, tid)
        assert len(saved) == 4
        assert {row["recipient_id"] for row in saved} == {"lead", helper["actor"]}
        assert all(row["source_turn_id"] is None for row in saved)
        assert "lead" in (await ctf.runtime_state(tid))["ready_member_ids"]
        turn = await ctf.claim_turn(tid, "lead", "progress-reader")
        claimed = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
        assert sum(row["challenge_id"] == challenge["id"] for row in claimed) == 2
        # Ordinary initial inputs created before the scoped extension still deliver.
        assert any(row["challenge_id"] is None for row in claimed)

        async def fail_mail(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("Synthetic rollback after mailbox insert")

        monkeypatch.setattr(ctf, "_message", fail_mail)
        before = await ctf.list_records(tid, challenge["id"])
        with pytest.raises(RuntimeError, match="rollback"):
            await ctf.append_record(tid, **progress(owner, challenge))
        assert await ctf.list_records(tid, challenge["id"]) == before
        assert len(await notices(ctf, tid)) == 4
    finally:
        await listener.close()


@pytest.mark.parametrize("stop_first", [False, True])
async def test_stop_serializes_with_progress_and_never_allows_new_execution(
    ctf, monkeypatch, stop_first
):
    tid, owner, helper, challenge = await assigned(ctf)
    request = progress(owner, challenge)
    finish = dict(
        actor="user",
        request_id=str(uuid4()),
        conclusion={"end_reason": "user_stop", "summary": "Stop"},
    )
    if stop_first:
        await ctf.request_finish(tid, **finish)
        with pytest.raises(HTTPException):
            await ctf.append_record(tid, **request)
        assert await notices(ctf, tid) == []
    else:
        entered, release = asyncio.Event(), asyncio.Event()
        original = ctf._notify_record

        async def pause(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(ctf, "_notify_record", pause)
        writer = asyncio.create_task(ctf.append_record(tid, **request))
        await asyncio.wait_for(entered.wait(), timeout=2)
        stop = asyncio.create_task(ctf.request_finish(tid, **finish))
        try:
            await asyncio.sleep(0.05)
            assert not stop.done()
        finally:
            release.set()
        await asyncio.gather(writer, stop)
        assert len(await notices(ctf, tid)) == 2
    # The read-only queue hint can still contain pre-stop inputs; admission is authoritative.
    assert (await ctf.runtime_state(tid))["task"]["ctf_control"]["phase"] == "closing"
    assert await ctf.claim_turn(tid, "lead", "must-not-restart") is None


async def test_batch_partial_cas_and_concurrent_source_uniqueness(ctf):
    tid, owner, helper, challenge = await assigned(ctf)
    requests = [
        {"request_id": str(uuid4()), "title": "Same source", "external_id": "fixture:shared"}
        for _ in range(2)
    ]
    results = await asyncio.gather(
        *(ctf.create_challenges(tid, "user", [item]) for item in requests)
    )
    assert sorted(result["results"][0]["status"] for result in results) == ["error", "ok"]
    created = await ctf.create_challenges(
        tid,
        "user",
        [{"request_id": str(uuid4()), "title": f"Independent {index}"} for index in range(3)],
    )
    items = [
        {
            "request_id": str(uuid4()),
            "challenge_id": row["challenge"]["id"],
            "expected_revision": 99 if index == 1 else 1,
            "owner_id": owner["actor"],
            "instructions": [{"recipient_id": owner["actor"], "body": "Work this target"}],
        }
        for index, row in enumerate(created["results"])
    ]
    result = await ctf.assign_challenges(tid, "user", items)
    assert [row["status"] for row in result["results"]] == ["ok", "error", "ok"]
    before = await ctf.state(tid)
    assert await ctf.assign_challenges(tid, "user", items) == result
    assert (await ctf.state(tid))["messages"] == before["messages"]


async def test_0020_adds_nullable_scope_to_existing_mail_and_old_input_still_claims(ctf):
    tid = await team(ctf)
    migration = runpy.run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "migrations/versions/0020_ctf_message_challenge.py"
        )
    )

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration["upgrade"]()

    async with ctf.repo.engine.begin() as conn:
        # Only this disposable database is changed: recreate the pre-0020 mailbox shape.
        await conn.execute(text("ALTER TABLE ctf_messages DROP COLUMN challenge_id"))
        await conn.run_sync(upgrade)
    turn = await ctf.claim_turn(tid, "lead", "old-input-after-migration")
    inputs = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    assert inputs and all(row["challenge_id"] is None for row in inputs)
