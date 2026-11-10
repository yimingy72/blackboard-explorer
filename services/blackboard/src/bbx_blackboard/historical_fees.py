"""Read settled and unconfirmed AgentGateway-era costs from local storage only."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import Numeric, cast, func, select

from bbx_blackboard.auth import require_task_reader
from bbx_blackboard.store import schema as s

router = APIRouter(tags=["historical-fees"])


@router.get("/api/tasks/{task_id}/gateway-model/review-costs")
async def review_costs(request: Request, response: Response, task_id: UUID) -> dict:
    if require_task_reader(request, task_id).kind != "user":
        raise HTTPException(403, "User token required")
    response.headers["Cache-Control"] = "no-store"
    attempts, costs = s.gateway_execution_attempts, s.native_request_costs
    rows = attempts.outerjoin(
        costs,
        (costs.c.accounting_source_id == attempts.c.accounting_source_id)
        & (costs.c.native_log_id == attempts.c.native_log_id)
        & (costs.c.state == "known"),
    )
    filters = [attempts.c.task_id == task_id, attempts.c.purpose == "review"]
    async with request.app.state.engine.connect() as conn:
        if await conn.scalar(select(s.tasks.c.id).where(s.tasks.c.id == task_id)) is None:
            raise HTTPException(404, "Task not found")
        amount, pending, count = (
            await conn.execute(
                select(
                    func.sum(cast(costs.c.cost_usd, Numeric)),
                    func.count().filter(costs.c.cost_usd.is_(None)),
                    func.count(),
                )
                .select_from(rows)
                .where(*filters)
            )
        ).one()
        evidence = (
            await conn.execute(
                select(
                    attempts.c.id, attempts.c.attributes, attempts.c.created_at, costs.c.cost_usd
                )
                .select_from(rows)
                .where(*filters)
                .order_by(attempts.c.created_at.desc(), attempts.c.id.desc())
                .limit(100)
            )
        ).mappings()
        calls = [
            {
                "attempt_id": str(row["id"]),
                "site_id": row["attributes"].get("bbx_site_id") or "historical",
                "task_id": str(task_id),
                "agent_id": row["attributes"].get("bbx_agent_id"),
                "conversation_id": row["attributes"].get("bbx_conversation_id"),
                "model_alias": row["attributes"].get("bbx_model_alias"),
                "at": row["created_at"].isoformat(),
                "cost_usd": row["cost_usd"],
            }
            for row in evidence
        ]
    return {
        "confirmed_usd": str(amount) if amount is not None else None,
        "pending": pending,
        "more": count > len(calls),
        "calls": calls,
    }
