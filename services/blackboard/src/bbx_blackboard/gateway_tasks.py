"""Frozen AgentGateway references for new execution, separate from historic model settings."""

from decimal import Decimal

from bbx_contracts.gateway_models import GatewayModelSnapshot
from bbx_contracts.models import ModelConfig, Price
from bbx_contracts.native_gateway import native_usd_amount
from fastapi import HTTPException
from sqlalchemy import insert, select

from bbx_blackboard.gateway_maintenance import admission
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.schema import gateway_task_models


def validate_task_threshold(amount: Decimal) -> None:
    try:
        native_usd_amount(amount)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


def gateway_display_model(model: GatewayModelSnapshot) -> ModelConfig:
    """Derive existing role estimates in USD, with no transport authority."""
    rates = model.usd_rates
    return ModelConfig(
        provider="openai_compatible",
        model=model.alias,
        base_url="provider-default",
        reasoning_effort="none",
        supports_vision="image" in model.input_modalities,
        context_window=model.context_window,
        max_input_tokens=model.max_input_tokens,
        max_output_tokens=model.max_output_tokens,
        price=Price(
            billing_mode="fixed",
            currency="USD",
            off_peak=False,
            cache_hit_per_m=rates.cache_read,
            cache_miss_per_m=rates.input,
            output_per_m=rates.output,
        ),
    )


async def freeze_gateway_model(conn, task_id, model, generation):
    await admission(conn, generation)
    await conn.execute(
        insert(gateway_task_models).values(
            task_id=task_id,
            snapshot=model.model_dump(mode="json"),
            budget_currency="USD",
        )
    )


async def task_gateway_model(conn, task_id):
    item = (
        (
            await conn.execute(
                select(gateway_task_models).where(gateway_task_models.c.task_id == task_id)
            )
        )
        .mappings()
        .first()
    )
    if item is None:
        return None
    return (
        GatewayModelSnapshot.model_validate(item["snapshot"]),
        item["budget_currency"],
    )


async def require_original_model(manager, model: GatewayModelSnapshot) -> GatewayModelSnapshot:
    active = await manager.require_active(model.alias)
    if (
        active.accounting_source_id != model.accounting_source_id
        or active.route_revision != model.route_revision
        or active.route_digest != model.route_digest
        or active.model_catalog_digest != model.model_catalog_digest
    ):
        raise HTTPException(409, "The Task's original AgentGateway model changed")
    return model


async def select_review_model(manager, engine, task_id):
    async with engine.connect() as conn:
        original = await task_gateway_model(conn, task_id)
    active = [model for model in await manager.list_active() if model.tool_call]
    if original:
        for item in active:
            if item.alias == original[0].alias and item.route_digest == original[0].route_digest:
                return item
    if not active:
        raise HTTPException(503, "终态复盘暂没有可用的 AgentGateway 模型")
    return min(active, key=lambda item: item.alias)


TERMINAL_TASKS = {"finished", "failed", "stopped"}


def ctf_review_allowed(task, member):
    return not task["deleting"] and (
        task["status"] in TERMINAL_TASKS or member["lifecycle"] in {"stopped", "removed"}
    )


async def ctf_review_generation(conn, task_id, member):
    prior_review = await conn.scalar(
        select(s.ctf_turns.c.review_origin_generation)
        .where(
            s.ctf_turns.c.task_id == task_id,
            s.ctf_turns.c.agent_id == member["id"],
            s.ctf_turns.c.generation == member["generation"],
            s.ctf_turns.c.purpose == "review",
        )
        .limit(1)
    )
    return prior_review if prior_review is not None else member["generation"]


async def review_source(engine, task, agent_id):
    if engine is None:
        return None
    ctf = task.get("mode") == "ctf"
    table = s.ctf_members if ctf else s.agent_runs
    async with engine.connect() as conn:
        agent = (
            (
                await conn.execute(
                    select(table).where(table.c.task_id == task["id"], table.c.id == agent_id)
                )
            )
            .mappings()
            .first()
        )
        if agent is None:
            raise HTTPException(404, "Agent not found")
        if ctf:
            if not ctf_review_allowed(task, agent):
                return None
            return {
                "generation": await ctf_review_generation(conn, task["id"], agent),
                "run_number": task["run_number"],
            }
        if agent["status"] in {"finished", "failed"} and not task["deleting"]:
            return {
                "started_at": agent["started_at"],
                "derive_round": agent["derive_round"],
                "run_number": task["run_number"],
            }
    return None
