"""Terminal CTF review shares the session, never execution authority or budget."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import insert, select

from bbx_blackboard.ctf_board import ensure
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import now, patch, row

TERMINAL = {"finished", "failed", "stopped"}


class CtfReviewMixin:
    repo: Any
    objects: Any
    sessions: Any
    _task: Any
    _fence: Any
    _message: Any
    _emit: Any
    _settle: Any

    async def authorize_review(self, tid, agent_id, generation, turn_id=None):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            ensure(task["status"] in TERMINAL, "Review requires a terminal task")
            member, turn = await self._fence(conn, tid, agent_id, generation, turn_id)
            ensure(turn["purpose"] == "review", "Review turn required")
            return member

    async def claim_review_turn(self, tid, agent_id, runtime_instance):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            member = await row(conn, s.ctf_members, tid, agent_id)
            if (
                task["status"] not in TERMINAL
                or not task["cleanup_ready"]
                or task["ctf_control"].get("unresolved_checkpoints")
                or member["current_turn_id"]
                or member.get("pending_operation")
            ):
                return None
            pending = (
                await conn.execute(
                    select(s.ctf_messages.c.id)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.recipient_id == agent_id,
                        s.ctf_messages.c.purpose == "review",
                        s.ctf_messages.c.sender_kind == "user",
                        s.ctf_messages.c.status == "queued",
                        s.ctf_messages.c.deferred.is_(False),
                    )
                    .order_by(s.ctf_messages.c.recipient_sequence)
                    .limit(1)
                )
            ).first()
            if pending is None:
                return None
            values = dict(
                id=str(uuid4()),
                agent_id=agent_id,
                generation=member["generation"] + 1,
                runtime_instance=runtime_instance,
                status="running",
                purpose="review",
                assignment_message_ids=[],
                explicit_reply_ids=[],
                checkpoint_revision=0,
                usage={},
                usage_requests=[],
                started_at=now(),
            )
            await conn.execute(insert(s.ctf_turns).values(task_id=tid, **values))
            # Review does not change removed/stopped lifecycle or execution run state.
            await patch(
                conn,
                s.ctf_members,
                tid,
                {"generation": values["generation"], "current_turn_id": values["id"]},
                agent_id,
            )
            await self._emit(conn, tid, "turn.started", "system", values)
            return values

    async def recover_reviews(self, tid, runtime_instance):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            if task["status"] not in TERMINAL:
                return {"recovered": 0}
            turns = list(
                (
                    await conn.execute(
                        select(s.ctf_turns).where(
                            s.ctf_turns.c.task_id == tid,
                            s.ctf_turns.c.purpose == "review",
                            s.ctf_turns.c.status == "running",
                            s.ctf_turns.c.runtime_instance != runtime_instance,
                        )
                    )
                ).mappings()
            )
            for turn in turns:
                member = await row(conn, s.ctf_members, tid, turn["agent_id"])
                ensure(member["current_turn_id"] == turn["id"], "Review owner mismatch")
                await self._settle_review(conn, tid, member, turn, "interrupted", "")
                await patch(
                    conn, s.ctf_members, tid, {"generation": member["generation"] + 1}, member["id"]
                )
            return {"recovered": len(turns)}

    async def _settle_review(self, conn, tid, member, turn, end_reason, answer):
        messages = list(
            (
                await conn.execute(
                    select(s.ctf_messages).where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.purpose == "review",
                        s.ctf_messages.c.claim_turn_id == turn["id"],
                        s.ctf_messages.c.sender_kind == "user",
                    )
                )
            ).mappings()
        )
        for message in messages:
            if message["status"] == "leased":
                # Nothing durable consumed this input: leave it available to a new review.
                await patch(
                    conn,
                    s.ctf_messages,
                    tid,
                    {
                        "status": "queued",
                        "claim_token": None,
                        "claim_turn_id": None,
                        "claim_generation": None,
                        "lease_until": None,
                    },
                    message["id"],
                )
                continue
            if message["status"] != "delivered":
                continue
            reply = await self._message(
                conn,
                tid,
                member["id"],
                member["id"],
                answer[:20000] or "Review interrupted; no completed answer was saved.",
                str(uuid5(NAMESPACE_URL, f"ctf-review:{tid}:{turn['id']}:{message['id']}")),
                "review_result",
                reply_to=message["id"],
                source_turn_id=turn["id"],
                notification_purpose=f"review_result:{message['id']}",
                purpose="review",
            )
            await patch(
                conn,
                s.ctf_messages,
                tid,
                {
                    "status": "delivered",
                    "delivered_turn_id": turn["id"],
                    "session_revision": turn["checkpoint_revision"],
                },
                reply["id"],
            )
        values = {
            "status": "finished",
            "end_reason": end_reason,
            "final_answer": answer,
            "finished_at": now(),
        }
        await patch(conn, s.ctf_turns, tid, values, turn["id"])
        await patch(conn, s.ctf_members, tid, {"current_turn_id": None}, member["id"])
        await self._emit(
            conn,
            tid,
            "turn.finished",
            member["id"],
            {"id": turn["id"], "purpose": "review", **values},
        )
        return {**turn, **values}

    async def read_review_artifact(
        self, tid, agent_id, turn_id, generation, artifact_id, offset=0, limit=16384
    ):
        ensure(
            isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0,
            "Invalid offset",
        )
        ensure(
            isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 16384,
            "Invalid limit",
        )
        await self.authorize_review(tid, agent_id, generation, turn_id)
        async with self.repo.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(s.ctf_records.c.data).where(s.ctf_records.c.task_id == tid)
                )
            ).scalars()
            artifact = next(
                (
                    record["artifact"]
                    for record in rows
                    if record.get("kind") == "artifact_registration"
                    and record["artifact"]["id"] == artifact_id
                ),
                None,
            )
        ensure(artifact is not None, "Registered artifact not found")
        assert artifact is not None
        ensure(self.objects is not None, "Object storage unavailable")
        content = await self.objects.get(artifact["uri"])
        await self.authorize_review(tid, agent_id, generation, turn_id)
        data = content[offset : offset + limit]
        return {
            "artifact": artifact,
            "text": data.decode("utf-8", errors="replace"),
            "offset": offset,
            "next_offset": offset + len(data) if offset + len(data) < len(content) else None,
        }
