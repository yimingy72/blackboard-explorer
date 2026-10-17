"""Account confirmed ordinary Session receipts using existing progress events."""

from decimal import Decimal
from typing import Any

from bbx_contracts.billing import effective_price
from bbx_contracts.heartbeat import (
    UNKNOWN_USAGE_WARNING,
    HeartbeatRequest,
    OrdinaryUsageReceipt,
    heartbeat_digest,
)
from bbx_contracts.models import ModelConfig
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from bbx_blackboard.domain import BoardState, calculate_cost, decide
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import agent_execution_matches, row


def check(condition: bool, message: str) -> None:
    if not condition:
        raise HTTPException(409, {"code": "usage_receipt_conflict", "message": message})


async def saved_receipts(conn, tid, aid) -> list[dict[str, Any]]:
    values = (
        await conn.execute(
            select(s.agent_sessions.c.session["state"]["bbx_usage_outbox"]).where(
                s.agent_sessions.c.task_id == tid, s.agent_sessions.c.agent_id == aid
            )
        )
    ).scalar_one_or_none()
    check(values is None or isinstance(values, list), "Invalid persisted usage outbox")
    check(
        all(
            isinstance(value, dict) and isinstance(value.get("request"), dict)
            for value in values or []
        ),
        "Invalid persisted usage receipt",
    )
    return values or []


def receipt_value(value) -> OrdinaryUsageReceipt:
    try:
        return OrdinaryUsageReceipt.model_validate(value)
    except ValidationError:
        raise HTTPException(
            409, {"code": "usage_receipt_conflict", "message": "Invalid persisted usage receipt"}
        ) from None


async def settle_receipt(service, conn, tid, aid, request: HeartbeatRequest):
    assert request.request_id is not None
    assert request.expected_run_number is not None
    assert request.requested_at is not None
    digest = heartbeat_digest(str(tid), aid, request.model_dump(mode="json"))
    request_id = str(request.request_id)
    existing = (
        (
            await conn.execute(
                select(s.events).where(
                    s.events.c.task_id == tid,
                    s.events.c.type == "agent.progress",
                    s.events.c.payload["request_id"].astext == request_id,
                )
            )
        )
        .mappings()
        .first()
    )
    if existing is not None:
        payload = existing["payload"]
        check(payload.get("usage_request_digest") == digest, "Usage request ID payload conflict")
        return {
            "events": [],
            "cost": Decimal(payload["usage"]["cost"]),
            "warning": payload.get("usage_warning"),
            "usage_known": payload.get("usage_known", True),
            "request_id": request_id,
            "receipt_digest": digest,
            "confirmed": True,
        }
    match = next(
        (
            value
            for value in await saved_receipts(conn, tid, aid)
            if value.get("request", {}).get("request_id") == request_id
        ),
        None,
    )
    check(match is not None, "Usage is absent from the confirmed Session")
    receipt = receipt_value(match)
    check(
        receipt.task_id == str(tid)
        and receipt.agent_id == aid
        and receipt.request.model_dump(mode="json") == request.model_dump(mode="json"),
        "Persisted usage request identity or payload mismatch",
    )
    task = await row(conn, s.tasks, tid)
    check(
        task.get("mode", "blackboard") == "blackboard" and not task["deleting"], "Task unavailable"
    )
    agent = await row(conn, s.agent_runs, tid, aid)
    check(receipt.task_type == agent["task_type"], "Usage role mismatch")
    source = task
    if request.expected_run_number != task["run_number"]:
        source = (
            (
                await conn.execute(
                    select(s.task_runs).where(
                        s.task_runs.c.task_id == tid,
                        s.task_runs.c.run_number == request.expected_run_number,
                    )
                )
            )
            .mappings()
            .first()
        )
        check(
            source is not None and request.expected_run_number < task["run_number"],
            "Usage belongs to an unknown task run",
        )
    assert source is not None
    check(
        source["agent_profile"] == receipt.profile_name
        and source["agent_profile_version"] == receipt.profile_version,
        "Usage profile does not match its original task run",
    )
    profile = (
        await conn.execute(
            select(s.agent_profiles.c.models).where(
                s.agent_profiles.c.name == receipt.profile_name,
                s.agent_profiles.c.version == receipt.profile_version,
            )
        )
    ).scalar_one()
    model = ModelConfig.model_validate(profile[agent["task_type"]])
    price, pricing = effective_price(
        model, request.requested_at, mode_override=receipt.billing_mode
    )
    amount, warning = calculate_cost(request.usage, price)
    if not request.usage_known:
        warning = UNKNOWN_USAGE_WARNING
    usage = request.usage.model_dump(mode="json")
    usage["cost"] = str(amount)
    current = (
        request.expected_run_number == task["run_number"]
        and agent["status"] in {"running", "concluding"}
        and task["status"] not in {"finished", "failed", "stopped"}
        and await agent_execution_matches(conn, tid, agent, request.expected_started_at)
        and (
            agent["task_type"] != "derive" or request.expected_derive_round == agent["derive_round"]
        )
    )
    if agent["task_type"] == "derive":
        check(
            request.expected_derive_round is not None
            and request.expected_derive_round <= agent["derive_round"],
            "Usage belongs to an unknown derive round",
        )
    data = {
        "agent_id": aid,
        "usage": usage,
        "pricing": {**pricing, "timestamp_basis": "request_start"},
        "request_id": request_id,
        "usage_request_digest": digest,
        "usage_warning": warning,
        "usage_known": request.usage_known,
        "receipt_scope": {
            "run_number": request.expected_run_number,
            "derive_round": request.expected_derive_round,
            "started_at": request.expected_started_at.isoformat()
            if request.expected_started_at
            else None,
        },
    }
    if current:
        data.update(
            steps=request.steps,
            context_tokens=request.context_tokens,
            last_seen_version=request.last_seen_version,
        )
    else:
        data["accounting_only"] = True
    events = decide(
        BoardState(task=task, agents={aid: agent}),
        "heartbeat" if current else "settle_usage",
        aid,
        data,
    )
    written = await service.repo.append(conn, tid, events)
    await service.repo.notify(tid, written[-1]["version"], connection=conn)
    return {
        "events": written,
        "cost": amount,
        "warning": warning,
        "usage_known": request.usage_known,
        "request_id": request_id,
        "receipt_digest": digest,
        "confirmed": True,
    }


async def reconcile_receipts(service, conn, tid, aid=None) -> bool:
    if aid is None:
        agents = (
            (
                await conn.execute(
                    select(s.agent_sessions.c.agent_id).where(s.agent_sessions.c.task_id == tid)
                )
            )
            .scalars()
            .all()
        )
    else:
        agents = [aid]
    changed = False
    for agent_id in agents:
        for value in await saved_receipts(conn, tid, agent_id):
            receipt = receipt_value(value)
            result = await settle_receipt(service, conn, tid, agent_id, receipt.request)
            changed = changed or bool(result["events"])
    return changed
