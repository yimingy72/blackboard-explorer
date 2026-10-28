"""Member removal and reopening notify Lead without reviving closed work."""

import json
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from ctf_solution_fixtures import solution
from sqlalchemy import select, update
from test_ctf_assignment_feedback import change_request, mailbox
from test_ctf_assignment_feedback import feedback as feedback_fixture
from test_ctf_communication_storage import storage as storage_fixture

feedback = feedback_fixture
storage = storage_fixture


async def assert_claim_blocked(service, tid, owner):
    before = next(
        member for member in (await service.runtime_state(tid))["members"] if member["id"] == owner
    )
    assert await service.claim_turn(tid, owner, "must-not-revive-stopped-owner") is None
    after = next(
        member for member in (await service.runtime_state(tid))["members"] if member["id"] == owner
    )
    assert (after["generation"], after["current_turn_id"]) == (
        before["generation"],
        before["current_turn_id"],
    )


async def idle_owner(feedback):
    await feedback.service.finish_turn(
        feedback.tid, "alice", feedback.turns["alice"], 1, "completed"
    )
    await feedback.service.finish_turn(feedback.tid, "lead", feedback.current_id, 2, "completed")
    with feedback.database.begin() as conn:
        conn.execute(
            update(s.ctf_members).where(s.ctf_members.c.id == "alice").values(operation_history=[])
        )
    await feedback.service.record_execution(feedback.tid, "alice", None, 1, "synthetic-boot")


async def remove_owner(feedback, operation="remove"):
    request = str(uuid4())
    await feedback.service.request_member_operation(
        feedback.tid, "user", "alice", operation, request
    )
    await assert_claim_blocked(feedback.service, feedback.tid, "alice")
    result = await feedback.service.complete_member_operation(
        feedback.tid,
        "alice",
        request,
        {"boot_id": "synthetic-boot", "generation": 1, "drained": True},
    )
    await assert_claim_blocked(feedback.service, feedback.tid, "alice")
    return request, result


@pytest.mark.parametrize("availability", ["active", "stopping", "stopped", "removed", "missing"])
async def test_reopen_keeps_only_available_owner_and_preserves_historical_delivery(
    feedback, availability
):
    assigned = await feedback.service.update_challenge(
        feedback.tid, "user", **change_request(feedback)
    )
    closed = await feedback.service.update_challenge(
        feedback.tid,
        "user",
        **change_request(
            feedback,
            action="set_status",
            owner_id=None,
            work_status="completed",
            expected_revision=assigned["revision"],
            solution=solution(),
        ),
    )
    completion = mailbox(feedback)
    assert len(completion) == 2
    assert {message["recipient_id"] for message in completion} == {"lead", "alice"}
    assert all(
        message["notification_purpose"] == "challenge_progress"
        and json.loads(message["body"])["record_id"] == closed["solution_record_id"]
        for message in completion
    )
    completion_ids = {message["id"] for message in completion}
    await idle_owner(feedback)
    if availability in {"stopped", "removed"}:
        await remove_owner(feedback, "stop" if availability == "stopped" else "remove")
        historical = await feedback.service.get_challenge(feedback.tid, closed["id"])
        assert historical["owner_id"] == "alice" and historical["work_status"] == "completed"
        assert historical["solution_record_id"] == closed["solution_record_id"]
    elif availability == "stopping":
        await feedback.service.request_member_operation(
            feedback.tid, "user", "alice", "stop", str(uuid4())
        )
        await assert_claim_blocked(feedback.service, feedback.tid, "alice")
    elif availability == "missing":
        with feedback.database.begin() as conn:
            conn.execute(s.ctf_members.delete().where(s.ctf_members.c.id == "alice"))
    request = change_request(
        feedback, action="reopen", owner_id=None, expected_revision=closed["revision"]
    )
    reopened = await feedback.service.update_challenge(feedback.tid, "user", **request)
    assert reopened["solution_record_id"] is None
    messages = mailbox(feedback)
    notifications = [message for message in messages if message["id"] not in completion_ids]
    assert {
        message["id"]
        for message in messages
        if message["notification_purpose"] == "challenge_progress"
    } == completion_ids
    if availability == "active":
        assert reopened["owner_id"] == "alice" and reopened["work_status"] == "in_progress"
        assert notifications == []
    else:
        assert reopened["owner_id"] is None and reopened["work_status"] == "pending"
        assert reopened["previous_owner_id"] == "alice"
        # The queue hint retains pre-stop input, but admission rejects a stopping owner.
        expected_ready = {"lead", "alice"} if availability == "stopping" else {"lead"}
        assert (
            set((await feedback.service.runtime_state(feedback.tid))["ready_member_ids"])
            == expected_ready
        )
        assert len(notifications) == 1
        assert notifications[0]["notification_purpose"] == "challenge_available"
        assert notifications[0]["recipient_id"] == "lead"
        assert json.loads(notifications[0]["body"])["challenge_id"] == closed["id"]
    assert reopened == await feedback.service.update_challenge(feedback.tid, "user", **request)
    assert mailbox(feedback) == messages


async def test_removing_idle_owner_wakes_lead_while_another_member_is_running(feedback):
    owned = await feedback.service.update_challenge(
        feedback.tid, "user", **change_request(feedback)
    )
    await idle_owner(feedback)
    request, removed = await remove_owner(feedback)
    assert removed["lifecycle"] == "removed"
    challenge = await feedback.service.get_challenge(feedback.tid, owned["id"])
    assert challenge["owner_id"] is None and challenge["work_status"] == "blocked"
    assert challenge["previous_owner_id"] == "alice"
    state = await feedback.service.runtime_state(feedback.tid)
    assert any(turn["agent_id"] == "bob" and turn["status"] == "running" for turn in state["turns"])
    assert state["ready_member_ids"] == ["lead"]
    notification = mailbox(feedback)[0]
    assert json.loads(notification["body"]) == {
        "reason": "owner_removed",
        "previous_owner_id": "alice",
        "challenge_ids": [owned["id"]],
        "challenge_count": 1,
        "truncated": False,
    }
    repeated = await feedback.service.complete_member_operation(feedback.tid, "alice", request, {})
    assert repeated["generation"] == removed["generation"]
    assert repeated["operation_history"] == removed["operation_history"]
    assert len(mailbox(feedback)) == 1


async def test_cancelled_challenge_stays_closed_after_owner_removal_and_allows_goal_claim(feedback):
    assigned = await feedback.service.update_challenge(
        feedback.tid, "user", **change_request(feedback)
    )
    cancelled = await feedback.service.update_challenge(
        feedback.tid,
        "user",
        **change_request(
            feedback,
            action="set_status",
            owner_id=None,
            work_status="cancelled",
            expected_revision=assigned["revision"],
        ),
    )
    await idle_owner(feedback)
    await remove_owner(feedback)
    assert await feedback.service.get_challenge(feedback.tid, cancelled["id"]) == cancelled
    assert mailbox(feedback) == []
    with feedback.database.begin() as conn:
        conn.execute(update(s.tasks).values(ctf_control={"phase": "running", "epoch": 1}))
    await feedback.service.post_message(
        feedback.tid, "user", "lead", "Review final state", str(uuid4())
    )
    lead = await feedback.service.claim_turn(feedback.tid, "lead", "synthetic-runtime")
    result = await feedback.service.request_finish(
        feedback.tid,
        "lead",
        str(uuid4()),
        {"end_reason": "goal_claimed", "lead_claim": True, "summary": "All requested work closed"},
        generation=lead["generation"],
        turn_id=lead["id"],
    )
    assert result["phase"] == "closing"
    with feedback.database.connect() as conn:
        assert (
            conn.execute(select(s.ctf_challenges.c.data)).scalar_one()["work_status"] == "cancelled"
        )
