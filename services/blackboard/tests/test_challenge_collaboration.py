"""Scheduling decisions do not consume Workers, time or claims twice."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from bbx_blackboard.domain import BoardState, RuleViolation
from bbx_blackboard.domain.challenge_collaboration import (
    assistance_slots,
    available_work,
    claim_challenge,
    claim_intent_events,
    clock_state,
    due_reminder,
    readable,
    reminder_deadline,
    require_claimable,
)
from bbx_contracts.ctf_collaboration import ChallengeCollaboration


def task():
    return {
        "status": "running",
        "ctf_control": {"native_worker_active": {}, "native_worker_ready": {}},
    }


def challenge(state, *, hard=False):
    cid = str(uuid4())
    state["ctf_control"]["native_worker_ready"][cid] = {
        "readiness": "ready",
        "priority": 1,
        "sequence": len(state["ctf_control"]["native_worker_ready"]),
    }
    return {
        "id": cid,
        "title": "Synthetic puzzle",
        "work_status": "pending",
        "revision": 1,
        "owner_id": None,
        "collaborator_ids": [],
        "collaboration": ChallengeCollaboration(
            difficulty="hard" if hard else "medium",
            assistance_target=1 if hard else 0,
        ).model_dump(mode="json"),
    }


def member(aid):
    return {"id": aid, "role": "teammate", "lifecycle": "active", "run_state": "idle"}


def take(state, item, aid, now):
    value = claim_challenge(state, item, aid, now)
    state["ctf_control"]["native_worker_active"][aid] = {"work_id": item["id"]}
    return value


def test_new_work_precedes_help_without_pulling_existing_assistants():
    state, now = task(), datetime.now(UTC)
    hard = take(state, challenge(state, hard=True), "w1", now)
    new = challenge(state)
    choices = available_work(state, [hard, new])
    assert choices["new_challenges"][0]["challenge_id"] == new["id"]
    assert choices["assistance"][0]["challenge_id"] == hard["id"]
    with pytest.raises(RuleViolation, match="优先"):
        require_claimable(state, hard, member("w2"), choices)
    require_claimable(state, new, member("w2"), choices)
    new = take(state, new, "w2", now)
    require_claimable(state, hard, member("w3"), available_work(state, [hard, new]))
    hard = take(state, hard, "w3", now)
    assert hard["owner_id"] == "w1" and hard["collaborator_ids"] == ["w3"]
    assert assistance_slots(state, hard) == 0
    assert available_work(state, [hard, new]) == {"new_challenges": [], "assistance": []}
    newer = challenge(state)
    assert available_work(state, [hard, new, newer])["new_challenges"]
    assert set(state["ctf_control"]["native_worker_active"]) == {"w1", "w2", "w3"}


def test_visibility_uses_current_members_and_actual_recruitment():
    state, now = task(), datetime.now(UTC)
    item = challenge(state, hard=True)
    assert readable(state, item, "w2")
    item = take(state, item, "w1", now)
    assert readable(state, item, "w2")
    item = take(state, item, "w2", now)
    assert readable(state, item, "w2") and not readable(state, item, "w3")
    item["work_status"] = "awaiting_review"
    assert assistance_slots(state, item) == 0 and readable(state, item, "w2")


def test_timer_excludes_review_and_pause_and_restarts_from_durable_segments():
    state, start = task(), datetime(2026, 11, 20, tzinfo=UTC)
    item = take(state, challenge(state), "w1", start)
    item["collaboration"] = clock_state(
        item, start + timedelta(minutes=10), running=False
    ).model_dump(mode="json")
    item["work_status"] = "awaiting_review"
    assert reminder_deadline(item) is None
    assert due_reminder(state, item, start + timedelta(hours=2)) is None
    item["work_status"] = "in_progress"
    item["collaboration"] = clock_state(item, start + timedelta(hours=2), running=True).model_dump(
        mode="json"
    )
    assert reminder_deadline(item) == start + timedelta(hours=2, minutes=20)
    assert due_reminder(state, item, start + timedelta(hours=2, minutes=19)) is None
    updated = due_reminder(state, item, start + timedelta(hours=2, minutes=20))
    assert updated and updated["collaboration"]["solve_elapsed_ms"] == 30 * 60 * 1000
    assert updated["collaboration"]["assistance_target"] == 1
    assert due_reminder(state, updated, start + timedelta(hours=3)) is None


def test_thirty_minutes_uses_actual_assistance_not_an_unfilled_request():
    state, start = task(), datetime.now(UTC)
    item = take(state, challenge(state, hard=True), "w1", start)
    due = start + timedelta(minutes=30)
    without = due_reminder(state, item, due)
    assert without and without["collaboration"]["assistance_target"] == 1
    item = take(state, item, "w2", start)
    with_help = due_reminder(state, item, due)
    assert with_help and with_help["collaboration"]["assistance_target"] == 1
    assert assistance_slots(state, with_help) == 0


def test_intent_claim_limit_counts_claimed_work_and_keeps_author_separate():
    cid = str(uuid4())
    intents = {
        f"I{i}": {
            "id": f"I{i}",
            "challenge_id": cid,
            "status": "open",
            "holder": None,
            "author": "w1",
            "version": i,
        }
        for i in range(1, 6)
    }
    board = BoardState(task={}, intents=intents)
    origin = {"agent_id": "w2", "assignment_id": str(uuid4())}
    for number in range(1, 4):
        events = claim_intent_events(board, f"I{number}", "w2", origin, number)
        assert events[0]["payload"]["claim_assignment_ref"] == origin
        intents[f"I{number}"].update(status="claimed", holder="w2")
    with pytest.raises(RuleViolation, match="3 条"):
        claim_intent_events(board, "I4", "w2", origin, 4)
    with pytest.raises(RuleViolation, match="认领或更新"):
        claim_intent_events(board, "I1", "w3", {"agent_id": "w3"}, 1)
    intents["I1"].update(status="closed", holder=None)
    assert claim_intent_events(board, "I4", "w2", origin, 4)
    assert intents["I4"]["author"] == "w1"
