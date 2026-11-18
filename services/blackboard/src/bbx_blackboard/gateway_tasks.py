"""Historic Task boundaries and model-independent review authorization."""

from decimal import Decimal

from bbx_contracts.gateway_models import GatewayModelSnapshot
from bbx_contracts.native_gateway import native_usd_amount
from fastapi import HTTPException
from sqlalchemy import literal, select
from sqlalchemy.dialects.postgresql import JSONB

from bbx_blackboard.store import schema as s


def validate_task_threshold(amount: Decimal) -> None:
    try:
        native_usd_amount(amount)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


async def task_gateway_model(conn, task_id):
    item = (
        (
            await conn.execute(
                select(s.gateway_task_models).where(s.gateway_task_models.c.task_id == task_id)
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


TERMINAL_TASKS = {"finished", "failed", "stopped"}


def native_ctf_review_closed(task_id):
    resources, owners, turns = s.task_runner_resources, s.runner_owners, s.ctf_turns
    return (
        select(resources.c.task_id)
        .where(
            resources.c.task_id == task_id,
            resources.c.state == "stopped",
            resources.c.data["exit_confirmed"] == literal(True, type_=JSONB),
        )
        .correlate(s.tasks)
        .exists()
        & ~select(owners.c.task_id)
        .where(owners.c.task_id == task_id, owners.c.current.is_(True), owners.c.revoked.is_(False))
        .correlate(s.tasks)
        .exists()
        & ~select(turns.c.task_id)
        .where(
            turns.c.task_id == task_id, turns.c.purpose == "execution", turns.c.status == "running"
        )
        .correlate(s.tasks)
        .exists()
    )


async def ctf_review_closed(conn, task) -> bool:
    if task["status"] not in TERMINAL_TASKS or task["cleanup_ready"]:
        return True
    # Native stops retain their original work volume and do not publish an ordinary tar.
    return bool(await conn.scalar(select(native_ctf_review_closed(task["id"]))))


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
