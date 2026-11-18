"""Ordinary Blackboard authority independent of CTF membership or Assignment."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from bbx_contracts.runner import CommandReceipt
from bbx_contracts.runner_control import BoardJournalProjection, BoardRunIdentity
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncConnection

from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import row

EXPLORATION_CONTRACT = "exploration-board-v1"
BOARD_CLOSE_DRAIN_SECONDS = 8


async def board_runner_frozen(conn: AsyncConnection, task_id: UUID) -> bool:
    resources = s.task_runner_resources
    row = (
        (
            await conn.execute(
                select(resources.c.config, resources.c.data).where(resources.c.task_id == task_id)
            )
        )
        .mappings()
        .first()
    )
    return bool(
        row
        and row["data"].get("execution_contract") == EXPLORATION_CONTRACT
        and row["data"].get("mcp_transport") == "direct-v2"
        and (
            row["config"] is None or row["config"].get("execution_contract") == EXPLORATION_CONTRACT
        )
    )


async def require_board_resource_ready(conn, task_id: UUID):
    resources = s.task_runner_resources
    result = (
        (
            await conn.execute(
                select(resources.c.state, resources.c.sandbox_host, resources.c.data).where(
                    resources.c.task_id == task_id
                )
            )
        )
        .mappings()
        .first()
    )
    if (
        result is None
        or result["state"] != "ready"
        or not result["sandbox_host"].get("ready")
        or not result["data"].get("activated")
    ):
        raise HTTPException(409, "Original Blackboard resource readiness is not confirmed")


async def require_board_resources_stopped(conn, tid):
    resources = s.task_runner_resources
    stopped = await conn.scalar(
        select(resources.c.operation_id).where(
            resources.c.task_id == tid,
            resources.c.state == "stopped",
            resources.c.data.contains({"exit_confirmed": True}),
        )
    )
    owners = s.runner_owners
    live = await conn.scalar(
        select(owners.c.epoch).where(owners.c.task_id == tid, owners.c.current, ~owners.c.revoked)
    )
    if stopped is None or live is not None:
        raise HTTPException(409, "Original Blackboard resource exit is not confirmed")


def confirmed_board_journals(bindings, run_number: int):
    """Require an exact projection for each confirmed cursor of this archived run."""
    journals = []
    for item in bindings:
        binding, business, cursor = item["binding"], item["business"], item["cursor"]
        if (
            binding.get("business_contract") != EXPLORATION_CONTRACT
            or (business.get("board_run") or {}).get("run_number") != run_number
            or cursor is None
        ):
            continue
        try:
            proof = BoardJournalProjection.model_validate(business.get("board_journal"))
        except ValueError as error:
            raise HTTPException(409, "Original Blackboard journal projection is pending") from error
        if (
            proof.run_number != run_number
            or proof.binding_id != binding["binding_id"]
            or proof.sdk_session_id != item["sdk_session_id"]
            or proof.size != proof.cursor.offset
            or proof.cursor.model_dump(mode="json") != cursor
        ):
            raise HTTPException(
                409, "Original Blackboard journal projection has not reached its cursor"
            )
        journals.append(
            {"agent_id": binding["agent_id"], "projection": proof.model_dump(mode="json")}
        )
    return sorted(journals, key=lambda item: item["projection"]["binding_id"])


async def require_board_run(
    conn: AsyncConnection,
    task_id: UUID,
    agent_id: str,
    proof: BoardRunIdentity,
    *,
    action: Literal["new_binding", "active_binding", "control_binding", "model_send"],
):
    """Resolve a frozen AgentRun under the caller's Task lock."""
    try:
        task = await row(conn, s.tasks, task_id)
        agent = await row(conn, s.agent_runs, task_id, agent_id)
    except NoResultFound as error:
        raise HTTPException(404, "Original Blackboard AgentRun was not found") from error
    role = agent["task_type"]
    dormant = action == "control_binding" and role == "derive" and agent["status"] == "finished"
    if (
        task["mode"] != "blackboard"
        or task["deleting"]
        or task["run_number"] != proof.run_number
        or task["status"] not in {"running", "closing"}
        or role not in {"explore", "derive", "close"}
        or (agent["status"] not in {"running", "concluding"} and not dormant)
        or proof.started_at != agent["started_at"]
        or proof.derive_round != (agent["derive_round"] if role == "derive" else None)
    ):
        raise HTTPException(409, "Original Blackboard AgentRun is not current")
    if action in {"model_send", "new_binding"} and not (
        role == "close" and agent["close_mode"] == "final"
    ):
        deadline = await conn.scalar(
            select(s.task_runner_resources.c.data["solver_deadline"].astext).where(
                s.task_runner_resources.c.task_id == task_id
            )
        )
        if deadline is not None and datetime.now(UTC) >= datetime.fromisoformat(deadline):
            raise HTTPException(409, "Blackboard solve deadline passed; only final Close may start")
    if task["status"] == "closing" and not (
        role == "close"
        and agent["close_mode"] == "final"
        or action == "active_binding"
        and agent["status"] == "concluding"
        or action == "control_binding"
        and agent["status"] in {"running", "concluding"}
    ):
        raise HTTPException(409, "Blackboard closing only permits final Close or existing handoff")
    return task, agent


async def board_message(conn, tid, binding, input_id, *, already_delivered=False):
    proof = BoardRunIdentity.model_validate(binding["business"]["board_run"])
    aid = binding["binding"]["agent_id"]
    _, agent = await require_board_run(conn, tid, aid, proof, action="active_binding")
    try:
        key = UUID(input_id)
    except ValueError as error:
        raise HTTPException(403, "Original Blackboard message is missing") from error
    messages = s.agent_messages
    current = (
        (
            await conn.execute(
                select(messages).where(
                    messages.c.task_id == tid,
                    messages.c.id == key,
                    messages.c.agent_id == aid,
                    messages.c.role == "user",
                    messages.c.purpose == "active",
                    messages.c.request_run_number == proof.run_number,
                    messages.c.request_started_at == proof.started_at,
                    messages.c.request_derive_round == agent["derive_round"],
                    messages.c.status.in_(
                        ("queued", "delivered") if already_delivered else ("queued",)
                    ),
                )
            )
        )
        .mappings()
        .first()
    )
    if current is None:
        raise HTTPException(403, "Original Blackboard message is missing")
    return dict(current)


async def require_board_terminal(conn, tid, task, agent, request_id, receipt, end_reason):
    """The coordinator may only project a confirmed original Runner terminal receipt."""
    if agent is None:
        raise HTTPException(409, "Original Blackboard AgentRun is missing")
    bindings, commands, receipts = s.runner_bindings, s.runner_commands, s.runner_receipts
    proof = BoardRunIdentity(
        run_number=task["run_number"],
        started_at=agent["started_at"],
        derive_round=agent["derive_round"] if agent["task_type"] == "derive" else None,
    ).model_dump(mode="json")
    scope = (
        bindings.c.task_id == tid,
        bindings.c.binding["agent_id"].astext == agent["id"],
        bindings.c.binding["business_contract"].astext == EXPLORATION_CONTRACT,
        bindings.c.business["board_run"] == proof,
    )
    if request_id is None:
        if (
            receipt == {"accepted": False, "reason": "claim_lost"}
            and end_reason == "normal"
            and not await conn.scalar(select(bindings.c.id).where(*scope).limit(1))
        ):
            return False
        raise HTTPException(409, "Confirmed Blackboard terminal receipt required")
    original = await conn.scalar(
        select(receipts.c.body_text)
        .join(
            commands,
            (commands.c.task_id == receipts.c.task_id)
            & (commands.c.request_id == receipts.c.request_id)
            & (commands.c.owner_epoch == receipts.c.owner_epoch),
        )
        .join(
            bindings,
            (bindings.c.task_id == commands.c.task_id) & (bindings.c.id == commands.c.binding_id),
        )
        .where(
            *scope,
            receipts.c.kind == "command",
            receipts.c.request_id == str(request_id),
            receipts.c.binding_id == bindings.c.id,
            (
                (receipts.c.response["status"].astext == "completed")
                & (commands.c.kind == "input")
                & (commands.c.status == "completed")
                & commands.c.message_proof.contains({"board_run_start": True})
                | (receipts.c.response["status"].astext == "aborted")
                & (commands.c.kind == "abort")
                & (commands.c.status == "aborted")
                | (receipts.c.response["status"].astext == "closed")
                & (commands.c.kind == "close")
                & (commands.c.status == "closed")
            ),
        )
        .limit(1)
    )
    if original is None:
        raise HTTPException(409, "Confirmed Blackboard terminal receipt required")
    observed = CommandReceipt.model_validate_json(original)
    terminal = observed.board_terminal
    if observed.status in {"aborted", "closed"}:
        if (
            observed.sdk_idle is not True
            or observed.journal_confirmed is not True
            or observed.pending_confirmation
            or observed.cursor is None
            or end_reason == "normal"
            or receipt.get("accepted") is True
        ):
            raise HTTPException(409, "Blackboard abort evidence is incomplete")
        return bool(observed.unresolved_attempts or observed.unresolved_invocations)
    elif terminal is None or (
        terminal.outcome != "completed"
        and (end_reason == "normal" or receipt.get("accepted") is True)
    ):
        raise HTTPException(409, "Blackboard projection does not match original terminal outcome")
    return False
