"""Persisted Task, Progress, and user-review authorization for Pi sends."""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from bbx_blackboard.gateway_tasks import ctf_review_allowed, ctf_review_closed
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import row

ACTIVE_AGENT = {"running", "concluding"}


def denied(message="The original model execution is not authorized"):
    return HTTPException(409, message)


async def ordinary_scope(conn, task_id, body):
    task = await row(conn, s.tasks, task_id)
    agent = await row(conn, s.agent_runs, task_id, body.agent_id)
    role = agent["task_type"]
    if (
        task["mode"] != "blackboard"
        or task["deleting"]
        or task["run_number"] != body.run_number
        or task["status"] not in {"running", "closing"}
        or task["status"] == "closing"
        and role != "close"
        or agent["status"] not in ACTIVE_AGENT
        or body.expected_started_at != agent["started_at"]
        or role == "derive"
        and body.expected_derive_round != agent["derive_round"]
        or role != "derive"
        and body.expected_derive_round is not None
    ):
        raise denied()
    return task, agent


async def progress_scope(conn, task_id, body):
    task = await row(conn, s.tasks, task_id)
    job = (
        (
            await conn.execute(
                select(s.progress_jobs).where(
                    s.progress_jobs.c.task_id == task_id,
                    s.progress_jobs.c.id == body.job_id,
                )
            )
        )
        .mappings()
        .first()
    )
    current = (
        await conn.execute(
            select(s.task_progress.c.data).where(s.task_progress.c.task_id == task_id)
        )
    ).scalar_one_or_none()
    if (
        task["deleting"]
        or not current
        or not current["settings"]["enabled"]
        or job is None
        or job["status"] != "running"
        or task["run_number"] != body.run_number
        or job["run_number"] != body.run_number
    ):
        raise denied("The original Progress job is not active")
    data = job["data"]
    if (
        data.get("claim_token") != str(body.claim_token)
        or datetime.fromisoformat(data["lease_until"]) <= datetime.now(UTC)
        or data.get("budget_currency") != "USD"
        or not data.get("pi_model")
    ):
        raise denied("The original Progress reservation is unavailable")
    return task, dict(job), current


async def review_scope(conn, task_id, body):
    task = await row(conn, s.tasks, task_id)
    if task["deleting"] or task["run_number"] != body.run_number:
        raise denied("Review requires the original Task run")
    if task["mode"] == "blackboard" and body.message_id is not None:
        agent = await row(conn, s.agent_runs, task_id, body.agent_id)
        message = (
            (
                await conn.execute(
                    select(s.agent_messages).where(
                        s.agent_messages.c.task_id == task_id,
                        s.agent_messages.c.id == body.message_id,
                        s.agent_messages.c.agent_id == body.agent_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if (
            agent["status"] not in {"finished", "failed"}
            or message is None
            or message["role"] != "user"
            or message["purpose"] != "review"
            or message["request_run_number"] != body.run_number
            or message["request_started_at"] != agent["started_at"]
            or message["request_derive_round"] != agent["derive_round"]
            or message["status"] != "processing"
            or message["claim_token"] != body.claim_token
            or message["lease_until"] is None
            or message["lease_until"] <= datetime.now(UTC)
        ):
            raise denied("The original review message claim changed")
        return str(body.message_id), message["review_model"]
    if task["mode"] == "ctf" and body.turn_id is not None:
        member = await row(conn, s.ctf_members, task_id, body.agent_id)
        turn = await row(conn, s.ctf_turns, task_id, str(body.turn_id))
        if (
            not ctf_review_allowed(task, member)
            or not await ctf_review_closed(conn, task)
            or task["ctf_control"].get("unresolved_checkpoints")
            or member.get("pending_operation")
            or member["current_turn_id"] != str(body.turn_id)
            or member["generation"] != body.generation
            or turn["generation"] != body.generation
            or turn["agent_id"] != body.agent_id
            or turn["purpose"] != "review"
            or turn["status"] != "running"
        ):
            raise denied("The original review turn changed")
        selected = await conn.scalar(
            select(s.ctf_messages.c.id)
            .where(
                s.ctf_messages.c.task_id == task_id,
                s.ctf_messages.c.id.in_(turn["assignment_message_ids"]),
                s.ctf_messages.c.claim_turn_id == str(body.turn_id),
                s.ctf_messages.c.claim_generation == body.generation,
                s.ctf_messages.c.status.in_(("leased", "delivered")),
                s.ctf_messages.c.sender_kind == "user",
                s.ctf_messages.c.purpose == "review",
                s.ctf_messages.c.request_run_number == body.run_number,
                s.ctf_messages.c.request_agent_generation == turn["review_origin_generation"],
            )
            .limit(1)
        )
        if selected is None:
            raise denied("The original terminal review message is not claimed")
        return str(body.turn_id), turn["review_model"]
    raise denied("An original terminal review claim is required")
