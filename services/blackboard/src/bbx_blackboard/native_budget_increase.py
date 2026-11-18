"""Increase a paused Pi Task's original USD threshold exactly once."""

from decimal import Decimal

from bbx_contracts.native_gateway import native_usd_amount
from fastapi import HTTPException
from sqlalchemy import update

from bbx_blackboard.board_events import digest, emit, prior
from bbx_blackboard.pi_ledger import pi_totals, task_pi_model
from bbx_blackboard.store import schema as s


async def increase_budget(pause, tid, request):
    repo = pause.repo
    hashed = digest({"operation": "native_budget_increase", **request.model_dump(mode="json")})
    async with repo.engine.begin() as conn:
        await repo.lock(conn, tid)
        if previous := await prior(conn, tid, "user", request.request_id, hashed):
            return previous
        frozen = await task_pi_model(conn, tid)
        resource = await pause.bootstrap._get(conn, tid)
        if (
            not resource
            or resource["data"].get("mcp_transport") != "direct-v2"
            or frozen is None
            or frozen[0] != pause.bootstrap.settings.pi_model_authority
        ):
            raise HTTPException(409, "Original execution cannot safely migrate to Pi")
        task, _, _, _ = await pause._confirmed_pause(conn, tid, request.pause_id)
        if request.additional_usd is None or task["budget"].get("currency") != "USD":
            raise HTTPException(422, "Pi budget increases use USD")
        total = native_usd_amount(Decimal(str(task["budget"]["max_cost"])) + request.additional_usd)
        spent, unknown = await pi_totals(conn, tid, pause.bootstrap.settings.pi_site_id)
        if unknown or total <= spent:
            raise HTTPException(
                409, "Confirm original fees and increase the limit above current use"
            )
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(budget={**task["budget"], "max_cost": str(total)})
        )
        return await emit(
            repo,
            conn,
            tid,
            "user",
            "board.request.linked",
            request.request_id,
            hashed,
            {
                "request_id": str(request.request_id),
                "pause_id": str(request.pause_id),
                "budget_currency": "USD",
                "threshold_usd": str(total),
            },
        )
