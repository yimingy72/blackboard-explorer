"""Retired checkpoint writers converge without pretending unsaved state is durable."""

from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import update
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_turn, history, team
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_review_integration import ReviewObjects
from test_ctf_usage_outbox_integration import saved_dispatch

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def closing_turn(ctf):
    tid = await team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "close-deadlock")
    await ctf.request_finish(
        tid,
        "lead",
        str(uuid4()),
        {"end_reason": "goal_claimed", "summary": "Fake completed work", "lead_claim": True},
        generation=turn["generation"],
        turn_id=turn["id"],
    )
    return tid, turn


async def finish(ctf, tid, turn):
    return await ctf.finish_turn(
        tid, "lead", turn["id"], turn["generation"], "completed", "Fake final answer"
    )


async def test_settlement_retires_barrier_and_fails_close_instead_of_losing_snapshot(ctf):
    tid, turn = await closing_turn(ctf)
    await ctf.checkpoint_failed(tid, "lead", turn["id"], turn["generation"])
    await ctf.bill_usage(tid, "lead", turn["id"], turn["generation"], "fake-usage", {"cost": "0.1"})
    await finish(ctf, tid, turn)
    await finish(ctf, tid, turn)
    state = await ctf.state(tid)
    control = state["task"]["ctf_control"]
    assert control["unresolved_checkpoints"] == []
    assert list(control["checkpoint_failures"]) == [turn["id"]]
    assert state["task"]["usage"]["cost"] == "0.1"
    assert await ctf.finalize_close(tid, True) == {"phase": "closed", "status": "failed"}
    state = await ctf.state(tid)
    assert state["task"]["ctf_conclusion"]["end_reason"] == "system_failure"
    assert turn["id"] in state["task"]["ctf_conclusion"]["unresolved_items"][0]
    assert state["turns"][0]["final_answer"] == "Fake final answer"
    with pytest.raises(HTTPException):
        await ctf.checkpoint(tid, "lead", turn["id"], turn["generation"], history([]), "", 0, [])


async def test_running_barrier_remains_even_when_close_failure_is_requested(ctf):
    tid, turn = await closing_turn(ctf)
    await ctf.checkpoint_failed(tid, "lead", turn["id"], turn["generation"])
    with pytest.raises(HTTPException, match="checkpoint"):
        await ctf.finalize_close(tid, True, checkpoint_failure="Retry deadline exceeded")
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["unresolved_checkpoints"] == [turn["id"]]
    assert state["turns"][0]["status"] == "running"


async def test_confirmed_retry_preserves_successful_close(ctf):
    tid, turn = await closing_turn(ctf)
    await ctf.checkpoint_failed(tid, "lead", turn["id"], turn["generation"])
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    await ctf.checkpoint(
        tid,
        "lead",
        turn["id"],
        turn["generation"],
        history(messages),
        "prompt",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
    )
    await finish(ctf, tid, turn)
    assert await ctf.finalize_close(tid, True) == {"phase": "closed", "status": "finished"}
    assert not (await ctf.state(tid))["task"]["ctf_control"].get("checkpoint_failures")


@pytest.mark.parametrize("repair", ["finish", "finalize"])
async def test_legacy_finished_barrier_is_repaired_without_reopening_writer(ctf, repair):
    tid, turn = await closing_turn(ctf)
    await finish(ctf, tid, turn)
    state = await ctf.state(tid)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={**state["task"]["ctf_control"], "unresolved_checkpoints": [turn["id"]]}
            )
        )
    if repair == "finish":
        with pytest.raises(HTTPException, match="Stale settlement"):
            await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"] + 1, "completed")
        assert (await ctf.state(tid))["task"]["ctf_control"]["unresolved_checkpoints"]
        await finish(ctf, tid, turn)
    assert await ctf.finalize_close(tid, True) == {"phase": "closed", "status": "failed"}
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["unresolved_checkpoints"] == []
    assert turn["id"] in state["task"]["ctf_control"]["checkpoint_failures"]
    assert state["turns"][0]["final_answer"] == "Fake final answer"


async def test_timeout_after_drained_settlement_is_explicit_failure(ctf):
    tid, turn = await closing_turn(ctf)
    await finish(ctf, tid, turn)
    assert await ctf.finalize_close(
        tid, True, checkpoint_failure="Local usage was not confirmed"
    ) == {"phase": "closed", "status": "failed"}
    assert (await ctf.state(tid))["task"]["ctf_conclusion"]["unresolved_items"] == [
        "Local usage was not confirmed"
    ]


@pytest.mark.parametrize("durable_usage", [True, False])
async def test_retired_barrier_preserves_confirmed_and_unknown_usage(ctf, durable_usage):
    tid, turn, _, entry = await saved_dispatch(ctf, entries=durable_usage)
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "partial", "summary": "Fake stop"}
    )
    await ctf.checkpoint_failed(tid, "lead", turn["id"], turn["generation"])
    await finish(ctf, tid, turn)
    assert await ctf.finalize_close(tid, True) == {"phase": "closed", "status": "failed"}
    snapshot = await ctf.state(tid)
    control = snapshot["task"]["ctf_control"]
    assert not control.get("budget_reservations")
    if durable_usage:
        assert snapshot["task"]["usage"]["cost"] == "0.12"
        assert not control.get("budget_unknown_reservations")
        assert (await ctf.reconcile_usage_outbox(tid, "lead"))["request_ids"] == [
            entry["request_id"]
        ]
        assert (await ctf.state(tid))["task"]["usage"]["cost"] == "0.12"
    else:
        assert not snapshot["task"]["usage"].get("cost")
        assert control["budget_unknown_reservations"][entry["reservation_id"]]["cost"] == "0.8"
    saved = await ctf.sessions.get_session(tid, "lead")
    assert saved["revision"] == 1


async def test_explicit_resume_retains_fault_history_without_poisoning_next_close(ctf):
    tid, turn = await closing_turn(ctf)
    await ctf.checkpoint_failed(tid, "lead", turn["id"], turn["generation"])
    await finish(ctf, tid, turn)
    await ctf.finalize_close(tid, True)
    before = (await ctf.state(tid))["task"]
    assert before["ctf_control"]["close_failure"]
    # Simulate operator recovery of the archive before allowing an explicit resume.
    objects = ReviewObjects()
    ctf.objects = objects
    uri = f"workspace/{tid}.tar.zst"
    objects.files[uri] = b"recovered fake archive"
    await ctf.record_archive(tid, uri, len(objects.files[uri]))
    await ctf.record_cleanup(tid)
    await ctf.resume(tid, uuid4(), "1", 1)
    resumed = (await ctf.state(tid))["task"]
    control = resumed["ctf_control"]
    assert "close_failure" not in control
    assert control["checkpoint_failures"] == before["ctf_control"]["checkpoint_failures"]
    assert control["conclusion_history"][-1]["conclusion"]["end_reason"] == "system_failure"
    await ctf.start(tid)
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "partial", "summary": "Recovered run"}
    )
    assert await ctf.finalize_close(tid, True) == {"phase": "closed", "status": "finished"}
    assert not (await ctf.state(tid))["task"]["ctf_control"].get("close_failure")


@pytest.mark.parametrize("operation", ["stop", "remove"])
async def test_closing_stop_escalation_requires_matching_proof_and_retains_unknown_usage(
    ctf, operation
):
    tid = await team(ctf)
    aid = (await ctf.create_member(tid, "user", "Stopping", str(uuid4())))["id"]
    turn = await ctf_turn(ctf, tid, aid)
    reservation = f"{turn['id']}:1"
    await ctf.authorize_member(tid, aid, turn["generation"], turn["id"], reservation, "0.8")
    await ctf.mark_reservation_sent(tid, aid, turn["generation"], turn["id"], reservation)
    await ctf.record_execution(tid, aid, turn["id"], turn["generation"], "fake-boot")
    await ctf.checkpoint_failed(tid, aid, turn["id"], turn["generation"])
    request = str(uuid4())
    await ctf.request_member_operation(tid, "user", aid, operation, request)
    proof = {"boot_id": "fake-boot", "generation": turn["generation"], "drained": True}
    with pytest.raises(HTTPException, match="requires closing"):
        await ctf.complete_member_operation(
            tid, aid, request, proof, checkpoint_failure="Deadline exceeded"
        )
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "partial", "summary": "Stop"}
    )
    with pytest.raises(HTTPException, match="checkpoint"):
        await ctf.complete_member_operation(tid, aid, request, proof)
    with pytest.raises(HTTPException, match="proof mismatch"):
        await ctf.complete_member_operation(
            tid,
            aid,
            request,
            {**proof, "boot_id": "stale-boot"},
            checkpoint_failure="Deadline exceeded",
        )
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid)
            .values(generation=turn["generation"] + 1)
        )
    with pytest.raises(HTTPException, match="Stale checkpoint failure settlement"):
        await ctf.complete_member_operation(
            tid, aid, request, proof, checkpoint_failure="Deadline exceeded"
        )
    snapshot = await ctf.state(tid)
    assert snapshot["task"]["ctf_control"]["unresolved_checkpoints"] == [turn["id"]]
    assert not snapshot["task"]["ctf_control"].get("close_failure")
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid)
            .values(generation=turn["generation"])
        )
    await ctf.complete_member_operation(
        tid, aid, request, proof, checkpoint_failure="Deadline exceeded"
    )
    await ctf.recover(tid)
    assert await ctf.finalize_close(tid, True) == {"phase": "closed", "status": "failed"}
    snapshot = await ctf.state(tid)
    control = snapshot["task"]["ctf_control"]
    member = next(m for m in snapshot["members"] if m["id"] == aid)
    assert member["pending_operation"] is None
    assert member["lifecycle"] == ("removed" if operation == "remove" else "stopped")
    assert turn["id"] in control["checkpoint_failures"]
    assert control["budget_unknown_reservations"][reservation]["cost"] == "0.8"
    assert "Deadline exceeded" in snapshot["task"]["ctf_conclusion"]["unresolved_items"]


@pytest.mark.parametrize("prior_recoveries", [0, 2])
async def test_recover_repairs_finished_orphan_pointer_and_fences_old_writer(ctf, prior_recoveries):
    tid = await team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "original")
    await finish(ctf, tid, turn)
    snapshot = await ctf.state(tid)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == "lead")
            .values(
                current_turn_id=turn["id"],
                generation=turn["generation"] + prior_recoveries,
                run_state="running",
            )
        )
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **snapshot["task"]["ctf_control"],
                    "unresolved_checkpoints": [turn["id"]],
                }
            )
        )
    recovered = await ctf.recover(tid)
    member = recovered["members"][0]
    assert member["current_turn_id"] is None and member["run_state"] == "idle"
    assert member["generation"] == turn["generation"] + prior_recoveries + 1
    assert recovered["turns"][0]["final_answer"] == "Fake final answer"
    with pytest.raises(HTTPException):
        await ctf.checkpoint(tid, "lead", turn["id"], turn["generation"], history([]), "", 0, [])
    await ctf.post_message(tid, "user", "lead", "New explicit work", str(uuid4()), "instruction")
    next_turn = await ctf.claim_turn(tid, "lead", "new")
    assert next_turn["generation"] == member["generation"] + 1


async def test_orphan_pointer_repair_never_overwrites_a_new_running_writer(ctf):
    tid = await team(ctf)
    old = await ctf.claim_turn(tid, "lead", "original")
    await finish(ctf, tid, old)
    new = await ctf_turn(ctf, tid)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == "lead")
            .values(current_turn_id=old["id"])
        )
    with pytest.raises(HTTPException, match="Running writer contradicts"):
        await ctf.recover(tid)
    snapshot = await ctf.state(tid)
    assert snapshot["members"][0]["generation"] == new["generation"]
    assert next(t for t in snapshot["turns"] if t["id"] == new["id"])["status"] == "running"
