"""Durable Agent sessions and user-directed messages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from bbx_contracts.storage import storage_safe
from fastapi import HTTPException
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from bbx_blackboard.domain.rules import event
from bbx_blackboard.store import Repository
from bbx_blackboard.store import schema as s

ACTIVE = {"running", "concluding"}
LEASE = timedelta(seconds=300)
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


def _public(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in MESSAGE_FIELDS}


class Conversations:
    def __init__(self, engine: AsyncEngine) -> None:
        self.repo = Repository(engine)

    async def _task_agent(
        self, conn: AsyncConnection, tid: UUID, aid: str, *, writable: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().first()
        if task is None:
            raise HTTPException(404, "Task not found")
        if writable and task["deleting"]:
            raise HTTPException(409, "Task is being deleted")
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
            key: found[key] for key in ("session", "opening_instructions", "origin", "revision")
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
    ) -> dict[str, Any]:
        existing = await self._session(conn, tid, aid)
        revision = existing["revision"] if existing else 0
        if revision != expected_revision:
            raise HTTPException(409, "Session revision conflict")
        session = storage_safe(session)
        opening_instructions = storage_safe(opening_instructions)
        if existing:
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
        return {
            "session": session,
            "opening_instructions": opening_instructions,
            "origin": origin,
            "revision": revision + 1,
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
    ) -> dict[str, Any]:
        written = []
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            _, agent = await self._task_agent(conn, tid, aid, writable=True)
            if (
                agent["task_type"] == "derive"
                and agent["status"] in ACTIVE
                and (int(agent.get("derive_round") or 1) != (expected_derive_round or 1))
            ):
                raise HTTPException(409, "Derive round changed")
            if review_claim is not None:
                message = await self._message(conn, tid, aid, review_claim["id"])
                self._check_claim(message, review_claim["claim_token"])
            for delivery in deliveries:
                message = await self._message(conn, tid, aid, delivery["id"])
                self._check_claim(message, delivery["claim_token"])
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
            _, agent = await self._task_agent(conn, tid, aid)
            stmt = select(s.agent_messages).where(
                s.agent_messages.c.task_id == tid, s.agent_messages.c.agent_id == aid
            )
            if status is not None:
                stmt = stmt.where(s.agent_messages.c.status == status)
            rows = (
                (
                    await conn.execute(
                        stmt.order_by(s.agent_messages.c.created_at, s.agent_messages.c.id)
                    )
                )
                .mappings()
                .all()
            )
            session = await self._session(conn, tid, aid)
        return {
            "messages": [_public(row) for row in rows],
            "session_available": session is not None,
            "session_origin": session["origin"] if session else None,
            "mode": "active" if agent["status"] in ACTIVE else "review",
        }

    async def post_message(self, tid: UUID, aid: str, mid: UUID, content: str) -> dict[str, Any]:
        content = storage_safe(content)
        written = []
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task_agent(conn, tid, aid, writable=True)
            result = await conn.execute(
                insert(s.agent_messages)
                .values(
                    id=mid,
                    task_id=tid,
                    agent_id=aid,
                    role="user",
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
            if (
                mode == "review"
                and agent["task_type"] == "derive"
                and task["status"] not in {"finished", "failed", "stopped"}
            ):
                raise HTTPException(409, "Derive session is reserved for the task")
            message = await self._message(conn, tid, aid, mid)
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
            return {"message": _public(result.mappings().one()), "claim_token": token}

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
    ) -> dict[str, Any]:
        content = storage_safe(content)
        usage = storage_safe(usage)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task_agent(conn, tid, aid, writable=True)
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
                            s.agent_runs.c.status.not_in(ACTIVE),
                            or_(
                                s.agent_runs.c.task_type != "derive",
                                s.tasks.c.status.in_(("finished", "failed", "stopped")),
                            ),
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
            if active_agent or processing:
                raise HTTPException(409, "Task has active work")
            await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(deleting=True))
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
                            "agent.message.failed",
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

    async def deletions(self) -> list[UUID]:
        async with self.repo.engine.connect() as conn:
            return list(
                (
                    await conn.execute(select(s.tasks.c.id).where(s.tasks.c.deleting.is_(True)))
                ).scalars()
            )

    async def purge(self, tid: UUID, objects: Any) -> dict[str, bool]:
        async with self.repo.engine.begin() as conn:
            try:
                await self.repo.lock(conn, tid)
            except KeyError:
                return {"purged": True}
            task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
            if not task["deleting"]:
                raise HTTPException(409, "Task deletion was not requested")
        for prefix in (f"evidence/{tid}/", f"toolcalls/{tid}/", f"traces/{tid}/"):
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
            for table in (
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
            await conn.execute(delete(s.tasks).where(s.tasks.c.id == tid))
        return {"purged": True}
