"""PostgreSQL transactions for atomic assignments and explicit coordination feedback."""

import asyncio
import json
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import update
from test_ctf_integration import board_team, history
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def dispatch_board(ctf):
    tid, owner, helper = await board_team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "assignment-feedback")
    lead = {"actor": "lead", "generation": turn["generation"], "turn_id": turn["id"]}
    challenge = await ctf.create_challenge(
        tid, **lead, request_id=str(uuid4()), title="Synthetic board"
    )
    return tid, owner, helper, lead, challenge


@pytest.mark.parametrize("action", ["assign", "collaborators"])
async def test_atomic_dispatch_has_record_permission_before_member_receives_instruction(
    ctf, action, monkeypatch
):
    tid, owner, helper, lead, challenge = await dispatch_board(ctf)
    members = [owner["actor"]] if action == "assign" else [owner["actor"], helper["actor"]]
    payload = {
        "challenge_id": challenge["id"],
        "request_id": str(uuid4()),
        "expected_revision": 1,
        "action": action,
        "instructions": [
            {"recipient_id": aid, "body": f"Synthetic atomic dispatch to {aid}"} for aid in members
        ],
    }
    payload["owner_id" if action == "assign" else "collaborator_ids"] = (
        members[0] if action == "assign" else members
    )
    original = ctf._message

    async def observe(conn, tid, actor, recipient_id, *args, **kwargs):
        saved = await ctf._challenge(conn, tid, challenge["id"])
        assert recipient_id in {saved["owner_id"], *saved["collaborator_ids"]}
        assert saved["revision"] == 2
        return await original(conn, tid, actor, recipient_id, *args, **kwargs)

    monkeypatch.setattr(ctf, "_message", observe)
    result = await ctf.update_challenge(tid, **lead, **payload)
    assert "instructions" not in result
    assert (
        await ctf.update_challenge(
            tid, **lead, **{**payload, "instructions": list(reversed(payload["instructions"]))}
        )
        == result
    )
    state = await ctf.state(tid)
    dispatched = [msg for msg in state["messages"] if msg["sender_id"] == "lead"]
    assert len(dispatched) == len(members)
    assert all(msg["kind"] == "instruction" for msg in dispatched)
    for identity in [owner, helper] if action == "collaborators" else [owner]:
        await ctf.append_record(
            tid,
            **identity,
            challenge_id=challenge["id"],
            request_id=str(uuid4()),
            body="Synthetic finding",
        )


async def test_partial_dispatch_failure_rolls_back_entire_board_and_mailbox_transaction(
    ctf, monkeypatch
):
    tid, owner, helper, lead, challenge = await dispatch_board(ctf)
    before = await ctf.state(tid)
    original = ctf._message
    calls = 0

    async def fail_second(conn, tid, actor, recipient_id, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Synthetic second recipient failure")
        return await original(conn, tid, actor, recipient_id, *args, **kwargs)

    monkeypatch.setattr(ctf, "_message", fail_second)
    request = {
        "challenge_id": challenge["id"],
        "request_id": str(uuid4()),
        "expected_revision": 1,
        "action": "collaborators",
        "collaborator_ids": [owner["actor"], helper["actor"]],
        "instructions": [
            {"recipient_id": item["actor"], "body": "Synthetic dispatch"}
            for item in (owner, helper)
        ],
    }
    with pytest.raises(RuntimeError, match="second recipient failure"):
        await ctf.update_challenge(tid, **lead, **request)
    assert await ctf.state(tid) == before
    monkeypatch.setattr(ctf, "_message", original)
    await ctf.update_challenge(tid, **lead, **request)
    assert (
        len([msg for msg in (await ctf.state(tid))["messages"] if msg["sender_id"] == "lead"]) == 2
    )


async def test_concurrent_assignment_cas_dispatches_only_winning_owner(ctf):
    tid, owner, helper, lead, challenge = await dispatch_board(ctf)

    async def assign(aid):
        return await ctf.update_challenge(
            tid,
            **lead,
            challenge_id=challenge["id"],
            request_id=str(uuid4()),
            expected_revision=1,
            action="assign",
            owner_id=aid,
            instructions=[{"recipient_id": aid, "body": f"Synthetic CAS dispatch to {aid}"}],
        )

    results = await asyncio.gather(
        assign(owner["actor"]), assign(helper["actor"]), return_exceptions=True
    )
    assert sum(isinstance(result, HTTPException) for result in results) == 1
    current = await ctf.get_challenge(tid, challenge["id"])
    dispatched = [msg for msg in (await ctf.state(tid))["messages"] if msg["sender_id"] == "lead"]
    assert len(dispatched) == 1
    assert dispatched[0]["recipient_id"] == current["owner_id"]


async def test_release_wakes_idle_lead_once_while_another_member_keeps_running(ctf):
    tid, owner, helper, lead, challenge = await dispatch_board(ctf)
    inputs = await ctf.claim_messages(tid, "lead", lead["turn_id"], lead["generation"])
    await ctf.checkpoint(
        tid,
        "lead",
        lead["turn_id"],
        lead["generation"],
        history(inputs),
        "Synthetic opening",
        0,
        [{"message_id": msg["id"], "claim_token": msg["claim_token"]} for msg in inputs],
    )
    await ctf.finish_turn(tid, "lead", lead["turn_id"], lead["generation"], "completed")
    challenge = await ctf.update_challenge(
        tid,
        "user",
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=1,
        action="assign",
        owner_id=owner["actor"],
    )
    assert "lead" not in (await ctf.runtime_state(tid))["ready_member_ids"]
    request = dict(
        challenge_id=challenge["id"], request_id=str(uuid4()), expected_revision=2, action="release"
    )
    result = await ctf.update_challenge(tid, **owner, **request)
    assert await ctf.update_challenge(tid, **owner, **request) == result
    state = await ctf.runtime_state(tid)
    assert "lead" in state["ready_member_ids"]
    assert (
        next(turn for turn in state["turns"] if turn["id"] == helper["turn_id"])["status"]
        == "running"
    )
    notices = [
        msg
        for msg in (await ctf.state(tid))["messages"]
        if msg["notification_purpose"] == "challenge_available"
    ]
    assert len(notices) == 1 and notices[0]["kind"] == "message"
    assert json.loads(notices[0]["body"])["challenge_id"] == challenge["id"]


@pytest.mark.parametrize("exhausted", [False, True])
async def test_assignment_result_reports_actual_retry_and_preserves_unknown_charge(ctf, exhausted):
    tid, owner, _, _, _ = await dispatch_board(ctf)
    aid, turn_id, generation = owner["actor"], owner["turn_id"], owner["generation"]
    messages = await ctf.claim_messages(tid, aid, turn_id, generation)
    await ctf.checkpoint(
        tid,
        aid,
        turn_id,
        generation,
        history(messages),
        "Synthetic opening",
        0,
        [{"message_id": msg["id"], "claim_token": msg["claim_token"]} for msg in messages],
    )
    await ctf.authorize_member(
        tid, aid, generation, turn_id, reservation_id="synthetic-unknown", reservation_cost="0.6"
    )
    await ctf.mark_reservation_sent(
        tid, aid, generation, turn_id, reservation_id="synthetic-unknown"
    )
    if exhausted:
        task = (await ctf.state(tid))["task"]
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={
                        **task["ctf_control"],
                        "turn_retries": {aid: {"provider_transient": 3}},
                    }
                )
            )
    await ctf.finish_turn(
        tid, aid, turn_id, generation, "provider_transient", "Synthetic partial result"
    )
    await ctf.finish_turn(
        tid, aid, turn_id, generation, "provider_transient", "Synthetic partial result"
    )
    state = await ctf.state(tid)
    notifications = [
        msg
        for msg in state["messages"]
        if msg["notification_purpose"] == "assignment_result" and msg["source_turn_id"] == turn_id
    ]
    assert len(notifications) == 1
    decision = json.loads(notifications[0]["body"])["recovery"]
    control = state["task"]["ctf_control"]
    assert control["budget_unknown_reservations"]["synthetic-unknown"]["cost"] == "0.6"
    assert decision["status"] == ("recovery_exhausted" if exhausted else "recovery_requested")
    if not exhausted:
        assert decision["ready_at"] == control["turn_retry_delays"][aid]["ready_at"]
        assert any(msg["id"] == decision["message_id"] for msg in state["messages"])
