"""Durable Agent sessions and user-directed messages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from bbx_contracts.challenge_board import BOARD_CONTRACT, business_contract
from bbx_contracts.heartbeat import usage_receipt_support
from bbx_contracts.mcp_catalog import ExternalMcpCheckpoint
from bbx_contracts.pi_models import PiModelVersion
from bbx_contracts.storage import storage_safe
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import Text, and_, delete, exists, func, or_, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from bbx_blackboard.domain.rules import dispute_fields, event, pending_claims
from bbx_blackboard.gateway_maintenance import admission
from bbx_blackboard.store import Repository
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import agent_execution_matches

ACTIVE = {"running", "concluding"}
LEASE = timedelta(seconds=300)
EXTERNAL_MCP_PENDING = "bbx_external_mcp_pending"
EXTERNAL_MCP_COMPLETED = "bbx_external_mcp_completed"


def checkpoint_state(session: dict[str, Any]) -> dict[str, Any]:
    state = session.get("state")
    return state if isinstance(state, dict) else {}


def external_mcp_marker(value: Any) -> ExternalMcpCheckpoint | None:
    if value is None:
        return None
    try:
        return ExternalMcpCheckpoint.model_validate(value)
    except ValidationError:
        raise HTTPException(422, "Invalid external MCP checkpoint identity") from None


def original_mcp_result(state: dict[str, Any], original: ExternalMcpCheckpoint) -> bool:
    if state.get(EXTERNAL_MCP_COMPLETED) != original.model_dump(mode="json"):
        return False

    def matches(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and value.get("type") == "function_result"
            and value.get("call_id") == original.call_id
            and value.get("exception") is None
        )

    results = state.get("bbx_tool_results")
    if isinstance(results, dict) and matches(results.get(original.call_id)):
        return True
    # A checkpoint can move this exact saved result into tool history while
    # flushing usage. Interrupted repairs are not evidence of a return.
    history = state.get("in_memory")
    if not isinstance(history, dict) or not isinstance(history.get("messages"), list):
        return False
    for message in reversed(history["messages"]):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        contents = message.get("contents")
        if not isinstance(contents, list):
            continue
        for item in contents:
            if not matches(item):
                continue
            properties = item.get("additional_properties")
            if (
                isinstance(properties, dict)
                and properties.get("bbx_recovery_repair") == "saved_tool_result"
            ):
                return True
    return False


MESSAGE_FIELDS = (
    "id",
    "task_id",
    "agent_id",
    "role",
    "content",
    "status",
    "reply_to",
    "usage",
    "error",
    "created_at",
    "updated_at",
)


def _now() -> datetime:
    return datetime.now(UTC)


async def require_settled_progress(conn, tid):
    jobs = (
        (
            await conn.execute(
                select(s.progress_jobs.c.data).where(
                    s.progress_jobs.c.task_id == tid,
                    s.progress_jobs.c.status.in_(("running", "cancelled")),
                )
            )
        )
        .scalars()
        .all()
    )
    if any(
        job.get("lease_until")
        and datetime.fromisoformat(job["lease_until"]) > _now()
        and any(receipt.get("status") == "reserved" for receipt in job.get("receipts", {}).values())
        for job in jobs
    ):
        raise HTTPException(409, "Progress request is still settling")


def _public(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in MESSAGE_FIELDS}


def _review_dispatched():
    pi = exists(
        select(s.pi_model_calls.c.call_id).where(
            s.pi_model_calls.c.task_id == s.agent_messages.c.task_id,
            s.pi_model_calls.c.purpose == "review",
            s.pi_model_calls.c.turn_id == sql_cast(s.agent_messages.c.id, Text),
            s.pi_model_calls.c.state.in_(("dispatched", "unknown", "complete")),
        )
    )
    return pi | exists(
        select(s.gateway_execution_attempts.c.id).where(
            s.gateway_execution_attempts.c.task_id == s.agent_messages.c.task_id,
            s.gateway_execution_attempts.c.purpose == "review",
            s.gateway_execution_attempts.c.attributes["bbx_conversation_id"].astext
            == sql_cast(s.agent_messages.c.id, Text),
        )
    )


def _require_original_review(task, agent, message):
    if (
        message["purpose"] != "review"
        or agent["status"] not in {"finished", "failed"}
        or message["request_run_number"] != task["run_number"]
        or message["request_started_at"] != agent["started_at"]
        or message["request_derive_round"] != agent["derive_round"]
    ):
        raise HTTPException(409, "The original review incarnation changed")


class Conversations:
    def __init__(self, engine: AsyncEngine) -> None:
        self.repo = Repository(engine)

    async def export_archive(self, tid: UUID) -> dict[str, Any]:
        async with self.repo.engine.begin() as conn:
            try:
                await self.repo.lock(conn, tid)
            except KeyError:
                raise HTTPException(404, "Task not found") from None
            state = await self.repo.load(conn, tid)
            if state.task["deleting"]:
                raise HTTPException(409, "Task is being deleted")
            if state.task["status"] not in {"finished", "failed", "stopped"}:
                raise HTTPException(409, "Task is not terminal")
            if any(agent["status"] in ACTIVE for agent in state.agents.values()):
                raise HTTPException(409, "Task has active agents")

            dispute = dispute_fields(state.facts)
            facts = {fid: {**fact, **dispute[fid]} for fid, fact in state.facts.items()}
            events = (
                (
                    await conn.execute(
                        select(s.events)
                        .where(s.events.c.task_id == tid)
                        .order_by(s.events.c.version)
                    )
                )
                .mappings()
                .all()
            )
            sessions = (
                (
                    await conn.execute(
                        select(s.agent_sessions)
                        .where(s.agent_sessions.c.task_id == tid)
                        .order_by(s.agent_sessions.c.agent_id)
                    )
                )
                .mappings()
                .all()
            )
            messages = (
                (
                    await conn.execute(
                        select(*(s.agent_messages.c[key] for key in MESSAGE_FIELDS))
                        .where(s.agent_messages.c.task_id == tid)
                        .order_by(s.agent_messages.c.created_at, s.agent_messages.c.id)
                    )
                )
                .mappings()
                .all()
            )
            task_runs = (
                (
                    await conn.execute(
                        select(s.task_runs)
                        .where(s.task_runs.c.task_id == tid)
                        .order_by(s.task_runs.c.run_number)
                    )
                )
                .mappings()
                .all()
            )
            return jsonable_encoder(
                {
                    "format": "bbx.task-archive.v1",
                    "task_id": str(tid),
                    "run_number": state.task["run_number"],
                    "state": {
                        "task": state.task,
                        "facts": facts,
                        "intents": state.intents,
                        "agents": state.agents,
                        "counters": state.counters,
                        "pending_claims": pending_claims(state),
                        "board_empty": not (state.facts or state.intents),
                        "last_change_version": state.task["last_change_version"],
                        "last_judgment_version": state.task["last_judgment_version"],
                    },
                    "events": [dict(row) for row in events],
                    "challenge_board": {
                        table.name: [
                            dict(item)
                            for item in (
                                await conn.execute(select(table).where(table.c.task_id == tid))
                            ).mappings()
                        ]
                        for table in (s.board_announcements, s.flag_candidates, s.flag_submissions)
                    },
                    "runner_control": {
                        table.name: [
                            dict(item)
                            for item in (
                                await conn.execute(select(table).where(table.c.task_id == tid))
                            ).mappings()
                        ]
                        for table in (
                            s.runner_owners,
                            s.runner_bindings,
                            s.runner_commands,
                            s.runner_receipts,
                            s.runner_calls,
                        )
                    },
                    "native_costs": [
                        dict(item)
                        for item in (
                            await conn.execute(
                                select(s.native_request_costs).where(
                                    s.native_request_costs.c.task_id == tid
                                )
                            )
                        ).mappings()
                    ],
                    "native_budget": [
                        {
                            **{
                                key: item[key]
                                for key in (
                                    "accounting_source_id",
                                    "key_name",
                                    "native_resource_id",
                                    "hold_reason",
                                    "status_snapshot",
                                )
                            },
                            "config": {
                                key: value
                                for key, value in item["config"].items()
                                if key != "native_value"
                            },
                        }
                        for item in (
                            await conn.execute(
                                select(s.native_task_budgets).where(
                                    s.native_task_budgets.c.task_id == tid
                                )
                            )
                        ).mappings()
                    ],
                    "sessions": [dict(row) for row in sessions],
                    "messages": [dict(row) for row in messages],
                    "task_runs": [dict(row) for row in task_runs],
                    "progress": (
                        await conn.execute(
                            select(s.task_progress.c.data).where(s.task_progress.c.task_id == tid)
                        )
                    ).scalar_one_or_none(),
                    "progress_jobs": [
                        dict(row)
                        for row in (
                            await conn.execute(
                                select(s.progress_jobs).where(s.progress_jobs.c.task_id == tid)
                            )
                        ).mappings()
                    ],
                }
            )

    async def _task_agent(
        self, conn: AsyncConnection, tid: UUID, aid: str, *, writable: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().first()
        if task is None:
            raise HTTPException(404, "Task not found")
        if writable and task["deleting"]:
            raise HTTPException(409, "Task is being deleted")
        if task.get("mode") == "ctf":
            if writable:
                raise HTTPException(409, "CTF writes require a generation-fenced checkpoint")
            member = (
                (
                    await conn.execute(
                        select(s.ctf_members).where(
                            s.ctf_members.c.task_id == tid,
                            s.ctf_members.c.id == aid,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if member is None:
                raise HTTPException(404, "Member not found")
            return dict(task), dict(member)
        agent = (
            (
                await conn.execute(
                    select(s.agent_runs).where(
                        s.agent_runs.c.task_id == tid, s.agent_runs.c.id == aid
                    )
                )
            )
            .mappings()
            .first()
        )
        if agent is None:
            raise HTTPException(404, "Agent not found")
        return dict(task), dict(agent)

    async def _session(self, conn: AsyncConnection, tid: UUID, aid: str) -> dict[str, Any] | None:
        row = (
            (
                await conn.execute(
                    select(s.agent_sessions).where(
                        s.agent_sessions.c.task_id == tid, s.agent_sessions.c.agent_id == aid
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def get_session(self, tid: UUID, aid: str) -> dict[str, Any]:
        async with self.repo.engine.connect() as conn:
            await self._task_agent(conn, tid, aid)
            found = await self._session(conn, tid, aid)
        if found is None:
            raise HTTPException(404, "Agent session not found")
        return {
            **{
                key: found[key] for key in ("session", "opening_instructions", "origin", "revision")
            },
            **usage_receipt_support(found["session"]),
        }

    async def _save_session(
        self,
        conn: AsyncConnection,
        tid: UUID,
        aid: str,
        session: dict[str, Any],
        opening_instructions: str,
        origin: str,
        expected_revision: int,
        *,
        return_session: bool = True,
    ) -> dict[str, Any]:
        marker = s.agent_sessions.c.session["state"][EXTERNAL_MCP_PENDING]
        existing = (
            await conn.execute(
                select(s.agent_sessions.c.revision, marker.label("external_mcp_pending")).where(
                    s.agent_sessions.c.task_id == tid, s.agent_sessions.c.agent_id == aid
                )
            )
        ).one_or_none()
        revision = existing.revision if existing else 0
        if revision != expected_revision:
            raise HTTPException(409, "Session revision conflict")
        previous = external_mcp_marker(existing.external_mcp_pending) if existing else None
        state = checkpoint_state(session)
        pending = external_mcp_marker(state.get(EXTERNAL_MCP_PENDING))
        if previous and pending != previous:
            if pending or not original_mcp_result(state, previous):
                raise HTTPException(409, "Original external MCP result is unconfirmed")
        if pending and not previous:
            other = await conn.scalar(
                select(s.agent_sessions.c.agent_id)
                .where(
                    s.agent_sessions.c.task_id == tid,
                    s.agent_sessions.c.agent_id != aid,
                    marker.astext.is_not(None),
                )
                .limit(1)
            )
            if other is not None:
                raise HTTPException(409, "Another external MCP result is unconfirmed for this Task")
        session = storage_safe(session)
        opening_instructions = storage_safe(opening_instructions)
        if existing is not None:
            await conn.execute(
                update(s.agent_sessions)
                .where(s.agent_sessions.c.task_id == tid, s.agent_sessions.c.agent_id == aid)
                .values(
                    session=session,
                    opening_instructions=opening_instructions,
                    origin=origin,
                    revision=revision + 1,
                    updated_at=_now(),
                )
            )
        else:
            await conn.execute(
                insert(s.agent_sessions).values(
                    task_id=tid,
                    agent_id=aid,
                    session=session,
                    opening_instructions=opening_instructions,
                    origin=origin,
                    revision=1,
                )
            )
        if not return_session:
            return {"revision": revision + 1}
        return {
            "session": session,
            "opening_instructions": opening_instructions,
            "origin": origin,
            "revision": revision + 1,
            **usage_receipt_support(session),
        }

    async def put_session(
        self,
        tid: UUID,
        aid: str,
        session: dict[str, Any],
        opening_instructions: str,
        origin: str,
        expected_revision: int,
        deliveries: list[dict[str, UUID]],
        review_claim: dict[str, UUID] | None = None,
        expected_derive_round: int | None = None,
        expected_started_at: datetime | None = None,
    ) -> dict[str, Any]:
        written = []
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, agent = await self._task_agent(conn, tid, aid, writable=True)
            if (
                agent["task_type"] == "derive"
                and agent["status"] in ACTIVE
                and (int(agent.get("derive_round") or 1) != (expected_derive_round or 1))
            ):
                raise HTTPException(409, "Derive round changed")
            if review_claim is not None:
                message = await self._message(conn, tid, aid, review_claim["id"])
                self._check_claim(message, review_claim["claim_token"])
                _require_original_review(task, agent, message)
            if agent["status"] in ACTIVE or review_claim is None or expected_started_at is not None:
                if not await agent_execution_matches(conn, tid, agent, expected_started_at):
                    raise HTTPException(
                        409, {"code": "stale_agent_execution", "message": "Agent execution changed"}
                    )
            for delivery in deliveries:
                message = await self._message(conn, tid, aid, delivery["id"])
                self._check_claim(message, delivery["claim_token"])
                if message["purpose"] == "review":
                    raise HTTPException(409, "Review cannot be delivered to execution")
                await conn.execute(
                    update(s.agent_messages)
                    .where(s.agent_messages.c.id == delivery["id"])
                    .values(status="delivered", updated_at=_now())
                )
                written.extend(
                    await self.repo.append(
                        conn,
                        tid,
                        [
                            event(
                                "agent.message.delivered",
                                "system",
                                {"id": str(delivery["id"]), "agent_id": aid},
                                addressed_to=[aid],
                            )
                        ],
                    )
                )
            result = await self._save_session(
                conn, tid, aid, session, opening_instructions, origin, expected_revision
            )
        if written:
            await self.repo.notify(tid, written[-1]["version"])
        return result

    async def list_messages(self, tid: UUID, aid: str, status: str | None = None) -> dict[str, Any]:
        async with self.repo.engine.connect() as conn:
            task, agent = await self._task_agent(conn, tid, aid)
            stmt = select(s.agent_messages).where(
                s.agent_messages.c.task_id == tid, s.agent_messages.c.agent_id == aid
            )
            if status is not None:
                stmt = stmt.where(s.agent_messages.c.status == status)
            if status == "queued":
                stmt = stmt.where(
                    s.agent_messages.c.purpose.is_(None)
                    | (
                        (s.agent_messages.c.purpose == "active")
                        & (s.agent_messages.c.request_run_number == task["run_number"])
                        & (s.agent_messages.c.request_started_at == agent["started_at"])
                        & (s.agent_messages.c.request_derive_round == agent["derive_round"])
                    )
                )
            rows = (
                (
                    await conn.execute(
                        stmt.order_by(s.agent_messages.c.created_at, s.agent_messages.c.id)
                    )
                )
                .mappings()
                .all()
            )
            session = (
                (
                    await conn.execute(
                        select(s.agent_sessions.c.origin).where(
                            s.agent_sessions.c.task_id == tid, s.agent_sessions.c.agent_id == aid
                        )
                    )
                )
                .mappings()
                .first()
            )
        return {
            "messages": [_public(row) for row in rows],
            "session_available": session is not None,
            "session_origin": session["origin"] if session else None,
            "mode": (
                "active"
                if agent["status"] in ACTIVE
                else "review"
                if agent["status"] in {"finished", "failed"}
                else "unavailable"
            ),
        }

    async def post_message(
        self,
        tid: UUID,
        aid: str,
        mid: UUID,
        content: str,
        *,
        review_model=None,
        review_generation=None,
        review_origin=None,
        request_user=None,
    ) -> dict[str, Any]:
        content = storage_safe(content)
        written = []
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, agent = await self._task_agent(conn, tid, aid, writable=True)
            review = agent["status"] in {"finished", "failed"}
            if review:
                if review_model is None or review_origin != {
                    "started_at": agent["started_at"],
                    "derive_round": agent["derive_round"],
                    "run_number": task["run_number"],
                }:
                    raise HTTPException(409, "No original terminal review scope is available")
                if review_generation is not None or request_user is None:
                    await admission(conn, review_generation)
            elif agent["status"] not in ACTIVE or review_origin is not None:
                raise HTTPException(409, "This Agent cannot be reviewed while the Task is active")
            result = await conn.execute(
                insert(s.agent_messages)
                .values(
                    id=mid,
                    task_id=tid,
                    agent_id=aid,
                    role="user",
                    purpose="review" if review else "active",
                    request_run_number=task["run_number"],
                    request_user=request_user if review else None,
                    request_started_at=agent["started_at"],
                    request_derive_round=agent["derive_round"],
                    review_model=review_model.model_dump(mode="json")
                    if review_model is not None
                    else None,
                    content=content,
                    status="queued",
                )
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(s.agent_messages)
            )
            row = result.mappings().first()
            if row is None:
                existing = (
                    (
                        await conn.execute(
                            select(s.agent_messages).where(s.agent_messages.c.id == mid)
                        )
                    )
                    .mappings()
                    .one()
                )
                if (
                    existing["task_id"] != tid
                    or existing["agent_id"] != aid
                    or existing["role"] != "user"
                    or existing["content"] != content
                ):
                    raise HTTPException(409, "Message ID conflict")
                return _public(existing)
            written = await self.repo.append(
                conn,
                tid,
                [
                    event(
                        "agent.message.posted",
                        "user",
                        {"id": str(mid), "agent_id": aid, "summary": content[:200]},
                        addressed_to=[aid],
                    )
                ],
            )
            message = _public(row)
        await self.repo.notify(tid, written[-1]["version"])
        return message

    async def _message(
        self, conn: AsyncConnection, tid: UUID, aid: str, mid: UUID
    ) -> dict[str, Any]:
        row = (
            (
                await conn.execute(
                    select(s.agent_messages).where(
                        s.agent_messages.c.id == mid,
                        s.agent_messages.c.task_id == tid,
                        s.agent_messages.c.agent_id == aid,
                        s.agent_messages.c.role == "user",
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(404, "Message not found")
        return dict(row)

    @staticmethod
    def _check_claim(message: dict[str, Any], token: UUID) -> None:
        if (
            message["status"] != "processing"
            or message["claim_token"] != token
            or message["lease_until"] is None
            or message["lease_until"] <= _now()
        ):
            raise HTTPException(409, "Message claim conflict")

    async def claim(self, tid: UUID, aid: str, mid: UUID, mode: str) -> dict[str, Any]:
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, agent = await self._task_agent(conn, tid, aid, writable=True)
            active = agent["status"] in ACTIVE
            if (mode == "active") != active:
                raise HTTPException(409, "Agent mode changed")
            if mode == "review":
                if agent["status"] not in {"finished", "failed"}:
                    raise HTTPException(409, "Review requires a finished Agent")
                processing = await conn.execute(
                    select(s.agent_messages.c.id)
                    .where(
                        s.agent_messages.c.task_id == tid,
                        s.agent_messages.c.agent_id == aid,
                        s.agent_messages.c.role == "user",
                        s.agent_messages.c.status == "processing",
                        s.agent_messages.c.lease_until > _now(),
                    )
                    .limit(1)
                )
                if processing.first():
                    raise HTTPException(409, "Agent review is already processing")
            message = await self._message(conn, tid, aid, mid)
            if mode == "review" and not (message.get("review_model") or {}).get("pi_model"):
                await admission(conn)
            if mode == "review" and (
                message["purpose"] != "review"
                or message["request_run_number"] != task["run_number"]
                or not message["review_model"]
                or message["request_started_at"] != agent["started_at"]
                or message["request_derive_round"] != agent["derive_round"]
            ):
                raise HTTPException(409, "Original terminal review is unavailable")
            if mode == "review" and await conn.scalar(
                select(s.agent_messages.c.id).where(
                    s.agent_messages.c.id == mid, _review_dispatched()
                )
            ):
                raise HTTPException(409, "The original review request cannot be sent again")
            if mode == "active" and (
                message["purpose"] == "review"
                or message["purpose"] == "active"
                and (
                    message["request_run_number"] != task["run_number"]
                    or message["request_started_at"] != agent["started_at"]
                    or message["request_derive_round"] != agent["derive_round"]
                )
            ):
                raise HTTPException(409, "Review cannot be injected into active execution")
            if message["status"] != "queued" and not (
                message["status"] == "processing" and message["lease_until"] <= _now()
            ):
                raise HTTPException(409, "Message is not claimable")
            token = uuid4()
            lease_until = _now() + LEASE
            result = await conn.execute(
                update(s.agent_messages)
                .where(s.agent_messages.c.id == mid)
                .values(
                    status="processing",
                    claim_token=token,
                    lease_until=lease_until,
                    updated_at=_now(),
                )
                .returning(s.agent_messages)
            )
            return {
                "message": _public(result.mappings().one()),
                "claim_token": token,
                **(
                    {
                        "pi_model": PiModelVersion.model_validate(
                            message["review_model"]["pi_model"]
                        )
                        .reference()
                        .model_dump(mode="json")
                    }
                    if mode == "review" and message["review_model"].get("pi_model")
                    else {"gateway_model": message["review_model"]}
                    if mode == "review"
                    else {}
                ),
            }

    async def complete(
        self,
        tid: UUID,
        aid: str,
        mid: UUID,
        token: UUID,
        session: dict[str, Any],
        opening_instructions: str,
        origin: str,
        expected_revision: int,
        content: str,
        usage: dict[str, Any],
        *,
        expected_started_at: datetime | None = None,
    ) -> dict[str, Any]:
        content = storage_safe(content)
        usage = storage_safe(usage)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, agent = await self._task_agent(conn, tid, aid, writable=True)
            message = await self._message(conn, tid, aid, mid)
            if message["status"] == "completed" and message["claim_token"] == token:
                reply = (
                    (
                        await conn.execute(
                            select(s.agent_messages).where(s.agent_messages.c.reply_to == mid)
                        )
                    )
                    .mappings()
                    .one()
                )
                return _public(reply)
            self._check_claim(message, token)
            if message["purpose"] == "review":
                _require_original_review(task, agent, message)
            if agent["status"] in ACTIVE or expected_started_at is not None:
                if not await agent_execution_matches(conn, tid, agent, expected_started_at):
                    raise HTTPException(
                        409, {"code": "stale_agent_execution", "message": "Agent execution changed"}
                    )
            await self._save_session(
                conn, tid, aid, session, opening_instructions, origin, expected_revision
            )
            await conn.execute(
                update(s.agent_messages)
                .where(s.agent_messages.c.id == mid)
                .values(status="completed", updated_at=_now())
            )
            reply = (
                (
                    await conn.execute(
                        insert(s.agent_messages)
                        .values(
                            id=uuid4(),
                            task_id=tid,
                            agent_id=aid,
                            role="assistant",
                            content=content,
                            status="completed",
                            reply_to=mid,
                            usage=usage,
                        )
                        .returning(s.agent_messages)
                    )
                )
                .mappings()
                .one()
            )
            written = await self.repo.append(
                conn,
                tid,
                [
                    event(
                        "agent.message.replied",
                        aid,
                        {
                            "id": str(reply["id"]),
                            "reply_to": str(mid),
                            "agent_id": aid,
                            "summary": content[:200],
                        },
                        addressed_to=[aid],
                    )
                ],
            )
        await self.repo.notify(tid, written[-1]["version"])
        return _public(reply)

    async def fail(self, tid: UUID, aid: str, mid: UUID, token: UUID, error: str) -> dict[str, Any]:
        error = storage_safe(error)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task_agent(conn, tid, aid, writable=True)
            message = await self._message(conn, tid, aid, mid)
            self._check_claim(message, token)
            failed = (
                (
                    await conn.execute(
                        update(s.agent_messages)
                        .where(s.agent_messages.c.id == mid)
                        .values(status="failed", error=error[:200], updated_at=_now())
                        .returning(s.agent_messages)
                    )
                )
                .mappings()
                .one()
            )
            written = await self.repo.append(
                conn,
                tid,
                [
                    event(
                        "agent.message.failed",
                        "system",
                        {"id": str(mid), "agent_id": aid, "summary": error[:200]},
                        addressed_to=[aid],
                    )
                ],
            )
        await self.repo.notify(tid, written[-1]["version"])
        return _public(failed)

    async def pending(self) -> list[dict[str, Any]]:
        async with self.repo.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(
                            s.agent_messages.c.task_id,
                            s.agent_messages.c.agent_id,
                            s.agent_messages.c.id,
                        )
                        .join(s.tasks, s.tasks.c.id == s.agent_messages.c.task_id)
                        .join(
                            s.agent_runs,
                            and_(
                                s.agent_runs.c.task_id == s.agent_messages.c.task_id,
                                s.agent_runs.c.id == s.agent_messages.c.agent_id,
                            ),
                        )
                        .where(
                            s.tasks.c.deleting.is_(False),
                            s.agent_messages.c.purpose == "review",
                            s.agent_messages.c.request_run_number == s.tasks.c.run_number,
                            s.agent_messages.c.request_started_at == s.agent_runs.c.started_at,
                            s.agent_messages.c.request_derive_round == s.agent_runs.c.derive_round,
                            s.agent_messages.c.review_model.is_not(None),
                            ~_review_dispatched(),
                            s.agent_runs.c.status.not_in(ACTIVE),
                            or_(
                                s.agent_messages.c.status == "queued",
                                and_(
                                    s.agent_messages.c.status == "processing",
                                    s.agent_messages.c.lease_until <= _now(),
                                ),
                            ),
                        )
                        .order_by(s.agent_messages.c.created_at)
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def recover(self) -> dict[str, int]:
        async with self.repo.engine.connect() as conn:
            task_ids = (
                (
                    await conn.execute(
                        select(s.agent_messages.c.task_id)
                        .where(s.agent_messages.c.status == "processing")
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
        count = 0
        for tid in task_ids:
            async with self.repo.engine.begin() as conn:
                try:
                    await self.repo.lock(conn, tid)
                except KeyError:
                    continue
                await conn.execute(
                    update(s.agent_messages)
                    .where(
                        s.agent_messages.c.task_id == tid,
                        s.agent_messages.c.status == "processing",
                        _review_dispatched(),
                    )
                    .values(
                        status="failed",
                        error="Original review result is unconfirmed",
                        updated_at=_now(),
                    )
                )
                result = await conn.execute(
                    update(s.agent_messages)
                    .where(
                        s.agent_messages.c.task_id == tid,
                        s.agent_messages.c.status == "processing",
                    )
                    .values(status="queued", claim_token=None, lease_until=None, updated_at=_now())
                )
                count += result.rowcount
        return {"requeued": count}

    async def request_delete(self, tid: UUID) -> dict[str, Any]:
        written = []
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
            if task["deleting"]:
                return {"task_id": tid, "deleting": True}
            if task["status"] not in {"created", "finished", "failed", "stopped"}:
                raise HTTPException(409, "Task is still active")
            active_agent = (
                await conn.execute(
                    select(s.agent_runs.c.id)
                    .where(s.agent_runs.c.task_id == tid, s.agent_runs.c.status.in_(ACTIVE))
                    .limit(1)
                )
            ).first()
            processing = (
                await conn.execute(
                    select(s.agent_messages.c.id)
                    .where(
                        s.agent_messages.c.task_id == tid,
                        s.agent_messages.c.status == "processing",
                    )
                    .limit(1)
                )
            ).first()
            ctf_active = None
            if task.get("mode") == "ctf":
                ctf_active = (
                    await conn.execute(
                        select(s.ctf_turns.c.id)
                        .where(s.ctf_turns.c.task_id == tid, s.ctf_turns.c.status == "running")
                        .limit(1)
                    )
                ).first()
            if active_agent or processing or ctf_active:
                raise HTTPException(409, "Task has active work")
            await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(deleting=True))
            if task.get("mode") == "ctf" and business_contract(task) == BOARD_CONTRACT:
                resources = (
                    (
                        await conn.execute(
                            select(s.task_runner_resources).where(
                                s.task_runner_resources.c.task_id == tid
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if resources is not None:
                    data = dict(resources["data"])
                    if not data.get("retirement"):
                        data["retirement"] = {
                            "operation_id": str(
                                uuid5(NAMESPACE_URL, "native-retire:" + resources["operation_id"])
                            ),
                            "policy": "user_delete",
                            "requested_at": _now().isoformat(),
                            "state": "pending",
                            "manifest": {},
                        }
                    await conn.execute(
                        update(s.task_runner_resources)
                        .where(s.task_runner_resources.c.task_id == tid)
                        .values(data=data, revision=s.task_runner_resources.c.revision + 1)
                    )
            await conn.execute(
                update(s.progress_jobs)
                .where(
                    s.progress_jobs.c.task_id == tid,
                    s.progress_jobs.c.status.in_(("queued", "running")),
                )
                .values(status="cancelled")
            )
            cancelled = (
                (
                    await conn.execute(
                        update(s.agent_messages)
                        .where(
                            s.agent_messages.c.task_id == tid, s.agent_messages.c.status == "queued"
                        )
                        .values(status="failed", error="Task deleted", updated_at=_now())
                        .returning(s.agent_messages.c.id, s.agent_messages.c.agent_id)
                    )
                )
                .mappings()
                .all()
            )
            if cancelled:
                written = await self.repo.append(
                    conn,
                    tid,
                    [
                        event(
                            "progress.turn.failed"
                            if row["agent_id"] == "__progress__"
                            else "agent.message.failed",
                            "system",
                            {
                                "id": str(row["id"]),
                                "agent_id": row["agent_id"],
                                "summary": "Task deleted",
                            },
                            addressed_to=[row["agent_id"]],
                        )
                        for row in cancelled
                    ],
                )
        if written:
            await self.repo.notify(tid, written[-1]["version"])
        return {"task_id": tid, "deleting": True}

    async def deletions(self, *, include_protected=False) -> list[UUID]:
        from bbx_blackboard.legacy_network import protected_task

        async with self.repo.engine.connect() as conn:
            statement = select(s.tasks.c.id).where(s.tasks.c.deleting.is_(True))
            if not include_protected:
                statement = statement.where(~protected_task())
            return list((await conn.execute(statement)).scalars())

    async def purge(self, tid: UUID, objects: Any) -> dict[str, bool]:
        from bbx_blackboard.native_traffic import close_archive_for_purge, traffic_purge_guard

        protect_archive = False
        async with self.repo.engine.begin() as conn:
            try:
                await self.repo.lock(conn, tid)
            except KeyError:
                return {"purged": True}
            task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
            if not task["deleting"]:
                raise HTTPException(409, "Task deletion was not requested")
            from bbx_blackboard.legacy_network import require_run_retired

            await require_run_retired(conn, task)
            if task.get("mode") == "ctf" and business_contract(task) == BOARD_CONTRACT:
                retired = False
                native_resources = (
                    (
                        await conn.execute(
                            select(s.task_runner_resources).where(
                                s.task_runner_resources.c.task_id == tid
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if native_resources is not None:
                    never_claimed = (
                        native_resources["config"] is None
                        and native_resources["controller_id"] is None
                        and native_resources["sandbox_create_claimed"] is False
                        and not native_resources["observation"]
                    )
                    retired = (
                        native_resources["state"] == "stopped"
                        and native_resources["data"].get("exit_confirmed") is True
                        and native_resources["data"].get("retirement", {}).get("state") == "retired"
                        and native_resources["data"].get("retirement", {}).get("policy")
                        == "user_delete"
                    )
                    if not never_claimed and not retired:
                        raise HTTPException(
                            409,
                            "Native namespace and retained volumes still require "
                            "ownership retirement",
                        )
                native_route = (
                    await conn.execute(
                        select(s.native_task_mcp_routes.c.publication_state).where(
                            s.native_task_mcp_routes.c.task_id == tid
                        )
                    )
                ).scalar_one_or_none()
                if native_route is not None and native_route != "retired":
                    raise HTTPException(409, "Native MCP route retirement is not confirmed")
                if native_route is not None and (
                    task["status"] not in {"stopped", "failed", "finished"}
                    or (not task["cleanup_ready"] and not retired)
                ):
                    raise HTTPException(409, "Original Runner cleanup is not confirmed")
                native_key = (
                    await conn.execute(
                        select(s.native_task_budgets.c.publication_state).where(
                            s.native_task_budgets.c.task_id == tid
                        )
                    )
                ).scalar_one_or_none()
                if native_key is not None and native_key != "retired":
                    raise HTTPException(409, "Native Task key retirement is not confirmed")
                unresolved = (
                    await conn.execute(
                        select(s.runner_calls.c.id)
                        .where(
                            s.runner_calls.c.task_id == tid,
                            s.runner_calls.c.state.in_(["dispatched", "unknown"]),
                        )
                        .limit(1)
                    )
                ).first()
                if unresolved:
                    raise HTTPException(409, "Original Runner calls still require reconciliation")
                if retired:
                    protect_archive = await close_archive_for_purge(conn, native_resources)
            await require_settled_progress(conn, tid)
        async with traffic_purge_guard(self.repo, tid, protect=protect_archive):
            return await self._purge_material(tid, objects)

    async def _purge_material(self, tid: UUID, objects: Any) -> dict[str, bool]:
        for prefix in (
            f"evidence/{tid}/",
            f"toolcalls/{tid}/",
            f"traces/{tid}/",
            f"inputs/{tid}/",
            f"traffic/{tid}/",
        ):
            for key in await objects.list(prefix):
                await objects.remove(key)
        for key in [f"reports/{tid}.md", f"workspace/{tid}.tar.zst"]:
            await objects.remove(key)
        for prefix in (f"reports/{tid}/", f"workspace/{tid}/"):
            for key in await objects.list(prefix):
                await objects.remove(key)
        async with self.repo.engine.begin() as conn:
            try:
                await self.repo.lock(conn, tid)
            except KeyError:
                return {"purged": True}
            # Review billing remains in the gateway-wide accounting ledger after a Task deletion.
            await conn.execute(
                delete(s.native_request_costs).where(
                    s.native_request_costs.c.task_id == tid,
                    func.coalesce(
                        s.native_request_costs.c.evidence["attributes"]["bbx_purpose"].astext, ""
                    )
                    != "review",
                )
            )
            for table in (
                s.task_runner_resources,
                s.native_task_mcp_routes,
                s.native_task_budgets,
                s.sandbox_command_records,
                s.runner_calls,
                s.runner_receipts,
                s.runner_commands,
                s.runner_bindings,
                s.runner_owners,
                s.flag_submissions,
                s.flag_candidates,
                s.board_announcements,
                s.progress_jobs,
                s.task_progress,
                s.ctf_records,
                s.ctf_challenges,
                s.ctf_messages,
                s.ctf_turns,
                s.ctf_members,
                s.agent_messages,
                s.agent_sessions,
                s.tool_calls,
                s.facts,
                s.intents,
                s.agent_runs,
                s.task_counters,
                s.resume_requests,
                s.task_runs,
                s.events,
            ):
                await conn.execute(delete(table).where(table.c.task_id == tid))
            await conn.execute(
                delete(s.task_input_groups).where(s.task_input_groups.c.bound_task_id == tid)
            )
            await conn.execute(delete(s.tasks).where(s.tasks.c.id == tid))
        return {"purged": True}
