"""Offline SQL coverage for atomic dispatch, advisory wakeups, and recovery decisions."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from ctf_solution_fixtures import solution
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import JSON, Column, Integer, MetaData, Table, Text, insert, select, update
from test_ctf_communication_storage import storage as storage_fixture

storage = storage_fixture


@pytest.fixture
async def feedback(storage, monkeypatch):
    challenge_table = Table(
        "ctf_challenges",
        MetaData(),
        Column("task_id", s.tasks.c.id.type, primary_key=True),
        Column("id", Text, primary_key=True),
        Column("data", JSON),
        Column("revision", Integer),
    )
    challenge_table.create(storage.database)
    monkeypatch.setattr(s, "ctf_challenges", challenge_table)
    original_append = storage.service.repo.append

    async def append(conn, tid, entries):
        result = await original_append(conn, tid, entries)
        for event in result:
            event.setdefault("created_at", datetime.now(UTC))
        for entry in entries:
            if entry["type"] in {"ctf.challenge.created", "ctf.challenge.updated"}:
                challenge = entry["payload"]["result"]
                values = {
                    "task_id": tid,
                    "id": challenge["id"],
                    "data": challenge,
                    "revision": challenge["revision"],
                }
                query = (
                    insert(challenge_table).values(**values)
                    if entry["type"] == "ctf.challenge.created"
                    else update(challenge_table)
                    .where(
                        challenge_table.c.task_id == tid, challenge_table.c.id == challenge["id"]
                    )
                    .values(**values)
                )
                await conn.execute(query)
        return result

    async def emit(conn, tid, kind, actor, payload, addressed_to=None):
        return await append(
            conn,
            tid,
            [
                {
                    "type": f"ctf.{kind}",
                    "actor": actor,
                    "object_id": payload.get("id"),
                    "payload": jsonable_encoder(payload),
                    "addressed_to": addressed_to,
                }
            ],
        )

    monkeypatch.setattr(storage.service.repo, "append", append)
    monkeypatch.setattr(storage.service, "_emit", emit)
    storage.turns = {}
    with storage.database.begin() as conn:
        conn.execute(
            update(s.tasks).values(
                budget={"max_cost": "10", "max_minutes": 60},
                usage={},
                active_seconds=0,
                run_number=1,
            )
        )
        for aid in ("alice", "bob"):
            turn_id = str(uuid4())
            storage.turns[aid] = turn_id
            conn.execute(
                insert(s.ctf_members).values(
                    task_id=storage.tid,
                    id=aid,
                    role="teammate",
                    lifecycle="active",
                    run_state="running",
                    generation=1,
                    current_turn_id=turn_id,
                    usage={},
                )
            )
            conn.execute(
                insert(s.ctf_turns).values(
                    task_id=storage.tid,
                    id=turn_id,
                    agent_id=aid,
                    generation=1,
                    purpose="execution",
                    status="running",
                    assignment_message_ids=[],
                    explicit_reply_ids=[],
                    checkpoint_revision=0,
                    usage={},
                    usage_requests=[],
                )
            )
    storage.challenge = await storage.service.create_challenge(
        storage.tid, "user", str(uuid4()), "Synthetic challenge"
    )
    return storage


def change_request(feedback, **changes):
    return {
        "challenge_id": feedback.challenge["id"],
        "request_id": str(uuid4()),
        "expected_revision": feedback.challenge["revision"],
        "action": "assign",
        "owner_id": "alice",
        **changes,
    }


def mailbox(feedback):
    with feedback.database.connect() as conn:
        return [dict(item) for item in conn.execute(select(s.ctf_messages)).mappings()]


def events(feedback):
    with feedback.database.connect() as conn:
        return [dict(item) for item in conn.execute(select(feedback.tables["events"])).mappings()]


@pytest.mark.parametrize("actor", ["user", "lead"])
async def test_dispatch_writes_permission_before_instructions_and_returns_only_challenge(
    feedback, monkeypatch, actor
):
    service = feedback.service
    original = service._message

    async def observe(conn, tid, actor, recipient_id, body, message_id, kind="message", **kwargs):
        saved = await service._challenge(conn, tid, feedback.challenge["id"])
        assert saved["owner_id"] == recipient_id == "alice"
        assert saved["revision"] == 2
        return await original(conn, tid, actor, recipient_id, body, message_id, kind, **kwargs)

    monkeypatch.setattr(service, "_message", observe)
    request = change_request(
        feedback, instructions=[{"recipient_id": "alice", "body": "Investigate synthetic input"}]
    )
    identity = {"generation": 2, "turn_id": feedback.current_id} if actor == "lead" else {}
    result = await service.update_challenge(feedback.tid, actor, **request, **identity)
    assert result["owner_id"] == "alice"
    assert "instructions" not in result
    assert "dispatch" not in result
    queued = mailbox(feedback)
    assert len(queued) == 1
    assert queued[0]["recipient_id"] == "alice" and queued[0]["kind"] == "instruction"
    assert queued[0]["body"] == "Investigate synthetic input"
    assert result == await service.update_challenge(feedback.tid, actor, **request, **identity)
    assert len(mailbox(feedback)) == 1
    with pytest.raises(HTTPException, match="Request ID conflict"):
        await service.update_challenge(
            feedback.tid,
            actor,
            **{**request, "instructions": [{"recipient_id": "alice", "body": "Changed"}]},
            **identity,
        )


async def test_collaborator_dispatch_is_order_independent_and_checks_updated_membership(feedback):
    request = change_request(
        feedback,
        action="collaborators",
        owner_id=None,
        collaborator_ids=["alice", "bob"],
        instructions=[
            {"recipient_id": "bob", "body": "Inspect synthetic B"},
            {"recipient_id": "alice", "body": "Inspect synthetic A"},
        ],
    )
    service = feedback.service
    result = await service.update_challenge(feedback.tid, "user", **request)
    assert result["collaborator_ids"] == ["alice", "bob"]
    assert [message["recipient_id"] for message in mailbox(feedback)] == ["alice", "bob"]
    assert result == await service.update_challenge(
        feedback.tid, "user", **{**request, "instructions": list(reversed(request["instructions"]))}
    )
    assert len(mailbox(feedback)) == 2


async def test_old_update_command_hash_is_identical_when_instructions_absent(feedback):
    request = change_request(feedback)
    await feedback.service.update_challenge(feedback.tid, "user", **request)
    command = {
        "operation": "update",
        **{key: value for key, value in request.items() if key != "request_id"},
        "collaborator_ids": None,
        "work_status": None,
    }
    digest = hashlib.sha256(json.dumps(command, sort_keys=True, default=str).encode()).hexdigest()
    assert events(feedback)[-1]["payload"]["request_hash"] == digest
    assert mailbox(feedback) == []


@pytest.mark.parametrize(
    "instructions",
    [
        [],
        [{"recipient_id": "alice", "body": ""}],
        [{"recipient_id": "alice", "body": "x" * 20001}],
        [{"recipient_id": "alice", "body": "one"}] * 2,
        [{"recipient_id": "alice", "body": "one"}] * 33,
        [{"recipient_id": "", "body": "one"}],
    ],
)
async def test_invalid_dispatch_has_no_board_or_mailbox_writes(feedback, instructions):
    with pytest.raises(HTTPException) as caught:
        await feedback.service.update_challenge(
            feedback.tid, "user", **change_request(feedback, instructions=instructions)
        )
    assert caught.value.status_code == 422
    assert (
        await feedback.service.get_challenge(feedback.tid, feedback.challenge["id"])
        == feedback.challenge
    )
    assert mailbox(feedback) == []
    assert len(events(feedback)) == 1


async def test_dispatch_rejects_nonlead_and_unassigned_or_stopping_recipients(feedback):
    request = change_request(feedback, instructions=[{"recipient_id": "bob", "body": "Inspect"}])
    with pytest.raises(HTTPException, match="Only Lead may dispatch"):
        await feedback.service.update_challenge(
            feedback.tid, "alice", **request, generation=1, turn_id=feedback.turns["alice"]
        )
    with pytest.raises(HTTPException, match="recipient must own or collaborate"):
        await feedback.service.update_challenge(feedback.tid, "user", **request)
    with feedback.database.begin() as conn:
        conn.execute(
            update(s.ctf_members).where(s.ctf_members.c.id == "alice").values(run_state="stopping")
        )
    with pytest.raises(HTTPException, match="Member is unavailable"):
        await feedback.service.update_challenge(
            feedback.tid,
            "user",
            **change_request(feedback, instructions=[{"recipient_id": "alice", "body": "Inspect"}]),
        )
    assert mailbox(feedback) == []
    assert len(events(feedback)) == 1


async def test_dispatch_failure_rolls_back_board_event_and_already_written_first_mail(
    feedback, monkeypatch
):
    original = feedback.service._message

    async def fail_second(conn, tid, actor, recipient_id, *args, **kwargs):
        if recipient_id == "bob":
            raise RuntimeError("Synthetic second mailbox failure")
        return await original(conn, tid, actor, recipient_id, *args, **kwargs)

    monkeypatch.setattr(feedback.service, "_message", fail_second)
    request = change_request(
        feedback,
        action="collaborators",
        owner_id=None,
        collaborator_ids=["alice", "bob"],
        instructions=[{"recipient_id": aid, "body": "Inspect"} for aid in ("alice", "bob")],
    )
    with pytest.raises(RuntimeError, match="second mailbox failure"):
        await feedback.service.update_challenge(feedback.tid, "user", **request)
    assert (
        await feedback.service.get_challenge(feedback.tid, feedback.challenge["id"])
        == feedback.challenge
    )
    assert mailbox(feedback) == []
    assert len(events(feedback)) == 1
    monkeypatch.setattr(feedback.service, "_message", original)
    await feedback.service.update_challenge(feedback.tid, "user", **request)
    assert len(mailbox(feedback)) == 2


async def test_stale_dispatch_revision_cannot_enqueue_an_instruction(feedback):
    await feedback.service.update_challenge(feedback.tid, "user", **change_request(feedback))
    with pytest.raises(HTTPException) as caught:
        await feedback.service.update_challenge(
            feedback.tid,
            "user",
            **change_request(feedback, instructions=[{"recipient_id": "alice", "body": "Inspect"}]),
        )
    assert caught.value.status_code == 409
    assert mailbox(feedback) == []


async def test_release_sends_one_short_advisory_while_another_member_remains_busy(feedback):
    service = feedback.service
    owned = await service.update_challenge(feedback.tid, "user", **change_request(feedback))
    request = change_request(
        feedback, action="release", owner_id=None, expected_revision=owned["revision"]
    )
    result = await service.update_challenge(
        feedback.tid, "alice", **request, generation=1, turn_id=feedback.turns["alice"]
    )
    assert result["owner_id"] is None and result["work_status"] == "pending"
    notices = mailbox(feedback)
    assert len(notices) == 1
    assert notices[0]["recipient_id"] == "lead"
    assert notices[0]["sender_kind"] == "system" and notices[0]["kind"] == "message"
    assert notices[0]["notification_purpose"] == "challenge_available"
    assert json.loads(notices[0]["body"]) == {
        "challenge_id": result["id"],
        "revision": result["revision"],
        "reason": "release",
    }
    assert result == await service.update_challenge(
        feedback.tid, "alice", **request, generation=1, turn_id=feedback.turns["alice"]
    )
    assert len(mailbox(feedback)) == 1
    with feedback.database.connect() as conn:
        assert (
            conn.execute(
                select(s.ctf_turns.c.status).where(s.ctf_turns.c.id == feedback.turns["bob"])
            ).scalar_one()
            == "running"
        )


@pytest.mark.parametrize("lead_actor", [False, True])
async def test_reopen_notifies_only_when_pending_and_not_lead_own_change(feedback, lead_actor):
    closed = await feedback.service.update_challenge(
        feedback.tid,
        "user",
        **change_request(feedback, action="set_status", owner_id=None, work_status="cancelled"),
    )
    actor = "lead" if lead_actor else "user"
    identity = {"generation": 2, "turn_id": feedback.current_id} if lead_actor else {}
    await feedback.service.update_challenge(
        feedback.tid,
        actor,
        **change_request(
            feedback, action="reopen", owner_id=None, expected_revision=closed["revision"]
        ),
        **identity,
    )
    assert len(mailbox(feedback)) == (0 if lead_actor else 1)


async def test_no_advisory_without_a_newly_available_challenge(feedback):
    pending = await feedback.service.update_challenge(
        feedback.tid, "user", **change_request(feedback, action="release", owner_id=None)
    )
    owned = await feedback.service.update_challenge(
        feedback.tid, "user", **change_request(feedback, expected_revision=pending["revision"])
    )
    closed = await feedback.service.update_challenge(
        feedback.tid,
        "user",
        **change_request(
            feedback,
            action="set_status",
            owner_id=None,
            work_status="completed",
            solution=solution(),
            expected_revision=owned["revision"],
        ),
    )
    reopened = await feedback.service.update_challenge(
        feedback.tid,
        "user",
        **change_request(
            feedback, action="reopen", owner_id=None, expected_revision=closed["revision"]
        ),
    )
    assert reopened["owner_id"] == "alice" and reopened["work_status"] == "in_progress"
    assert mailbox(feedback) == []


async def dispatched_turn(feedback):
    await feedback.service.update_challenge(
        feedback.tid,
        "user",
        **change_request(feedback, instructions=[{"recipient_id": "alice", "body": "Inspect"}]),
    )
    await feedback.service.claim_messages(feedback.tid, "alice", feedback.turns["alice"], 1)


@pytest.mark.parametrize("end_reason", ["completed", "interrupted", "provider_unknown", "stopped"])
async def test_result_notification_explains_no_retry_and_omits_explicit_reply_body(
    feedback, end_reason
):
    await dispatched_turn(feedback)
    with feedback.database.begin() as conn:
        conn.execute(
            update(s.ctf_turns)
            .where(s.ctf_turns.c.id == feedback.turns["alice"])
            .values(explicit_reply_ids=["synthetic-reply"])
        )
    await feedback.service.finish_turn(
        feedback.tid, "alice", feedback.turns["alice"], 1, end_reason, "Synthetic final output"
    )
    result = next(
        msg for msg in mailbox(feedback) if msg["notification_purpose"] == "assignment_result"
    )
    body = json.loads(result["body"])
    assert body["final_answer"] is None and body["reply_refs"] == ["synthetic-reply"]
    assert body["recovery"] == {
        "status": "recovery_not_requested",
        "reason": end_reason,
        "attempt": 0,
        "limit": 0,
    }
    assert not any(msg["notification_purpose"] == "turn_recovery" for msg in mailbox(feedback))
    await feedback.service.finish_turn(
        feedback.tid, "alice", feedback.turns["alice"], 1, end_reason
    )
    assert (
        len(
            [msg for msg in mailbox(feedback) if msg["notification_purpose"] == "assignment_result"]
        )
        == 1
    )


async def test_provider_result_preserves_actual_decision_order_and_unknown_cost(
    feedback,
):
    await dispatched_turn(feedback)
    with feedback.database.begin() as conn:
        control = conn.execute(select(s.tasks.c.ctf_control)).scalar_one()
        control["budget_reservations"] = {
            "uncertain": {
                "turn_id": feedback.turns["alice"],
                "agent_id": "alice",
                "cost": "0.6",
                "state": "sent",
            }
        }
        conn.execute(update(s.tasks).values(ctf_control=control))
    await feedback.service.finish_turn(
        feedback.tid, "alice", feedback.turns["alice"], 1, "provider_transient", "x" * 2500
    )
    queued = mailbox(feedback)
    result = next(msg for msg in queued if msg["notification_purpose"] == "assignment_result")
    retry = next(msg for msg in queued if msg["notification_purpose"] == "turn_recovery")
    body = json.loads(result["body"])
    assert body["truncated"] and len(body["final_answer"]) == 2000
    assert body["recovery"]["status"] == "recovery_requested"
    assert body["recovery"]["message_id"] == retry["id"]
    with feedback.database.connect() as conn:
        control = conn.execute(select(s.tasks.c.ctf_control)).scalar_one()
    assert control["budget_unknown_reservations"]["uncertain"]["cost"] == "0.6"
    assert control["budget_unknown_reservations"]["uncertain"]["state"] == "unknown"
    assert control["turn_retry_delays"]["alice"]["ready_at"] == body["recovery"]["ready_at"]
    assert control["turn_retries"]["alice"]["provider_transient"] == 1
    written = events(feedback)
    finished = next(evt["version"] for evt in written if evt["type"] == "ctf.turn.finished")
    result_version = next(
        evt["version"] for evt in written if evt["payload"].get("id") == result["id"]
    )
    assert finished < result_version


@pytest.mark.parametrize("exhausted", [False, True])
async def test_context_decision_preserves_new_block_and_history_across_recovery(
    feedback, exhausted
):
    await dispatched_turn(feedback)
    turn_id = feedback.turns["alice"]
    snapshot = {
        "state": {
            "ctf_context_blocked": {
                "turn_id": turn_id,
                "message_ids": [mailbox(feedback)[0]["id"]],
                "branch": "protected_input",
            }
        }
    }
    await feedback.service.checkpoint(feedback.tid, "alice", turn_id, 1, snapshot, "prompt", 0, [])
    if exhausted:
        with feedback.database.begin() as conn:
            control = conn.execute(select(s.tasks.c.ctf_control)).scalar_one()
            control["turn_retries"] = {"alice": {"context_blocked": 1}}
            conn.execute(update(s.tasks).values(ctf_control=control))
    await feedback.service.finish_turn(feedback.tid, "alice", turn_id, 1, "context_blocked")
    result = next(
        msg for msg in mailbox(feedback) if msg["notification_purpose"] == "assignment_result"
    )
    decision = json.loads(result["body"])["recovery"]
    assert decision["limit"] == 1 and decision["attempt"] == 1
    assert decision["status"] == ("recovery_exhausted" if exhausted else "recovery_requested")
    with feedback.database.connect() as conn:
        control = conn.execute(select(s.tasks.c.ctf_control)).scalar_one()
    assert control["context_blocked_members"]["alice"]["turn_id"] == turn_id
    assert turn_id in control["context_blocked_history"]
    notices = [
        msg
        for msg in mailbox(feedback)
        if msg["notification_purpose"] == "context_recovery_exhausted"
    ]
    assert len(notices) == (1 if exhausted else 0)


async def test_step_limit_result_identifies_immediate_bounded_retry(feedback):
    await dispatched_turn(feedback)
    await feedback.service.finish_turn(
        feedback.tid, "alice", feedback.turns["alice"], 1, "step_limit"
    )
    result = next(
        msg for msg in mailbox(feedback) if msg["notification_purpose"] == "assignment_result"
    )
    decision = json.loads(result["body"])["recovery"]
    assert decision["status"] == "recovery_requested"
    assert decision["limit"] == 3 and decision["attempt"] == 1
    assert decision["ready_at"] is None
    assert decision["message_id"] in {msg["id"] for msg in mailbox(feedback)}


@pytest.mark.parametrize("disabled", ["budget", "phase", "replacement", "exhausted"])
async def test_result_carries_disabled_or_exhausted_recovery_without_spending_allowance(
    feedback, disabled
):
    await dispatched_turn(feedback)
    with feedback.database.begin() as conn:
        control = conn.execute(select(s.tasks.c.ctf_control)).scalar_one()
        if disabled == "phase":
            control["phase"] = "closing"
        elif disabled == "replacement":
            control["replacement"] = {"phase": "begin"}
        elif disabled == "exhausted":
            control["turn_retries"] = {"alice": {"provider_transient": 3}}
        conn.execute(
            update(s.tasks).values(
                ctf_control=control, usage={"cost": "10"} if disabled == "budget" else {}
            )
        )
    await feedback.service.finish_turn(
        feedback.tid, "alice", feedback.turns["alice"], 1, "provider_transient"
    )
    result = next(
        msg for msg in mailbox(feedback) if msg["notification_purpose"] == "assignment_result"
    )
    decision = json.loads(result["body"])["recovery"]
    assert decision["status"] == (
        "recovery_exhausted" if disabled == "exhausted" else "recovery_disabled"
    )
    assert decision["reason"] == ("retry_limit" if disabled == "exhausted" else disabled)
    assert decision["attempt"] == (3 if disabled == "exhausted" else 0)
    assert decision["limit"] == 3
    assert "message_id" not in decision
    assert not any(msg["notification_purpose"] == "turn_recovery" for msg in mailbox(feedback))
    if disabled == "exhausted":
        assert (
            len(
                [
                    msg
                    for msg in mailbox(feedback)
                    if msg["notification_purpose"] == "provider_recovery_exhausted"
                ]
            )
            == 1
        )
