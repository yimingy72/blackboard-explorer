"""Persistent finite control; Pi owns history and every execution loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from bbx_contracts.challenge_board import BOARD_CONTRACT, business_contract, native_dispatch_enabled
from bbx_contracts.runner import (
    PROTOCOL,
    CommandReceipt,
    JournalConfirmation,
    RunnerBinding,
    RunnerCommand,
    RunnerFence,
    RunnerPayload,
    SessionLeaseRequest,
    SessionSpec,
    runner_json_text,
    runner_payload_digest,
)
from bbx_contracts.runner_control import (
    RunnerBindingRegistration,
    RunnerCommandSubmission,
    RunnerOwnerRegistration,
    RunnerOwnerUpdate,
    RunnerPromptPublication,
)
from bbx_contracts.sandbox_tools import SANDBOX_TOOLS, tool_alias
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import and_, insert, literal, or_, select, update
from sqlalchemy.exc import NoResultFound

from bbx_blackboard.store import Repository
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import row as stored_row

TERMINAL = {"finished", "failed", "stopped"}
OWNER_SECONDS = 60
PERMIT_SECONDS = 30
POLL_RECHECK_SECONDS = 5


async def row(conn, table, tid, oid=None):
    try:
        return await stored_row(conn, table, tid, oid)
    except NoResultFound as error:
        raise HTTPException(404, "Runner referenced record is unavailable") from error


def decode(model, raw):
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "Runner metadata exceeds its bound")
    try:
        text = raw.decode("utf-8")
        return model.model_validate_json(text), text, hashlib.sha256(raw).hexdigest()
    except (UnicodeError, ValidationError) as error:
        raise HTTPException(422, "Invalid Runner metadata") from error


def fence(owner):
    return {
        "task_id": str(owner["task_id"]),
        "runner_id": owner["runner_id"],
        "pod_uid": owner["pod_uid"],
        "epoch": owner["epoch"],
    }


def command_key(epoch, request_id):
    return f"{epoch}:{request_id}"


class RunnerControl:
    def __init__(self, engine, *, clock=None):
        self.repo = Repository(engine)
        self.clock = clock or (lambda: datetime.now(UTC))

    async def _task(self, conn, tid, *, live=False, allow_held=False):
        task = await row(conn, s.tasks, tid)
        if task["mode"] != "ctf" or business_contract(task) != BOARD_CONTRACT:
            raise HTTPException(409, "Frozen challenge-board-v1 task required")
        if live and (
            task["deleting"]
            or task["ctf_control"].get("native_stop_requested")
            or (task["ctf_control"].get("native_budget_hold") and not allow_held)
            or task["status"] in TERMINAL
            or task["ctf_control"]["phase"]
            in {"closing", "failure_hold", "failed", "stopped", "finished"}
        ):
            raise HTTPException(409, "Runner control is stopped")
        return task

    async def _current_owner(self, conn, tid):
        found = (
            (
                await conn.execute(
                    select(s.runner_owners).where(
                        s.runner_owners.c.task_id == tid, s.runner_owners.c.current.is_(True)
                    )
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            raise HTTPException(403, "Runner owner is unavailable")
        owner = dict(found)
        self._owner_live(owner)
        return owner

    def _owner_live(self, owner):
        if (
            not owner["current"]
            or owner["revoked"]
            or owner["expires_at"] <= self.clock()
            or owner["hard_deadline"] <= self.clock()
        ):
            raise HTTPException(409, "Runner owner fence expired or was revoked")

    async def _identity(self, conn, tid, identity, *, live=False):
        owner = (
            (
                await conn.execute(
                    select(s.runner_owners).where(
                        s.runner_owners.c.task_id == tid,
                        s.runner_owners.c.epoch == identity.fence.epoch,
                    )
                )
            )
            .mappings()
            .first()
        )
        if (
            owner is None
            or fence(owner) != identity.fence.model_dump(mode="json")
            or owner["token_version"] != identity.token_version
        ):
            raise HTTPException(403, "Runner identity does not match its registered owner")
        if live:
            await self._task(conn, tid, live=True)
            self._owner_live(owner)
        return dict(owner)

    async def _mapping(self, conn, tid, value, *, allow_pending=False):
        binding, business = value["binding"], value["business"]
        member = await row(conn, s.ctf_members, tid, binding["agent_id"])
        if (
            member["lifecycle"] != "active"
            or member["run_state"] == "stopping"
            or (member.get("pending_operation") and not allow_pending)
            or business["purpose"] != "execution"
            or member["generation"] != business["generation"]
            or member["current_turn_id"] != business["turn_id"]
        ):
            raise HTTPException(403, "Current business turn mapping is unavailable")
        turn = await row(conn, s.ctf_turns, tid, business["turn_id"])
        if (
            turn["agent_id"] != member["id"]
            or turn["generation"] != member["generation"]
            or turn["status"] != "running"
            or turn["purpose"] != business["purpose"]
        ):
            raise HTTPException(403, "Current business turn mapping is unavailable")
        if business["challenge_id"]:
            challenge = await row(conn, s.ctf_challenges, tid, business["challenge_id"])
            data = challenge["data"]
            if (
                data.get("owner_id") != member["id"]
                or data.get("scope_revision", 1) != business["scope_revision"]
                or data.get("work_status") in {"cancelled", "completed"}
                or data.get("tombstone")
            ):
                raise HTTPException(403, "Current Assignment is unavailable")
        if business.get("native_assignment_id"):
            task = await row(conn, s.tasks, tid)
            assignment = task["ctf_control"].get("native_worker_active", {}).get(member["id"], {})
            if (
                assignment.get("assignment_ref") != business["native_assignment_id"]
                or assignment.get("work_id") != business["challenge_id"]
                or assignment.get("turn_id") != business["turn_id"]
                or assignment.get("generation") != business["generation"]
                or assignment.get("epoch") != value["owner_epoch"]
            ):
                raise HTTPException(403, "Original native Assignment changed")
        return member, turn

    async def _binding(self, conn, tid, bid, owner, *, live=False, permit=False):
        value = await row(conn, s.runner_bindings, tid, bid)
        if live:
            if value["owner_epoch"] != owner["epoch"] or value["status"] not in {
                "registered",
                "prepared",
            }:
                raise HTTPException(409, "Runner binding is closed or belongs to another owner")
            await self._mapping(conn, tid, value)
            if permit and (
                value["permit_expires_at"] is None or value["permit_expires_at"] <= self.clock()
            ):
                raise HTTPException(409, "Session permit expired")
        else:
            original = (
                await conn.execute(
                    select(s.runner_commands.c.id).where(
                        s.runner_commands.c.task_id == tid,
                        s.runner_commands.c.owner_epoch == owner["epoch"],
                        s.runner_commands.c.binding_id == bid,
                        s.runner_commands.c.kind == "prepare",
                    )
                )
            ).first()
            if original is None:
                raise HTTPException(403, "Owner has no registered binding evidence")
        return value

    async def _receipt(
        self, conn, tid, epoch, kind, request_id, text, sha, response, bid=None, late=False
    ):
        key = f"{epoch}:{kind}:{request_id}:{sha}"
        existing = (
            await conn.execute(
                select(s.runner_receipts.c.response).where(
                    s.runner_receipts.c.task_id == tid, s.runner_receipts.c.id == key
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            await conn.execute(
                insert(s.runner_receipts).values(
                    task_id=tid,
                    id=key,
                    owner_epoch=epoch,
                    kind=kind,
                    request_id=request_id,
                    binding_id=bid,
                    body_text=text,
                    body_digest=sha,
                    response=response,
                    late=late,
                )
            )
        return existing or response

    async def native_worker_cleanup_changed(self, conn, tid, bid):
        task = await row(conn, s.tasks, tid)
        active = task["ctf_control"].get("native_worker_active", {})
        if not any(value["binding_id"] == bid for value in active.values()):
            return
        written = await self.repo.append(
            conn,
            tid,
            [
                {
                    "type": "ctf.native_worker.cleanup_changed",
                    "actor": "system",
                    "object_id": bid,
                    "payload": {"binding_id": bid},
                    "addressed_to": None,
                }
            ],
        )
        await self.repo.notify(tid, written[-1]["version"], connection=conn)

    async def _prior(self, conn, tid, epoch, kind, request_id, sha):
        previous = (
            (
                await conn.execute(
                    select(s.runner_receipts)
                    .where(
                        s.runner_receipts.c.task_id == tid,
                        s.runner_receipts.c.owner_epoch == epoch,
                        s.runner_receipts.c.kind == kind,
                        s.runner_receipts.c.request_id == request_id,
                    )
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        if previous and previous["body_digest"] != sha:
            raise HTTPException(409, "Runner request bytes conflict")
        return dict(previous) if previous else None

    async def register_owner(self, tid, raw):
        body, _, sha = decode(RunnerOwnerRegistration, raw)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task = await self._task(conn, tid, live=True)
            original = (
                (
                    await conn.execute(
                        select(s.runner_owners).where(
                            s.runner_owners.c.task_id == tid,
                            s.runner_owners.c.registration_id == str(body.request_id),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if original:
                if original["registration_digest"] != sha:
                    raise HTTPException(409, "Owner registration bytes conflict")
                return dict(original)
            previous = (
                (
                    await conn.execute(
                        select(s.runner_owners)
                        .where(s.runner_owners.c.task_id == tid)
                        .order_by(s.runner_owners.c.epoch.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            epoch = previous["epoch"] if previous else 0
            if body.expected_epoch != epoch:
                raise HTTPException(409, "Runner owner epoch changed")
            instant = self.clock()
            remaining = task["budget"]["max_minutes"] * 60 - task["active_seconds"]
            if task["active_since"]:
                remaining -= max(0, (instant - task["active_since"]).total_seconds())
            deadline = instant + timedelta(seconds=max(0, remaining))
            if body.hard_deadline is not None:
                deadline = min(deadline, body.hard_deadline)
            if previous:
                deadline = min(deadline, previous["hard_deadline"])
            if deadline <= instant:
                raise HTTPException(409, "Task deadline has expired")
            await conn.execute(
                update(s.runner_owners)
                .where(s.runner_owners.c.task_id == tid, s.runner_owners.c.current.is_(True))
                .values(current=False, revoked=True)
            )
            value = dict(
                task_id=tid,
                epoch=epoch + 1,
                current=True,
                runner_id=body.runner_id,
                pod_uid=body.pod_uid,
                token_version=str(uuid4()),
                expires_at=min(deadline, instant + timedelta(seconds=OWNER_SECONDS)),
                hard_deadline=deadline,
                revoked=False,
                registration_id=str(body.request_id),
                registration_digest=sha,
                next_seq=0,
            )
            await conn.execute(insert(s.runner_owners).values(**value))
            await self.repo.notify(tid, 0, connection=conn)
            from bbx_blackboard.runner_gateway import cancel_admitted

            await cancel_admitted(conn, tid)
            return value

    async def update_owner(self, tid, raw, *, revoke=False):
        body, _, _ = decode(RunnerOwnerUpdate, raw)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task(conn, tid, live=not revoke, allow_held=True)
            owner = (
                (
                    await conn.execute(
                        select(s.runner_owners).where(
                            s.runner_owners.c.task_id == tid,
                            s.runner_owners.c.epoch == body.expected_epoch,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if owner is None:
                raise HTTPException(404, "Runner owner not found")
            if not revoke:
                self._owner_live(owner)
            patch = (
                {"revoked": True, "current": False}
                if revoke
                else {
                    "expires_at": min(
                        owner["hard_deadline"], self.clock() + timedelta(seconds=OWNER_SECONDS)
                    )
                }
            )
            await conn.execute(
                update(s.runner_owners)
                .where(
                    s.runner_owners.c.task_id == tid, s.runner_owners.c.epoch == body.expected_epoch
                )
                .values(**patch)
            )
            await self.repo.notify(tid, 0, connection=conn)
            if revoke:
                from bbx_blackboard.runner_gateway import cancel_admitted

                await cancel_admitted(conn, tid, epoch=body.expected_epoch)
            return {**owner, **patch}

    async def register_binding(self, tid, raw):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            return await self._register_binding(conn, tid, raw)

    async def _register_binding(self, conn, tid, raw, *, assignment=None):
        body, _, sha = decode(RunnerBindingRegistration, raw)
        task = await self._task(conn, tid, live=True)
        owner = await self._current_owner(conn, tid)
        if body.agent_id != "lead" and native_dispatch_enabled(task):
            current = task["ctf_control"].get("native_worker_active", {}).get(body.agent_id)
            if not assignment or current != assignment:
                raise HTTPException(403, "Native Worker binding requires its original Assignment")
        if body.expected_epoch != owner["epoch"]:
            raise HTTPException(409, "Runner owner epoch changed")
        original = (
            (
                await conn.execute(
                    select(s.runner_bindings).where(
                        s.runner_bindings.c.task_id == tid,
                        s.runner_bindings.c.registration_id == str(body.request_id),
                    )
                )
            )
            .mappings()
            .first()
        )
        if original:
            if original["registration_digest"] != sha:
                raise HTTPException(409, "Binding registration bytes conflict")
            return dict(original)
        member = await row(conn, s.ctf_members, tid, body.agent_id)
        if member["current_turn_id"] is None:
            raise HTTPException(403, "Business turn mapping is not ready")
        if member["role"] not in {"lead", "teammate"}:
            raise HTTPException(403, "Runner role is not ready")
        if (member["role"] == "teammate") != (body.challenge_id is not None):
            raise HTTPException(403, "Worker requires its current challenge Assignment")
        if (
            await conn.execute(
                select(s.sandbox_command_records.c.id)
                .where(
                    s.sandbox_command_records.c.task_id == tid,
                    s.sandbox_command_records.c.agent_id == body.agent_id,
                    s.sandbox_command_records.c.data["scope_state"].astext != "sealed",
                )
                .limit(1)
            )
        ).first():
            raise HTTPException(409, "Original Sandbox Assignment cleanup is not confirmed")
        if (
            await conn.execute(
                select(s.runner_calls.c.id)
                .where(
                    s.runner_calls.c.task_id == tid,
                    s.runner_calls.c.provider_key == "sandbox",
                    s.runner_calls.c.state.in_(["dispatched", "unknown"]),
                    s.runner_calls.c.data["binding"]["agent_id"].astext == body.agent_id,
                )
                .limit(1)
            )
        ).first():
            raise HTTPException(409, "Original Sandbox tool outcome remains unknown")
        cid = str(body.challenge_id) if body.challenge_id else None
        scope_revision = None
        if cid:
            challenge = await row(conn, s.ctf_challenges, tid, cid)
            scope_revision = challenge["data"].get("scope_revision", 1)
        prior_bindings = (
            (
                await conn.execute(
                    select(s.runner_bindings.c.binding).where(
                        s.runner_bindings.c.task_id == tid,
                        s.runner_bindings.c.binding["agent_id"].astext == body.agent_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if any(
            (
                await conn.execute(
                    select(s.runner_bindings.c.id).where(
                        s.runner_bindings.c.task_id == tid,
                        s.runner_bindings.c.binding["agent_id"].astext == body.agent_id,
                        s.runner_bindings.c.status.in_(["registered", "prepared", "closing"]),
                    )
                )
            ).all()
        ):
            raise HTTPException(409, "Member already has a live Runner binding")
        bid = str(uuid5(NAMESPACE_URL, f"runner-binding:{tid}:{body.request_id}"))
        binding = RunnerBinding(
            business_contract=BOARD_CONTRACT,
            binding_id=bid,
            platform_session_id=str(uuid4()),
            agent_id=body.agent_id,
            generation=max((x["generation"] for x in prior_bindings), default=0) + 1,
            journal_generation=str(uuid4()),
            assignment_ref=(
                assignment["assignment_ref"]
                if assignment
                else str(
                    uuid5(
                        NAMESPACE_URL,
                        f"assignment:{tid}:{cid}:{scope_revision}:{body.agent_id}",
                    )
                )
                if cid
                else None
            ),
        ).model_dump(mode="json")
        spec = SessionSpec.model_validate(
            {
                "binding": binding,
                **body.model_dump(
                    exclude={"request_id", "expected_epoch", "agent_id", "challenge_id"}
                ),
                "mcp_tool_aliases": {name: tool_alias(name) for name in body.allowed_tools},
            }
        )
        payload = runner_json_text({"kind": "prepare", "spec": spec.model_dump(mode="json")})
        value = dict(
            task_id=tid,
            id=bid,
            owner_epoch=owner["epoch"],
            registration_id=str(body.request_id),
            registration_digest=sha,
            binding=binding,
            business={
                "turn_id": member["current_turn_id"],
                "generation": member["generation"],
                "purpose": "execution",
                "challenge_id": cid,
                "scope_revision": scope_revision,
                **({"native_assignment_id": assignment["assignment_ref"]} if assignment else {}),
            },
            prepare_request_id=str(uuid5(NAMESPACE_URL, f"runner-prepare:{bid}")),
            payload_json=payload,
            payload_digest=runner_payload_digest(payload),
            status="registered",
            sdk_session_id=None,
            cursor=None,
            permit_expires_at=None,
            permit_revision=0,
            prompt_revision=body.prompt_revision,
            instructions=body.instructions,
        )
        await self._mapping(conn, tid, value)
        await conn.execute(insert(s.runner_bindings).values(**value))
        await self._enqueue(
            conn, tid, owner, value["prepare_request_id"], payload, value["payload_digest"]
        )
        return value

    async def attach(self, tid, bid, raw):
        body, _, _ = decode(RunnerOwnerUpdate, raw)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task(conn, tid, live=True)
            owner = await self._current_owner(conn, tid)
            if body.expected_epoch != owner["epoch"]:
                raise HTTPException(409, "Runner owner epoch changed")
            value = await row(conn, s.runner_bindings, tid, bid)
            if value["status"] not in {"registered", "prepared"}:
                raise HTTPException(409, "Closed binding cannot be restored")
            await self._mapping(conn, tid, value)
            if value["owner_epoch"] == owner["epoch"]:
                return value
            if set(json.loads(value["payload_json"])["spec"]["allowed_tools"]).intersection(
                SANDBOX_TOOLS
            ):
                raise HTTPException(409, "Sandbox bindings cannot attach to another owner epoch")
            commands = (
                (
                    await conn.execute(
                        select(s.runner_commands)
                        .where(
                            s.runner_commands.c.task_id == tid,
                            s.runner_commands.c.owner_epoch == value["owner_epoch"],
                            s.runner_commands.c.binding_id == bid,
                            s.runner_commands.c.kind.in_(["prepare", "input"]),
                        )
                        .order_by(s.runner_commands.c.command_seq)
                    )
                )
                .mappings()
                .all()
            )
            patch = {
                "owner_epoch": owner["epoch"],
                "permit_expires_at": None,
                "permit_revision": value["permit_revision"] + 1,
            }
            await conn.execute(
                update(s.runner_bindings)
                .where(s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == bid)
                .values(**patch)
            )
            for command in commands:
                await self._enqueue(
                    conn,
                    tid,
                    owner,
                    command["request_id"],
                    command["payload_json"],
                    command["payload_digest"],
                    restored=command,
                )
            return {**value, **patch}

    async def _enqueue(
        self, conn, tid, owner, request_id, payload_json, payload_digest, restored=None
    ):
        try:
            payload = RunnerPayload.model_validate_json(payload_json).root
        except ValidationError as error:
            raise HTTPException(422, "Invalid finite Runner payload") from error
        if runner_payload_digest(payload_json) != payload_digest:
            raise HTTPException(422, "Control payload bytes do not match digest")
        key = command_key(owner["epoch"], request_id)
        old = (
            (
                await conn.execute(
                    select(s.runner_commands).where(
                        s.runner_commands.c.task_id == tid, s.runner_commands.c.id == key
                    )
                )
            )
            .mappings()
            .first()
        )
        if old:
            if old["payload_json"] != payload_json or old["payload_digest"] != payload_digest:
                raise HTTPException(409, "Control operation bytes conflict")
            return dict(old)
        bid = (
            payload.spec.binding.binding_id
            if payload.kind == "prepare"
            else getattr(payload, "binding_id", None)
        )
        binding = await row(conn, s.runner_bindings, tid, bid) if bid else None
        if payload.kind in {"prepare", "input"} and binding is None:
            raise HTTPException(403, "Registered binding required")
        proof = None
        if binding:
            if payload.kind in {"prepare", "input"}:
                await self._binding(conn, tid, bid, owner, live=True)
            else:
                await self._binding(conn, tid, bid, owner)
                if binding["owner_epoch"] != owner["epoch"]:
                    raise HTTPException(409, "Stop operation belongs to another owner")
        if payload.kind == "prepare":
            assert binding is not None
            if payload_json != binding["payload_json"]:
                raise HTTPException(409, "Frozen prepare specification changed")
        if payload.kind == "input":
            assert binding is not None
            duplicate = (
                (
                    await conn.execute(
                        select(s.runner_commands).where(
                            s.runner_commands.c.task_id == tid,
                            s.runner_commands.c.owner_epoch == owner["epoch"],
                            s.runner_commands.c.binding_id == bid,
                            s.runner_commands.c.input_id == payload.input_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if duplicate:
                if duplicate["payload_json"] != payload_json:
                    raise HTTPException(409, "Input identity already has different control bytes")
                return dict(duplicate)
            if restored:
                proof = restored["message_proof"]
            else:
                message = await row(conn, s.ctf_messages, tid, payload.input_id)
                business = binding["business"]
                task_lead = (
                    binding["binding"]["agent_id"] == "lead" and business["challenge_id"] is None
                )
                if (
                    message["recipient_id"] != binding["binding"]["agent_id"]
                    or message["purpose"] != business["purpose"]
                    or message["body"] != payload.content
                    or message["deferred"]
                    or message["status"] not in {"queued", "leased"}
                    or message["claim_turn_id"] not in {None, business["turn_id"]}
                    or message["claim_generation"] not in {None, business["generation"]}
                    or (
                        not task_lead
                        and message["challenge_id"] not in {None, business["challenge_id"]}
                    )
                ):
                    raise HTTPException(403, "Input does not match its original mailbox claim")
                proof = {
                    "id": message["id"],
                    "recipient_id": message["recipient_id"],
                    "purpose": message["purpose"],
                    "claim_turn_id": business["turn_id"],
                    "claim_generation": business["generation"],
                    "claim_token": message["claim_token"] or str(uuid4()),
                }
                await conn.execute(
                    update(s.ctf_messages)
                    .where(s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == payload.input_id)
                    .values(
                        status="leased",
                        lease_until=owner["expires_at"],
                        **{
                            key: proof[key]
                            for key in ("claim_turn_id", "claim_generation", "claim_token")
                        },
                    )
                )
        owner["next_seq"] += 1
        await conn.execute(
            update(s.runner_owners)
            .where(s.runner_owners.c.task_id == tid, s.runner_owners.c.epoch == owner["epoch"])
            .values(next_seq=owner["next_seq"])
        )
        value = dict(
            task_id=tid,
            id=key,
            owner_epoch=owner["epoch"],
            request_id=request_id,
            command_seq=owner["next_seq"],
            payload_json=payload_json,
            payload_digest=payload_digest,
            kind=payload.kind,
            binding_id=bid,
            input_id=getattr(payload, "input_id", None),
            message_proof=proof,
            native_proof=restored["native_proof"] if restored else None,
            status=restored["status"] if restored else "queued",
            origin_epoch=restored["origin_epoch"] if restored else owner["epoch"],
        )
        await conn.execute(insert(s.runner_commands).values(**value))
        if payload.kind in {"close", "abort", "shutdown"}:
            query = update(s.runner_bindings).where(
                s.runner_bindings.c.task_id == tid,
                s.runner_bindings.c.owner_epoch == owner["epoch"],
            )
            if bid:
                query = query.where(s.runner_bindings.c.id == bid)
            await conn.execute(query.values(status="closing", permit_expires_at=None))
            from bbx_blackboard.runner_gateway import cancel_admitted

            await cancel_admitted(conn, tid, bid)
        # Runner commands have their own cursor; Task notifications only wake readers.
        await self.repo.notify(tid, 0, connection=conn)
        return value

    async def enqueue(self, tid, raw):
        body, _, _ = decode(RunnerCommandSubmission, raw)
        try:
            cleanup = RunnerPayload.model_validate_json(body.payload_json).root.kind in {
                "abort",
                "close",
                "shutdown",
            }
        except ValidationError as error:
            raise HTTPException(422, "Invalid finite Runner payload") from error
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task(conn, tid, live=not cleanup)
            owner = await self._current_owner(conn, tid)
            if body.expected_epoch != owner["epoch"]:
                raise HTTPException(409, "Runner owner epoch changed")
            return await self._enqueue(
                conn, tid, owner, body.request_id, body.payload_json, body.payload_digest
            )

    def _lease(self, owner, binding):
        return {
            "protocol": PROTOCOL,
            "confirmed": True,
            "request_id": binding["prepare_request_id"],
            "fence": fence(owner),
            "binding_id": binding["id"],
            "binding": binding["binding"],
            "lease_expires_at": int(binding["permit_expires_at"].timestamp()),
            "lease_revision": binding["permit_revision"],
            "sdk_session_id": binding["sdk_session_id"],
            "cursor": binding["cursor"],
        }

    async def acquire(self, tid, bid, identity, raw):
        body, text, sha = decode(SessionLeaseRequest, raw)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            owner = await self._identity(conn, tid, identity, live=True)
            value = await self._binding(conn, tid, bid, owner, live=True)
            command = await row(
                conn, s.runner_commands, tid, command_key(owner["epoch"], body.request_id)
            )
            if (
                body.fence.model_dump(mode="json") != fence(owner)
                or body.binding.model_dump(mode="json") != value["binding"]
                or body.request_id != value["prepare_request_id"]
                or body.payload_digest != command["payload_digest"]
            ):
                raise HTTPException(403, "Prepare lease identity does not match registration")
            await self._prior(conn, tid, owner["epoch"], "acquire", body.request_id, sha)
            if (
                value["permit_expires_at"] is not None
                and value["permit_expires_at"] <= self.clock()
            ):
                raise HTTPException(409, "Expired session permit requires service reconciliation")
            if value["permit_expires_at"] is None:
                value = {
                    **value,
                    "permit_expires_at": min(
                        owner["expires_at"],
                        owner["hard_deadline"],
                        self.clock() + timedelta(seconds=PERMIT_SECONDS),
                    ),
                    "permit_revision": value["permit_revision"] + 1,
                }
                await conn.execute(
                    update(s.runner_bindings)
                    .where(s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == bid)
                    .values(
                        permit_expires_at=value["permit_expires_at"],
                        permit_revision=value["permit_revision"],
                    )
                )
                await self.repo.notify(tid, 0, connection=conn)
            receipt = self._lease(owner, value)
            await self._receipt(
                conn, tid, owner["epoch"], "acquire", body.request_id, text, sha, receipt, bid
            )
            return receipt

    async def poll(self, tid, identity, after_seq=0, wait_seconds: float = 15, *, dispatcher=None):
        end = asyncio.get_running_loop().time() + min(15, max(0, wait_seconds))
        subscription = (
            dispatcher.subscribe(tid, wait_ready=False)
            if dispatcher is not None
            else nullcontext(asyncio.Queue(maxsize=1))
        )
        async with subscription as wake:
            return await self._poll(tid, identity, after_seq, end, wake)

    async def _poll(self, tid, identity, after_seq, end, wake):
        while True:
            async with self.repo.engine.begin() as conn:
                await self.repo.lock(conn, tid)
                task = await self._task(conn, tid)
                closing = (
                    task["deleting"]
                    or task["ctf_control"].get("native_stop_requested")
                    or task["status"] in TERMINAL
                    or task["ctf_control"]["phase"] in {"closing", "closed"}
                )
                if not closing:
                    task = await self._task(conn, tid, live=True, allow_held=True)
                held = closing or bool(task["ctf_control"].get("native_budget_hold"))
                owner = await self._identity(conn, tid, identity)
                self._owner_live(owner)
                recheck_at = min(owner["expires_at"], owner["hard_deadline"])
                bindings = (
                    (
                        await conn.execute(
                            select(
                                s.runner_bindings.c.id,
                                s.runner_bindings.c.binding,
                                s.runner_bindings.c.business,
                                s.runner_bindings.c.owner_epoch,
                                s.runner_bindings.c.status,
                                s.runner_bindings.c.prepare_request_id,
                                s.runner_bindings.c.sdk_session_id,
                                s.runner_bindings.c.cursor,
                                s.runner_bindings.c.permit_expires_at,
                                s.runner_bindings.c.permit_revision,
                            ).where(
                                s.runner_bindings.c.task_id == tid,
                                s.runner_bindings.c.owner_epoch == owner["epoch"],
                                s.runner_bindings.c.status.in_(["registered", "prepared"]),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                active = {}
                renewals = []
                renewed = False
                for found in bindings:
                    if found["status"] not in {"registered", "prepared"}:
                        continue
                    value = dict(found)
                    try:
                        if held:
                            raise HTTPException(409, "Native budget admission is held")
                        await self._mapping(conn, tid, value)
                        if (
                            value["permit_expires_at"] is not None
                            and value["permit_expires_at"] <= self.clock()
                        ):
                            raise HTTPException(409, "Session permit expired")
                    except HTTPException as error:
                        if error.status_code not in {403, 404, 409}:
                            raise
                        payload = runner_json_text({"kind": "close", "binding_id": value["id"]})
                        await self._enqueue(
                            conn,
                            tid,
                            owner,
                            str(
                                uuid5(
                                    NAMESPACE_URL,
                                    f"runner-close:{tid}:{owner['epoch']}:{value['id']}",
                                )
                            ),
                            payload,
                            runner_payload_digest(payload),
                        )
                        continue
                    active[value["id"]] = value
                    expiry = value["permit_expires_at"]
                    if expiry is not None and expiry > self.clock():
                        extended = min(
                            owner["expires_at"],
                            owner["hard_deadline"],
                            self.clock() + timedelta(seconds=PERMIT_SECONDS),
                        )
                        if (
                            expiry <= self.clock() + timedelta(seconds=PERMIT_SECONDS / 2)
                            and extended > expiry
                        ):
                            renewed = True
                            expiry = extended
                            value = {
                                **value,
                                "permit_expires_at": extended,
                                "permit_revision": value["permit_revision"] + 1,
                            }
                            await conn.execute(
                                update(s.runner_bindings)
                                .where(
                                    s.runner_bindings.c.task_id == tid,
                                    s.runner_bindings.c.id == value["id"],
                                )
                                .values(
                                    permit_expires_at=extended,
                                    permit_revision=value["permit_revision"],
                                )
                            )
                        renewals.append(self._lease(owner, value))
                        renew_at = expiry - timedelta(seconds=PERMIT_SECONDS / 2)
                        if renew_at <= self.clock():
                            renew_at = expiry
                        recheck_at = min(recheck_at, renew_at)
                commands = (
                    (
                        await conn.execute(
                            select(
                                s.runner_commands.c.request_id,
                                s.runner_commands.c.command_seq,
                                s.runner_commands.c.payload_json,
                                s.runner_commands.c.payload_digest,
                                s.runner_commands.c.kind,
                                s.runner_commands.c.binding_id,
                                s.runner_commands.c.status,
                            )
                            .outerjoin(
                                s.runner_bindings,
                                and_(
                                    s.runner_bindings.c.task_id == s.runner_commands.c.task_id,
                                    s.runner_bindings.c.id == s.runner_commands.c.binding_id,
                                ),
                            )
                            .where(
                                s.runner_commands.c.task_id == tid,
                                s.runner_commands.c.owner_epoch == owner["epoch"],
                                s.runner_commands.c.command_seq > after_seq,
                                or_(
                                    literal(not held),
                                    s.runner_commands.c.kind.in_(["abort", "close", "shutdown"]),
                                ),
                                or_(
                                    and_(
                                        s.runner_commands.c.kind == "prepare",
                                        s.runner_bindings.c.status.in_(["registered", "prepared"]),
                                        s.runner_bindings.c.owner_epoch == owner["epoch"],
                                    ),
                                    and_(
                                        s.runner_commands.c.kind == "input",
                                        s.runner_commands.c.status.in_(["queued", "accepted"]),
                                        s.runner_bindings.c.status.in_(["registered", "prepared"]),
                                        s.runner_bindings.c.owner_epoch == owner["epoch"],
                                    ),
                                    and_(
                                        s.runner_commands.c.kind.in_(
                                            ["abort", "close", "shutdown"]
                                        ),
                                        s.runner_commands.c.status == "queued",
                                    ),
                                ),
                            )
                            .order_by(s.runner_commands.c.command_seq)
                            .limit(100)
                        )
                    )
                    .mappings()
                    .all()
                )
                selected = []
                for command in commands:
                    if command["kind"] == "prepare":
                        include = command["binding_id"] in active
                    elif command["kind"] == "input":
                        include = command["binding_id"] in active and command["status"] in {
                            "queued",
                            "accepted",
                        }
                    else:
                        include = command["status"] == "queued"
                    if include:
                        selected.append(
                            RunnerCommand(
                                protocol=PROTOCOL,
                                request_id=command["request_id"],
                                command_seq=command["command_seq"],
                                fence=RunnerFence.model_validate(fence(owner)),
                                payload_json=command["payload_json"],
                                payload_digest=command["payload_digest"],
                            ).model_dump(mode="json")
                        )
                    if len(selected) == 100:
                        break
            if selected or renewed or asyncio.get_running_loop().time() >= end:
                return {"protocol": PROTOCOL, "commands": selected, "lease_renewals": renewals}
            delay = min(
                POLL_RECHECK_SECONDS,
                max(0, end - asyncio.get_running_loop().time()),
                max(0, (recheck_at - self.clock()).total_seconds()),
            )
            try:
                await asyncio.wait_for(wake.get(), delay)
            except TimeoutError:
                pass

    async def _evidence_context(self, conn, tid, bid, identity):
        await self._task(conn, tid)
        owner = await self._identity(conn, tid, identity)
        binding = await self._binding(conn, tid, bid, owner)
        live = True
        try:
            await self._task(conn, tid, live=True)
            self._owner_live(owner)
            await self._binding(conn, tid, bid, owner, live=True, permit=True)
        except HTTPException as error:
            if error.status_code not in {403, 404, 409}:
                raise
            live = False
        return owner, binding, live

    async def _confirmed_cursor(self, conn, tid, binding, cursor):
        if cursor is None:
            return False
        if binding["cursor"] == cursor:
            return True
        return (
            await conn.execute(
                select(s.runner_receipts.c.id)
                .where(
                    s.runner_receipts.c.task_id == tid,
                    s.runner_receipts.c.binding_id == binding["id"],
                    s.runner_receipts.c.kind == "journal",
                    s.runner_receipts.c.late.is_(False),
                    s.runner_receipts.c.response["cursor"] == cursor,
                    s.runner_receipts.c.response["sdk_session_id"].astext
                    == binding["sdk_session_id"],
                )
                .limit(1)
            )
        ).first() is not None

    async def confirm_journal(self, tid, bid, identity, raw):
        body, text, sha = decode(JournalConfirmation, raw)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            owner, value, live = await self._evidence_context(conn, tid, bid, identity)
            if (
                body.fence.model_dump(mode="json") != fence(owner)
                or body.binding.model_dump(mode="json") != value["binding"]
            ):
                raise HTTPException(403, "Journal identity does not match registration")
            previous = await self._prior(conn, tid, owner["epoch"], "journal", body.request_id, sha)
            if previous:
                return previous["response"]
            acquire = (
                await conn.execute(
                    select(s.runner_receipts.c.id)
                    .where(
                        s.runner_receipts.c.task_id == tid,
                        s.runner_receipts.c.owner_epoch == owner["epoch"],
                        s.runner_receipts.c.kind == "acquire",
                        s.runner_receipts.c.binding_id == bid,
                    )
                    .limit(1)
                )
            ).first()
            if acquire is None:
                raise HTTPException(403, "Journal has no registered acquire")
            if (
                value["sdk_session_id"] is not None
                and value["sdk_session_id"] != body.sdk_session_id
            ):
                raise HTTPException(403, "SDK session identity changed")
            cursor = body.cursor.model_dump(mode="json")
            receipt = {
                "protocol": PROTOCOL,
                "confirmed": True,
                "request_id": body.request_id,
                "binding_id": bid,
                "sdk_session_id": body.sdk_session_id,
                "cursor": cursor,
            }
            if not live:
                known = (
                    await conn.execute(
                        select(s.runner_receipts.c.body_text).where(
                            s.runner_receipts.c.task_id == tid,
                            s.runner_receipts.c.owner_epoch == owner["epoch"],
                            s.runner_receipts.c.kind == "command",
                            s.runner_receipts.c.binding_id == bid,
                        )
                    )
                ).scalars()
                pending = [json.loads(raw).get("pending_confirmation") for raw in known]
                if body.model_dump(mode="json") not in pending:
                    raise HTTPException(
                        409, "Late journal is outside the registered control evidence"
                    )
                return await self._receipt(
                    conn,
                    tid,
                    owner["epoch"],
                    "journal",
                    body.request_id,
                    text,
                    sha,
                    receipt,
                    bid,
                    late=True,
                )
            empty = {
                "offset": 0,
                "seq": 0,
                "hash": "0" * 64,
                "leaf_id": None,
                "anchor_offset": 0,
                "anchor_size": 0,
                "anchor_hash": hashlib.sha256(b"").hexdigest(),
            }
            expected = value["cursor"] or empty
            if body.previous.model_dump(mode="json") != expected:
                raise HTTPException(409, "Journal compare-and-swap conflict")
            if (
                cursor["offset"] <= expected["offset"]
                or cursor["seq"] <= expected["seq"]
                or cursor["anchor_offset"] < expected["offset"]
                or cursor["anchor_size"] < 1
                or cursor["anchor_offset"] + cursor["anchor_size"] != cursor["offset"]
            ):
                raise HTTPException(409, "Journal cursor does not advance a complete record")
            seen = set()
            applications = []
            for applied in body.applied_inputs:
                if applied.input_id in seen:
                    raise HTTPException(409, "Duplicate input proof")
                seen.add(applied.input_id)
                command = (
                    (
                        await conn.execute(
                            select(s.runner_commands).where(
                                s.runner_commands.c.task_id == tid,
                                s.runner_commands.c.owner_epoch == owner["epoch"],
                                s.runner_commands.c.binding_id == bid,
                                s.runner_commands.c.input_id == applied.input_id,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if command is None or command["status"] == "rejected":
                    raise HTTPException(403, "Input has no registered control operation")
                proof = applied.model_dump(mode="json")
                if not expected["seq"] < applied.seq <= cursor["seq"] or (
                    command["native_proof"] is not None and command["native_proof"] != proof
                ):
                    raise HTTPException(409, "Native input identity changed")
                message = await self._check_message(conn, tid, command)
                applications.append((dict(command), proof, message))
            await conn.execute(
                update(s.runner_bindings)
                .where(s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == bid)
                .values(sdk_session_id=body.sdk_session_id, cursor=cursor)
            )
            for command, proof, _message in applications:
                await conn.execute(
                    update(s.runner_commands)
                    .where(
                        s.runner_commands.c.task_id == tid, s.runner_commands.c.id == command["id"]
                    )
                    .values(status="applied", native_proof=proof)
                )
                await conn.execute(
                    update(s.ctf_messages)
                    .where(
                        s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == command["input_id"]
                    )
                    .values(
                        status="delivered",
                        delivered_turn_id=command["message_proof"]["claim_turn_id"],
                        session_revision=None,
                    )
                )
            if applications:
                instructions = [
                    command["input_id"]
                    for command, _, message in applications
                    if message["kind"] == "instruction"
                ]
                if instructions:
                    turn = await row(conn, s.ctf_turns, tid, value["business"]["turn_id"])
                    assigned = list(dict.fromkeys([*turn["assignment_message_ids"], *instructions]))
                    await conn.execute(
                        update(s.ctf_turns)
                        .where(s.ctf_turns.c.task_id == tid, s.ctf_turns.c.id == turn["id"])
                        .values(assignment_message_ids=assigned)
                    )
                events = [
                    {
                        "type": "ctf.message.delivered",
                        "actor": value["binding"]["agent_id"],
                        "object_id": command["input_id"],
                        "addressed_to": sorted({message["sender_id"], message["recipient_id"]}),
                        "payload": {
                            "id": command["input_id"],
                            "binding_id": bid,
                            "native_entry_id": proof["entry_id"],
                            "native_seq": proof["seq"],
                        },
                    }
                    for command, proof, message in applications
                ]
                written = await self.repo.append(conn, tid, events)
                await self.repo.notify(tid, written[-1]["version"], connection=conn)
            return await self._receipt(
                conn, tid, owner["epoch"], "journal", body.request_id, text, sha, receipt, bid
            )

    async def _check_message(self, conn, tid, command):
        proof = command["message_proof"]
        message = await row(conn, s.ctf_messages, tid, command["input_id"])
        if (
            proof is None
            or any(
                message[key] != proof[key]
                for key in (
                    "id",
                    "recipient_id",
                    "purpose",
                    "claim_turn_id",
                    "claim_generation",
                    "claim_token",
                )
            )
            or message["status"] not in {"leased", "delivered"}
        ):
            raise HTTPException(403, "Original mailbox claim changed")
        return message

    async def acknowledge(self, tid, request_id, identity, raw):
        body, text, sha = decode(CommandReceipt, raw)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task(conn, tid)
            owner = await self._identity(conn, tid, identity)
            command = await row(
                conn, s.runner_commands, tid, command_key(owner["epoch"], request_id)
            )
            if (
                body.request_id != request_id
                or body.payload_digest != command["payload_digest"]
                or body.fence.model_dump(mode="json") != fence(owner)
                or (
                    body.binding_id != command["binding_id"]
                    and not (body.status == "rejected" and body.binding_id is None)
                )
            ):
                raise HTTPException(403, "Command receipt identity changed")
            allowed = {
                "prepare": {"prepared", "rejected"},
                "input": {"accepted", "applied", "rejected"},
                "abort": {"aborted", "rejected"},
                "close": {"closed", "rejected"},
                "shutdown": {"closed", "rejected"},
            }
            if body.status not in allowed[command["kind"]]:
                raise HTTPException(409, "Command receipt status is incompatible")
            live = False
            value = None
            if command["binding_id"]:
                owner, value, live = await self._evidence_context(
                    conn, tid, command["binding_id"], identity
                )
                if (
                    value["sdk_session_id"] is not None
                    and body.sdk_session_id != value["sdk_session_id"]
                    and not (body.status == "rejected" and body.sdk_session_id is None)
                ):
                    raise HTTPException(403, "Receipt SDK session changed")
                cursor = body.cursor.model_dump(mode="json") if body.cursor else None
                if body.journal_confirmed and not await self._confirmed_cursor(
                    conn, tid, value, cursor
                ):
                    raise HTTPException(409, "Journal confirmation is not registered")
                if body.status == "prepared":
                    if value["sdk_session_id"] is None or not await self._confirmed_cursor(
                        conn, tid, value, cursor
                    ):
                        raise HTTPException(409, "Prepared receipt has no confirmed SDK journal")
                if body.status == "applied":
                    proof = command["native_proof"]
                    if (
                        body.input_id != command["input_id"]
                        or proof is None
                        or cursor is None
                        or cursor["seq"] < proof["seq"]
                        or not await self._confirmed_cursor(conn, tid, value, cursor)
                    ):
                        raise HTTPException(409, "Applied receipt has no confirmed native input")
                if body.pending_confirmation:
                    pending = body.pending_confirmation
                    if (
                        pending.fence.model_dump(mode="json") != fence(owner)
                        or pending.binding.model_dump(mode="json") != value["binding"]
                        or pending.sdk_session_id != body.sdk_session_id
                    ):
                        raise HTTPException(403, "Pending confirmation identity changed")
                    for applied in pending.applied_inputs:
                        known = (
                            await conn.execute(
                                select(s.runner_commands.c.id).where(
                                    s.runner_commands.c.task_id == tid,
                                    s.runner_commands.c.owner_epoch == owner["epoch"],
                                    s.runner_commands.c.binding_id == value["id"],
                                    s.runner_commands.c.input_id == applied.input_id,
                                )
                            )
                        ).first()
                        if known is None:
                            raise HTTPException(403, "Pending input has no registered operation")
            else:
                try:
                    await self._identity(conn, tid, identity, live=True)
                    live = True
                except HTTPException as error:
                    if error.status_code not in {403, 409}:
                        raise
            # Local close evidence is retained even after its permit was revoked.
            closing = body.status in {"closed", "aborted"}
            if live or closing:
                status = command["status"]
                if status not in {"applied", "prepared", "closed", "aborted", "rejected"}:
                    await conn.execute(
                        update(s.runner_commands)
                        .where(
                            s.runner_commands.c.task_id == tid,
                            s.runner_commands.c.id == command["id"],
                        )
                        .values(status=body.status)
                    )
                if value and body.status in {"prepared", "rejected", "closed", "aborted"}:
                    if body.status == "prepared" and live:
                        patch = {"status": "prepared"}
                    elif closing and value["owner_epoch"] == owner["epoch"]:
                        patch = {"status": "closed", "permit_expires_at": None}
                    elif body.status == "rejected" and command["kind"] == "prepare" and live:
                        patch = {"status": "revoked", "permit_expires_at": None}
                    else:
                        patch = {}
                    if patch:
                        await conn.execute(
                            update(s.runner_bindings)
                            .where(
                                s.runner_bindings.c.task_id == tid,
                                s.runner_bindings.c.id == value["id"],
                            )
                            .values(**patch)
                        )
            ack = {
                "protocol": PROTOCOL,
                "confirmed": True,
                "request_id": request_id,
                "status": body.status,
                "receipt_digest": sha,
            }
            wake = False
            if (
                value
                and value["business"].get("native_assignment_id")
                and command["kind"] == "close"
                and body.status == "closed"
                and body.sdk_idle is True
                and not body.pending_confirmation
                and (body.journal_confirmed or value["permit_revision"] == 0)
            ):
                key = f"{owner['epoch']}:command:{request_id}:{sha}"
                wake = (
                    await conn.execute(
                        select(s.runner_receipts.c.id).where(
                            s.runner_receipts.c.task_id == tid,
                            s.runner_receipts.c.id == key,
                        )
                    )
                ).first() is None
            result = await self._receipt(
                conn,
                tid,
                owner["epoch"],
                "command",
                request_id,
                text,
                sha,
                ack,
                command["binding_id"],
                late=not live,
            )
            if wake:
                await self.native_worker_cleanup_changed(conn, tid, command["binding_id"])
            return result

    async def journal_state(self, tid, bid, identity):
        async with self.repo.engine.connect() as conn:
            owner = await self._identity(conn, tid, identity, live=True)
            value = await self._binding(conn, tid, bid, owner, live=True, permit=True)
            if value["sdk_session_id"] is None:
                raise HTTPException(404, "No journal has been confirmed")
            return {
                "protocol": PROTOCOL,
                "binding_id": bid,
                "sdk_session_id": value["sdk_session_id"],
                "cursor": value["cursor"],
            }

    async def publish_prompt(self, tid, bid, raw):
        body, _, _ = decode(RunnerPromptPublication, raw)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task(conn, tid, live=True)
            owner = await self._current_owner(conn, tid)
            if body.expected_epoch != owner["epoch"]:
                raise HTTPException(409, "Runner owner epoch changed")
            value = await self._binding(conn, tid, bid, owner, live=True)
            if value["prompt_revision"] != body.expected_revision:
                raise HTTPException(409, "Published prompt revision changed")
            await conn.execute(
                update(s.runner_bindings)
                .where(s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == bid)
                .values(prompt_revision=body.expected_revision + 1, instructions=body.instructions)
            )
            return {"revision": body.expected_revision + 1}

    async def prompt(self, tid, bid, identity, known_revision):
        async with self.repo.engine.connect() as conn:
            owner = await self._identity(conn, tid, identity, live=True)
            value = await self._binding(conn, tid, bid, owner, live=True, permit=True)
            if known_revision > value["prompt_revision"]:
                raise HTTPException(409, "Published prompt revision moved backwards")
            if known_revision == value["prompt_revision"]:
                return None
            return {
                "protocol": PROTOCOL,
                "fence": fence(owner),
                "binding_id": bid,
                "prompt_revision": value["prompt_revision"],
                "instructions": value["instructions"],
            }
