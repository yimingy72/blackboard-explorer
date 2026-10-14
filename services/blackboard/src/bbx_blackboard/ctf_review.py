"""Terminal CTF review shares the session, never execution authority or budget."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from bbx_contracts.ctf import CtfFailureDiagnostic
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
    _retire_checkpoint_barrier: Any

    def _authorize_review(self, task, turn):
        ensure(task["status"] in TERMINAL, "Review requires a terminal task")
        ensure(turn["purpose"] == "review", "Review turn required")

    async def authorize_review(self, tid, agent_id, generation, turn_id=None):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            ensure(task["status"] in TERMINAL, "Review requires a terminal task")
            member, turn = await self._fence(conn, tid, agent_id, generation, turn_id)
            self._authorize_review(task, turn)
            return member

    async def claim_review_turn(self, tid, agent_id, runtime_instance, expected_turn_id=None):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            member = await row(conn, s.ctf_members, tid, agent_id)
            if expected_turn_id is not None and member["current_turn_id"] != str(expected_turn_id):
                return None
            if (
                task["status"] not in TERMINAL
                or not task["cleanup_ready"]
                or task["ctf_control"].get("unresolved_checkpoints")
                or member.get("pending_operation")
            ):
                return None
            if member["current_turn_id"]:
                turn = await row(conn, s.ctf_turns, tid, member["current_turn_id"])
                if (
                    turn["status"] == "running"
                    and turn["purpose"] == "review"
                    and turn["runtime_instance"] == runtime_instance
                    and turn["agent_id"] == agent_id
                    and turn["generation"] == member["generation"]
                ):
                    return turn
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

    async def recover_reviews(
        self,
        tid,
        runtime_instance,
        *,
        failed_turn_id=None,
        agent_id=None,
        generation=None,
        joined=False,
        answer="",
        failure=None,
    ):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            if failed_turn_id is not None:
                ensure(task["status"] in TERMINAL, "Review requires a terminal task")
                ensure(joined is True, "Review writer has not joined")
                turn = await row(conn, s.ctf_turns, tid, str(failed_turn_id))
                ensure(
                    turn["purpose"] == "review"
                    and turn["runtime_instance"] == runtime_instance
                    and turn["agent_id"] == agent_id
                    and type(generation) is int
                    and turn["generation"] == generation,
                    "Joined review identity mismatch",
                )
                if turn["status"] == "finished":
                    return {"recovered": 0, "turn": turn}
                member = await row(conn, s.ctf_members, tid, agent_id)
                ensure(
                    turn["status"] == "running"
                    and member["current_turn_id"] == turn["id"]
                    and member["generation"] == generation
                    and not member.get("pending_operation"),
                    "Joined review owner mismatch",
                )
                ensure(
                    turn["id"] not in task["ctf_control"].get("unresolved_checkpoints", []),
                    "Review checkpoint remains unresolved",
                )
                ensure(isinstance(answer, str), "Review answer must be text")
                saved = await self.sessions._session(conn, tid, agent_id)
                ensure(
                    saved is not None
                    and turn["checkpoint_revision"] > 0
                    and saved["revision"] == turn["checkpoint_revision"],
                    "Review has no confirmed current checkpoint",
                )
                assert saved is not None
                state = saved["session"].get("state", {})
                for key, purpose in (
                    ("ctf_review_usage_outbox", "review"),
                    ("ctf_usage_outbox", "execution"),
                ):
                    for entry in state.get(key, []):
                        ensure(isinstance(entry, dict), "Invalid persisted review usage entry")
                        usage_turn = await row(
                            conn, s.ctf_turns, tid, str(entry.get("turn_id", turn["id"]))
                        )
                        ensure(
                            usage_turn["purpose"] == purpose
                            and usage_turn["agent_id"] == agent_id
                            and entry.get("agent_id", agent_id) == agent_id
                            and entry.get("generation", usage_turn["generation"])
                            == usage_turn["generation"]
                            and entry.get("request_id") in usage_turn["usage_requests"],
                            "Review usage remains unconfirmed",
                        )
                traces = (
                    await conn.execute(
                        select(s.events.c.payload).where(
                            s.events.c.task_id == tid,
                            s.events.c.type == "agent.trace.recorded",
                            s.events.c.payload["turn_id"].astext == turn["id"],
                            s.events.c.payload["agent_id"].astext == agent_id,
                            s.events.c.payload["kind"].astext.in_(["model_output", "model_error"]),
                        )
                    )
                ).scalars()
                confirmed_steps = {
                    trace["step"] for trace in traces if trace.get("generation") == generation
                }
                steps = state.get("ctf_steps", {}).get(turn["id"], 0)
                ensure(
                    type(steps) is int
                    and steps >= 0
                    and set(range(1, steps + 1)).issubset(confirmed_steps),
                    "Review observations remain unconfirmed",
                )
                diagnostic = (
                    CtfFailureDiagnostic.model_validate(failure).model_dump(mode="json")
                    if failure is not None
                    else None
                )
                result = await self._settle_review(conn, tid, member, turn, "interrupted", answer)
                await patch(
                    conn, s.ctf_members, tid, {"generation": member["generation"] + 1}, agent_id
                )
                await self._emit(
                    conn,
                    tid,
                    "coordination.reminded",
                    "system",
                    {
                        "kind": "review_settlement_recovery",
                        "status": "joined_review_settled",
                        "reason": "control_settlement_failed",
                        "turn_id": turn["id"],
                        "agent_id": agent_id,
                        "generation": generation,
                        "runtime_instance": runtime_instance,
                        "failure": diagnostic,
                    },
                )
                return {"recovered": 1, "turn": result}
            ensure(
                agent_id is None
                and generation is None
                and not joined
                and not answer
                and failure is None,
                "Target review ID is required",
            )
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
                result = await self._settle_review(conn, tid, member, turn, "interrupted", "")
                await self._retire_checkpoint_barrier(conn, tid, result)
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
