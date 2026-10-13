"""Persistent budget waits retain queued work without creating failed turns."""

from typing import Any, cast
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import update
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_turn, team
from test_ctf_integration import ctf_url as ctf_url

pytestmark = pytest.mark.integration


async def limited_team(ctf):
    tid = await team(ctf)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(budget={"max_cost": "1", "max_minutes": 60})
        )
    member = await ctf.create_member(tid, "user", "Alice", str(uuid4()))
    return tid, member


async def test_usage_settlement_admits_waiting_turn_without_receipts_or_reclaim(ctf):
    tid, member = await limited_team(ctf)
    lead = await ctf_turn(ctf, tid)
    waiting = await ctf_turn(ctf, tid, member["id"])
    await ctf.authorize_member(tid, "lead", lead["generation"], lead["id"], "lead:1", "0.6")
    for _ in range(3):
        with pytest.raises(HTTPException) as caught:
            await ctf.authorize_member(
                tid, member["id"], waiting["generation"], waiting["id"], "member:1", "0.6"
            )
        assert isinstance(caught.value.detail, dict)
        assert cast(dict[str, Any], caught.value.detail)["code"] == "ctf_budget_wait"
    state = await ctf.state(tid)
    assert len(state["turns"]) == 2
    assert all(turn["status"] == "running" for turn in state["turns"])
    assert all(message["status"] == "queued" for message in state["messages"])
    assert not any(m["notification_purpose"] == "assignment_result" for m in state["messages"])
    await ctf.mark_reservation_sent(tid, "lead", lead["generation"], lead["id"], "lead:1")
    await ctf.bill_usage(
        tid, "lead", lead["id"], lead["generation"], "usage", {"cost": "0.1"}, "lead:1"
    )
    await ctf.authorize_member(
        tid, member["id"], waiting["generation"], waiting["id"], "member:1", "0.6"
    )
    state = await ctf.state(tid)
    assert len(state["turns"]) == 2
    assert (
        state["task"]["ctf_control"]["budget_reservations"]["member:1"]["turn_id"] == waiting["id"]
    )
    messages = await ctf.claim_messages(tid, member["id"], waiting["id"], waiting["generation"])
    assert len(messages) == 1 and messages[0]["claim_turn_id"] == waiting["id"]


async def test_claim_returns_no_work_when_active_reservations_fill_budget(ctf):
    tid, member = await limited_team(ctf)
    lead = await ctf_turn(ctf, tid)
    await ctf.post_message(tid, "user", member["id"], "Compute", str(uuid4()), "instruction")
    await ctf.authorize_member(tid, "lead", lead["generation"], lead["id"], "lead:1", "1")
    for _ in range(3):
        assert await ctf.claim_turn(tid, member["id"], "runtime") is None
    state = await ctf.state(tid)
    assert len(state["turns"]) == 1 and state["task"]["ctf_control"]["phase"] == "running"
    await ctf.bill_usage(
        tid, "lead", lead["id"], lead["generation"], "usage", {"cost": "0.1"}, "lead:1"
    )
    assert await ctf.claim_turn(tid, member["id"], "runtime") is not None


async def test_unknown_reservations_produce_final_shortfall_and_preserve_fencing(ctf):
    tid, member = await limited_team(ctf)
    lead = await ctf_turn(ctf, tid)
    await ctf.authorize_member(tid, "lead", lead["generation"], lead["id"], "lead:1", "0.6")
    await ctf.mark_reservation_sent(tid, "lead", lead["generation"], lead["id"], "lead:1")
    await ctf.recover(tid)
    waiting = await ctf_turn(ctf, tid, member["id"])
    with pytest.raises(HTTPException) as caught:
        await ctf.authorize_member(
            tid, member["id"], waiting["generation"], waiting["id"], "member:1", "0.6"
        )
    assert isinstance(caught.value.detail, dict)
    assert cast(dict[str, Any], caught.value.detail)["code"] == "ctf_budget_exhausted"
    with pytest.raises(HTTPException) as stale:
        await ctf.authorize_member(tid, "lead", lead["generation"], lead["id"], "lead:2", "0.1")
    assert isinstance(stale.value.detail, str) and "Stale" in stale.value.detail


@pytest.mark.parametrize("gate", ["closing", "replacement", "elapsed"])
async def test_idempotent_reservation_does_not_double_count_or_bypass_admission_gates(ctf, gate):
    tid, _ = await limited_team(ctf)
    lead = await ctf_turn(ctf, tid)
    arguments = (tid, "lead", lead["generation"], lead["id"], "lead:1", "1")
    await ctf.authorize_member(*arguments)
    await ctf.authorize_member(*arguments)
    state = await ctf.state(tid)
    control = state["task"]["ctf_control"]
    assert list(control["budget_reservations"]) == ["lead:1"]
    values = {}
    if gate == "closing":
        values["ctf_control"] = {**control, "phase": "closing"}
    elif gate == "replacement":
        values["ctf_control"] = {**control, "replacement": {"phase": "draining"}}
    else:
        values["active_seconds"] = 3601
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(**values))
    with pytest.raises(HTTPException) as caught:
        await ctf.authorize_member(*arguments)
    if gate == "elapsed":
        assert cast(dict[str, Any], caught.value.detail)["code"] == "ctf_budget_exhausted"
    else:
        assert isinstance(caught.value.detail, str)


@pytest.mark.parametrize("already_sent", [False, True])
@pytest.mark.parametrize("stop_member", [False, True])
async def test_provider_boundary_rejects_stop_after_authorization(ctf, already_sent, stop_member):
    tid, member = await limited_team(ctf)
    turn = await ctf_turn(ctf, tid, member["id"])
    await ctf.authorize_member(tid, member["id"], turn["generation"], turn["id"], "member:1", "1")
    arguments = (tid, member["id"], turn["generation"], turn["id"], "member:1")
    if already_sent:
        await ctf.mark_reservation_sent(*arguments)
        await ctf.mark_reservation_sent(*arguments)
    if stop_member:
        await ctf.request_member_operation(tid, "user", member["id"], "stop", str(uuid4()))
    else:
        await ctf.request_finish(
            tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Stop"}
        )
    with pytest.raises(HTTPException):
        await ctf.mark_reservation_sent(*arguments)
    state = await ctf.state(tid)
    reservation = state["task"]["ctf_control"]["budget_reservations"]["member:1"]
    assert reservation["state"] == ("sent" if already_sent else "admitted")
