"""Failure admission recovery on a disposable PostgreSQL; no model calls."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def running_member(ctf):
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Offline failure isolation",
            "budget": {"max_cost": "10", "max_minutes": 10},
            "ctf_options": {"max_teammates": 2, "dispatch_mode": "automatic"},
        }
    )
    await ctf.provision(tid)
    await ctf.start(tid)
    member = await ctf.create_member(tid, "user", "Isolated", str(uuid4()))
    await ctf.post_message(
        tid, "user", member["id"], "Read synthetic input", str(uuid4()), "instruction"
    )
    turn = await ctf.claim_turn(tid, member["id"], "failure-test")
    await ctf.record_execution(tid, member["id"], turn["id"], turn["generation"], "boot-test")
    return tid, member, turn


def diagnostic():
    return {
        "error_type": "RuntimeError",
        "phase": "turn",
        "summary": "Synthetic member failure",
        "occurred_at": datetime.now(UTC).isoformat(),
        "correlation_id": str(uuid4()),
    }


async def test_failure_hold_requires_proven_stop_and_preserves_unknown_cost(ctf):
    tid, member, turn = await running_member(ctf)
    await ctf.authorize_member(
        tid,
        member["id"],
        turn["generation"],
        turn["id"],
        reservation_id="call",
        reservation_cost="0.5",
    )
    await ctf.mark_reservation_sent(tid, member["id"], turn["generation"], turn["id"], "call")
    assert await ctf.pause_for_failure(
        tid, diagnostic(), member["id"], turn["id"], turn["generation"]
    ) == {"held": True}
    assert await ctf.claim_turn(tid, "lead", "failure-test") is None
    assert await ctf.reconcile_member_failures(tid) == {"held": True}
    state = await ctf.runtime_state(tid)
    stopped = next(m for m in state["members"] if m["id"] == member["id"])
    proof = {"boot_id": "boot-test", "generation": turn["generation"], "drained": True}
    await ctf.record_execution_drained(tid, member["id"], proof)
    await ctf.complete_member_operation(
        tid, member["id"], stopped["pending_operation"]["request_id"], proof
    )
    assert await ctf.reconcile_member_failures(tid) == {"held": False}
    assert await ctf.reconcile_member_failures(tid) == {"held": False}
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["phase"] == "running"
    assert state["task"]["ctf_control"]["budget_unknown_reservations"]["call"]["cost"] == "0.5"
    assert next(m for m in state["members"] if m["id"] == member["id"])["lifecycle"] == "stopped"


async def test_task_wide_unknown_failure_is_not_cleared_by_reconciliation(ctf):
    tid, _, _ = await running_member(ctf)
    await ctf.pause_for_failure(tid, diagnostic())
    assert await ctf.reconcile_member_failures(tid) == {"held": True}
    assert await ctf.claim_turn(tid, "lead", "failure-test") is None
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["phase"] == "running"


async def test_manual_task_rejects_automatic_hold(ctf):
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Legacy task",
            "budget": {"max_cost": "10", "max_minutes": 10},
        }
    )
    await ctf.provision(tid)
    await ctf.start(tid)
    with pytest.raises(HTTPException, match="automatic mode"):
        await ctf.pause_for_failure(tid, diagnostic())
