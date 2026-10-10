"""Transactional CTF mailbox, persistent turns and generation fencing."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from bbx_contracts.billing import add_usage
from bbx_contracts.ctf import CtfConclusion
from bbx_contracts.models import Usage
from bbx_contracts.storage import storage_safe
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bbx_blackboard.conversations import Conversations
from bbx_blackboard.ctf_board import CtfBoardMixin
from bbx_blackboard.ctf_lifecycle import CtfLifecycleMixin
from bbx_blackboard.ctf_platform import CtfPlatformMixin
from bbx_blackboard.ctf_review import CtfReviewMixin
from bbx_blackboard.input_groups import InputGroups, check_mutable, check_owner
from bbx_blackboard.store import Repository
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import now, patch, row


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise HTTPException(409, detail)


def history_ids(session: dict[str, Any]) -> set[str]:
    """Collect stable IDs from persisted history, never queued pending inputs."""
    messages = session.get("state", {}).get("in_memory", {}).get("messages", [])
    ids = [
        str(message["message_id"])
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user" and message.get("message_id")
    ]
    require(len(ids) == len(set(ids)), "Duplicate history message ID")
    return set(ids)


def _reservations(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    reservations = task["ctf_control"].get("budget_reservations", {})
    return dict(reservations) if isinstance(reservations, dict) else {}


def _reserved_cost(task: dict[str, Any]) -> Decimal:
    control = task["ctf_control"]
    entries = [
        *_reservations(task).values(),
        *(
            control.get("budget_unknown_reservations", {}).values()
            if isinstance(control.get("budget_unknown_reservations", {}), dict)
            else []
        ),
    ]
    return sum(
        (Decimal(str(item.get("cost", 0))) for item in entries),
        Decimal(0),
    )


def check_budget(task: dict[str, Any], reservation_cost: Decimal | str = Decimal(0)) -> None:
    require(
        task["ctf_control"].get("replacement", {}).get("phase", "ready") == "ready",
        "Execution replacement is in progress",
    )
    require(task["ctf_control"]["phase"] == "running", "Task is not running")
    active = task["active_seconds"] + (
        max(0, (now() - task["active_since"]).total_seconds()) if task["active_since"] else 0
    )
    reservation = Decimal(str(reservation_cost))
    require(reservation.is_finite() and reservation >= 0, "Invalid budget reservation")
    spent = Decimal(str(task["usage"].get("cost", 0)))
    remaining = Decimal(str(task["budget"]["max_cost"])) - spent - _reserved_cost(task)
    require(
        (remaining > 0 if not reservation else reservation <= remaining)
        and active < task["budget"]["max_minutes"] * 60,
        "Task budget exhausted",
    )


def initial_input_parts(fields: dict[str, Any]) -> list[str]:
    """Keep all original input in ordered, bounded, user-authored messages."""
    content = json.dumps(
        {
            "goal": fields["goal"],
            "domain_context": fields.get("domain_context"),
            "completion_requirements": fields["ctf_control"].get("completion_requirements"),
            "initial_attachments": fields["initial_attachments"],
        },
        ensure_ascii=False,
        indent=2,
    )
    parts = [content[offset : offset + 18000] for offset in range(0, len(content), 18000)]
    return [
        f"Initial task input (part {index}/{len(parts)}):\n{part}"
        for index, part in enumerate(parts, start=1)
    ]


class CtfService(CtfLifecycleMixin, CtfReviewMixin, CtfPlatformMixin, CtfBoardMixin):
    def __init__(self, engine: AsyncEngine, objects=None) -> None:
        self.objects = objects
        self.repo = Repository(engine)
        self.sessions = Conversations(engine)

    async def _task(self, conn, tid):
        await self.repo.lock(conn, tid)
        task = await row(conn, s.tasks, tid)
        require(task["mode"] == "ctf", "Task mode is not CTF")
        require(not task["deleting"], "Task is deleting")
        return task

    async def _emit(self, conn, tid, kind, actor, payload):
        addressed_to = None
        if kind.startswith("message."):
            message = await row(conn, s.ctf_messages, tid, payload["id"])
            addressed_to = [message["sender_id"], message["recipient_id"]]
        elif kind.startswith("turn."):
            turn = await row(conn, s.ctf_turns, tid, payload["id"])
            addressed_to = [turn["agent_id"]]
        await self.repo.append(
            conn,
            tid,
            [
                {
                    "type": f"ctf.{kind}",
                    "actor": actor,
                    "object_id": payload.get("id"),
                    "payload": jsonable_encoder(payload),
                    "addressed_to": addressed_to,
                }
            ],
        )

    async def _fence(self, conn, tid, aid, generation, turn_id=None):
        member = await row(conn, s.ctf_members, tid, aid)
        require(
            member["generation"] == generation,
            "Stale CTF generation or inactive member",
        )
        require(
            member["current_turn_id"] is not None
            and (turn_id is None or member["current_turn_id"] == str(turn_id)),
            "Stale CTF turn",
        )
        turn = await row(conn, s.ctf_turns, tid, member["current_turn_id"])
        require(turn["status"] == "running", "CTF turn is not running")
        if turn["purpose"] == "review":
            task = await row(conn, s.tasks, tid)
            require(task["status"] in {"finished", "failed", "stopped"}, "Review task resumed")
        else:
            require(member["lifecycle"] == "active", "Inactive execution member")
        return member, turn

    async def authorize_member(
        self,
        tid,
        agent_id,
        generation,
        turn_id=None,
        reservation_id=None,
        reservation_cost: Decimal | str = Decimal(0),
    ):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            member, turn = await self._fence(conn, tid, agent_id, generation, turn_id)
            require(turn["purpose"] == "execution", "Execution turn required")
            require(
                member["run_state"] != "stopping" and not member.get("pending_operation"),
                "Member is stopping",
            )
            cost = Decimal(str(reservation_cost))
            require(cost.is_finite() and cost >= 0, "Invalid budget reservation")
            reservations = _reservations(task)
            key = str(reservation_id) if reservation_id is not None else None
            if key is not None and key in reservations:
                existing = reservations[key]
                require(
                    existing["turn_id"] == str(turn["id"])
                    and existing["agent_id"] == agent_id
                    and Decimal(str(existing["cost"])) == cost,
                    "Budget reservation conflict",
                )
                return member
            require(
                not any(item.get("turn_id") == str(turn["id"]) for item in reservations.values()),
                "A model call is already reserved for this turn",
            )
            check_budget(task, cost)
            if key is not None and cost:
                reservations[key] = {
                    "agent_id": agent_id,
                    "turn_id": str(turn["id"]),
                    "generation": generation,
                    "cost": str(cost),
                    "state": "admitted",
                }
                await patch(
                    conn,
                    s.tasks,
                    tid,
                    {"ctf_control": {**task["ctf_control"], "budget_reservations": reservations}},
                )
            return member

    async def mark_reservation_sent(self, tid, agent_id, generation, turn_id, reservation_id):
        """Fence a reservation before entering the provider call boundary."""
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            member, turn = await self._fence(conn, tid, agent_id, generation, turn_id)
            require(turn["purpose"] == "execution", "Execution turn required")
            key = str(reservation_id)
            reservations = _reservations(task)
            reservation = reservations.get(key)
            require(
                reservation is not None
                and reservation["agent_id"] == agent_id
                and reservation["turn_id"] == str(turn["id"])
                and reservation["generation"] == generation,
                "Budget reservation is missing or stale",
            )
            assert reservation is not None
            if reservation.get("state") == "sent":
                return member
            require(
                reservation.get("state", "unknown") == "admitted",
                "Budget reservation is closed",
            )
            reservations[key] = {**reservation, "state": "sent"}
            await patch(
                conn,
                s.tasks,
                tid,
                {"ctf_control": {**task["ctf_control"], "budget_reservations": reservations}},
            )
            return member

    async def authorize_reader(self, tid, agent_id, generation):
        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            member = await row(conn, s.ctf_members, tid, agent_id)
            require(member["generation"] == generation, "Stale CTF reader")
            if member["lifecycle"] == "removed":
                _, turn = await self._fence(conn, tid, agent_id, generation)
                require(turn["purpose"] == "review", "Removed member has no reader authority")
            return member

    async def _actor(self, conn, tid, actor, generation, turn_id):
        if actor not in {"user", "system", "service"}:
            task = await row(conn, s.tasks, tid)
            require(
                task["ctf_control"].get("replacement", {}).get("phase", "ready") == "ready",
                "Execution replacement is in progress",
            )
            member, turn = await self._fence(conn, tid, actor, generation, turn_id)
            require(turn["purpose"] == "execution", "Review cannot mutate execution state")
            require(
                member["run_state"] != "stopping" and not member.get("pending_operation"),
                "Member is stopping",
            )

    async def create_task(
        self,
        spec,
        *,
        profile_version=1,
        input_group_id=None,
        input_file_ids=None,
        input_owner=None,
        create_request_hash=None,
    ):
        from bbx_contracts.ctf import CtfTaskSpec

        spec = CtfTaskSpec.model_validate(spec)
        data = spec.model_dump(mode="json")
        tid = input_group_id or uuid4()
        fields = dict(
            name=data.get("name"),
            goal=data["goal"],
            initial_attachments=[],
            domain_context=data.get("domain_context"),
            egress_allowlist=data.get("egress_allowlist", []),
            acceptance=[],
            acceptance_state={},
            budget=data["budget"],
            params={},
            agent_profile=data.get("agent_profile", "ctf"),
            agent_profile_version=profile_version,
            status="created",
            usage={},
            mode="ctf",
            ctf_options=data["ctf_options"],
            ctf_control={
                "phase": "created",
                "epoch": 0,
                "completion_requirements": data.get("completion_requirements"),
            },
        )
        async with self.repo.engine.begin() as conn:
            if input_group_id is not None:
                group = await InputGroups.row(conn, input_group_id, lock=True)
                check_owner(group, input_owner or "")
                require(bool(create_request_hash), "A grouped task requires a request hash")
                if group["bound_task_id"] is not None:
                    require(
                        group["create_request_hash"] == create_request_hash,
                        "Input group request conflict",
                    )
                    existing = await row(conn, s.tasks, tid)
                    require(
                        existing["mode"] == "ctf" and not existing["deleting"],
                        "Input task unavailable",
                    )
                    return tid
                check_mutable(group, input_owner or "")
                files = group["files"]
                if input_file_ids is not None:
                    selected = {str(file_id) for file_id in input_file_ids}
                    require(selected <= {item["id"] for item in files}, "Input file missing")
                    files = [item for item in files if item["id"] in selected]
                require(self.objects is not None or not files, "Object storage is unavailable")
                for item in files:
                    assert self.objects is not None
                    require(await self.objects.exists(item["uri"]), "Input object missing")
                fields["initial_attachments"] = files
                await conn.execute(
                    update(s.task_input_groups)
                    .where(s.task_input_groups.c.id == tid)
                    .values(files=files, create_request_hash=create_request_hash)
                )
            await conn.execute(insert(s.tasks).values(id=tid, **fields))
            if input_group_id is not None:
                await conn.execute(
                    update(s.task_input_groups)
                    .where(s.task_input_groups.c.id == tid)
                    .values(bound_task_id=tid)
                )
            await self.repo.append(
                conn,
                tid,
                [
                    {
                        "type": "task.created",
                        "actor": "user",
                        "object_id": None,
                        "payload": fields,
                        "addressed_to": None,
                    }
                ],
            )
            await self._member(conn, tid, "lead", "Lead", str(uuid4()), "lead")
            for body in initial_input_parts(fields):
                await self._message(conn, tid, "user", "lead", body, str(uuid4()), "instruction")
        return tid

    async def _member(self, conn, tid, aid, name, request_id, role):
        values = dict(
            id=aid,
            role=role,
            display_name=name,
            normalized_name=name.casefold().strip(),
            create_request_id=str(request_id),
            lifecycle="active",
            run_state="idle",
            generation=0,
            current_turn_id=None,
            usage={},
            created_at=now(),
        )
        await conn.execute(insert(s.ctf_members).values(task_id=tid, **values))
        await self._emit(conn, tid, "member.created", "system", values)
        return {**values, "agent_id": aid}

    async def provision(self, tid):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            require(task["status"] in {"created", "provisioning"}, "Task cannot provision")
            await patch(conn, s.tasks, tid, {"status": "provisioning"})
        return await self.state(tid)

    async def start(self, tid):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            require(task["status"] in {"created", "provisioning", "running"}, "Task cannot start")
            if task["status"] != "running":
                await patch(
                    conn,
                    s.tasks,
                    tid,
                    {
                        "status": "running",
                        "ctf_control": {**task["ctf_control"], "phase": "running"},
                        "active_since": now(),
                        "started_at": task["started_at"] or now(),
                        "run_started": True,
                    },
                )
                await self._emit(conn, tid, "started", "system", {})
        return await self.state(tid)

    async def fail_start(self, tid, reason="CTF execution startup failed", failure=None):
        """Persist a provisioning failure instead of retrying it forever."""
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            if task["status"] in {"finished", "failed", "stopped"}:
                return task
            require(task["status"] in {"created", "provisioning"}, "Task already started")
            summary = str(reason)[:2000]
            conclusion = CtfConclusion(
                end_reason="system_failure",
                summary=summary,
                failure=failure,
            ).model_dump(mode="json")
            control = {**task["ctf_control"], "phase": "closed", "start_error": summary}
            await patch(
                conn,
                s.tasks,
                tid,
                {
                    "status": "failed",
                    "ctf_control": control,
                    "ctf_conclusion": conclusion,
                    "finished_at": now(),
                },
            )
            await self._emit(
                conn,
                tid,
                "conclusion.finalized",
                "system",
                {"conclusion": conclusion, "status": "failed"},
            )
            return {
                **task,
                "status": "failed",
                "ctf_control": control,
                "ctf_conclusion": conclusion,
            }

    async def state(self, tid):
        async with self.repo.engine.connect() as conn:
            task = await row(conn, s.tasks, tid)
            require(task["mode"] == "ctf", "Task mode is not CTF")
            result: dict[str, Any] = {"task": task}
            for key, table in (
                ("members", s.ctf_members),
                ("turns", s.ctf_turns),
                ("messages", s.ctf_messages),
            ):
                result[key] = [
                    dict(r)
                    for r in (
                        await conn.execute(select(table).where(table.c.task_id == tid))
                    ).mappings()
                ]
            result["challenges"] = [
                item["data"]
                for item in (
                    await conn.execute(
                        select(s.ctf_challenges).where(s.ctf_challenges.c.task_id == tid)
                    )
                ).mappings()
            ]
            records = [
                item["data"]
                for item in (
                    await conn.execute(
                        select(s.ctf_records)
                        .where(s.ctf_records.c.task_id == tid)
                        .order_by(s.ctf_records.c.version)
                    )
                ).mappings()
            ]
            result["records"] = [
                item for item in records if item.get("kind") != "artifact_registration"
            ]
            result["artifacts"] = [
                item["artifact"] for item in records if item.get("kind") == "artifact_registration"
            ]
            for member in result["members"]:
                member["agent_id"] = member["id"]
            return result

    async def list_members(self, tid):
        return {"members": (await self.state(tid))["members"]}

    async def list_team(self, tid):
        return await self.list_members(tid)

    async def create_member(
        self, tid, actor, display_name, request_id, generation=None, turn_id=None
    ):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            await self._actor(conn, tid, actor, generation, turn_id)
            require(
                actor in {"lead", "user", "system", "service"}, "Only Lead can create teammates"
            )
            require(task["ctf_control"]["phase"] == "running", "Task is not running")
            members = list(
                (
                    await conn.execute(select(s.ctf_members).where(s.ctf_members.c.task_id == tid))
                ).mappings()
            )
            for member in members:
                if member["create_request_id"] == str(request_id):
                    require(member["display_name"] == display_name, "Request ID conflict")
                    return {**member, "agent_id": member["id"]}
            require(
                bool(display_name.strip()) and len(display_name) <= 100,
                "Name must contain 1 to 100 characters",
            )
            require(
                not any(m["normalized_name"] == display_name.casefold().strip() for m in members),
                "Name is reserved",
            )
            require(
                sum(m["role"] != "lead" and m["lifecycle"] != "removed" for m in members)
                < task["ctf_options"]["max_teammates"],
                "Teammate limit reached",
            )
            return await self._member(
                conn, tid, f"member-{len(members)}", display_name, request_id, "teammate"
            )

    async def _message(
        self,
        conn,
        tid,
        actor,
        recipient_id,
        body,
        message_id,
        kind="message",
        reply_to=None,
        deferred=False,
        source_turn_id=None,
        notification_purpose=None,
        purpose="execution",
    ):
        try:
            message_id = str(UUID(str(message_id)))
            if reply_to is not None:
                reply_to = str(UUID(str(reply_to)))
        except ValueError as exc:
            raise HTTPException(422, "Message IDs must be UUIDs") from exc
        existing = (
            (
                await conn.execute(
                    select(s.ctf_messages).where(
                        s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == str(message_id)
                    )
                )
            )
            .mappings()
            .first()
        )
        if existing:
            require(
                all(
                    existing[k] == v
                    for k, v in {
                        "sender_id": actor,
                        "recipient_id": recipient_id,
                        "body": body,
                        "kind": kind,
                        "reply_to": reply_to,
                        "purpose": purpose,
                    }.items()
                ),
                "Message ID conflict",
            )
            return dict(existing)
        recipient = await row(conn, s.ctf_members, tid, recipient_id)
        require(purpose == "review" or recipient["lifecycle"] != "removed", "Recipient is removed")
        require(purpose == "review" or actor != recipient_id, "Cannot send a message to yourself")
        require(0 < len(body) <= 20000, "Message must contain 1 to 20000 characters")
        sequence = (
            await conn.execute(
                select(func.coalesce(func.max(s.ctf_messages.c.recipient_sequence), 0)).where(
                    s.ctf_messages.c.task_id == tid, s.ctf_messages.c.recipient_id == recipient_id
                )
            )
        ).scalar_one() + 1
        values = dict(
            id=str(message_id),
            sender_kind=actor if actor in {"user", "system"} else "agent",
            sender_id=actor,
            recipient_id=recipient_id,
            recipient_sequence=sequence,
            kind=kind,
            body=body,
            purpose=purpose,
            reply_to=reply_to,
            status="queued",
            deferred=deferred,
            source_turn_id=source_turn_id,
            notification_purpose=notification_purpose,
            created_at=now(),
        )
        await conn.execute(insert(s.ctf_messages).values(task_id=tid, **values))
        await self._emit(conn, tid, "message.posted", actor, values)
        return values

    async def post_message(
        self,
        tid,
        actor,
        recipient_id,
        body,
        message_id,
        kind="message",
        reply_to=None,
        generation=None,
        turn_id=None,
    ):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            if actor == "user" and task["status"] in {"finished", "failed", "stopped"}:
                return await self._message(
                    conn, tid, actor, recipient_id, body, message_id, "review", purpose="review"
                )
            await self._actor(conn, tid, actor, generation, turn_id)
            phase = task["ctf_control"]["phase"]
            require(
                phase == "running" or (phase == "closing" and actor == "user"),
                "Task is not accepting execution messages",
            )
            require(kind in {"instruction", "message", "help_request"}, "Reserved message kind")
            result = await self._message(
                conn, tid, actor, recipient_id, body, message_id, kind, reply_to, phase == "closing"
            )
            if reply_to and recipient_id == "lead" and actor not in {"user", "system", "service"}:
                member = await row(conn, s.ctf_members, tid, actor)
                turn = await row(conn, s.ctf_turns, tid, member["current_turn_id"])
                if reply_to in turn["assignment_message_ids"]:
                    await patch(
                        conn,
                        s.ctf_turns,
                        tid,
                        {
                            "explicit_reply_ids": list(
                                dict.fromkeys([*turn["explicit_reply_ids"], str(message_id)])
                            )
                        },
                        turn["id"],
                    )
            return result

    async def enqueue_continuation(self, tid, agent_id, turn_id, generation):
        """Queue one bounded continuation after an application context boundary."""
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            require(task["ctf_control"]["phase"] == "running", "Task is not running")
            member = await row(conn, s.ctf_members, tid, agent_id)
            require(member["lifecycle"] == "active", "Inactive execution member")
            require(not member.get("pending_operation"), "Member is stopping")
            require(member["current_turn_id"] is None, "Member already has a running turn")
            turn = await row(conn, s.ctf_turns, tid, str(turn_id))
            require(
                turn["agent_id"] == agent_id
                and turn["generation"] == generation
                and turn["status"] == "finished"
                and turn["purpose"] == "execution"
                and turn["end_reason"] == "context_limit",
                "Context-limited turn is not continuable",
            )
            message_id = str(uuid5(NAMESPACE_URL, f"bbx:ctf:context-continuation:{tid}:{turn_id}"))
            existing = (
                (
                    await conn.execute(
                        select(s.ctf_messages).where(
                            s.ctf_messages.c.task_id == tid,
                            s.ctf_messages.c.id == message_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                return dict(existing)
            check_budget(task)
            return await self._message(
                conn,
                tid,
                "system",
                agent_id,
                "继续处理当前任务。上一轮达到上下文边界；请基于已保存的会话、团队消息和记录继续执行，避免重复已完成工作。",
                message_id,
                kind="instruction",
                source_turn_id=str(turn_id),
                notification_purpose="context_continuation",
            )

    async def claim_turn(self, tid, agent_id, runtime_instance):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            member = await row(conn, s.ctf_members, tid, agent_id)
            if (
                task["ctf_control"]["phase"] != "running"
                or member["lifecycle"] != "active"
                or member["run_state"] == "stopping"
                or task["ctf_control"].get("replacement", {}).get("phase", "ready") != "ready"
                or member.get("pending_operation")
                or member["current_turn_id"]
            ):
                return None
            check_budget(task)
            pending = (
                await conn.execute(
                    select(s.ctf_messages.c.id, s.ctf_messages.c.kind)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.recipient_id == agent_id,
                        s.ctf_messages.c.status == "queued",
                        s.ctf_messages.c.purpose == "execution",
                        s.ctf_messages.c.deferred.is_(False),
                    )
                    .order_by(s.ctf_messages.c.recipient_sequence)
                    .limit(1)
                )
            ).first()
            if not pending:
                return None
            values = dict(
                id=str(uuid4()),
                agent_id=agent_id,
                generation=member["generation"] + 1,
                runtime_instance=runtime_instance,
                status="running",
                purpose="execution",
                assignment_message_ids=[pending.id] if pending.kind == "instruction" else [],
                explicit_reply_ids=[],
                checkpoint_revision=0,
                usage={},
                usage_requests=[],
                started_at=now(),
            )
            await conn.execute(insert(s.ctf_turns).values(task_id=tid, **values))
            await patch(
                conn,
                s.ctf_members,
                tid,
                {
                    "generation": values["generation"],
                    "current_turn_id": values["id"],
                    "run_state": "running",
                },
                agent_id,
            )
            await self._emit(conn, tid, "turn.started", "system", values)
            return values

    async def claim_messages(self, tid, agent_id, turn_id, generation):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            member, turn = await self._fence(conn, tid, agent_id, generation, turn_id)
            if (
                (turn["purpose"] == "execution" and task["ctf_control"]["phase"] != "running")
                or member.get("pending_operation")
                or task["ctf_control"].get("replacement", {}).get("phase", "ready") != "ready"
            ):
                return []
            messages = (
                await conn.execute(
                    select(s.ctf_messages)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.recipient_id == agent_id,
                        s.ctf_messages.c.status.in_(["queued", "leased"]),
                        s.ctf_messages.c.purpose == turn["purpose"],
                        s.ctf_messages.c.kind != "review_result",
                        s.ctf_messages.c.deferred.is_(False),
                    )
                    .order_by(s.ctf_messages.c.recipient_sequence)
                )
            ).mappings()
            result = []
            for message in messages:
                values = {
                    "status": "leased",
                    "claim_turn_id": str(turn_id),
                    "claim_generation": generation,
                    "claim_token": message["claim_token"] or str(uuid4()),
                    "lease_until": now() + timedelta(seconds=300),
                }
                require(
                    message["claim_turn_id"] in {None, str(turn_id)},
                    "Mailbox owned by another turn",
                )
                await patch(conn, s.ctf_messages, tid, values, message["id"])
                result.append({**message, **values})
            return result

    async def renew_turn(self, tid, agent_id, turn_id, generation):
        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            await self._fence(conn, tid, agent_id, generation, turn_id)
            await conn.execute(
                update(s.ctf_messages)
                .where(
                    s.ctf_messages.c.task_id == tid,
                    s.ctf_messages.c.claim_turn_id == str(turn_id),
                    s.ctf_messages.c.claim_generation == generation,
                    s.ctf_messages.c.status == "leased",
                )
                .values(lease_until=now() + timedelta(seconds=300))
            )
        return {"renewed": True}

    async def authorize_object(self, tid, agent_id, uri):
        if await self.authorize_shared_artifact(tid, uri) or await self.authorize_platform_object(
            tid, uri
        ):
            return True
        async with self.repo.engine.connect() as conn:
            match = (
                await conn.execute(
                    select(s.events.c.version)
                    .where(
                        s.events.c.task_id == tid,
                        s.events.c.type == "agent.trace.recorded",
                        s.events.c.payload["agent_id"].astext == agent_id,
                        s.events.c.payload["uri"].astext == uri,
                    )
                    .limit(1)
                )
            ).first()
            return match is not None

    async def record_observation(self, tid, agent_id, turn_id, generation, kind, body, request_id):
        """Persist full diagnostics off-lock, then fence and register bounded references."""
        require(
            kind
            in {
                "initial_context",
                "turn_context",
                "model_output",
                "model_error",
                "tool_call",
                "tool_result",
            },
            "Unsupported observation kind",
        )
        require(self.objects is not None, "Object storage is unavailable")
        encoded = json.dumps(
            storage_safe(jsonable_encoder(body)), ensure_ascii=False, sort_keys=True
        ).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        observation_id = hashlib.sha256(
            f"{agent_id}:{turn_id}:{kind}:{request_id}".encode()
        ).hexdigest()

        async def existing(conn):
            condition = s.events.c.payload["observation_id"].astext == observation_id
            if kind == "initial_context":
                condition = (s.events.c.payload["agent_id"].astext == agent_id) & (
                    s.events.c.payload["kind"].astext == kind
                )
            found = (
                await conn.execute(
                    select(s.events.c.payload)
                    .where(
                        s.events.c.task_id == tid,
                        s.events.c.type == "agent.trace.recorded",
                        condition,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if found and kind != "initial_context":
                require(found["sha256"] == digest, "Observation request ID conflict")
            return found

        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            await self._fence(conn, tid, agent_id, generation, turn_id)
            previous = await existing(conn)
            if previous:
                return previous
        uri = f"traces/{tid}/{agent_id}/{uuid4()}.json"
        assert self.objects is not None
        await self.objects.put(uri, encoded, content_type="application/json")
        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            await self._fence(conn, tid, agent_id, generation, turn_id)
            previous = await existing(conn)
            if previous:
                return previous
            payload = {
                "agent_id": agent_id,
                "turn_id": str(turn_id),
                "generation": generation,
                "kind": kind,
                "uri": uri,
                "observation_id": observation_id,
                "sha256": digest,
                "step": 0 if kind == "initial_context" else max(1, int(body.get("step", 1))),
                "summary": kind,
                "mode": "ctf",
            }
            entries = [
                {
                    "type": "agent.trace.recorded",
                    "actor": agent_id,
                    "object_id": observation_id,
                    "payload": payload,
                    "addressed_to": [agent_id],
                }
            ]
            if kind == "tool_result":
                entries.append(
                    {
                        "type": "tool_call.recorded",
                        "actor": agent_id,
                        "object_id": observation_id,
                        "addressed_to": [agent_id],
                        "payload": {
                            "id": observation_id,
                            "agent_id": agent_id,
                            "tool": str(body.get("tool", "unknown"))[:200],
                            "args": {"full_payload_uri": uri},
                            "result_uri": uri,
                            "result_head": "Full payload in linked observation.",
                            "turn_id": str(turn_id),
                            "generation": generation,
                            "mode": "ctf",
                        },
                    }
                )
            written = await self.repo.append(conn, tid, entries)
        await self.repo.notify(tid, written[-1]["version"])
        return payload

    async def checkpoint_failed(self, tid, agent_id, turn_id, generation):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            await self._fence(conn, tid, agent_id, generation, turn_id)
            control = task["ctf_control"]
            unresolved = list(
                dict.fromkeys([*control.get("unresolved_checkpoints", []), str(turn_id)])
            )
            await patch(
                conn,
                s.tasks,
                tid,
                {"ctf_control": {**control, "unresolved_checkpoints": unresolved}},
            )
        return {"checkpoint_unresolved": True}

    async def checkpoint(
        self,
        tid,
        agent_id,
        turn_id,
        generation,
        session,
        opening_instructions,
        expected_revision,
        deliveries,
    ):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            _, turn = await self._fence(conn, tid, agent_id, generation, turn_id)
            ids = history_ids(session)
            accepted = []
            for delivery in deliveries:
                mid = str(delivery.get("message_id", delivery.get("id")))
                msg = await row(conn, s.ctf_messages, tid, mid)
                require(mid in ids, "Delivery is absent from persisted history")
                require(
                    msg["recipient_id"] == agent_id
                    and msg["purpose"] == turn["purpose"]
                    and msg["claim_turn_id"] == str(turn_id)
                    and msg["claim_generation"] == generation
                    and msg["claim_token"] == str(delivery["claim_token"]),
                    "Stale mailbox claim",
                )
                accepted.append(msg)
            saved = await self.sessions._save_session(
                conn, tid, agent_id, session, opening_instructions, "native", expected_revision
            )
            assignments = list(turn["assignment_message_ids"])
            for msg in accepted:
                await patch(
                    conn,
                    s.ctf_messages,
                    tid,
                    {
                        "status": "delivered",
                        "delivered_turn_id": str(turn_id),
                        "session_revision": saved["revision"],
                    },
                    msg["id"],
                )
                if (
                    turn["purpose"] == "execution"
                    and msg["kind"] == "instruction"
                    and msg["id"] not in assignments
                ):
                    assignments.append(msg["id"])
                await self._emit(
                    conn,
                    tid,
                    "message.delivered",
                    agent_id,
                    {"id": msg["id"], "session_revision": saved["revision"]},
                )
            await patch(
                conn,
                s.ctf_turns,
                tid,
                {"checkpoint_revision": saved["revision"], "assignment_message_ids": assignments},
                str(turn_id),
            )
            control = task["ctf_control"]
            if str(turn_id) in control.get("unresolved_checkpoints", []):
                await patch(
                    conn,
                    s.tasks,
                    tid,
                    {
                        "ctf_control": {
                            **control,
                            "unresolved_checkpoints": [
                                item
                                for item in control["unresolved_checkpoints"]
                                if item != str(turn_id)
                            ],
                        }
                    },
                )
            return saved

    async def _settle_turn_reservations(self, conn, tid, turn_id):
        task = await row(conn, s.tasks, tid)
        reservations = _reservations(task)
        unknown = dict(task["ctf_control"].get("budget_unknown_reservations", {}))
        remaining = {
            key: value
            for key, value in reservations.items()
            if value.get("turn_id") != str(turn_id)
        }
        moved = {
            key: {**value, "state": "unknown"}
            for key, value in reservations.items()
            if value.get("turn_id") == str(turn_id) and value.get("state") != "admitted"
        }
        if moved:
            unknown.update(moved)
        if remaining != reservations or moved:
            control = dict(task["ctf_control"])
            if remaining:
                control["budget_reservations"] = remaining
            else:
                control.pop("budget_reservations", None)
            if unknown:
                control["budget_unknown_reservations"] = unknown
            else:
                control.pop("budget_unknown_reservations", None)
            await patch(conn, s.tasks, tid, {"ctf_control": control})

    async def bill_usage(
        self,
        tid,
        agent_id,
        turn_id,
        generation,
        request_id,
        usage,
        reservation_id=None,
    ):
        usage = Usage.model_validate(usage).model_dump(mode="json")
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            member, turn = await self._fence(conn, tid, agent_id, generation, turn_id)
            duplicate = (
                await conn.execute(
                    select(s.ctf_turns.c.id).where(
                        s.ctf_turns.c.task_id == tid,
                        s.ctf_turns.c.usage_requests.contains([str(request_id)]),
                    )
                )
            ).first()
            if duplicate:
                return turn["usage"]
            total = add_usage(turn["usage"], usage)
            control = dict(task["ctf_control"])
            reservations = _reservations(task)
            unknown = dict(task["ctf_control"].get("budget_unknown_reservations", {}))
            key = str(reservation_id) if reservation_id is not None else None
            if key is None or key not in reservations:
                key = next(
                    (
                        item_key
                        for item_key, item in reservations.items()
                        if item.get("turn_id") == str(turn_id)
                    ),
                    None,
                )
            if key is None or key not in reservations:
                key = next(
                    (
                        item_key
                        for item_key, item in unknown.items()
                        if item.get("turn_id") == str(turn_id)
                    ),
                    None,
                )
            if key is not None:
                source = reservations if key in reservations else unknown
                reserved = Decimal(str(source[key]["cost"]))
                actual = Decimal(str(usage.get("cost", 0)))
                if actual > reserved:
                    control["budget_overrun"] = {
                        "turn_id": str(turn_id),
                        "reserved": str(reserved),
                        "actual": str(actual),
                    }
                reservations.pop(key, None)
                unknown.pop(key, None)
                if reservations:
                    control["budget_reservations"] = reservations
                else:
                    control.pop("budget_reservations", None)
                if unknown:
                    control["budget_unknown_reservations"] = unknown
                else:
                    control.pop("budget_unknown_reservations", None)
            await patch(
                conn,
                s.ctf_turns,
                tid,
                {"usage": total, "usage_requests": [*turn["usage_requests"], str(request_id)]},
                str(turn_id),
            )
            if key is not None:
                await patch(conn, s.tasks, tid, {"ctf_control": control})
            if turn["purpose"] == "review":
                control = task["ctf_control"]
                await patch(
                    conn,
                    s.tasks,
                    tid,
                    {
                        "ctf_control": {
                            **control,
                            "review_usage": add_usage(control.get("review_usage", {}), usage),
                        }
                    },
                )
                return total
            await patch(
                conn, s.ctf_members, tid, {"usage": add_usage(member["usage"], usage)}, agent_id
            )
            await patch(conn, s.tasks, tid, {"usage": add_usage(task["usage"], usage)})
            return total

    async def _settle(self, conn, tid, member, turn, end_reason, answer):
        if turn["status"] != "running":
            return turn
        await self._settle_turn_reservations(conn, tid, turn["id"])
        if turn["purpose"] == "review":
            return await self._settle_review(conn, tid, member, turn, end_reason, answer)
        # Include leased assignments when a stream failed before any checkpoint.
        leased = (
            (
                await conn.execute(
                    select(s.ctf_messages).where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.claim_turn_id == turn["id"],
                        s.ctf_messages.c.status == "leased",
                    )
                )
            )
            .mappings()
            .all()
        )
        assignments = list(
            dict.fromkeys(
                [
                    *turn["assignment_message_ids"],
                    *[m["id"] for m in leased if m["kind"] == "instruction"],
                ]
            )
        )
        if member["role"] != "lead" and assignments:
            body = json.dumps(
                {
                    "assignment_message_ids": assignments[:50],
                    "assignment_count": len(assignments),
                    "result_ref": turn["id"],
                    "end_reason": end_reason,
                    "final_answer": None
                    if turn["explicit_reply_ids"]
                    else (answer[:2000] or "Interrupted; no final answer"),
                    "truncated": len(answer) > 2000,
                    "reply_refs": turn["explicit_reply_ids"][:50],
                },
                ensure_ascii=False,
            )
            await self._message(
                conn,
                tid,
                "system",
                "lead",
                body,
                str(uuid4()),
                "turn_finished",
                assignments[0],
                source_turn_id=turn["id"],
                notification_purpose="assignment_result",
            )
        for message in leased:
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
        values = {
            "status": "finished",
            "end_reason": end_reason,
            "final_answer": answer,
            "finished_at": now(),
            "assignment_message_ids": assignments,
        }
        await patch(conn, s.ctf_turns, tid, values, turn["id"])
        await patch(
            conn, s.ctf_members, tid, {"current_turn_id": None, "run_state": "idle"}, member["id"]
        )
        await self._emit(conn, tid, "turn.finished", member["id"], {"id": turn["id"], **values})
        return {**turn, **values}

    async def finish_turn(self, tid, agent_id, turn_id, generation, end_reason, answer=""):
        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            turn = await row(conn, s.ctf_turns, tid, str(turn_id))
            require(
                turn["agent_id"] == agent_id and turn["generation"] == generation,
                "Stale settlement",
            )
            if turn["status"] != "running":
                return turn
            member, _ = await self._fence(conn, tid, agent_id, generation, turn_id)
            require(not member.get("pending_operation"), "Member stop requires remote proof")
            return await self._settle(conn, tid, member, turn, end_reason, answer)

    async def turn_result(self, tid, turn_id, actor):
        """Expose only settled assignment output, never another member's Session."""
        async with self.repo.engine.connect() as conn:
            turn = await row(conn, s.ctf_turns, tid, str(turn_id))
            require(
                actor in {"user", turn["agent_id"]}
                or (actor == "lead" and bool(turn["assignment_message_ids"])),
                "Private turn",
            )
            require(turn["status"] == "finished", "Turn result is not settled")
            return {
                key: turn[key]
                for key in (
                    "id",
                    "agent_id",
                    "end_reason",
                    "final_answer",
                    "assignment_message_ids",
                    "explicit_reply_ids",
                )
            }

    async def request_finish(
        self, tid, actor, request_id, conclusion, generation=None, turn_id=None
    ):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            await self._actor(conn, tid, actor, generation, turn_id)
            require(actor in {"lead", "user", "system", "service"}, "Only Lead can finish")
            control = task["ctf_control"]
            if control["phase"] in {"closing", "closed"}:
                require(
                    control.get("closing_request_id") == str(request_id),
                    "Closing already requested",
                )
                return {"operation_id": str(request_id), "phase": control["phase"]}
            require(control["phase"] == "running", "Task is not running")
            require(
                not conclusion.get("verification_refs"),
                "Verification requires a trusted platform adapter",
            )
            validated = CtfConclusion.model_validate(conclusion).model_dump(mode="json")
            require(
                validated["end_reason"] != "goal_claimed"
                or (actor == "lead" and validated["lead_claim"]),
                "Goal completion requires an explicit Lead claim",
            )
            if validated["end_reason"] == "goal_claimed":
                challenges = (
                    (
                        await conn.execute(
                            select(s.ctf_challenges.c.data).where(s.ctf_challenges.c.task_id == tid)
                        )
                    )
                    .scalars()
                    .all()
                )
                require(
                    all(
                        item.get("tombstone") or item["work_status"] in {"completed", "cancelled"}
                        for item in challenges
                    ),
                    "Unfinished challenges remain",
                )
                require(
                    all(
                        item.get("tombstone")
                        or not item.get("verification", {}).get("required")
                        or item["verification"].get("status") == "accepted"
                        for item in challenges
                    ),
                    "Required verification is not accepted",
                )
            stored = {
                **validated,
                "requested_by": actor,
                "run_number": task["run_number"],
                "requested_at": now().isoformat(),
            }
            control = {
                **control,
                "phase": "closing",
                "closing_request_id": str(request_id),
                "epoch": control["epoch"] + 1,
            }
            await patch(conn, s.tasks, tid, {"ctf_control": control, "ctf_conclusion": stored})
            await self._emit(conn, tid, "conclusion.requested", actor, {"conclusion": stored})
            return {"operation_id": str(request_id), "phase": "closing"}

    async def finalize_close(self, tid, drained):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            control = task["ctf_control"]
            if control["phase"] == "closed":
                return task
            require(control["phase"] == "closing" and drained, "Drain is not confirmed")
            require(
                control.get("replacement", {}).get("phase", "ready") == "ready",
                "Execution replacement is in progress",
            )
            require(
                not control.get("unresolved_checkpoints"), "Final checkpoint remains unresolved"
            )
            require(not control.get("budget_reservations"), "Budget reservations remain active")
            running = (
                await conn.execute(
                    select(s.ctf_turns.c.id)
                    .where(s.ctf_turns.c.task_id == tid, s.ctf_turns.c.status == "running")
                    .limit(1)
                )
            ).first()
            require(running is None, "Turns are still running")
            members = (
                (await conn.execute(select(s.ctf_members).where(s.ctf_members.c.task_id == tid)))
                .mappings()
                .all()
            )
            require(
                all(
                    not member["pending_operation"]
                    and (not member["execution"] or member["execution"].get("drained"))
                    for member in members
                ),
                "Member executions have not drained",
            )
            conclusion = {**task["ctf_conclusion"], "finalized_at": now().isoformat()}
            status = {"user_stop": "stopped", "system_failure": "failed"}.get(
                conclusion.get("end_reason", "partial"), "finished"
            )
            active = task["active_seconds"] + (
                max(0, int((now() - task["active_since"]).total_seconds()))
                if task["active_since"]
                else 0
            )
            await patch(
                conn,
                s.tasks,
                tid,
                {
                    "status": status,
                    "ctf_control": {**control, "phase": "closed"},
                    "ctf_conclusion": conclusion,
                    "finished_at": now(),
                    "active_since": None,
                    "active_seconds": active,
                },
            )
            await self._emit(
                conn,
                tid,
                "conclusion.finalized",
                "system",
                {"conclusion": conclusion, "status": status},
            )
            return {"phase": "closed", "status": status}

    async def recover(self, tid):
        """Fence abandoned writers only after the coordinator has confirmed drain."""
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            interrupted = []
            members = (
                (await conn.execute(select(s.ctf_members).where(s.ctf_members.c.task_id == tid)))
                .mappings()
                .all()
            )
            for member in members:
                if member.get("pending_operation"):
                    continue
                if member["current_turn_id"]:
                    turn = await row(conn, s.ctf_turns, tid, member["current_turn_id"])
                    await self._settle(conn, tid, member, turn, "interrupted", "")
                    interrupted.append(turn["id"])
                    await patch(
                        conn,
                        s.ctf_members,
                        tid,
                        {"generation": member["generation"] + 1},
                        member["id"],
                    )
            if interrupted and task["ctf_control"]["phase"] == "running":
                message_id = str(
                    uuid5(NAMESPACE_URL, f"ctf-recovery:{tid}:{','.join(sorted(interrupted))}")
                )
                body = (
                    "Runtime recovery interrupted previous turns. Before assigning more work, "
                    "check the saved team state, execution environment and target connections. "
                    "Do not replay tools with unknown outcomes; query their state first. "
                    f"Interrupted turn references: {', '.join(sorted(interrupted)[:50])}"
                )
                await self._message(conn, tid, "system", "lead", body, message_id, "instruction")
        return await self.state(tid)

    async def record_execution(self, tid, agent_id, turn_id, generation, boot_id):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            require(
                task["ctf_control"].get("replacement", {}).get("phase", "ready") == "ready",
                "Execution replacement is in progress",
            )
            member = await row(conn, s.ctf_members, tid, agent_id)
            require(
                member["generation"] == generation and member["lifecycle"] != "removed",
                "Stale execution registration",
            )
            require(
                member["current_turn_id"] == (str(turn_id) if turn_id else None),
                "Stale execution turn",
            )
            execution = member.get("execution")
            if execution and (
                execution["boot_id"] != boot_id or execution["generation"] != generation
            ):
                require(
                    execution.get("drained", False), "Previous boot or generation has not drained"
                )
            binding = (
                execution
                if execution
                and execution["boot_id"] == boot_id
                and execution["generation"] == generation
                else {"boot_id": boot_id, "generation": generation, "drained": False}
            )
            pending = member.get("pending_operation")
            if pending:
                require(pending["boot_id"] in {None, boot_id}, "Pending stop targets another boot")
                pending = {**pending, "boot_id": boot_id}
            await patch(
                conn,
                s.ctf_members,
                tid,
                {"execution": binding, "pending_operation": pending},
                agent_id,
            )
            return binding

    async def record_execution_drained(self, tid, agent_id, proof):
        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            member = await row(conn, s.ctf_members, tid, agent_id)
            execution = member.get("execution")
            require(
                bool(execution)
                and proof.get("drained") is True
                and proof.get("boot_id") == execution["boot_id"]
                and proof.get("generation") == execution["generation"],
                "Remote drain proof mismatch",
            )
            assert execution is not None
            await patch(
                conn, s.ctf_members, tid, {"execution": {**execution, "drained": True}}, agent_id
            )
            return {"drained": True}

    async def request_member_operation(
        self, tid, actor, member_id, operation, request_id, generation=None, turn_id=None
    ):
        require(operation in {"stop", "resume", "remove"}, "Unknown member operation")
        request_id = str(UUID(str(request_id)))
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            await self._actor(conn, tid, actor, generation, turn_id)
            require(actor in {"lead", "user"}, "Only Lead or the user can manage teammates")
            require(task["ctf_control"]["phase"] == "running", "Task is not running")
            member = await row(conn, s.ctf_members, tid, member_id)
            require(member["role"] != "lead", "Lead cannot be managed as a teammate")
            history = member["operation_history"]
            for completed in history:
                if completed["request_id"] == request_id:
                    require(completed["kind"] == operation, "Operation request ID conflict")
                    return member
            pending = member["pending_operation"]
            if pending:
                require(
                    pending["request_id"] == request_id and pending["kind"] == operation,
                    "Another member operation is pending",
                )
                return member
            require(member["lifecycle"] != "removed", "Member is removed")
            if operation == "resume":
                require(
                    member["lifecycle"] == "stopped" and member["current_turn_id"] is None,
                    "Member is not stopped",
                )
                require(
                    not member["execution"] or member["execution"].get("drained"),
                    "Remote execution has not drained",
                )
                values = {
                    "lifecycle": "active",
                    "run_state": "idle",
                    "operation_history": [*history, {"request_id": request_id, "kind": operation}],
                }
            else:
                pending = {
                    "request_id": request_id,
                    "kind": operation,
                    "target_generation": (member["execution"] or {}).get(
                        "generation", member["generation"]
                    ),
                    "boot_id": (member["execution"] or {}).get("boot_id"),
                    "phase": "stopping",
                }
                values = {"pending_operation": pending, "run_state": "stopping"}
            await patch(conn, s.ctf_members, tid, values, member_id)
            await self._emit(
                conn, tid, "member.state_changed", actor, {"id": member_id, "operation": operation}
            )
            return {**member, **values}

    async def complete_member_operation(self, tid, member_id, request_id, proof):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            member = await row(conn, s.ctf_members, tid, member_id)
            pending = member["pending_operation"]
            if not pending:
                require(
                    any(
                        item["request_id"] == str(request_id)
                        for item in member["operation_history"]
                    ),
                    "No matching operation",
                )
                return member
            require(pending["request_id"] == str(request_id), "Operation request ID conflict")
            execution = member["execution"]
            require(
                bool(execution)
                and proof.get("drained") is True
                and proof.get("boot_id") == pending["boot_id"] == execution["boot_id"]
                and proof.get("generation")
                == pending["target_generation"]
                == execution["generation"],
                "Remote drain proof mismatch",
            )
            require(
                member["current_turn_id"]
                not in task["ctf_control"].get("unresolved_checkpoints", []),
                "Final checkpoint remains unresolved",
            )
            if member["current_turn_id"]:
                turn = await row(conn, s.ctf_turns, tid, member["current_turn_id"])
                await self._settle(conn, tid, member, turn, "stopped", "")
            removed = pending["kind"] == "remove"
            values = {
                "lifecycle": "removed" if removed else "stopped",
                "run_state": "idle",
                "generation": member["generation"] + 1,
                "current_turn_id": None,
                "pending_operation": None,
                "execution": {**execution, "drained": True},
                "operation_history": [*member["operation_history"], pending],
                "removed_at": now() if removed else member["removed_at"],
            }
            await patch(conn, s.ctf_members, tid, values, member_id)
            if removed:
                await conn.execute(
                    update(s.ctf_messages)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.recipient_id == member_id,
                        s.ctf_messages.c.status.in_(["queued", "leased"]),
                    )
                    .values(
                        status="cancelled",
                        claim_token=None,
                        claim_turn_id=None,
                        claim_generation=None,
                        lease_until=None,
                    )
                )
                challenges = (
                    (
                        await conn.execute(
                            select(s.ctf_challenges).where(s.ctf_challenges.c.task_id == tid)
                        )
                    )
                    .mappings()
                    .all()
                )
                for challenge in challenges:
                    data = challenge["data"]
                    updated = {
                        **data,
                        "collaborator_ids": [
                            aid for aid in data.get("collaborator_ids", []) if aid != member_id
                        ],
                    }
                    if data.get("owner_id") == member_id and data.get("work_status") != "completed":
                        updated.update(
                            owner_id=None, previous_owner_id=member_id, work_status="blocked"
                        )
                    if updated != data:
                        updated["revision"] = challenge["revision"] + 1
                        await self._board_event(
                            conn,
                            tid,
                            "system",
                            "ctf.challenge.updated",
                            f"remove:{request_id}:{challenge['id']}",
                            "member_removed",
                            updated,
                        )
            await self._emit(
                conn,
                tid,
                "member.removed" if removed else "member.state_changed",
                "system",
                {"id": member_id, "lifecycle": values["lifecycle"]},
            )
            return {**member, **values}

    async def execution_replacement(
        self, tid, *, phase, old_container_id, old_boot_id, archive_uri=None, new_boot_id=None
    ):
        """Record one exact-container replacement after trusted archive and destroy steps."""
        require(
            phase in {"begin", "archive_saved", "destroyed", "ready"}, "Unknown replacement phase"
        )
        require(bool(old_container_id) and bool(old_boot_id), "Replacement identity is required")
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            control = task["ctf_control"]
            previous = control.get("replacement")
            identity = {"old_container_id": old_container_id, "old_boot_id": old_boot_id}
            same = previous and all(previous.get(key) == value for key, value in identity.items())
            if previous and not same:
                require(
                    previous["phase"] == "ready" and phase == "begin",
                    "Another replacement is pending",
                )
            if phase == "begin" and same:
                return previous
            if phase == "begin":
                record = {**identity, "phase": "begin"}
                if previous:
                    control = {
                        **control,
                        "replacement_history": [*control.get("replacement_history", []), previous],
                    }
            else:
                require(bool(same), "Replacement identity mismatch")
                assert previous is not None
                order = {"begin": 0, "archive_saved": 1, "destroyed": 2, "ready": 3}
                require(
                    order[phase] <= order[previous["phase"]] + 1, "Replacement phase out of order"
                )
                if archive_uri is not None and previous.get("archive_uri") is not None:
                    require(
                        archive_uri == previous["archive_uri"], "Replacement archive is immutable"
                    )
                if new_boot_id is not None and previous.get("new_boot_id") is not None:
                    require(new_boot_id == previous["new_boot_id"], "Replacement boot is immutable")
                if order[phase] <= order[previous["phase"]]:
                    return previous
                record = {**previous, "phase": phase}
                if phase == "archive_saved":
                    require(
                        isinstance(archive_uri, str)
                        and archive_uri.startswith(f"workspace/{tid}/")
                        and ".." not in archive_uri.split("/"),
                        "Archive must belong to this task",
                    )
                    record["archive_uri"] = archive_uri
                if phase == "ready":
                    require(
                        bool(new_boot_id) and new_boot_id != old_boot_id,
                        "Replacement requires a new boot",
                    )
                    record["new_boot_id"] = new_boot_id
                    members = (
                        (
                            await conn.execute(
                                select(s.ctf_members).where(s.ctf_members.c.task_id == tid)
                            )
                        )
                        .mappings()
                        .all()
                    )
                    for member in members:
                        await patch(
                            conn,
                            s.ctf_members,
                            tid,
                            {"generation": member["generation"] + 1},
                            member["id"],
                        )
                        execution = member["execution"]
                        if execution:
                            require(
                                execution["boot_id"] == old_boot_id or execution.get("drained"),
                                "Unaccounted execution boot",
                            )
                            await patch(
                                conn,
                                s.ctf_members,
                                tid,
                                {
                                    "execution": {
                                        **execution,
                                        "drained": True,
                                        "container_destroyed": old_container_id,
                                        "replacement_boot_id": new_boot_id,
                                    },
                                },
                                member["id"],
                            )
            await patch(conn, s.tasks, tid, {"ctf_control": {**control, "replacement": record}})
            return record
