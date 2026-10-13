"""Persistent bounded provider retries without a model or execution environment."""

import asyncio
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from bbx_blackboard import ctf as module
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.store import schema as s
from sqlalchemy import update
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_integration import history
from test_ctf_usage_outbox_integration import saved_dispatch

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def team(ctf):
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Preserve fake work during provider recovery",
            "budget": {"max_cost": "10", "max_minutes": 60},
            "ctf_options": {"max_teammates": 2},
        }
    )
    await ctf.provision(tid)
    await ctf.start(tid)
    members = []
    for name in ("Alice", "Bob"):
        member = await ctf.create_member(tid, "user", name, str(uuid4()))
        await ctf.post_message(
            tid, "user", member["id"], "Continue assigned fake work", str(uuid4()), "instruction"
        )
        members.append(member)
    return tid, members


async def test_provider_delay_survives_restart_and_preserves_other_turn_and_unknown_cost(
    ctf, monkeypatch
):
    instant = module.now()
    monkeypatch.setattr(module, "now", lambda: instant)
    tid, (alice, bob) = await team(ctf)
    alice_turn = await ctf.claim_turn(tid, alice["id"], "runtime")
    bob_turn = await ctf.claim_turn(tid, bob["id"], "runtime")
    messages = await ctf.claim_messages(
        tid, alice["id"], alice_turn["id"], alice_turn["generation"]
    )
    await ctf.checkpoint(
        tid,
        alice["id"],
        alice_turn["id"],
        alice_turn["generation"],
        history(messages),
        "Prompt",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
    )
    await ctf.authorize_member(
        tid,
        alice["id"],
        alice_turn["generation"],
        alice_turn["id"],
        reservation_id="uncertain-call",
        reservation_cost="0.6",
    )
    await ctf.mark_reservation_sent(
        tid,
        alice["id"],
        generation=alice_turn["generation"],
        turn_id=alice_turn["id"],
        reservation_id="uncertain-call",
    )
    await asyncio.gather(
        *(
            ctf.finish_turn(
                tid, alice["id"], alice_turn["id"], alice_turn["generation"], "provider_transient"
            )
            for _ in range(2)
        )
    )
    restarted = CtfService(ctf.repo.engine)
    state = await restarted.state(tid)
    control = state["task"]["ctf_control"]
    assert state["task"]["status"] == "running" and control["phase"] == "running"
    assert control["turn_retries"][alice["id"]] == {"provider_transient": 1}
    assert datetime.fromisoformat(control["turn_retry_delays"][alice["id"]]["ready_at"]) == (
        instant + timedelta(seconds=2)
    )
    assert control["budget_unknown_reservations"]["uncertain-call"]["cost"] == "0.6"
    assert control["budget_unknown_reservations"]["uncertain-call"]["state"] == "unknown"
    assert (
        next(turn for turn in state["turns"] if turn["id"] == bob_turn["id"])["status"] == "running"
    )
    assert await restarted.claim_turn(tid, alice["id"], "restarted") is None
    instant += timedelta(seconds=1)
    assert alice["id"] not in (await restarted.runtime_state(tid))["ready_member_ids"]
    assert await restarted.claim_turn(tid, alice["id"], "restarted") is None
    instant += timedelta(seconds=1)
    assert alice["id"] in (await restarted.runtime_state(tid))["ready_member_ids"]
    retry = await restarted.claim_turn(tid, alice["id"], "restarted")
    assert retry is not None
    assert retry["generation"] == alice_turn["generation"] + 1
    await restarted.authorize_member(tid, bob["id"], bob_turn["generation"], bob_turn["id"])
    assert (await restarted.state(tid))["task"]["ctf_control"]["budget_unknown_reservations"]


async def test_provider_retries_are_bounded_and_exhaustion_preserves_owner(ctf, monkeypatch):
    instant = module.now()
    monkeypatch.setattr(module, "now", lambda: instant)
    tid, (alice, _) = await team(ctf)
    challenge = await ctf.create_challenge(tid, "user", str(uuid4()), "Fake owned work")
    await ctf.update_challenge(
        tid, "user", challenge["id"], str(uuid4()), 1, "assign", owner_id=alice["id"]
    )
    session: dict[str, Any] = history([])
    revision = 0
    for attempt in range(4):
        turn = await ctf.claim_turn(tid, alice["id"], "runtime")
        messages = await ctf.claim_messages(tid, alice["id"], turn["id"], turn["generation"])
        session["state"]["in_memory"]["messages"].extend(
            history(messages)["state"]["in_memory"]["messages"]
        )
        saved = await ctf.checkpoint(
            tid,
            alice["id"],
            turn["id"],
            turn["generation"],
            session,
            "Prompt",
            revision,
            [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
        )
        revision = saved["revision"]
        await ctf.finish_turn(
            tid, alice["id"], turn["id"], turn["generation"], "provider_transient"
        )
        await ctf.finish_turn(
            tid, alice["id"], turn["id"], turn["generation"], "provider_transient"
        )
        control = (await ctf.state(tid))["task"]["ctf_control"]
        assert control["turn_retries"][alice["id"]]["provider_transient"] == min(attempt + 1, 3)
        if attempt < 3:
            delay = 2 ** (attempt + 1)
            assert datetime.fromisoformat(
                control["turn_retry_delays"][alice["id"]]["ready_at"]
            ) == (instant + timedelta(seconds=delay))
            assert await ctf.claim_turn(tid, alice["id"], "runtime") is None
            instant += timedelta(seconds=delay)
    state = await ctf.state(tid)
    assert await ctf.claim_turn(tid, alice["id"], "runtime") is None
    assert state["task"]["ctf_control"]["phase"] == "running"
    assert state["challenges"][0]["owner_id"] == alice["id"]
    assert (
        next(member for member in state["members"] if member["id"] == alice["id"])["lifecycle"]
        == "active"
    )
    notices = [
        message
        for message in state["messages"]
        if message["notification_purpose"] == "provider_recovery_exhausted"
    ]
    assert len(notices) == 1 and notices[0]["recipient_id"] == "lead"
    assert alice["id"] in notices[0]["body"]
    assert (
        len(
            [
                message
                for message in state["messages"]
                if message["recipient_id"] == alice["id"]
                and message["notification_purpose"] == "turn_recovery"
            ]
        )
        == 3
    )


async def test_explicit_instruction_bypasses_delay_without_replaying_pending_work(ctf, monkeypatch):
    instant = module.now()
    monkeypatch.setattr(module, "now", lambda: instant)
    tid, (alice, _) = await team(ctf)
    turn = await ctf.claim_turn(tid, alice["id"], "runtime")
    old = await ctf.claim_messages(tid, alice["id"], turn["id"], turn["generation"])
    await ctf.finish_turn(tid, alice["id"], turn["id"], turn["generation"], "provider_transient")
    assert await ctf.claim_turn(tid, alice["id"], "runtime") is None
    fresh = await ctf.post_message(
        tid, "user", alice["id"], "Review saved state first", str(uuid4()), "instruction"
    )
    retry = await ctf.claim_turn(tid, alice["id"], "runtime")
    messages = await ctf.claim_messages(tid, alice["id"], retry["id"], retry["generation"])
    assert [message["id"] for message in messages] == [fresh["id"]]
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["turn_retries"][alice["id"]] == {"provider_transient": 1}
    assert alice["id"] not in state["task"]["ctf_control"]["turn_retry_delays"]
    deferred = [m for m in state["messages"] if m["recipient_id"] == alice["id"] and m["deferred"]]
    assert {message["id"] for message in old} <= {message["id"] for message in deferred}
    assert any(message["notification_purpose"] == "turn_recovery" for message in deferred)


@pytest.mark.parametrize("no_budget", [False, True])
async def test_exhausted_lead_notification_cannot_start_an_unbounded_recovery_loop(ctf, no_budget):
    tid, _ = await team(ctf)
    state = await ctf.state(tid)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **state["task"]["ctf_control"],
                    "turn_retries": {"lead": {"provider_transient": 3}},
                }
            )
        )
    turn = await ctf.claim_turn(tid, "lead", "runtime")
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    await ctf.checkpoint(
        tid,
        "lead",
        turn["id"],
        turn["generation"],
        history(messages),
        "Prompt",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
    )
    if no_budget:
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.tasks).where(s.tasks.c.id == tid).values(usage={"cost": "10"})
            )
    await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "provider_transient")
    state = await ctf.state(tid)
    notice = next(
        message
        for message in state["messages"]
        if message["notification_purpose"] == "provider_recovery_exhausted"
    )
    assert notice["recipient_id"] == "lead" and notice["deferred"] is True
    assert "lead" not in (await ctf.runtime_state(tid))["ready_member_ids"]
    assert await ctf.claim_turn(tid, "lead", "runtime") is None
    assert state["task"]["ctf_control"]["phase"] == "running"


@pytest.mark.parametrize("with_uncertain_call", [False, True])
async def test_provider_failure_reconciles_confirmed_usage_but_retains_uncertain_call(
    ctf, with_uncertain_call
):
    tid, turn, _, receipt = await saved_dispatch(ctf)
    uncertain = f"{turn['id']}:2"
    if with_uncertain_call:
        # The next call is admitted only after the prior receipt has settled.
        await ctf.reconcile_usage_outbox(tid, "lead")
        await ctf.authorize_member(tid, "lead", turn["generation"], turn["id"], uncertain, "0.2")
        await ctf.mark_reservation_sent(tid, "lead", turn["generation"], turn["id"], uncertain)
    await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "provider_transient")
    state = await ctf.state(tid)
    control = state["task"]["ctf_control"]
    assert state["task"]["usage"]["cost"] == "0.12"
    assert set(control.get("budget_unknown_reservations", {})) == (
        {uncertain} if with_uncertain_call else set()
    )
    if with_uncertain_call:
        assert control["budget_unknown_reservations"][uncertain]["cost"] == "0.2"
    assert not control.get("budget_reservations")
    assert control["turn_retries"]["lead"] == {"provider_transient": 1}
    assert (await ctf.reconcile_usage_outbox(tid, "lead"))["request_ids"] == [receipt["request_id"]]
    after = await ctf.state(tid)
    assert after["task"]["usage"]["cost"] == "0.12"
    assert set(after["task"]["ctf_control"].get("budget_unknown_reservations", {})) == (
        {uncertain} if with_uncertain_call else set()
    )
