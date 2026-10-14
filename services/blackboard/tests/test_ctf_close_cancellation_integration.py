"""Closing records cancellation intent atomically without claiming cancelled output."""

import asyncio
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import select, update
from test_ctf_integration import board_team, ctf_turn
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def cancellation_events(ctf, tid):
    async with ctf.repo.engine.connect() as conn:
        payloads = (
            await conn.execute(
                select(s.events.c.payload).where(
                    s.events.c.task_id == tid, s.events.c.type == "ctf.coordination.reminded"
                )
            )
        ).scalars()
        return [item for item in payloads if item.get("kind") == "closing_turn_cancellation"]


@pytest.mark.parametrize("review", [False, True])
async def test_close_cancellation_request_lists_only_active_execution_turns_once(ctf, review):
    tid, owner, helper = await board_team(ctf)
    lead = await ctf_turn(ctf, tid)
    await ctf.finish_turn(tid, "lead", lead["id"], lead["generation"], "completed", "Saved")
    if review:
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.ctf_turns)
                .where(s.ctf_turns.c.task_id == tid, s.ctf_turns.c.id == helper["turn_id"])
                .values(purpose="review")
            )
    before = await ctf.state(tid)
    request_id = str(uuid4())
    request = {"end_reason": "system_failure", "summary": "Scripted control failure"}
    results = await asyncio.gather(
        ctf.request_finish(tid, "user", request_id, request),
        ctf.request_finish(tid, "user", request_id, request),
    )
    assert results == [{"operation_id": request_id, "phase": "closing"}] * 2
    events = await cancellation_events(ctf, tid)
    actors = [owner] if review else [owner, helper]
    turns = [
        {
            "turn_id": actor["turn_id"],
            "member_id": actor["actor"],
            "generation": actor["generation"],
        }
        for actor in actors
    ]
    assert events == [
        {
            "kind": "closing_turn_cancellation",
            "status": "cancellation_requested",
            "reason": "system_failure",
            "operation_id": request_id,
            "epoch": before["task"]["ctf_control"]["epoch"] + 1,
            "run_number": before["task"]["run_number"],
            "turn_count": len(turns),
            "turn_ids": [turn["turn_id"] for turn in turns],
            "turns": turns,
        }
    ]
    state = await ctf.state(tid)
    assert state["turns"] == before["turns"]
    assert state["task"]["status"] == before["task"]["status"]
    with pytest.raises(HTTPException, match="Closing already requested"):
        await ctf.request_finish(tid, "user", str(uuid4()), request)
    assert await cancellation_events(ctf, tid) == events


async def test_close_cancellation_event_failure_rolls_back_transition_and_zero_count(
    ctf, monkeypatch
):
    from test_ctf_integration import team

    tid = await team(ctf)
    emit = ctf._emit

    async def fail_event(conn, tid, kind, actor, payload):
        if kind == "coordination.reminded":
            raise RuntimeError("Scripted event failure")
        return await emit(conn, tid, kind, actor, payload)

    monkeypatch.setattr(ctf, "_emit", fail_event)
    request_id = str(uuid4())
    request = {"end_reason": "user_stop", "summary": "Stop"}
    with pytest.raises(RuntimeError, match="Scripted event failure"):
        await ctf.request_finish(tid, "user", request_id, request)
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["phase"] == "running"
    assert not state["task"]["ctf_conclusion"]
    assert await cancellation_events(ctf, tid) == []
    monkeypatch.setattr(ctf, "_emit", emit)
    await ctf.request_finish(tid, "user", request_id, request)
    event = (await cancellation_events(ctf, tid))[0]
    assert event["turn_count"] == 0 and event["turn_ids"] == [] and event["turns"] == []


@pytest.mark.parametrize("metadata", [False, True])
async def test_failure_diagnostic_survives_conclusion_storage_and_events(ctf, metadata):
    from bbx_blackboard.store.repository import now
    from bbx_contracts.ctf import CtfConclusion
    from test_ctf_integration import team

    tid = await team(ctf)
    turn = await ctf_turn(ctf, tid)
    diagnostic = {
        "error_type": "RuntimeError",
        "phase": "turn",
        "occurred_at": now().isoformat(),
        "correlation_id": str(uuid4()),
        "summary": "Coordinator stopped during turn; see logs for correlation_id.",
    }
    if metadata:
        diagnostic.update(
            task_id=str(tid), member_id="lead", turn_id=turn["id"], generation=turn["generation"]
        )
    expected = CtfConclusion.model_validate(
        {"end_reason": "system_failure", "summary": diagnostic["summary"], "failure": diagnostic}
    ).model_dump(mode="json")["failure"]
    await ctf.request_finish(
        tid,
        "system",
        str(uuid4()),
        {"end_reason": "system_failure", "summary": diagnostic["summary"], "failure": diagnostic},
    )
    stored = (await ctf.state(tid))["task"]["ctf_conclusion"]
    assert stored["failure"] == expected
    assert CtfConclusion.model_validate(stored).failure is not None
    await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "interrupted")
    await ctf.finalize_close(tid, True)
    async with ctf.repo.engine.connect() as conn:
        conclusions = (
            (
                await conn.execute(
                    select(s.events.c.payload)
                    .where(
                        s.events.c.task_id == tid,
                        s.events.c.type.in_(
                            ["ctf.conclusion.requested", "ctf.conclusion.finalized"]
                        ),
                    )
                    .order_by(s.events.c.version)
                )
            )
            .scalars()
            .all()
        )
    assert len(conclusions) == 2
    for event in conclusions:
        failure = CtfConclusion.model_validate(event["conclusion"]).failure
        assert failure is not None
        assert event["conclusion"]["failure"] == expected
