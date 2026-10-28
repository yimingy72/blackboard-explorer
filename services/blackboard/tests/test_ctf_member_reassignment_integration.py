"""PostgreSQL member reassignment preserves closed work and persistent Lead wakeups."""

import json
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from ctf_solution_fixtures import solution
from sqlalchemy import select, update
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_ctf_integration import history, team
from test_ctf_member_reassignment import assert_claim_blocked

pytestmark = pytest.mark.integration


async def assigned_idle_owner(ctf):
    tid = await team(ctf)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks).where(s.tasks.c.id == tid).values(ctf_options={"max_teammates": 2})
        )
    lead = await ctf.claim_turn(tid, "lead", "synthetic-runtime")
    messages = await ctf.claim_messages(tid, "lead", lead["id"], lead["generation"])
    await ctf.checkpoint(
        tid,
        "lead",
        lead["id"],
        lead["generation"],
        history(messages),
        "role",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
    )
    await ctf.finish_turn(tid, "lead", lead["id"], lead["generation"], "completed")
    owner = await ctf.create_member(tid, "user", "Owner", str(uuid4()))
    helper = await ctf.create_member(tid, "user", "Busy helper", str(uuid4()))
    await ctf.post_message(tid, "user", helper["id"], "Inspect synthetic input", str(uuid4()))
    busy = await ctf.claim_turn(tid, helper["id"], "synthetic-runtime")
    challenge = await ctf.create_challenge(tid, "user", str(uuid4()), "Synthetic reassignment")
    challenge = await ctf.update_challenge(
        tid,
        "user",
        challenge["id"],
        str(uuid4()),
        challenge["revision"],
        "assign",
        owner_id=owner["id"],
    )
    await ctf.record_execution(tid, owner["id"], None, 0, "synthetic-boot")
    return tid, owner["id"], busy, challenge


async def remove(ctf, tid, owner, operation="remove"):
    request = str(uuid4())
    await ctf.request_member_operation(tid, "user", owner, operation, request)
    await assert_claim_blocked(ctf, tid, owner)
    proof = {"boot_id": "synthetic-boot", "generation": 0, "drained": True}
    await ctf.complete_member_operation(tid, owner, request, proof)
    await assert_claim_blocked(ctf, tid, owner)
    return request


async def test_idle_owner_removal_queues_exactly_one_reassignment_while_helper_runs(ctf):
    tid, owner, busy, challenge = await assigned_idle_owner(ctf)
    request = await remove(ctf, tid, owner)
    result = await ctf.get_challenge(tid, challenge["id"])
    assert result["owner_id"] is None and result["work_status"] == "blocked"
    state = await ctf.runtime_state(tid)
    assert "lead" in state["ready_member_ids"]
    assert any(turn["id"] == busy["id"] for turn in state["turns"])
    async with ctf.repo.engine.connect() as conn:
        notices = (
            (
                await conn.execute(
                    select(s.ctf_messages).where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.recipient_id == "lead",
                        s.ctf_messages.c.status == "queued",
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(notices) == 1
    assert json.loads(notices[0]["body"])["challenge_ids"] == [challenge["id"]]
    await ctf.complete_member_operation(tid, owner, request, {})
    async with ctf.repo.engine.connect() as conn:
        repeated = (
            await conn.execute(
                select(s.ctf_messages.c.id).where(
                    s.ctf_messages.c.task_id == tid,
                    s.ctf_messages.c.recipient_id == "lead",
                    s.ctf_messages.c.status == "queued",
                )
            )
        ).all()
    assert len(repeated) == 1


@pytest.mark.parametrize("closed_status", ["completed", "cancelled"])
@pytest.mark.parametrize("operation", ["stop", "remove"])
async def test_closed_work_keeps_history_until_reopened_without_unavailable_owner(
    ctf, closed_status, operation
):
    tid, owner, _, challenge = await assigned_idle_owner(ctf)
    closed = await ctf.update_challenge(
        tid,
        "user",
        challenge["id"],
        str(uuid4()),
        challenge["revision"],
        "set_status",
        work_status=closed_status,
        solution=solution() if closed_status == "completed" else None,
    )
    completion = [
        message
        for message in (await ctf.state(tid))["messages"]
        if message["notification_purpose"] == "challenge_progress"
    ]
    assert len(completion) == (2 if closed_status == "completed" else 0)
    assert all(
        json.loads(message["body"])["record_id"] == closed["solution_record_id"]
        for message in completion
    )
    completion_ids = {message["id"] for message in completion}
    await remove(ctf, tid, owner, operation)
    assert await ctf.get_challenge(tid, closed["id"]) == closed
    assert ("lead" in (await ctf.runtime_state(tid))["ready_member_ids"]) == (
        closed_status == "completed"
    )
    before = (await ctf.state(tid))["messages"]
    assert not any(message["notification_purpose"] == "challenge_available" for message in before)
    request = str(uuid4())
    reopened = await ctf.update_challenge(
        tid, "user", closed["id"], request, closed["revision"], "reopen"
    )
    assert reopened["owner_id"] is None and reopened["work_status"] == "pending"
    assert reopened["previous_owner_id"] == owner and reopened["solution_record_id"] is None
    assert "lead" in (await ctf.runtime_state(tid))["ready_member_ids"]
    messages = (await ctf.state(tid))["messages"]
    assert {
        message["id"]
        for message in messages
        if message["notification_purpose"] == "challenge_progress"
    } == completion_ids
    new = [message for message in messages if message["id"] not in {old["id"] for old in before}]
    assert len(new) == 1 and new[0]["notification_purpose"] == "challenge_available"
    assert new[0]["recipient_id"] == "lead"
    assert reopened == await ctf.update_challenge(
        tid, "user", closed["id"], request, closed["revision"], "reopen"
    )
    assert (await ctf.state(tid))["messages"] == messages


async def test_cancelled_owner_removal_does_not_block_goal_claim(ctf):
    tid, owner, _, challenge = await assigned_idle_owner(ctf)
    await ctf.update_challenge(
        tid,
        "user",
        challenge["id"],
        str(uuid4()),
        challenge["revision"],
        "set_status",
        work_status="cancelled",
    )
    await remove(ctf, tid, owner)
    await ctf.post_message(tid, "user", "lead", "Review final state", str(uuid4()))
    lead = await ctf.claim_turn(tid, "lead", "synthetic-runtime")
    result = await ctf.request_finish(
        tid,
        "lead",
        str(uuid4()),
        {"end_reason": "goal_claimed", "lead_claim": True, "summary": "All requested work closed"},
        generation=lead["generation"],
        turn_id=lead["id"],
    )
    assert result["phase"] == "closing"
