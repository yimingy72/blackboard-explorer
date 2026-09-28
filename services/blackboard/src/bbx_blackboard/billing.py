"""Explicit terminal-task estimate reconciliation without changing original usage events."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from bbx_contracts.billing import (
    SCHEDULE_VERSION,
    effective_price,
    supports_deepseek_schedule,
    token_cost,
)
from bbx_contracts.models import ModelConfig, Usage
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from bbx_blackboard.auth import require_user_or_service
from bbx_blackboard.domain.rules import event
from bbx_blackboard.store import schema as s

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class ReconcileBody(BaseModel):
    expected_version: int = Field(ge=0)


def estimate_calls(
    agents: dict[str, Any], events: list[dict[str, Any]], models: dict[str, Any]
) -> dict[str, Any]:
    costs: dict[str, Decimal] = {aid: Decimal(0) for aid in agents}
    tokens: dict[str, dict[str, int]] = {aid: {} for aid in agents}
    legacy = 0
    calls = 0
    last_version = 0
    for item in events:
        payload = item["payload"]
        if item["type"] != "agent.progress" or "usage" not in payload:
            continue
        aid = payload["agent_id"]
        usage = Usage.model_validate(payload["usage"])
        model = ModelConfig.model_validate(models[agents[aid]["task_type"]])
        if not supports_deepseek_schedule(model.base_url, model.model):
            raise ValueError("任务含非官方 DeepSeek 模型，不适用此时段校正")
        saved = payload.get("pricing") or {}
        stamp = saved.get("requested_at") or item["created_at"]
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp)
        price, _ = effective_price(model, stamp, mode_override="deepseek_schedule")
        cost, warning = token_cost(usage, price)
        if warning:
            raise ValueError("原任务价格表不完整，不能重算费用")
        costs[aid] += cost
        for key in ("cache_hit_tokens", "cache_miss_tokens", "output_tokens", "reasoning_tokens"):
            tokens[aid][key] = tokens[aid].get(key, 0) + int(getattr(usage, key))
        legacy += int(saved.get("timestamp_basis") != "request_start")
        calls += int(payload.get("steps") or 0)
        last_version = item["version"]
    for aid, agent in agents.items():
        if any(int(agent["usage"].get(key) or 0) != value for key, value in tokens[aid].items()):
            raise ValueError("用量事件与账本不一致，暂不能安全校正")
        if int(agent.get("steps") or 0) and not tokens[aid]:
            raise ValueError("任务缺少逐调用用量事件，不能校正")
    return {
        "cost": str(sum(costs.values(), Decimal(0))),
        "agent_costs": {aid: str(cost) for aid, cost in costs.items()},
        "calls": calls,
        "last_usage_version": last_version,
        "legacy_timestamp_calls": legacy,
        "schedule_version": SCHEDULE_VERSION,
        "warning": "历史调用未保存请求开始时间，按用量事件时间估算；跨费率边界可能有差异"
        if legacy
        else None,
    }


async def _reconcile(request: Request, tid: UUID, body: ReconcileBody | None) -> dict[str, Any]:
    require_user_or_service(request)
    service = request.app.state.board_service
    repo = service.repo
    written = []
    async with repo.engine.begin() as conn:
        await repo.lock(conn, tid)
        state = await repo.load(conn, tid)
        task = state.task
        if task.get("deleting") or task["status"] not in {"finished", "failed", "stopped"}:
            raise HTTPException(409, "请等待任务结束后校正费用")
        profile = await request.app.state.profile_store.get(
            task["agent_profile"], task["agent_profile_version"]
        )
        events = [
            dict(row)
            for row in (
                await conn.execute(
                    select(s.events).where(s.events.c.task_id == tid).order_by(s.events.c.version)
                )
            ).mappings()
        ]
        try:
            estimate = estimate_calls(state.agents, events, profile["models"])
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        result = {
            **estimate,
            "version": task["version"],
            "previous_cost": str(task["usage"].get("cost") or 0),
            "applied": False,
        }
        previous = next(
            (e["payload"] for e in reversed(events) if e["type"] == "cost.reconciled"), None
        )
        already_applied = (
            previous
            and previous["last_usage_version"] == estimate["last_usage_version"]
            and previous["schedule_version"] == estimate["schedule_version"]
        )
        if body is not None and not already_applied:
            if body.expected_version != task["version"]:
                raise HTTPException(409, "任务记录已变化，请重新预览")
            payload = {
                **result,
                "billing_mode": "deepseek_schedule",
                "previous_agent_costs": {
                    aid: str(agent["usage"].get("cost") or 0) for aid, agent in state.agents.items()
                },
            }
            written = await repo.append(conn, tid, [event("cost.reconciled", "user", payload)])
            result["version"] = written[-1]["version"]
        result["applied"] = body is not None or bool(already_applied)
    if written:
        await repo.notify(tid, written[-1]["version"])
    return result


@router.get("/{task_id}/cost-reconciliation")
async def preview(request: Request, task_id: UUID) -> dict[str, Any]:
    return await _reconcile(request, task_id, None)


@router.post("/{task_id}/cost-reconciliation")
async def reconcile(request: Request, task_id: UUID, body: ReconcileBody) -> dict[str, Any]:
    return await _reconcile(request, task_id, body)
