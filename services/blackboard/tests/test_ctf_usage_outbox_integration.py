"""Confirmed model usage settles old calls without reopening execution fences."""

import copy
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


def usage_state(turn, *, entries=True):
    entry = {
        "agent_id": "lead",
        "turn_id": str(turn["id"]),
        "generation": turn["generation"],
        "reservation_id": f"{turn['id']}:1",
        "request_id": str(uuid4()),
        "usage": {"cost": "0.12", "output_tokens": 10},
    }
    state = {"ctf_steps": {str(turn["id"]): 1}, "ctf_usage_outbox": [entry] if entries else []}
    return state, entry


async def saved_dispatch(ctf, *, entries=True):
    tid = await team(ctf)
    turn = await ctf_turn(ctf, tid)
    state, entry = usage_state(turn, entries=entries)
    await ctf.authorize_member(
        tid, "lead", turn["generation"], turn["id"], entry["reservation_id"], "0.8"
    )
    await ctf.mark_reservation_sent(
        tid, "lead", turn["generation"], turn["id"], entry["reservation_id"]
    )
    await ctf.checkpoint(
        tid, "lead", turn["id"], turn["generation"], {"state": state}, "role", 0, []
    )
    return tid, turn, state, entry


async def test_recovery_bills_persisted_usage_before_classifying_dispatch_unknown(ctf):
    tid, turn, _, entry = await saved_dispatch(ctf)
    await ctf.recover(tid)
    snapshot = await ctf.state(tid)
    assert snapshot["task"]["usage"]["cost"] == "0.12"
    assert not snapshot["task"]["ctf_control"].get("budget_unknown_reservations")
    assert not snapshot["task"]["ctf_control"].get("budget_reservations")
    assert snapshot["turns"][0]["usage"]["output_tokens"] == 10
    with pytest.raises(HTTPException, match="Stale CTF generation"):
        await ctf.bill_usage(
            tid,
            "lead",
            turn["id"],
            turn["generation"],
            entry["request_id"],
            entry["usage"],
            entry["reservation_id"],
        )
    first = await ctf.reconcile_usage_outbox(tid, "lead")
    # The committed reconciliation response may be lost; a repeat is accounting-only.
    second = await ctf.reconcile_usage_outbox(tid, "lead")
    assert first == second == {"request_ids": [entry["request_id"]]}
    assert (await ctf.state(tid))["task"]["usage"]["cost"] == "0.12"


async def test_late_confirmed_outbox_replaces_only_matching_unknown_and_retains_other_calls(ctf):
    tid, turn, state, entry = await saved_dispatch(ctf, entries=False)
    await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "interrupted")
    second = await ctf_turn(ctf, tid)
    other = f"{second['id']}:1"
    await ctf.authorize_member(tid, "lead", second["generation"], second["id"], other, "0.8")
    await ctf.mark_reservation_sent(tid, "lead", second["generation"], second["id"], other)
    await ctf.finish_turn(tid, "lead", second["id"], second["generation"], "interrupted")
    state["ctf_usage_outbox"] = [entry]
    async with ctf.repo.engine.begin() as conn:
        await ctf.sessions._save_session(conn, tid, "lead", {"state": state}, "role", "native", 1)
    await ctf.reconcile_usage_outbox(tid, "lead")
    snapshot = await ctf.state(tid)
    unknown = snapshot["task"]["ctf_control"]["budget_unknown_reservations"]
    assert set(unknown) == {other}
    assert unknown[other]["cost"] == "0.8"
    assert snapshot["task"]["usage"]["cost"] == "0.12"
    assert next(t for t in snapshot["turns"] if t["id"] == turn["id"])["usage"]["cost"] == "0.12"
    assert next(t for t in snapshot["turns"] if t["id"] == second["id"])["usage"] == {}


@pytest.mark.parametrize(
    "field,value", [("generation", -1), ("agent_id", "other"), ("turn_id", str(uuid4()))]
)
async def test_reconciliation_rejects_persisted_identity_conflict_without_releasing_unknown(
    ctf, field, value
):
    tid, turn, state, entry = await saved_dispatch(ctf, entries=False)
    await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "interrupted")
    invalid = copy.deepcopy(entry)
    invalid[field] = value
    state["ctf_usage_outbox"] = [invalid]
    async with ctf.repo.engine.begin() as conn:
        await ctf.sessions._save_session(conn, tid, "lead", {"state": state}, "role", "native", 1)
    with pytest.raises(HTTPException, match="identity mismatch"):
        await ctf.reconcile_usage_outbox(tid, "lead")
    snapshot = await ctf.state(tid)
    assert entry["reservation_id"] in snapshot["task"]["ctf_control"]["budget_unknown_reservations"]
    assert snapshot["task"]["usage"] == {}


async def test_confirmed_legacy_outbox_uses_reservation_identity_without_inventing_usage(ctf):
    tid, _, state, entry = await saved_dispatch(ctf, entries=False)
    for field in ("agent_id", "turn_id", "generation"):
        entry.pop(field)
    state["ctf_usage_outbox"] = [entry]
    async with ctf.repo.engine.begin() as conn:
        await ctf.sessions._save_session(conn, tid, "lead", {"state": state}, "role", "native", 1)
    await ctf.recover(tid)
    assert (await ctf.state(tid))["task"]["usage"]["cost"] == "0.12"


async def test_admitted_but_unsent_call_releases_without_usage_or_unknown(ctf):
    tid = await team(ctf)
    turn = await ctf_turn(ctf, tid)
    await ctf.authorize_member(
        tid, "lead", turn["generation"], turn["id"], f"{turn['id']}:1", "0.8"
    )
    await ctf.checkpoint(
        tid,
        "lead",
        turn["id"],
        turn["generation"],
        {
            "state": {
                "ctf_context_blocked": {
                    "turn_id": str(turn["id"]),
                    "fingerprint": "fake-view",
                    "message_ids": [],
                    "remaining_chars": 10000,
                    "limit_chars": 1000,
                    "excluded_before": 0,
                }
            }
        },
        "role",
        0,
        [],
    )
    await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "context_blocked")
    snapshot = await ctf.state(tid)
    assert not snapshot["task"]["ctf_control"].get("budget_reservations")
    assert not snapshot["task"]["ctf_control"].get("budget_unknown_reservations")
    assert snapshot["task"]["usage"] == {}


async def test_legacy_duplicate_does_not_release_current_reservation_and_reconcile_rejects_alias(
    ctf,
):
    tid, first, _, entry = await saved_dispatch(ctf)
    await ctf.recover(tid)
    second = await ctf_turn(ctf, tid)
    other = f"{second['id']}:1"
    await ctf.authorize_member(tid, "lead", second["generation"], second["id"], other, "0.8")
    await ctf.mark_reservation_sent(tid, "lead", second["generation"], second["id"], other)
    await ctf.bill_usage(
        tid, "lead", second["id"], second["generation"], entry["request_id"], entry["usage"], other
    )
    before = await ctf.state(tid)
    assert other in before["task"]["ctf_control"]["budget_reservations"]
    assert before["task"]["usage"]["cost"] == "0.12"
    state, alias = usage_state(second)
    alias["request_id"] = entry["request_id"]
    await ctf.checkpoint(
        tid, "lead", second["id"], second["generation"], {"state": state}, "role", 1, []
    )
    with pytest.raises(HTTPException, match="Usage request belongs to another turn"):
        await ctf.reconcile_usage_outbox(tid, "lead")
    after = await ctf.state(tid)
    assert after["task"]["usage"]["cost"] == "0.12"
    assert other in after["task"]["ctf_control"]["budget_reservations"]
    assert next(t for t in after["turns"] if t["id"] == first["id"])["usage"]["cost"] == "0.12"


@pytest.mark.parametrize("confirmed", [False, True])
async def test_admission_reconciles_confirmed_old_usage_before_permanent_shortfall(ctf, confirmed):
    tid, first, state, entry = await saved_dispatch(ctf, entries=False)
    await ctf.finish_turn(tid, "lead", first["id"], first["generation"], "interrupted")
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(budget={"max_cost": "1", "max_minutes": 60})
        )
        if confirmed:
            state["ctf_usage_outbox"] = [entry]
            await ctf.sessions._save_session(
                conn, tid, "lead", {"state": state}, "role", "native", 1
            )
    second = await ctf_turn(ctf, tid)
    if confirmed:
        await ctf.authorize_member(
            tid, "lead", second["generation"], second["id"], f"{second['id']}:1", "0.5"
        )
        snapshot = await ctf.state(tid)
        assert snapshot["task"]["usage"]["cost"] == "0.12"
        assert not snapshot["task"]["ctf_control"].get("budget_unknown_reservations")
        assert set(snapshot["task"]["ctf_control"]["budget_reservations"]) == {f"{second['id']}:1"}
        assert (
            next(t for t in snapshot["turns"] if t["id"] == first["id"])["usage"]["cost"] == "0.12"
        )
        assert await ctf.reconcile_budget(tid) == {"members": 1, "request_count": 1}
        assert (await ctf.state(tid))["task"]["usage"]["cost"] == "0.12"
    else:
        with pytest.raises(HTTPException) as caught:
            await ctf.authorize_member(
                tid, "lead", second["generation"], second["id"], f"{second['id']}:1", "0.5"
            )
        assert isinstance(caught.value.detail, dict)
        assert cast(dict[str, Any], caught.value.detail)["code"] == "ctf_budget_exhausted"
        snapshot = await ctf.state(tid)
        assert (
            entry["reservation_id"]
            in snapshot["task"]["ctf_control"]["budget_unknown_reservations"]
        )
        assert snapshot["task"]["usage"] == {}
        assert await ctf.reconcile_budget(tid) == {"members": 0, "request_count": 0}


async def test_rejected_new_call_does_not_roll_back_confirmed_old_usage(ctf):
    tid, first, state, entry = await saved_dispatch(ctf, entries=False)
    await ctf.finish_turn(tid, "lead", first["id"], first["generation"], "interrupted")
    state["ctf_usage_outbox"] = [entry]
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(budget={"max_cost": "1", "max_minutes": 60})
        )
        await ctf.sessions._save_session(conn, tid, "lead", {"state": state}, "role", "native", 1)
    second = await ctf_turn(ctf, tid)
    with pytest.raises(HTTPException) as caught:
        await ctf.authorize_member(
            tid, "lead", second["generation"], second["id"], f"{second['id']}:1", "0.95"
        )
    assert cast(dict[str, Any], caught.value.detail)["code"] == "ctf_budget_exhausted"
    snapshot = await ctf.state(tid)
    assert snapshot["task"]["usage"]["cost"] == "0.12"
    assert next(t for t in snapshot["turns"] if t["id"] == first["id"])["usage"]["cost"] == "0.12"
    assert not snapshot["task"]["ctf_control"].get("budget_unknown_reservations")
    assert not snapshot["task"]["ctf_control"].get("budget_reservations")
