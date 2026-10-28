"""Transactional CTF mailbox, persistent turns and generation fencing."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from bbx_contracts.billing import add_usage
from bbx_contracts.ctf import CtfConclusion
from bbx_contracts.models import Usage
from bbx_contracts.storage import storage_safe
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import cast, func, insert, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bbx_blackboard.conversations import Conversations
from bbx_blackboard.ctf_board import CtfBoardMixin
from bbx_blackboard.ctf_lifecycle import CtfLifecycleMixin
from bbx_blackboard.ctf_platform import CtfPlatformMixin
from bbx_blackboard.ctf_readback import CtfReadbackMixin
from bbx_blackboard.ctf_review import CtfReviewMixin
from bbx_blackboard.domain.rules import event
from bbx_blackboard.input_groups import InputGroups, check_mutable, check_owner
from bbx_blackboard.store import Repository
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import now, patch, row

LOGGER = logging.getLogger(__name__)
MAILBOX_BATCH_SIZE = 1000


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


def clear_context_blocks(task, control):
    blocked_history = dict(control.get("context_blocked_history", {}))
    for agent_id, entry in control.get("context_blocked_members", {}).items():
        entry = {
            **entry,
            **{
                key: list(
                    dict.fromkeys(
                        value for value in entry.get(key, []) if isinstance(value, str) and value
                    )
                )
                for key in ("message_ids", "input_message_ids")
            },
        }
        key = entry.get("turn_id") or f"legacy:{task['run_number']}:{agent_id}"
        blocked_history.setdefault(
            key, {**entry, "agent_id": agent_id, "run_number": task["run_number"]}
        )
    control = {
        **control,
        "context_blocked_members": {},
        "context_blocked_history": blocked_history,
    }
    return control


def dispatch_message_condition(task: dict[str, Any], *, instant=None):
    """Gate durable context blocks and delayed automatic provider recovery."""
    blocked = task["ctf_control"].get("context_blocked_members", {})
    allowed = s.ctf_messages.c.recipient_id.not_in(list(blocked))
    for agent_id, entry in blocked.items():
        allowed = allowed | (
            (s.ctf_messages.c.recipient_id == agent_id)
            & ((s.ctf_messages.c.kind == "instruction") | (s.ctf_messages.c.sender_kind == "user"))
            & s.ctf_messages.c.id.not_in(
                [value for value in entry.get("input_message_ids", []) if isinstance(value, str)]
            )
        )
    for entry in task["ctf_control"].get("turn_retry_delays", {}).values():
        if datetime.fromisoformat(entry["ready_at"]) > (instant or now()):
            allowed = allowed & (s.ctf_messages.c.id != entry["message_id"])
    return allowed


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
    ceiling = Decimal(str(task["budget"]["max_cost"])) - spent
    unknown = sum(
        (
            Decimal(str(item.get("cost", 0)))
            for item in task["ctf_control"].get("budget_unknown_reservations", {}).values()
        ),
        Decimal(0),
    )
    remaining = ceiling - _reserved_cost(task)
    fits_without_active = (
        ceiling - unknown > 0 if not reservation else reservation <= ceiling - unknown
    )
    fits = remaining > 0 if not reservation else reservation <= remaining
    if active >= task["budget"]["max_minutes"] * 60 or not fits_without_active:
        raise HTTPException(
            409, {"code": "ctf_budget_exhausted", "message": "Task budget exhausted"}
        )
    if not fits:
        raise HTTPException(
            409, {"code": "ctf_budget_wait", "message": "Task budget is reserved by active calls"}
        )


def check_reserved_call(task: dict[str, Any], reservation_id: str, cost: Decimal | str) -> None:
    """Recheck admission without charging an existing reservation twice."""
    check_budget(
        {
            **task,
            "ctf_control": {
                **task["ctf_control"],
                "budget_reservations": {
                    key: item for key, item in _reservations(task).items() if key != reservation_id
                },
            },
        },
        cost,
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


class CtfService(
    CtfLifecycleMixin, CtfReviewMixin, CtfPlatformMixin, CtfBoardMixin, CtfReadbackMixin
):
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

    async def cleanup_terminal_context_blocks(self, tid):
        """Archive legacy terminal blockers without changing history or execution state."""
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            require(task["status"] in {"finished", "failed", "stopped"}, "Task is not terminal")
            require(task["ctf_control"]["phase"] == "closed", "Task is not closed")
            running = (
                await conn.execute(
                    select(s.ctf_turns.c.id)
                    .where(s.ctf_turns.c.task_id == tid, s.ctf_turns.c.status == "running")
                    .limit(1)
                )
            ).first()
            require(running is None, "Task has an active execution or review turn")
            members = (
                (await conn.execute(select(s.ctf_members).where(s.ctf_members.c.task_id == tid)))
                .mappings()
                .all()
            )
            require(
                not any(
                    member["current_turn_id"] or member["pending_operation"] for member in members
                ),
                "Task has an active member operation or turn",
            )
            control = task["ctf_control"]
            blocked = control.get("context_blocked_members", {})
            if not blocked:
                return {"task_id": str(tid), "changed": False, "cleared_members": 0}
            await patch(conn, s.tasks, tid, {"ctf_control": clear_context_blocks(task, control)})
            await self._emit(
                conn,
                tid,
                "coordination.reminded",
                "system",
                {
                    "status": "terminal_context_blocks_cleared",
                    "run_number": task["run_number"],
                    "cleared_members": len(blocked),
                },
            )
            return {"task_id": str(tid), "changed": True, "cleared_members": len(blocked)}

    async def _recovery_diagnostics(self, conn, tid, control, agent_id=None):
        """Bound references and numeric context diagnostics without exposing private inputs."""
        query = select(s.ctf_messages.c.id, s.ctf_messages.c.recipient_id).where(
            s.ctf_messages.c.task_id == tid,
            s.ctf_messages.c.purpose == "execution",
            s.ctf_messages.c.status == "queued",
            s.ctf_messages.c.deferred.is_(True),
        )
        if agent_id is not None:
            query = query.where(s.ctf_messages.c.recipient_id == agent_id)
        messages = (
            (
                await conn.execute(
                    query.order_by(
                        s.ctf_messages.c.recipient_id, s.ctf_messages.c.recipient_sequence
                    ).limit(51)
                )
            )
            .mappings()
            .all()
        )
        blocked = control.get("context_blocked_members", {})
        entries = [
            (aid, entry)
            for aid, entry in sorted(blocked.items())
            if agent_id is None or aid == agent_id
        ]
        known_branches = {
            "no_loaded_prefix",
            "loaded_prefix_mismatch",
            "unconfirmed_prefix",
            "missing_recovery_seed",
            "no_safe_boundary",
            "unsafe_tool_boundary",
            "remaining_over_limit",
            "compacted",
            "unsafe_loaded_boundary",
            "unshrinkable_loaded_history",
            "protected_input",
            "legacy",
        }
        diagnostics = []
        for aid, entry in entries[:20]:
            values = {
                key: value if type(value) is int and 0 <= value <= 1_000_000_000 else None
                for key in ("eligible", "cut", "estimated_input_tokens", "limit_tokens")
                for value in [entry.get(key)]
            }
            diagnostics.append(
                {
                    "agent_id": aid,
                    "branch": entry.get("branch")
                    if isinstance(entry.get("branch"), str) and entry["branch"] in known_branches
                    else "unknown",
                    **values,
                }
            )
        return {
            "blocked_members": diagnostics,
            "blocked_members_truncated": len(entries) > 20,
            "deferred_messages": [
                {
                    "id": message["id"],
                    "recipient_id": message["recipient_id"],
                    "status": "queued",
                    "deferred": True,
                }
                for message in messages[:50]
            ],
            "deferred_messages_truncated": len(messages) > 50,
        }

    async def _emit(self, conn, tid, kind, actor, payload):
        addressed_to = None
        if kind.startswith("message."):
            message = await row(conn, s.ctf_messages, tid, payload["id"])
            addressed_to = [message["sender_id"], message["recipient_id"]]
        elif kind.startswith("turn."):
            turn = await row(conn, s.ctf_turns, tid, payload["id"])
            addressed_to = [turn["agent_id"]]
        events = await self.repo.append(
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
        await self.repo.notify(tid, events[-1]["version"], connection=conn)

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
        context = await self._authorize_with_budget(
            tid, agent_id, generation, turn_id, reservation_id, reservation_cost
        )
        return context["member"]

    async def authorize_tool(self, tid, agent_id, generation, turn_id):
        """Fence one tool request and expose only its purpose and fixed profile identity."""
        context = await self._authorize_with_budget(
            tid, agent_id, generation, turn_id, allow_review=True
        )
        task, member = context["task"], context["member"]
        return {
            "task": {key: task[key] for key in ("agent_profile", "agent_profile_version")},
            "member": {key: member[key] for key in ("id", "role")},
            "purpose": context["purpose"],
        }

    async def _authorize_with_budget(
        self,
        tid,
        agent_id,
        generation,
        turn_id=None,
        reservation_id=None,
        reservation_cost: Decimal | str = Decimal(0),
        *,
        allow_review=False,
    ):
        try:
            return await self._authorize_member(
                tid,
                agent_id,
                generation,
                turn_id,
                reservation_id,
                reservation_cost,
                allow_review=allow_review,
            )
        except HTTPException as error:
            if (
                not isinstance(error.detail, dict)
                or error.detail.get("code") != "ctf_budget_exhausted"
            ):
                raise
            # Commit known old usage even when the next call still cannot be admitted.
            async with self.repo.engine.begin() as conn:
                task = await self._task(conn, tid)
                if not task["ctf_control"].get("budget_unknown_reservations"):
                    raise
                await self._reconcile_budget(conn, tid)
            return await self._authorize_member(
                tid,
                agent_id,
                generation,
                turn_id,
                reservation_id,
                reservation_cost,
                allow_review=allow_review,
            )

    async def _authorize_member(
        self,
        tid,
        agent_id,
        generation,
        turn_id=None,
        reservation_id=None,
        reservation_cost: Decimal | str = Decimal(0),
        *,
        allow_review=False,
    ):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            member, turn = await self._fence(conn, tid, agent_id, generation, turn_id)
            context = {"task": task, "member": member, "purpose": turn["purpose"]}
            if allow_review and turn["purpose"] == "review":
                self._authorize_review(task, turn)
                return context
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
                check_reserved_call(task, key, cost)
                return context
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
            return context

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
            require(
                member["run_state"] != "stopping" and not member.get("pending_operation"),
                "Member is stopping",
            )
            check_reserved_call(task, key, reservation["cost"])
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

    async def runtime_state(self, tid):
        """Return scheduling hints; claims and generation fences remain authoritative."""
        async with self.repo.engine.connect() as conn:
            # Read the cursor first: later commits may appear in state, never be skipped by SSE.
            event_version = (
                await conn.execute(
                    select(func.coalesce(func.max(s.events.c.version), 0)).where(
                        s.events.c.task_id == tid
                    )
                )
            ).scalar_one()
            task = await row(conn, s.tasks, tid)
            require(task["mode"] == "ctf", "Task mode is not CTF")
            result: dict[str, Any] = {"task": task, "event_version": int(event_version)}
            for key, table in (("members", s.ctf_members), ("turns", s.ctf_turns)):
                query = select(table).where(table.c.task_id == tid)
                if key == "turns":
                    query = query.where(table.c.status == "running")
                result[key] = [dict(item) for item in (await conn.execute(query)).mappings()]
            ready = (
                (
                    await conn.execute(
                        select(s.ctf_messages.c.recipient_id, s.ctf_messages.c.purpose)
                        .where(
                            s.ctf_messages.c.task_id == tid,
                            s.ctf_messages.c.status == "queued",
                            s.ctf_messages.c.deferred.is_(False),
                            (s.ctf_messages.c.purpose != "execution")
                            | (
                                dispatch_message_condition(task)
                                & s.ctf_messages.c.recipient_id.in_(
                                    select(s.ctf_members.c.id).where(
                                        s.ctf_members.c.task_id == tid,
                                        s.ctf_members.c.lifecycle == "active",
                                    )
                                )
                            ),
                            (s.ctf_messages.c.purpose == "execution")
                            | (
                                (s.ctf_messages.c.purpose == "review")
                                & (s.ctf_messages.c.sender_kind == "user")
                            ),
                        )
                        .distinct()
                    )
                )
                .mappings()
                .all()
            )
            result["ready_member_ids"] = [
                item["recipient_id"] for item in ready if item["purpose"] == "execution"
            ]
            result["review_ready_member_ids"] = [
                item["recipient_id"] for item in ready if item["purpose"] == "review"
            ]
            return result

    async def idle_watchdog(self, tid, *, instant=None, idle_seconds=60, max_nudges=3):
        """Persist bounded coordination reminders, serialized with claims and user actions."""
        instant = instant or now()
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            control = dict(task["ctf_control"])
            members = (
                (await conn.execute(select(s.ctf_members).where(s.ctf_members.c.task_id == tid)))
                .mappings()
                .all()
            )
            lead = next((member for member in members if member["id"] == "lead"), None)
            pending = (
                await conn.execute(
                    select(s.ctf_messages.c.id)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.purpose == "execution",
                        s.ctf_messages.c.recipient_id.in_(
                            [member["id"] for member in members if member["lifecycle"] == "active"]
                        ),
                        (s.ctf_messages.c.status == "leased")
                        | (
                            (s.ctf_messages.c.status == "queued")
                            & s.ctf_messages.c.deferred.is_(False)
                            & dispatch_message_condition(task, instant=instant)
                        ),
                    )
                    .limit(1)
                )
            ).first()
            turns = (
                await conn.execute(
                    select(s.ctf_turns.c.status, s.ctf_turns.c.finished_at).where(
                        s.ctf_turns.c.task_id == tid, s.ctf_turns.c.purpose == "execution"
                    )
                )
            ).all()
            blocked = (
                control["phase"] != "running"
                or lead is None
                or lead["lifecycle"] != "active"
                or any(
                    member["current_turn_id"]
                    or member.get("pending_operation")
                    or member["run_state"] != "idle"
                    for member in members
                )
                or any(turn.status == "running" for turn in turns)
                or pending is not None
                or control.get("unresolved_checkpoints")
                or control.get("budget_reservations")
                or control.get("replacement", {}).get("phase", "ready") != "ready"
            )
            monitor = dict(control.get("idle_watchdog", {}))
            if blocked:
                return {"action": "blocked"}
            try:
                check_budget(task)
            except HTTPException:
                return {"action": "blocked"}
            challenges = (
                (
                    await conn.execute(
                        select(
                            s.ctf_challenges.c.id,
                            s.ctf_challenges.c.revision,
                            s.ctf_challenges.c.data,
                        )
                        .where(s.ctf_challenges.c.task_id == tid)
                        .order_by(s.ctf_challenges.c.id)
                    )
                )
                .mappings()
                .all()
            )
            live = [item for item in challenges if not item["data"].get("tombstone")]
            if not live:
                return {"action": "blocked"}
            records = (
                await conn.execute(
                    select(func.count(), func.max(s.ctf_records.c.version)).where(
                        s.ctf_records.c.task_id == tid
                    )
                )
            ).one()
            progress = hashlib.sha256(
                json.dumps(
                    {
                        "challenges": [[item["id"], item["revision"]] for item in challenges],
                        "records": list(records),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if monitor.get("progress") != progress:
                monitor = {"progress": progress, "nudges": 0, "idle_since": instant.isoformat()}
            elif not monitor.get("idle_since"):
                monitor["idle_since"] = instant.isoformat()
            idle_since = datetime.fromisoformat(monitor["idle_since"])
            finished = [turn.finished_at for turn in turns if turn.finished_at is not None]
            if finished:
                idle_since = max(idle_since, max(finished))
                monitor["idle_since"] = idle_since.isoformat()
            if (instant - idle_since).total_seconds() < idle_seconds:
                if monitor != control.get("idle_watchdog"):
                    await patch(
                        conn, s.tasks, tid, {"ctf_control": {**control, "idle_watchdog": monitor}}
                    )
                return {"action": "waiting"}
            unfinished = [
                item["id"]
                for item in live
                if item["data"]["work_status"] not in {"completed", "cancelled"}
            ]
            lead_blocked = "lead" in control.get("context_blocked_members", {})
            recovered = control.get("turn_retries", {}).get("lead", {}).get("context_blocked", 0)
            limit = (0 if recovered else 1) if lead_blocked else max_nudges if unfinished else 1
            attempt = monitor["nudges"] + 1
            operation_id = str(
                uuid5(NAMESPACE_URL, f"ctf-idle:{tid}:{task['run_number']}:{progress}:{attempt}")
            )
            diagnostics = await self._recovery_diagnostics(conn, tid, control)
            if attempt > limit:
                if lead_blocked:
                    summary = (
                        "Coordination stalled: Lead context remains blocked after bounded "
                        "recovery; saved work and queued inputs remain. A new explicit user "
                        "message or resume can request another recovery."
                    )
                elif unfinished:
                    summary = (
                        f"Coordination stalled: {monitor['nudges']} reminders produced no "
                        "challenge or record progress; unfinished work remains without "
                        "an active turn or executable message."
                    )
                else:
                    summary = (
                        "Coordination stalled: the completed board received a "
                        "final-summary reminder but no task conclusion."
                    )
                if diagnostics["deferred_messages"] or diagnostics["blocked_members"]:
                    summary += " Deferred input references and blocked diagnostics remain saved: "
                    summary += json.dumps(diagnostics)
                stored = {
                    **CtfConclusion.model_validate(
                        {
                            "end_reason": "partial",
                            "summary": summary,
                            "unresolved_items": unfinished[:100],
                            "lead_claim": False,
                        }
                    ).model_dump(mode="json"),
                    "requested_by": "system",
                    "run_number": task["run_number"],
                    "requested_at": instant.isoformat(),
                }
                control.update(
                    phase="closing",
                    closing_request_id=operation_id,
                    epoch=control["epoch"] + 1,
                    idle_watchdog={**monitor, "stalled": True},
                )
                await patch(conn, s.tasks, tid, {"ctf_control": control, "ctf_conclusion": stored})
                await self._emit(
                    conn, tid, "conclusion.requested", "system", {"conclusion": stored}
                )
                return {"action": "stalled", "attempt": monitor["nudges"]}
            monitor.update(nudges=attempt, idle_since=instant.isoformat())
            await patch(conn, s.tasks, tid, {"ctf_control": {**control, "idle_watchdog": monitor}})
            body = (
                "The team is idle with no executable messages. Review shared challenge and "
                "record state; coordinate remaining work or request a partial conclusion with "
                "reasons. Do not replay tools "
                "with unknown outcomes. This is bounded coordination reminder "
                + str(attempt)
                + "/"
                + str(limit)
                + "."
                if unfinished
                else "All recorded challenges are complete or cancelled and the team is idle. "
                "Review shared results and request the final task conclusion. "
                "This is the only automatic summary reminder."
            )
            if diagnostics["deferred_messages"] or diagnostics["blocked_members"]:
                body += " Deferred work needs a relevance decision; no messages were released. "
                body += (
                    "Lead can explicitly confirm relevant deferred messages "
                    "or issue a smaller new instruction. "
                )
                body += json.dumps(diagnostics)
            await self._message(conn, tid, "system", "lead", body, operation_id, "instruction")
            if lead_blocked:
                retries = dict(control.get("turn_retries", {}))
                retries["lead"] = {**retries.get("lead", {}), "context_blocked": 1}
                await patch(
                    conn,
                    s.tasks,
                    tid,
                    {"ctf_control": {**control, "idle_watchdog": monitor, "turn_retries": retries}},
                )
            await self._emit(
                conn,
                tid,
                "coordination.reminded",
                "system",
                {"attempt": attempt, "progress": progress, "message_id": operation_id},
            )
            return {"action": "nudged", "attempt": attempt}

    async def state(self, tid):
        async with self.repo.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            task = await row(conn, s.tasks, tid)
            require(task["mode"] == "ctf", "Task mode is not CTF")
            # The task, cursor and folded rows must share one committed snapshot.
            event_version = (
                await conn.execute(
                    select(func.coalesce(func.max(s.events.c.version), 0)).where(
                        s.events.c.task_id == tid
                    )
                )
            ).scalar_one()
            result: dict[str, Any] = {"task": task, "event_version": int(event_version)}
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
        async with self.repo.engine.connect() as conn:
            task = await row(conn, s.tasks, tid)
            require(task["mode"] == "ctf", "Task mode is not CTF")
            members = [
                {**item, "agent_id": item["id"]}
                for item in (
                    await conn.execute(select(s.ctf_members).where(s.ctf_members.c.task_id == tid))
                ).mappings()
            ]
            return {"members": members}

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
        challenge_id=None,
    ):
        try:
            message_id = str(UUID(str(message_id)))
            if challenge_id is not None:
                challenge_id = str(UUID(str(challenge_id)))
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
                        "challenge_id": challenge_id,
                    }.items()
                ),
                "Message ID conflict",
            )
            return dict(existing)
        if challenge_id is not None:
            await self._challenge(conn, tid, challenge_id)
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
            challenge_id=challenge_id,
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
        challenge_id=None,
    ):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            if actor == "user" and task["status"] in {"finished", "failed", "stopped"}:
                return await self._message(
                    conn,
                    tid,
                    actor,
                    recipient_id,
                    body,
                    message_id,
                    "review",
                    purpose="review",
                    challenge_id=challenge_id,
                )
            await self._actor(conn, tid, actor, generation, turn_id)
            phase = task["ctf_control"]["phase"]
            require(
                phase == "running" or (phase == "closing" and actor == "user"),
                "Task is not accepting execution messages",
            )
            require(kind in {"instruction", "message", "help_request"}, "Reserved message kind")
            result = await self._message(
                conn,
                tid,
                actor,
                recipient_id,
                body,
                message_id,
                kind,
                reply_to,
                phase == "closing",
                challenge_id=challenge_id,
            )
            blocked = task["ctf_control"].get("context_blocked_members", {}).get(recipient_id)
            if blocked is not None and (actor == "user" or kind == "instruction"):
                result = {
                    **result,
                    "dispatch_status": "deferred"
                    if phase == "closing" or result["deferred"]
                    else "context_recovery_requested"
                    if result["status"] == "queued"
                    and result["id"] not in blocked.get("input_message_ids", [])
                    else "context_blocked",
                }
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

    async def _defer_inputs(self, conn, tid, agent_id, message_ids, reason):
        deferred = (
            await conn.execute(
                update(s.ctf_messages)
                .where(
                    s.ctf_messages.c.task_id == tid,
                    s.ctf_messages.c.recipient_id == agent_id,
                    s.ctf_messages.c.status == "queued",
                    s.ctf_messages.c.purpose == "execution",
                    s.ctf_messages.c.deferred.is_(False),
                    s.ctf_messages.c.id.in_(message_ids),
                )
                .values(deferred=True)
                .returning(s.ctf_messages)
            )
        ).mappings()
        for message in deferred:
            if message["notification_purpose"] != "turn_recovery":
                continue
            await self._emit(
                conn,
                tid,
                "coordination.reminded",
                "system",
                {
                    "agent_id": agent_id,
                    "turn_id": message["source_turn_id"],
                    "message_id": message["id"],
                    "status": "recovery_deferred",
                    "reason": reason,
                },
            )

    async def claim_turn(self, tid, agent_id, runtime_instance, expected_turn_id=None):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            member = await row(conn, s.ctf_members, tid, agent_id)
            if expected_turn_id is not None and member["current_turn_id"] != str(expected_turn_id):
                return None
            if (
                task["ctf_control"]["phase"] != "running"
                or member["lifecycle"] != "active"
                or member["run_state"] == "stopping"
                or task["ctf_control"].get("replacement", {}).get("phase", "ready") != "ready"
                or member.get("pending_operation")
            ):
                return None
            if member["current_turn_id"]:
                existing = await row(conn, s.ctf_turns, tid, member["current_turn_id"])
                if (
                    existing["status"] == "running"
                    and existing["runtime_instance"] == runtime_instance
                    and existing["purpose"] == "execution"
                    and existing["generation"] == member["generation"]
                ):
                    return existing
                return None
            try:
                check_budget(task)
            except HTTPException as error:
                if isinstance(error.detail, dict) and error.detail.get("code") in {
                    "ctf_budget_wait",
                    "ctf_budget_exhausted",
                }:
                    return None
                raise
            pending = (
                await conn.execute(
                    select(s.ctf_messages.c.id, s.ctf_messages.c.kind)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.recipient_id == agent_id,
                        s.ctf_messages.c.status == "queued",
                        s.ctf_messages.c.purpose == "execution",
                        s.ctf_messages.c.deferred.is_(False),
                        dispatch_message_condition(task),
                    )
                    .order_by(s.ctf_messages.c.recipient_sequence)
                    .limit(1)
                )
            ).first()
            if not pending:
                return None
            control = dict(task["ctf_control"])
            blocked = dict(control.get("context_blocked_members", {}))
            if agent_id in blocked:
                # A fresh recovery instruction cannot replay old unconfirmed blocked inputs.
                await self._defer_inputs(
                    conn,
                    tid,
                    agent_id,
                    [
                        value
                        for value in blocked[agent_id].get("input_message_ids", [])
                        if isinstance(value, str)
                    ],
                    "blocked_input_from_previous_turn",
                )
                blocked.pop(agent_id)
                control["context_blocked_members"] = blocked
            delays = dict(control.get("turn_retry_delays", {}))
            if agent_id in delays:
                delayed = delays.pop(agent_id)
                if delayed["message_id"] != pending.id:
                    await self._defer_inputs(
                        conn, tid, agent_id, [delayed["message_id"]], "superseded_by_new_input"
                    )
                control["turn_retry_delays"] = delays
            if control != task["ctf_control"]:
                await patch(
                    conn,
                    s.tasks,
                    tid,
                    {"ctf_control": control},
                )
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
                (
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
                )
                .mappings()
                .all()
            )
            for message in messages:
                require(
                    message["claim_turn_id"] in {None, str(turn_id)},
                    "Mailbox owned by another turn",
                )
            if not messages:
                return []
            result = []
            lease_until = now() + timedelta(seconds=300)
            for offset in range(0, len(messages), MAILBOX_BATCH_SIZE):
                batch = messages[offset : offset + MAILBOX_BATCH_SIZE]
                updated = await conn.execute(
                    update(s.ctf_messages)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.id.in_([message["id"] for message in batch]),
                    )
                    .values(
                        status="leased",
                        claim_turn_id=str(turn_id),
                        claim_generation=generation,
                        claim_token=func.coalesce(
                            s.ctf_messages.c.claim_token,
                            cast(func.gen_random_uuid(), s.ctf_messages.c.claim_token.type),
                        ),
                        lease_until=lease_until,
                    )
                    .returning(s.ctf_messages)
                )
                result.extend(dict(message) for message in updated.mappings())
            return sorted(result, key=lambda message: message["recipient_sequence"])

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
            condition = s.events.c.object_id == observation_id
            if kind == "initial_context":
                condition = (s.events.c.payload["agent_id"].astext == agent_id) & (
                    s.events.c.payload["kind"].astext == kind
                )
            found = (
                await conn.execute(
                    select(s.events.c.payload)
                    .where(
                        s.events.c.task_id == tid,
                        # A fixed predicate also lets generic prepared plans use the partial index.
                        s.events.c.type == literal_column("'agent.trace.recorded'"),
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
            message_ids = [
                str(delivery.get("message_id", delivery.get("id"))) for delivery in deliveries
            ]
            claimed = {}
            for offset in range(0, len(message_ids), MAILBOX_BATCH_SIZE):
                records = await conn.execute(
                    select(
                        *(
                            s.ctf_messages.c[name]
                            for name in (
                                "id",
                                "recipient_id",
                                "sender_id",
                                "kind",
                                "purpose",
                                "claim_turn_id",
                                "claim_generation",
                                "claim_token",
                            )
                        )
                    ).where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.id.in_(message_ids[offset : offset + MAILBOX_BATCH_SIZE]),
                    )
                )
                claimed.update((message["id"], message) for message in records.mappings())
            accepted = []
            for mid, delivery in zip(message_ids, deliveries, strict=True):
                require(mid in ids, "Delivery is absent from persisted history")
                require(mid in claimed, "Stale mailbox claim")
                msg = claimed[mid]
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
                conn,
                tid,
                agent_id,
                session,
                opening_instructions,
                "native",
                expected_revision,
                return_session=False,
            )
            for offset in range(0, len(accepted), MAILBOX_BATCH_SIZE):
                await conn.execute(
                    update(s.ctf_messages)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.id.in_(
                            [msg["id"] for msg in accepted[offset : offset + MAILBOX_BATCH_SIZE]]
                        ),
                    )
                    .values(
                        status="delivered",
                        delivered_turn_id=str(turn_id),
                        session_revision=saved["revision"],
                    )
                )
            assignments = list(turn["assignment_message_ids"])
            assignment_ids = set(assignments)
            for msg in accepted:
                if (
                    turn["purpose"] == "execution"
                    and msg["kind"] == "instruction"
                    and msg["id"] not in assignment_ids
                ):
                    assignments.append(msg["id"])
                    assignment_ids.add(msg["id"])
            if accepted:
                written = await self.repo.append(
                    conn,
                    tid,
                    [
                        event(
                            "ctf.message.delivered",
                            agent_id,
                            {"id": msg["id"], "session_revision": saved["revision"]},
                            msg["id"],
                            addressed_to=[msg["sender_id"], msg["recipient_id"]],
                        )
                        for msg in accepted
                    ],
                )
                await self.repo.notify(tid, written[-1]["version"], connection=conn)
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
            return {
                "revision": saved["revision"],
                "checkpoint_id": session.get("state", {}).get("bbx_checkpoint_id"),
                "task_id": str(tid),
                "agent_id": agent_id,
                "turn_id": str(turn_id),
                "generation": generation,
                "purpose": turn["purpose"],
                "delivered_message_ids": [msg["id"] for msg in accepted],
            }

    async def recovery_input_proofs(self, tid, agent_id, turn_id, generation, message_ids):
        """Read bounded delivery/completion proofs without returning private message bodies."""
        if not isinstance(message_ids, list) or len(message_ids) > 100:
            raise HTTPException(422, "At most 100 message UUIDs are required")
        try:
            if any(not isinstance(mid, str) or str(UUID(mid)) != mid for mid in message_ids):
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(422, "Message IDs must be canonical UUIDs") from exc
        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            _, current = await self._fence(conn, tid, agent_id, generation, turn_id)
            require(current["purpose"] == "execution", "Recovery proofs require execution turn")
            if not message_ids:
                return {"proofs": []}
            messages, turns = s.ctf_messages.c, s.ctf_turns.c
            one_shot = ((messages.sender_kind == "agent") & (messages.kind == "instruction")) | (
                (messages.sender_kind == "system")
                & (
                    (
                        (messages.kind == "turn_finished")
                        & (messages.notification_purpose == "assignment_result")
                    )
                    | (
                        (messages.kind == "instruction")
                        & messages.notification_purpose.in_(
                            ["turn_recovery", "context_continuation"]
                        )
                    )
                )
            )
            query = (
                select(
                    *[
                        messages[name]
                        for name in (
                            "id",
                            "recipient_id",
                            "sender_kind",
                            "sender_id",
                            "kind",
                            "purpose",
                            "session_revision",
                            "delivered_turn_id",
                            "claim_generation",
                            "source_turn_id",
                            "notification_purpose",
                        )
                    ],
                    turns.generation.label("turn_generation"),
                    turns.status.label("turn_status"),
                    turns.end_reason,
                )
                .select_from(
                    s.ctf_messages.join(
                        s.ctf_turns,
                        (messages.task_id == turns.task_id)
                        & (messages.delivered_turn_id == turns.id)
                        & (messages.recipient_id == turns.agent_id)
                        & (messages.claim_turn_id == turns.id)
                        & (messages.claim_generation == turns.generation),
                    )
                )
                .where(
                    messages.task_id == tid,
                    messages.recipient_id == agent_id,
                    messages.id.in_(message_ids),
                    messages.purpose == "execution",
                    messages.status == "delivered",
                    messages.session_revision > 0,
                    turns.purpose == "execution",
                    turns.id != str(turn_id),
                    turns.generation < generation,
                    turns.status == "finished",
                    turns.end_reason == "completed",
                    one_shot,
                )
                .order_by(messages.recipient_sequence)
            )
            proofs = (await conn.execute(query)).mappings().all()
            return {"proofs": [dict(proof) for proof in proofs]}

    async def _settle_turn_reservations(self, conn, tid, turn_id):
        turn = await row(conn, s.ctf_turns, tid, str(turn_id))
        if turn["purpose"] == "execution":
            await self._reconcile_usage_outbox(conn, tid, turn["agent_id"])
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

    async def reconcile_usage_outbox(self, tid, agent_id):
        """Service-only settlement reads usage exclusively from a confirmed Session."""
        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            return await self._reconcile_usage_outbox(conn, tid, agent_id)

    async def reconcile_budget(self, tid):
        """Reconcile only confirmed nonempty outboxes before a terminal budget decision."""
        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            return await self._reconcile_budget(conn, tid)

    async def _reconcile_budget(self, conn, tid):
        outbox = s.agent_sessions.c.session["state"]["ctf_usage_outbox"]
        members = (
            (
                await conn.execute(
                    select(s.agent_sessions.c.agent_id).where(
                        s.agent_sessions.c.task_id == tid,
                        func.jsonb_typeof(outbox) == "array",
                        outbox != [],
                    )
                )
            )
            .scalars()
            .all()
        )
        count = 0
        for agent_id in members:
            result = await self._reconcile_usage_outbox(conn, tid, agent_id)
            count += len(result["request_ids"])
        return {"members": len(members), "request_count": count}

    async def _reconcile_usage_outbox(self, conn, tid, agent_id):
        saved = (
            (
                await conn.execute(
                    select(
                        s.agent_sessions.c.session["state"]["ctf_usage_outbox"].label(
                            "ctf_usage_outbox"
                        ),
                        s.agent_sessions.c.session["state"]["ctf_review_usage_outbox"].label(
                            "ctf_review_usage_outbox"
                        ),
                        s.agent_sessions.c.session["state"]["ctf_steps"].label("ctf_steps"),
                    ).where(
                        s.agent_sessions.c.task_id == tid,
                        s.agent_sessions.c.agent_id == agent_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if saved is None:
            return {"request_ids": []}
        state = {key: value for key, value in saved.items() if value is not None}
        confirmed = []
        for key, purpose in (
            ("ctf_usage_outbox", "execution"),
            ("ctf_review_usage_outbox", "review"),
        ):
            for entry in state.get(key, []):
                require(isinstance(entry, dict), "Invalid persisted usage entry")
                reservation_id = str(entry.get("reservation_id", ""))
                try:
                    original_turn_id, raw_step = reservation_id.rsplit(":", 1)
                    step = int(raw_step)
                    UUID(original_turn_id)
                except (ValueError, TypeError):
                    raise HTTPException(409, "Invalid persisted usage call identity") from None
                turn = await row(conn, s.ctf_turns, tid, original_turn_id)
                require(
                    turn["agent_id"] == agent_id
                    and turn["purpose"] == purpose
                    and entry.get("agent_id", agent_id) == agent_id
                    and entry.get("turn_id", original_turn_id) == original_turn_id
                    and entry.get("generation", turn["generation"]) == turn["generation"]
                    and 0 < step <= int(state.get("ctf_steps", {}).get(original_turn_id, 0)),
                    "Persisted usage call identity mismatch",
                )
                request_id = str(entry.get("request_id", ""))
                require(bool(request_id), "Missing persisted usage request identity")
                if purpose == "review":
                    require(
                        isinstance(entry.get("request_id"), str)
                        and bool(entry["request_id"])
                        and ("generation" not in entry or type(entry["generation"]) is int),
                        "Invalid persisted review usage identity",
                    )
                usage = Usage.model_validate(entry.get("usage")).model_dump(mode="json")
                task = await row(conn, s.tasks, tid)
                member = await row(conn, s.ctf_members, tid, agent_id)
                await self._apply_usage(
                    conn, tid, task, member, turn, request_id, usage, reservation_id
                )
                confirmed.append(request_id)
        return {"request_ids": confirmed}

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
            # Legacy callers can retry an old receipt using the current execution fence.
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
            return await self._apply_usage(
                conn, tid, task, member, turn, request_id, usage, reservation_id
            )

    async def _apply_usage(self, conn, tid, task, member, turn, request_id, usage, reservation_id):
        agent_id, turn_id = member["id"], turn["id"]
        duplicate = (
            await conn.execute(
                select(s.ctf_turns.c.id).where(
                    s.ctf_turns.c.task_id == tid,
                    s.ctf_turns.c.usage_requests.contains([str(request_id)]),
                )
            )
        ).first()
        if duplicate:
            require(str(duplicate[0]) == str(turn_id), "Usage request belongs to another turn")
            return turn["usage"]
        total = add_usage(turn["usage"], usage)
        control = dict(task["ctf_control"])
        reservations = _reservations(task)
        unknown = dict(task["ctf_control"].get("budget_unknown_reservations", {}))
        key = str(reservation_id) if reservation_id is not None else None
        if key is None:
            key = next(
                (
                    item_key
                    for item_key, item in reservations.items()
                    if item.get("turn_id") == str(turn_id)
                ),
                None,
            )
        if key is None:
            key = next(
                (
                    item_key
                    for item_key, item in unknown.items()
                    if item.get("turn_id") == str(turn_id)
                ),
                None,
            )
        if key is not None and key not in reservations and key not in unknown:
            key = None
        if key is not None:
            source = reservations if key in reservations else unknown
            require(
                source[key].get("agent_id") == agent_id
                and source[key].get("turn_id") == str(turn_id)
                and source[key].get("generation") == turn["generation"],
                "Usage reservation identity mismatch",
            )
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

    async def _retire_checkpoint_barrier(self, conn, tid, turn):
        """Retain an unconfirmed snapshot as a fault when its writer has exited."""
        task = await row(conn, s.tasks, tid)
        control = task["ctf_control"]
        if turn["id"] not in control.get("unresolved_checkpoints", []):
            return
        await self._settle_turn_reservations(conn, tid, turn["id"])
        task = await row(conn, s.tasks, tid)
        control = task["ctf_control"]
        failures = dict(control.get("checkpoint_failures", {}))
        failures.setdefault(
            turn["id"],
            {
                "agent_id": turn["agent_id"],
                "generation": turn["generation"],
                "run_number": task["run_number"],
                "purpose": turn["purpose"],
                "checkpoint_revision": turn["checkpoint_revision"],
                "reason": "Turn exited before its final checkpoint was confirmed",
            },
        )
        await patch(
            conn,
            s.tasks,
            tid,
            {
                "ctf_control": {
                    **control,
                    "unresolved_checkpoints": [
                        item for item in control["unresolved_checkpoints"] if item != turn["id"]
                    ],
                    "checkpoint_failures": failures,
                }
            },
        )

    async def _settle(self, conn, tid, member, turn, end_reason, answer):
        if turn["status"] != "running":
            await self._retire_checkpoint_barrier(conn, tid, turn)
            return turn
        await self._settle_turn_reservations(conn, tid, turn["id"])
        if turn["purpose"] == "review":
            result = await self._settle_review(conn, tid, member, turn, end_reason, answer)
            await self._retire_checkpoint_barrier(conn, tid, turn)
            return result
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
        if end_reason == "context_blocked":
            saved = await self.sessions._session(conn, tid, member["id"])
            entry = (saved or {}).get("session", {}).get("state", {}).get("ctf_context_blocked", {})
            require(
                entry.get("turn_id") == turn["id"], "Context block is absent from confirmed Session"
            )
            task = await row(conn, s.tasks, tid)
            blocked = dict(task["ctf_control"].get("context_blocked_members", {}))
            entry = {
                **entry,
                "message_ids": list(
                    dict.fromkeys(
                        value
                        for value in entry.get("message_ids", [])
                        if isinstance(value, str) and value
                    )
                ),
            }
            blocked[member["id"]] = {
                **entry,
                "input_message_ids": list(
                    dict.fromkeys(
                        value
                        for value in [
                            *assignments,
                            *[m["id"] for m in leased],
                            *entry.get("message_ids", []),
                        ]
                        if isinstance(value, str) and value
                    )
                ),
            }
            history = dict(task["ctf_control"].get("context_blocked_history", {}))
            history[turn["id"]] = {
                **blocked[member["id"]],
                "agent_id": member["id"],
                "generation": turn["generation"],
                "run_number": task["run_number"],
            }
            await patch(
                conn,
                s.tasks,
                tid,
                {
                    "ctf_control": {
                        **task["ctf_control"],
                        "context_blocked_members": blocked,
                        "context_blocked_history": history,
                    }
                },
            )
        for message in leased:
            await patch(
                conn,
                s.ctf_messages,
                tid,
                {
                    "status": "queued",
                    "deferred": end_reason in {"context_blocked", "provider_transient"}
                    or message["deferred"],
                    "claim_token": None,
                    "claim_turn_id": None,
                    "claim_generation": None,
                    "lease_until": None,
                },
                message["id"],
            )
            if message["notification_purpose"] == "turn_recovery" and end_reason in {
                "context_blocked",
                "provider_transient",
            }:
                await self._emit(
                    conn,
                    tid,
                    "coordination.reminded",
                    "system",
                    {
                        "agent_id": member["id"],
                        "turn_id": turn["id"],
                        "message_id": message["id"],
                        "status": "recovery_deferred",
                        "reason": end_reason,
                    },
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
        await self._retire_checkpoint_barrier(conn, tid, turn)
        recovery = {
            "status": "recovery_not_requested",
            "reason": end_reason,
            "attempt": 0,
            "limit": 0,
        }
        if end_reason in {"step_limit", "context_blocked", "provider_transient"}:
            recovery = await self._retry_limited_turn(conn, tid, member, turn, end_reason)
        elif end_reason == "context_limit":
            recovery["status"] = "continuation_pending"
            recovery["reason"] = "runner_reconciliation"
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
                    "recovery": recovery,
                },
                ensure_ascii=False,
            )
            await self._message(
                conn,
                tid,
                "system",
                "lead",
                body,
                str(uuid5(NAMESPACE_URL, f"ctf-assignment-result:{tid}:{turn['id']}")),
                "turn_finished",
                assignments[0],
                source_turn_id=turn["id"],
                notification_purpose="assignment_result",
            )
        return {**turn, **values}

    async def _retry_limited_turn(self, conn, tid, member, turn, end_reason):
        """Continue the same assignment a bounded number of times per member and run."""
        if end_reason not in {"step_limit", "context_blocked", "provider_transient"}:
            return {
                "status": "recovery_not_requested",
                "reason": end_reason,
                "attempt": 0,
                "limit": 0,
            }
        task = await row(conn, s.tasks, tid)
        control = task["ctf_control"]
        retries = dict(control.get("turn_retries", {}))
        counts = dict(retries.get(member["id"], {}))
        count = counts.get(end_reason, 0)
        limit = 1 if end_reason == "context_blocked" else 3
        reason = next(
            (
                reason
                for reason, disabled in (
                    ("lifecycle", member["lifecycle"] != "active"),
                    ("pending_operation", bool(member.get("pending_operation"))),
                    ("phase", control["phase"] != "running"),
                    (
                        "replacement",
                        control.get("replacement", {}).get("phase", "ready") != "ready",
                    ),
                )
                if disabled
            ),
            None,
        )
        if reason is None and count < limit:
            try:
                check_budget(task)
            except HTTPException:
                reason = "budget"
        if reason is not None or count >= limit:
            exhausted = reason is None
            if exhausted and end_reason in {"provider_transient", "context_blocked"}:
                label = "context" if end_reason == "context_blocked" else "provider"
                diagnostics = await self._recovery_diagnostics(conn, tid, control, member["id"])
                await self._message(
                    conn,
                    tid,
                    "system",
                    "lead",
                    f"Member {member['id']} exhausted {limit} automatic {label} recoveries "
                    "in this run. Full saved history, records, ownership and queued inputs remain. "
                    "Inspect saved work; Lead can explicitly confirm relevant deferred messages "
                    "or issue a smaller new instruction. Confirmation does not bypass a context "
                    "block on the same old input. No further automatic retry is allowed. "
                    + json.dumps(diagnostics),
                    str(uuid5(NAMESPACE_URL, f"ctf-{label}-exhausted:{tid}:{turn['id']}")),
                    deferred=member["role"] == "lead" or control["phase"] != "running",
                    source_turn_id=turn["id"],
                    notification_purpose=f"{label}_recovery_exhausted",
                )
            payload = {
                "agent_id": member["id"],
                "turn_id": turn["id"],
                "generation": turn["generation"],
                "end_reason": end_reason,
                "attempt": count,
                "limit": limit,
                "status": "recovery_exhausted" if exhausted else "recovery_disabled",
                "reason": "retry_limit" if exhausted else reason,
                "notification_deferred": exhausted and member["role"] == "lead",
            }
            LOGGER.warning("CTF turn recovery declined: %s", payload)
            await self._emit(conn, tid, "coordination.reminded", "system", payload)
            return {key: payload[key] for key in ("status", "reason", "attempt", "limit")}
        counts[end_reason] = count + 1
        retries[member["id"]] = counts
        message_id = str(uuid5(NAMESPACE_URL, f"ctf-turn-retry:{tid}:{turn['id']}"))
        control = {**control, "turn_retries": retries}
        ready_at = None
        if end_reason == "provider_transient":
            ready_at = (now() + timedelta(seconds=2 ** (count + 1))).isoformat()
            control["turn_retry_delays"] = {
                **control.get("turn_retry_delays", {}),
                member["id"]: {
                    "message_id": message_id,
                    "ready_at": ready_at,
                },
            }
        await patch(conn, s.tasks, tid, {"ctf_control": control})
        await self._message(
            conn,
            tid,
            "system",
            member["id"],
            f"The previous turn ended with {end_reason}. Continue your existing assignment "
            "from saved team state and records; avoid repeating completed work or tools with "
            "unknown outcomes. Ask Lead if you need a new assignment. "
            f"This is automatic recovery {count + 1}/{limit} for this limit in this run.",
            message_id,
            "instruction",
            source_turn_id=turn["id"],
            notification_purpose="turn_recovery",
        )
        await self._emit(
            conn,
            tid,
            "coordination.reminded",
            "system",
            {
                "agent_id": member["id"],
                "turn_id": turn["id"],
                "end_reason": end_reason,
                "attempt": count + 1,
                "limit": limit,
                "message_id": message_id,
                "status": "recovery_requested",
                "reason": "bounded_retry",
                "deferred": False,
                "generation": turn["generation"],
            },
        )
        return {
            "status": "recovery_requested",
            "reason": "bounded_retry",
            "attempt": count + 1,
            "limit": limit,
            "message_id": message_id,
            "ready_at": ready_at,
        }

    async def finish_turn(self, tid, agent_id, turn_id, generation, end_reason, answer=""):
        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            turn = await row(conn, s.ctf_turns, tid, str(turn_id))
            require(
                turn["agent_id"] == agent_id and turn["generation"] == generation,
                "Stale settlement",
            )
            if turn["status"] != "running":
                await self._retire_checkpoint_barrier(conn, tid, turn)
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
            turns = [
                {
                    "turn_id": turn["id"],
                    "member_id": turn["agent_id"],
                    "generation": turn["generation"],
                }
                for turn in (
                    await conn.execute(
                        select(s.ctf_turns)
                        .where(
                            s.ctf_turns.c.task_id == tid,
                            s.ctf_turns.c.status == "running",
                            s.ctf_turns.c.purpose == "execution",
                        )
                        .order_by(s.ctf_turns.c.agent_id, s.ctf_turns.c.id)
                    )
                )
                .mappings()
                .all()
            ]
            await patch(conn, s.tasks, tid, {"ctf_control": control, "ctf_conclusion": stored})
            await self._emit(conn, tid, "conclusion.requested", actor, {"conclusion": stored})
            await self._emit(
                conn,
                tid,
                "coordination.reminded",
                actor,
                {
                    "kind": "closing_turn_cancellation",
                    "status": "cancellation_requested",
                    "reason": validated["end_reason"],
                    "operation_id": str(request_id),
                    "epoch": control["epoch"],
                    "run_number": task["run_number"],
                    "turn_count": len(turns),
                    "turn_ids": [turn["turn_id"] for turn in turns],
                    "turns": turns,
                },
            )
            return {"operation_id": str(request_id), "phase": "closing"}

    async def finalize_close(self, tid, drained, checkpoint_failure=None):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            control = task["ctf_control"]
            if control["phase"] == "closed":
                if control.get("context_blocked_members"):
                    control = clear_context_blocks(task, control)
                    await patch(conn, s.tasks, tid, {"ctf_control": control})
                    return {**task, "ctf_control": control}
                return task
            require(control["phase"] == "closing" and drained, "Drain is not confirmed")
            require(
                control.get("replacement", {}).get("phase", "ready") == "ready",
                "Execution replacement is in progress",
            )
            # Repair legacy finished writers without accepting writes from an old generation.
            for turn_id in list(control.get("unresolved_checkpoints", [])):
                turn = await row(conn, s.ctf_turns, tid, turn_id)
                if turn["status"] != "running":
                    await self._retire_checkpoint_barrier(conn, tid, turn)
            task = await row(conn, s.tasks, tid)
            control = task["ctf_control"]
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
            failures = {
                key: value
                for key, value in control.get("checkpoint_failures", {}).items()
                if value.get("run_number") == task["run_number"]
                and value.get("purpose") == "execution"
            }
            if failures or checkpoint_failure or control.get("close_failure"):
                issues = list(control.get("close_failure", [])) + [
                    f"Unconfirmed final checkpoint for turn {turn_id}; latest Session and usage "
                    "may be incomplete. Saved state and unknown usage reservations are retained."
                    for turn_id in failures
                ]
                if checkpoint_failure:
                    issues.append(str(checkpoint_failure)[:2000])
                issues = list(dict.fromkeys(issues))
                conclusion = {
                    **conclusion,
                    "end_reason": "system_failure",
                    "unresolved_items": list(
                        dict.fromkeys([*conclusion.get("unresolved_items", []), *issues])
                    ),
                }
                control = {**control, "close_failure": issues}
            status = {"user_stop": "stopped", "system_failure": "failed"}.get(
                conclusion.get("end_reason", "partial"), "finished"
            )
            active = task["active_seconds"] + (
                max(0, int((now() - task["active_since"]).total_seconds()))
                if task["active_since"]
                else 0
            )
            control = clear_context_blocks(task, control)
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
                    require(turn["agent_id"] == member["id"], "Recovery owner mismatch")
                    if turn["status"] == "running":
                        execution = member.get("execution") or {}
                        require(
                            turn["generation"] == member["generation"]
                            or (
                                turn["generation"] < member["generation"]
                                and execution.get("generation") == turn["generation"]
                                and execution.get("drained") is True
                            ),
                            "Stale recovery writer",
                        )
                        interrupted.append(turn["id"])
                    else:
                        other = (
                            await conn.execute(
                                select(s.ctf_turns.c.id)
                                .where(
                                    s.ctf_turns.c.task_id == tid,
                                    s.ctf_turns.c.agent_id == member["id"],
                                    s.ctf_turns.c.status == "running",
                                )
                                .limit(1)
                            )
                        ).first()
                        require(
                            other is None, "Running writer contradicts finished recovery pointer"
                        )
                    await self._settle(conn, tid, member, turn, "interrupted", "")
                    await patch(
                        conn,
                        s.ctf_members,
                        tid,
                        {
                            "generation": member["generation"] + 1,
                            "current_turn_id": None,
                            "run_state": "idle",
                        },
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
                if pending["target_generation"] != generation:
                    require(
                        turn_id is not None
                        and execution is not None
                        and execution.get("drained") is True
                        and pending["target_generation"] == execution["generation"]
                        and pending["boot_id"] in {None, execution["boot_id"]}
                        and generation > execution["generation"],
                        "Pending stop targets another generation",
                    )
                    pending = {**pending, "target_generation": generation, "boot_id": boot_id}
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

    async def complete_member_operation(
        self, tid, member_id, request_id, proof, checkpoint_failure=None
    ):
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
            if checkpoint_failure:
                require(
                    task["ctf_control"]["phase"] == "closing",
                    "Checkpoint failure escalation requires closing",
                )
                failures = list(task["ctf_control"].get("close_failure", []))
                failures = list(dict.fromkeys([*failures, str(checkpoint_failure)[:2000]]))
                await patch(
                    conn,
                    s.tasks,
                    tid,
                    {"ctf_control": {**task["ctf_control"], "close_failure": failures}},
                )
            else:
                require(
                    member["current_turn_id"]
                    not in task["ctf_control"].get("unresolved_checkpoints", []),
                    "Final checkpoint remains unresolved",
                )
            if member["current_turn_id"]:
                turn = await row(conn, s.ctf_turns, tid, member["current_turn_id"])
                if checkpoint_failure:
                    require(
                        turn["agent_id"] == member_id
                        and turn["generation"] == member["generation"] == execution["generation"],
                        "Stale checkpoint failure settlement",
                    )
                pending = {
                    **pending,
                    "settled_turn_id": turn["id"],
                    "settled_turn_generation": turn["generation"],
                }
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
                cancelled = (
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
                        .returning(s.ctf_messages)
                    )
                ).mappings()
                for message in cancelled:
                    if message["notification_purpose"] == "turn_recovery":
                        await self._emit(
                            conn,
                            tid,
                            "coordination.reminded",
                            "system",
                            {
                                "agent_id": member_id,
                                "turn_id": message["source_turn_id"],
                                "message_id": message["id"],
                                "status": "recovery_cancelled",
                                "reason": "member_removed",
                            },
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
                unassigned = []
                for challenge in challenges:
                    data = challenge["data"]
                    updated = {
                        **data,
                        "collaborator_ids": [
                            aid for aid in data.get("collaborator_ids", []) if aid != member_id
                        ],
                    }
                    if data.get("owner_id") == member_id and data.get("work_status") not in {
                        "completed",
                        "cancelled",
                    }:
                        updated.update(
                            owner_id=None, previous_owner_id=member_id, work_status="blocked"
                        )
                        if not data.get("tombstone"):
                            unassigned.append(challenge["id"])
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
                if unassigned:
                    await self._message(
                        conn,
                        tid,
                        "system",
                        "lead",
                        json.dumps(
                            {
                                "reason": "owner_removed",
                                "previous_owner_id": member_id,
                                "challenge_ids": sorted(unassigned)[:100],
                                "challenge_count": len(unassigned),
                                "truncated": len(unassigned) > 100,
                            },
                            sort_keys=True,
                        ),
                        str(
                            uuid5(
                                NAMESPACE_URL, f"ctf-owner-removed:{tid}:{member_id}:{request_id}"
                            )
                        ),
                        notification_purpose="challenge_available",
                        deferred=task["ctf_control"]["phase"] != "running",
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
