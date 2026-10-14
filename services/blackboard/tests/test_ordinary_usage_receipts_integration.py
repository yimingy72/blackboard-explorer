"""Confirmed ordinary usage is priced once, including late and closing receipts."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from bbx_blackboard.conversations import Conversations
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.store import schema as s
from bbx_contracts.billing import effective_price, token_cost
from bbx_contracts.heartbeat import UNKNOWN_USAGE_WARNING, HeartbeatRequest, OrdinaryUsageReceipt
from bbx_contracts.models import ModelConfig, Usage
from fastapi import HTTPException
from sqlalchemy import select, update
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url
from test_store_integration import task

pytestmark = pytest.mark.integration


async def prepared(board, *, role="explore", persisted=True):
    profile = await ProfileStore(board.repo.engine).ensure_default(
        Path(__file__).resolve().parents[3] / "profiles/default"
    )
    tid = await task(board)
    aid = await board.register_agent(tid, role)
    request = HeartbeatRequest(
        request_id=uuid4(),
        expected_run_number=1,
        expected_derive_round=1 if role == "derive" else None,
        requested_at=datetime(2026, 10, 14, 8, tzinfo=UTC),
        steps=1,
        context_tokens=1000,
        last_seen_version=1,
        usage=Usage(cache_miss_tokens=1000, output_tokens=300, cost=Decimal("999")),
    )
    receipt = OrdinaryUsageReceipt.model_validate(
        {
            "request": request.model_dump(mode="json"),
            "task_id": str(tid),
            "agent_id": aid,
            "task_type": role,
            "profile_name": "default",
            "profile_version": 1,
        }
    )
    if persisted:
        await save(board, tid, aid, receipt)
    model = ModelConfig.model_validate(profile["models"][role])
    assert request.requested_at is not None
    selected, _ = effective_price(model, request.requested_at)
    expected, _ = token_cost(request.usage, selected)
    return tid, aid, receipt, expected


async def save(board, tid, aid, receipt, revision=0):
    session = {
        "state": {
            "in_memory": {
                "messages": [{"role": "assistant", "contents": [{"type": "text", "text": "done"}]}]
            },
            "bbx_usage_outbox": [receipt.model_dump(mode="json")],
        }
    }
    async with board.repo.engine.begin() as conn:
        await board.repo.lock(conn, tid)
        await Conversations(board.repo.engine)._save_session(
            conn, tid, aid, session, "role", "native", revision
        )


async def bill(board, tid, aid, receipt, **changes):
    body = {**receipt.request.model_dump(), **changes}
    return await board.heartbeat(tid, aid, **body)


async def test_receipt_requires_durable_actor_proof_and_rejects_request_payload_conflicts(
    board_service,
):
    tid, aid, receipt, amount = await prepared(board_service, persisted=False)
    with pytest.raises(HTTPException) as absent:
        await bill(board_service, tid, aid, receipt)
    assert absent.value.status_code == 409
    assert (await board_service.state(tid))["task"]["usage"] == {}
    await save(board_service, tid, aid, receipt)
    saved = await Conversations(board_service.repo.engine).get_session(tid, aid)
    assert saved["usage_receipt_protocol"] == 1
    first = await bill(board_service, tid, aid, receipt)
    second = await bill(board_service, tid, aid, receipt)
    assert first["confirmed"] and second["confirmed"] and second["events"] == []
    assert first["cost"] == second["cost"] == amount and amount != Decimal("999")
    snapshot = await board_service.state(tid)
    assert snapshot["task"]["usage"]["output_tokens"] == 300
    assert snapshot["agents"][aid]["steps"] == 1
    with pytest.raises(HTTPException) as conflict:
        await bill(board_service, tid, aid, receipt, context_tokens=1001)
    assert conflict.value.status_code == 409
    other = await board_service.register_agent(tid, "explore")
    with pytest.raises(HTTPException) as foreign:
        await bill(board_service, tid, other, receipt)
    assert foreign.value.status_code == 409
    assert (await board_service.state(tid))["task"]["usage"]["output_tokens"] == 300


async def test_incomplete_usage_confirms_known_tokens_and_preserves_warning_on_duplicate(
    board_service,
):
    tid, aid, receipt, _ = await prepared(board_service, persisted=False)
    receipt.request.usage_known = False
    receipt.request.usage.cache_miss_tokens = 0
    await save(board_service, tid, aid, receipt)
    first = await bill(board_service, tid, aid, receipt)
    second = await bill(board_service, tid, aid, receipt)
    for ack in (first, second):
        assert ack["confirmed"] is True and ack["usage_known"] is False
        assert ack["warning"] == UNKNOWN_USAGE_WARNING
    assert first["events"][0]["payload"]["usage_known"] is False
    assert first["events"][0]["payload"]["usage_warning"] == UNKNOWN_USAGE_WARNING
    assert first["cost"] > 0 and second["events"] == []
    snapshot = await board_service.state(tid)
    assert snapshot["task"]["usage"]["output_tokens"] == 300
    assert snapshot["agents"][aid]["steps"] == 1
    with pytest.raises(HTTPException) as changed:
        await bill(board_service, tid, aid, receipt, usage_known=True)
    assert changed.value.status_code == 409


@pytest.mark.parametrize("late", ["round", "terminal"])
async def test_late_receipt_accounts_cost_without_touching_current_control_or_failure(
    board_service, late
):
    tid, aid, receipt, amount = await prepared(board_service, role="derive")
    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.agent_runs)
            .where(s.agent_runs.c.task_id == tid, s.agent_runs.c.id == aid)
            .values(
                derive_round=2 if late == "round" else 1,
                status="running" if late == "round" else "finished",
                steps=7,
                context_tokens=123,
                last_seen_version=99,
            )
        )
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(failure_streak=2, failure_window_kind="model_transient")
        )
    before = (await board_service.state(tid))["agents"][aid]
    result = await bill(board_service, tid, aid, receipt)
    assert result["cost"] == amount
    snapshot = await board_service.state(tid)
    after = snapshot["agents"][aid]
    for key in (
        "steps",
        "context_tokens",
        "last_seen_version",
        "last_heartbeat_at",
        "status",
        "derive_round",
    ):
        assert before[key] == after[key]
    assert snapshot["task"]["failure_streak"] == 2
    assert snapshot["task"]["usage"]["output_tokens"] == 300
    assert result["events"][0]["payload"]["accounting_only"] is True
    await bill(board_service, tid, aid, receipt)
    assert (await board_service.state(tid))["task"]["usage"]["output_tokens"] == 300
    await board_service.replay(tid)
    assert (await board_service.state(tid))["task"]["usage"]["output_tokens"] == 300


async def test_finish_and_terminal_transition_settle_confirmed_usage_before_closing(board_service):
    tid, aid, _, amount = await prepared(board_service)
    await board_service.finish_agent(
        tid, aid, {"accepted": False, "reason": "control failure"}, "runtime_error"
    )
    snapshot = await board_service.state(tid)
    assert Decimal(snapshot["task"]["usage"]["cost"]) == amount
    assert snapshot["agents"][aid]["steps"] == 1
    await board_service.transition(tid, "failed", reason="Synthetic close")
    assert (await board_service.state(tid))["task"]["usage"]["output_tokens"] == 300


async def test_final_report_settles_usage_saved_after_entering_closing(board_service):
    tid, aid, receipt, amount = await prepared(board_service, persisted=False)
    await board_service.transition(tid, "closing", reason="Synthetic closing")
    close = await board_service.register_agent(tid, "close", close_mode="final")
    # A confirmed response can arrive after the initial closing transition.
    await save(board_service, tid, aid, receipt)
    await board_service.submit_close(
        tid,
        close,
        {"verdicts": [{"id": "A1", "verdict": "unmet", "reason": "pending", "missing": "cause"}]},
        report_uri="reports/final",
    )
    snapshot = await board_service.state(tid)
    assert snapshot["task"]["status"] == "finished"
    assert Decimal(snapshot["task"]["usage"]["cost"]) == amount
    assert snapshot["agents"][aid]["steps"] == 1
    async with board_service.repo.engine.connect() as conn:
        events = (
            (await conn.execute(select(s.events).where(s.events.c.task_id == tid))).mappings().all()
        )
    billed = next(e["version"] for e in events if e["type"] == "budget.updated")
    report = next(e["version"] for e in events if e["type"] == "task.report")
    finished = next(e["version"] for e in events if e["type"] == "task.finished")
    assert billed < report < finished


async def test_legacy_heartbeat_remains_additive_without_claiming_receipt_confirmation(
    board_service,
):
    tid, aid, _, _ = await prepared(board_service, persisted=False)
    body = dict(steps=1, context_tokens=10, usage={"output_tokens": 5}, last_seen_version=0)
    first = await board_service.heartbeat(tid, aid, **body)
    second = await board_service.heartbeat(tid, aid, **body)
    assert "confirmed" not in first and "confirmed" not in second
    snapshot = await board_service.state(tid)
    assert snapshot["agents"][aid]["steps"] == 2
    assert snapshot["task"]["usage"]["output_tokens"] == 10


async def terminal_archive(board, tid, aid):
    await board.finish_agent(tid, aid, {"accepted": False, "reason": "ended"}, "runtime_error")
    await board.transition(tid, "failed", reason="Synthetic stop")
    await board.record_archive(tid, f"workspace/{tid}.tar.zst", 0, "none")
    await board.record_cleanup(tid)


async def test_old_run_receipt_uses_original_profile_without_changing_new_agent_control(
    board_service,
):
    tid, aid, receipt, amount = await prepared(board_service, persisted=False)
    await terminal_archive(board_service, tid, aid)
    await board_service.resume(tid, uuid4(), Decimal("1"), 10)
    await board_service.transition(tid, "running")
    fresh = await board_service.register_agent(tid, "explore")
    before = (await board_service.state(tid))["agents"][fresh]
    await save(board_service, tid, aid, receipt)
    assert (await bill(board_service, tid, aid, receipt))["cost"] == amount
    after = await board_service.state(tid)
    assert after["agents"][fresh] == before
    assert after["agents"][aid]["steps"] == 0
    assert after["task"]["usage"]["output_tokens"] == 300


async def test_rejected_resume_commits_known_fees_and_retains_the_original_task_run(board_service):
    tid, aid, receipt, amount = await prepared(board_service, persisted=False)
    assert amount > 0
    await terminal_archive(board_service, tid, aid)
    await save(board_service, tid, aid, receipt)
    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(budget={"max_cost": str(amount / 2), "max_minutes": 60})
        )
    with pytest.raises(RuleViolation) as exhausted:
        await board_service.resume(tid, uuid4(), Decimal("0"), 0)
    assert exhausted.value.code == "resume_budget_exhausted"
    snapshot = await board_service.state(tid)
    assert snapshot["task"]["run_number"] == 1 and snapshot["task"]["status"] == "failed"
    assert Decimal(snapshot["task"]["usage"]["cost"]) == amount
    assert snapshot["agents"][aid]["steps"] == 0
