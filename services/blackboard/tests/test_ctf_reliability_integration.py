"""Transactional control recovery and bounded idle coordination, using fake work."""

import asyncio
import json
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import update
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_integration import history, team

ctf = ctf_fixture
ctf_url = ctf_url_fixture

pytestmark = pytest.mark.integration


async def set_message(ctf, tid, identifier, **values):
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages)
            .where(s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == identifier)
            .values(**values)
        )


async def idle_board(ctf, *, completed=False):
    tid = await team(ctf)
    challenge = await ctf.create_challenge(tid, "user", str(uuid4()), "Fake pending work")
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages).where(s.ctf_messages.c.task_id == tid).values(status="delivered")
        )
        if completed:
            await conn.execute(
                update(s.ctf_challenges)
                .where(s.ctf_challenges.c.task_id == tid)
                .values(data={**challenge, "work_status": "completed"})
            )
    return tid, challenge


async def test_same_instance_claim_is_idempotent_and_other_instance_is_fenced(ctf):
    tid = await team(ctf)
    claims = await asyncio.gather(*(ctf.claim_turn(tid, "lead", "owner") for _ in range(2)))
    assert claims[0]["id"] == claims[1]["id"]
    assert claims[0]["generation"] == claims[1]["generation"]
    assert await ctf.claim_turn(tid, "lead", "other-owner") is None
    state = await ctf.state(tid)
    assert len(state["turns"]) == 1
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Stop"}
    )
    assert await ctf.claim_turn(tid, "lead", "owner") is None


@pytest.mark.parametrize("repeat_status", ["queued", "leased", "delivered"])
async def test_confirm_releases_only_deferred_and_is_safe_to_repeat(ctf, repeat_status):
    tid = await team(ctf)
    lead = await ctf.claim_turn(tid, "lead", "confirm")
    recipient = (await ctf.create_member(tid, "user", "Owner", str(uuid4())))["id"]
    identifier = str(uuid4())
    await ctf.post_message(tid, "user", recipient, "Private work body", identifier, "instruction")
    await set_message(ctf, tid, identifier, deferred=True)
    args = dict(
        actor="lead", message_ids=[identifier], generation=lead["generation"], turn_id=lead["id"]
    )
    assert (await ctf.confirm_messages(tid, **args))["message_ids"] == [identifier]
    await set_message(ctf, tid, identifier, status=repeat_status)
    assert (await ctf.confirm_messages(tid, **args))["message_ids"] == [identifier]
    message = next(m for m in (await ctf.state(tid))["messages"] if m["id"] == identifier)
    assert message["status"] == repeat_status and message["deferred"] is False


@pytest.mark.parametrize(
    "status,lifecycle",
    [("cancelled", "active"), ("failed", "active"), ("queued", "stopped"), ("queued", "removed")],
)
async def test_confirm_conflict_is_atomic_and_does_not_restore_forbidden_messages(
    ctf, status, lifecycle
):
    tid = await team(ctf)
    lead = await ctf.claim_turn(tid, "lead", "confirm")
    recipient = (await ctf.create_member(tid, "user", "Owner", str(uuid4())))["id"]
    good, bad = str(uuid4()), str(uuid4())
    for identifier, target in ((good, "lead"), (bad, recipient)):
        await ctf.post_message(
            tid, "user", target, "Private body must not appear", identifier, "instruction"
        )
        await set_message(ctf, tid, identifier, deferred=True)
    await set_message(ctf, tid, bad, status=status)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == recipient)
            .values(lifecycle=lifecycle)
        )
    with pytest.raises(HTTPException) as caught:
        await ctf.confirm_messages(
            tid,
            actor="lead",
            message_ids=[good, bad],
            generation=lead["generation"],
            turn_id=lead["id"],
        )
    detail = cast(dict[str, Any], caught.value.detail)
    assert detail["code"] == "message_confirmation_conflict"
    assert detail["conflicts"] == [{"id": bad, "status": status, "deferred": True}]
    assert "Private" not in json.dumps(detail)
    state = await ctf.state(tid)
    assert all(m["deferred"] for m in state["messages"] if m["id"] in {good, bad})


async def test_confirmation_stale_authority_is_not_a_recoverable_message_conflict(ctf):
    tid = await team(ctf)
    lead = await ctf.claim_turn(tid, "lead", "confirm")
    with pytest.raises(HTTPException) as caught:
        await ctf.confirm_messages(
            tid,
            actor="lead",
            message_ids=[str(uuid4())],
            generation=lead["generation"] + 1,
            turn_id=lead["id"],
        )
    assert not isinstance(caught.value.detail, dict)


async def test_watchdog_cooldown_persists_and_closes_after_three_unproductive_reminders(ctf):
    tid, _ = await idle_board(ctf)
    instant = (await ctf.state(tid))["task"]["created_at"]
    assert (await ctf.idle_watchdog(tid, instant=instant))["action"] == "waiting"
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=59)))[
        "action"
    ] == "waiting"
    for attempt in range(1, 4):
        instant += timedelta(seconds=60)
        result = await ctf.idle_watchdog(tid, instant=instant)
        assert result["action"] == "nudged" and result["attempt"] == attempt
        assert (await ctf.idle_watchdog(tid, instant=instant))["action"] == "blocked"
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.ctf_messages)
                .where(s.ctf_messages.c.task_id == tid)
                .values(status="delivered")
            )
        assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=1)))[
            "action"
        ] == "waiting"
    result = await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=60))
    assert result["action"] == "stalled"
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["phase"] == "closing"
    assert state["task"]["ctf_conclusion"]["end_reason"] == "partial"
    assert "stalled" in state["task"]["ctf_conclusion"]["summary"]
    assert len([m for m in state["messages"] if m["sender_kind"] == "system"]) == 3


@pytest.mark.parametrize("blocker", ["turn", "lead_stopped", "checkpoint", "settlement"])
async def test_watchdog_never_bypasses_safety_barriers(ctf, blocker):
    tid, _ = await idle_board(ctf)
    instant = (await ctf.state(tid))["task"]["created_at"]
    await ctf.idle_watchdog(tid, instant=instant)
    async with ctf.repo.engine.begin() as conn:
        if blocker == "deferred":
            await conn.execute(
                update(s.ctf_messages)
                .where(s.ctf_messages.c.task_id == tid)
                .values(status="queued", deferred=True)
            )
        elif blocker == "turn":
            await conn.execute(
                update(s.ctf_members)
                .where(s.ctf_members.c.task_id == tid)
                .values(current_turn_id="unresolved-turn")
            )
        elif blocker == "lead_stopped":
            await conn.execute(
                update(s.ctf_members)
                .where(s.ctf_members.c.task_id == tid)
                .values(lifecycle="stopped")
            )
        else:
            task = (await ctf.state(tid))["task"]
            key = {
                "checkpoint": "unresolved_checkpoints",
                "settlement": "budget_reservations",
                "context": "context_blocked_members",
            }[blocker]
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(ctf_control={**task["ctf_control"], key: {"blocked": {}}})
            )
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=120)))[
        "action"
    ] == "blocked"


async def test_completed_board_gets_one_summary_reminder_and_empty_board_gets_none(ctf):
    empty = await team(ctf)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages)
            .where(s.ctf_messages.c.task_id == empty)
            .values(status="delivered")
        )
    assert (await ctf.idle_watchdog(empty))["action"] == "blocked"
    tid, _ = await idle_board(ctf, completed=True)
    instant = (await ctf.state(tid))["task"]["created_at"]
    await ctf.idle_watchdog(tid, instant=instant)
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=60)))[
        "action"
    ] == "nudged"
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages).where(s.ctf_messages.c.task_id == tid).values(status="delivered")
        )
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=120)))[
        "action"
    ] == "stalled"


async def test_context_blocked_turn_retries_once_then_waits_for_explicit_user_input(ctf):
    tid = await team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "runtime")
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    original = next(m for m in messages if m["sender_kind"] == "user")
    session: dict[str, Any] = history(messages)
    session["state"]["ctf_context_blocked"] = {
        "turn_id": turn["id"],
        "fingerprint": "confirmed-fingerprint",
        "message_ids": [m["id"] for m in messages],
        "limit_chars": 100,
    }
    await ctf.checkpoint(tid, "lead", turn["id"], turn["generation"], session, "Prompt", 0, [])
    await ctf.finish_turn(
        tid,
        "lead",
        turn["id"],
        turn["generation"],
        "context_blocked",
        "Context needs a smaller input",
    )
    state = await ctf.state(tid)
    assert (
        state["task"]["ctf_control"]["context_blocked_members"]["lead"]["fingerprint"]
        == "confirmed-fingerprint"
    )
    assert all(m["status"] == "queued" for m in state["messages"])
    assert "lead" in (await ctf.runtime_state(tid))["ready_member_ids"]
    retry = await ctf.claim_turn(tid, "lead", "runtime")
    messages = await ctf.claim_messages(tid, "lead", retry["id"], retry["generation"])
    session = history(messages)
    session["state"]["ctf_context_blocked"] = {
        "turn_id": retry["id"],
        "fingerprint": "still-blocked",
        "message_ids": [m["id"] for m in messages],
        "limit_chars": 100,
    }
    await ctf.checkpoint(tid, "lead", retry["id"], retry["generation"], session, "Prompt", 1, [])
    await ctf.finish_turn(tid, "lead", retry["id"], retry["generation"], "context_blocked")
    state = await ctf.state(tid)
    assert len([m for m in state["messages"] if m["notification_purpose"] == "turn_recovery"]) == 1
    assert {m["id"] for m in messages} <= {m["id"] for m in state["messages"]}
    assert "lead" not in (await ctf.runtime_state(tid))["ready_member_ids"]
    assert await ctf.claim_turn(tid, "lead", "runtime") is None
    repeated = await ctf.post_message(
        tid, "user", "lead", original["body"], original["id"], original["kind"]
    )
    assert repeated["dispatch_status"] == "deferred"
    assert "lead" not in (await ctf.runtime_state(tid))["ready_member_ids"]
    await ctf.post_message(tid, "system", "lead", "A status notification", str(uuid4()))
    assert "lead" not in (await ctf.runtime_state(tid))["ready_member_ids"]
    result = await ctf.post_message(
        tid, "user", "lead", "Use smaller inputs and continue", str(uuid4())
    )
    assert result["dispatch_status"] == "context_recovery_requested"
    assert "lead" in (await ctf.runtime_state(tid))["ready_member_ids"]
    assert await ctf.claim_turn(tid, "lead", "runtime") is not None
    assert not (await ctf.state(tid))["task"]["ctf_control"]["context_blocked_members"]


async def test_context_recovery_claim_handles_legacy_null_ids_and_keeps_old_inputs_blocked(ctf):
    tid = await team(ctf)
    member = await ctf.create_member(tid, "user", "Blocked member", str(uuid4()))
    old = await ctf.post_message(
        tid, "user", member["id"], "Original work", str(uuid4()), "instruction"
    )
    turn = await ctf.claim_turn(tid, member["id"], "runtime")
    messages = await ctf.claim_messages(tid, member["id"], turn["id"], turn["generation"])
    session: dict[str, Any] = history(messages)
    session["state"]["ctf_context_blocked"] = {
        "turn_id": turn["id"],
        "message_ids": [old["id"], None],
    }
    await ctf.checkpoint(
        tid, member["id"], turn["id"], turn["generation"], session, "Prompt", 0, []
    )
    await ctf.finish_turn(tid, member["id"], turn["id"], turn["generation"], "context_blocked")
    state = await ctf.state(tid)
    control = state["task"]["ctf_control"]
    entry = control["context_blocked_members"][member["id"]]
    assert None not in entry["input_message_ids"]
    recovery = next(m for m in state["messages"] if m["notification_purpose"] == "turn_recovery")
    # Restore the old deployed shape: recovery must not depend on rewriting saved Sessions.
    entry["input_message_ids"].append(None)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(ctf_control=control))
    assert member["id"] in (await ctf.runtime_state(tid))["ready_member_ids"]
    claimed = await ctf.claim_turn(tid, member["id"], "runtime")
    assert claimed["assignment_message_ids"] == [recovery["id"]]
    assert claimed["generation"] == turn["generation"] + 1
    claimed_messages = await ctf.claim_messages(
        tid, member["id"], claimed["id"], claimed["generation"]
    )
    assert [message["id"] for message in claimed_messages] == [recovery["id"]]
    assert (await ctf.state(tid))["task"]["ctf_control"]["turn_retries"][member["id"]] == {
        "context_blocked": 1
    }
    session = history(claimed_messages)
    session["state"]["ctf_context_blocked"] = {
        "turn_id": claimed["id"],
        "message_ids": [recovery["id"]],
    }
    await ctf.checkpoint(
        tid, member["id"], claimed["id"], claimed["generation"], session, "Prompt", 1, []
    )
    await ctf.finish_turn(
        tid, member["id"], claimed["id"], claimed["generation"], "context_blocked"
    )
    assert member["id"] not in (await ctf.runtime_state(tid))["ready_member_ids"]
    lead = await ctf.claim_turn(tid, "lead", "runtime")
    fresh = await ctf.post_message(
        tid,
        "lead",
        member["id"],
        "Continue using saved records",
        str(uuid4()),
        "instruction",
        generation=lead["generation"],
        turn_id=lead["id"],
    )
    assert fresh["dispatch_status"] == "context_recovery_requested"
    explicit = await ctf.claim_turn(tid, member["id"], "runtime")
    delivered = await ctf.claim_messages(tid, member["id"], explicit["id"], explicit["generation"])
    assert [message["id"] for message in delivered] == [fresh["id"]]
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["turn_retries"][member["id"]] == {"context_blocked": 1}
    assert (
        next(message for message in state["messages"] if message["id"] == old["id"])["deferred"]
        is True
    )


async def test_step_limit_retry_is_atomic_idempotent_and_bounded(ctf):
    tid = await team(ctf)
    session, revision = history([]), 0
    for attempt in range(4):
        turn = await ctf.claim_turn(tid, "lead", "runtime")
        assert turn is not None
        messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
        session["state"]["in_memory"]["messages"].extend(
            history(messages)["state"]["in_memory"]["messages"]
        )
        saved = await ctf.checkpoint(
            tid,
            "lead",
            turn["id"],
            turn["generation"],
            session,
            "Prompt",
            revision,
            [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
        )
        revision = saved["revision"]
        await asyncio.gather(
            *(
                ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "step_limit")
                for _ in range(2)
            )
        )
        state = await ctf.state(tid)
        retries = [m for m in state["messages"] if m["notification_purpose"] == "turn_recovery"]
        assert len(retries) == min(attempt + 1, 3)
    assert await ctf.claim_turn(tid, "lead", "runtime") is None
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["turn_retries"] == {"lead": {"step_limit": 3}}
    assert state["challenges"] == [] and state["records"] == []


async def test_member_recovery_preserves_assignment_and_candidate_for_lead_review(ctf):
    tid = await team(ctf)
    member = await ctf.create_member(tid, "user", "Assigned member", str(uuid4()))
    challenge = await ctf.create_challenge(tid, "user", str(uuid4()), "Assigned fake work")
    await ctf.update_challenge(
        tid, "user", challenge["id"], str(uuid4()), 1, "assign", owner_id=member["id"]
    )
    await ctf.post_message(
        tid, "user", member["id"], "Continue assigned work", str(uuid4()), "instruction"
    )
    turn = await ctf.claim_turn(tid, member["id"], "runtime")
    identity = {"actor": member["id"], "generation": turn["generation"], "turn_id": turn["id"]}
    await ctf.record_candidate(
        tid,
        **identity,
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=2,
        status="candidate",
        summary="Unverified fake result",
    )
    before = await ctf.state(tid)
    await ctf.finish_turn(tid, member["id"], turn["id"], turn["generation"], "step_limit")
    state = await ctf.state(tid)
    assert state["challenges"] == before["challenges"]
    assert state["challenges"][0]["verification"]["status"] == "candidate"
    assert state["challenges"][0]["owner_id"] == member["id"]
    recovery = next(m for m in state["messages"] if m["notification_purpose"] == "turn_recovery")
    assert recovery["recipient_id"] == member["id"]
    notification = next(
        m for m in state["messages"] if m["notification_purpose"] == "assignment_result"
    )
    assert notification["recipient_id"] == "lead"
    assert next(m for m in state["members"] if m["id"] == "lead")["role"] == "lead"


@pytest.mark.parametrize("barrier", ["closing", "budget", "removed"])
async def test_limited_turn_retry_never_bypasses_admission_barriers(ctf, barrier):
    tid = await team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "runtime")
    if barrier == "closing":
        await ctf.request_finish(
            tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Stop"}
        )
    else:
        async with ctf.repo.engine.begin() as conn:
            table = s.tasks if barrier == "budget" else s.ctf_members
            values = {"usage": {"cost": "10"}} if barrier == "budget" else {"lifecycle": "removed"}
            await conn.execute(
                update(table)
                .where(table.c.id == (tid if barrier == "budget" else "lead"))
                .where(table.c.id == tid if barrier == "budget" else table.c.task_id == tid)
                .values(**values)
            )
    if barrier == "removed":
        with pytest.raises(HTTPException, match="Inactive execution member"):
            await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "step_limit")
    else:
        await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "step_limit")
    state = await ctf.state(tid)
    assert not [m for m in state["messages"] if m["notification_purpose"] == "turn_recovery"]


async def test_watchdog_semantic_revision_progress_resets_reminder_count(ctf):
    tid, challenge = await idle_board(ctf)
    instant = (await ctf.state(tid))["task"]["created_at"]
    await ctf.idle_watchdog(tid, instant=instant)
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=60)))["attempt"] == 1
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages).where(s.ctf_messages.c.task_id == tid).values(status="delivered")
        )
        await conn.execute(
            update(s.ctf_challenges)
            .where(s.ctf_challenges.c.task_id == tid)
            .values(revision=challenge["revision"] + 1)
        )
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=120)))[
        "action"
    ] == "waiting"
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=180)))["attempt"] == 1


async def test_resume_notice_has_deferred_ids_without_private_bodies_and_clears_blocks(ctf):
    class Objects:
        async def exists(self, _):
            return True

    ctf.objects = Objects()
    tid = await team(ctf)
    identifier = str(uuid4())
    await ctf.post_message(tid, "user", "lead", "private-resume-sentinel", identifier)
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Stop"}
    )
    await ctf.finalize_close(tid, True)
    state = await ctf.state(tid)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                workspace_uri=f"workspace/{tid}.tar.zst",
                cleanup_ready=True,
                ctf_control={
                    **state["task"]["ctf_control"],
                    "context_blocked_members": {"lead": {}},
                    "idle_watchdog": {"nudges": 3},
                    "turn_retries": {
                        "lead": {"context_blocked": 1, "step_limit": 3, "provider_transient": 3}
                    },
                    "turn_retry_delays": {
                        "lead": {
                            "message_id": identifier,
                            "ready_at": state["task"]["created_at"].isoformat(),
                        }
                    },
                },
            )
        )
    await ctf.resume(tid, uuid4(), "0", 0)
    state = await ctf.state(tid)
    notice = next(m for m in state["messages"] if m["sender_kind"] == "system")
    assert identifier in notice["body"] and "private-resume-sentinel" not in notice["body"]
    assert "context_blocked_members" not in state["task"]["ctf_control"]
    assert "idle_watchdog" not in state["task"]["ctf_control"]
    assert "turn_retries" not in state["task"]["ctf_control"]
    assert "turn_retry_delays" not in state["task"]["ctf_control"]


@pytest.mark.parametrize("waiting_work", ["deferred", "context_blocked", "stopped", "unknown_cost"])
async def test_idle_lead_can_coordinate_preserved_work_without_releasing_it(ctf, waiting_work):
    tid, _ = await idle_board(ctf)
    member = await ctf.create_member(tid, "user", "Waiting owner", str(uuid4()))
    identifier = str(uuid4())
    await ctf.post_message(
        tid, "user", member["id"], "private-waiting-sentinel", identifier, "instruction"
    )
    state = await ctf.state(tid)
    async with ctf.repo.engine.begin() as conn:
        control = state["task"]["ctf_control"]
        if waiting_work == "deferred":
            await conn.execute(
                update(s.ctf_messages)
                .where(s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == identifier)
                .values(deferred=True)
            )
        elif waiting_work == "context_blocked":
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={
                        **control,
                        "context_blocked_members": {
                            member["id"]: {"input_message_ids": [identifier]}
                        },
                    }
                )
            )
        elif waiting_work == "stopped":
            await conn.execute(
                update(s.ctf_members)
                .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == member["id"])
                .values(lifecycle="stopped")
            )
        else:
            await conn.execute(
                update(s.ctf_messages)
                .where(s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == identifier)
                .values(status="delivered")
            )
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={
                        **control,
                        "budget_unknown_reservations": {"unknown-call": {"cost": "1"}},
                    }
                )
            )
    instant = state["task"]["created_at"]
    assert not (await ctf.runtime_state(tid))["ready_member_ids"]
    assert (await ctf.idle_watchdog(tid, instant=instant))["action"] == "waiting"
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=60)))[
        "action"
    ] == "nudged"
    state = await ctf.state(tid)
    notice = next(m for m in state["messages"] if m["sender_kind"] == "system")
    assert notice["recipient_id"] == "lead" and "private-waiting-sentinel" not in notice["body"]
    original = next(m for m in state["messages"] if m["id"] == identifier)
    assert original["deferred"] is (waiting_work == "deferred")
    assert next(m for m in state["members"] if m["id"] == member["id"])["lifecycle"] == (
        "stopped" if waiting_work == "stopped" else "active"
    )
    assert (await ctf.runtime_state(tid))["ready_member_ids"] == ["lead"]


async def test_legacy_context_blocked_lead_gets_one_recovery_before_partial(ctf):
    tid, _ = await idle_board(ctf)
    state = await ctf.state(tid)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **state["task"]["ctf_control"],
                    "context_blocked_members": {"lead": {"input_message_ids": []}},
                }
            )
        )
    instant = state["task"]["created_at"]
    await ctf.idle_watchdog(tid, instant=instant)
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=60)))[
        "action"
    ] == "nudged"
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages).where(s.ctf_messages.c.task_id == tid).values(status="delivered")
        )
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=120)))[
        "action"
    ] == "stalled"
    state = await ctf.state(tid)
    assert state["task"]["ctf_conclusion"]["end_reason"] == "partial"
    assert (
        "Lead context remains blocked after bounded recovery"
        in state["task"]["ctf_conclusion"]["summary"]
    )
    assert len([m for m in state["messages"] if m["sender_kind"] == "system"]) == 1


async def test_unchanged_idle_monitor_does_not_write_again(ctf, monkeypatch):
    from unittest.mock import AsyncMock

    from bbx_blackboard import ctf as module

    tid, _ = await idle_board(ctf)
    instant = (await ctf.state(tid))["task"]["created_at"]
    await ctf.idle_watchdog(tid, instant=instant)
    patched = AsyncMock(wraps=module.patch)
    monkeypatch.setattr(module, "patch", patched)
    for seconds in (1, 5, 10, 59):
        assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=seconds)))[
            "action"
        ] == "waiting"
    assert patched.await_count == 0


async def test_same_run_recovery_keeps_reminder_count_but_explicit_resume_resets_it(ctf):
    class Objects:
        async def exists(self, _):
            return True

    tid, _ = await idle_board(ctf)
    instant = (await ctf.state(tid))["task"]["created_at"]
    await ctf.idle_watchdog(tid, instant=instant)
    for attempt in range(1, 4):
        if attempt == 2:
            ctf = CtfService(ctf.repo.engine)
            await ctf.recover(tid)
            assert (await ctf.state(tid))["task"]["ctf_control"]["idle_watchdog"]["nudges"] == 1
        assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=60 * attempt)))[
            "attempt"
        ] == attempt
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.ctf_messages)
                .where(s.ctf_messages.c.task_id == tid)
                .values(status="delivered")
            )
    await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=240))
    await ctf.finalize_close(tid, True)
    stopped = (await ctf.state(tid))["task"]["ctf_conclusion"]
    state = await ctf.state(tid)
    ctf.objects = Objects()
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(workspace_uri=f"workspace/{tid}.tar.zst", cleanup_ready=True)
        )
    await ctf.resume(tid, uuid4(), "0", 0)
    await ctf.start(tid)
    state = await ctf.state(tid)
    assert "idle_watchdog" not in state["task"]["ctf_control"]
    assert state["task"]["ctf_control"]["conclusion_history"][-1]["conclusion"] == stopped
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages).where(s.ctf_messages.c.task_id == tid).values(status="delivered")
        )
    instant = state["task"]["active_since"]
    await ctf.idle_watchdog(tid, instant=instant)
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=60)))["attempt"] == 1
