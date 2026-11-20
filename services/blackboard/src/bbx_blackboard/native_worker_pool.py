"""Persistent Worker sessions with one independently versioned challenge Assignment."""

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from bbx_contracts.runner import runner_json_text, runner_payload_digest
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import insert, select, update

from bbx_blackboard.domain.challenge_collaboration import (
    available_work,
    claim_challenge,
    require_claimable,
)
from bbx_blackboard.domain.rules import RuleViolation
from bbx_blackboard.runner_control import row
from bbx_blackboard.store import schema as s


def worker_id(*parts):
    return str(uuid5(NAMESPACE_URL, "ctf-peer-worker:" + ":".join(map(str, parts))))


def worker_message(content, fallback):
    encoded = runner_json_text(content)
    return encoded if len(encoded) <= 20000 else runner_json_text(fallback)


def work_context(task, aid):
    session = task["ctf_control"].get("native_worker_sessions", {}).get(aid)
    if session is None:
        raise HTTPException(403, "Worker session is unavailable")
    active = task["ctf_control"].get("native_worker_active", {}).get(aid)
    return {
        "revision": session["work_revision"],
        "assignment_ref": active["assignment_ref"] if active else None,
        "challenge_id": active["work_id"] if active else None,
    }


class NativeWorkerPool:
    def __init__(self, ctf, control):
        self.ctf, self.control, self.repo = ctf, control, ctf.repo

    async def record_board_cursor(self, conn, tid, binding, entries):
        if not binding["business"].get("worker_pool") or not entries:
            return
        versions = [
            entry["data"]["version"]
            for entry in map(json.loads, entries.splitlines())
            if entry.get("type") == "custom"
            and entry.get("customType") == "bbx.board-context"
            and type(entry.get("data", {}).get("version")) is int
        ]
        if versions:
            current = await row(conn, s.runner_bindings, tid, binding["id"])
            seen = current["business"].get("worker_board_seen", 0)
            await conn.execute(
                update(s.runner_bindings)
                .where(
                    s.runner_bindings.c.task_id == tid,
                    s.runner_bindings.c.id == binding["id"],
                )
                .values(business={**current["business"], "worker_board_seen": max(seen, *versions)})
            )

    async def record_idle(self, conn, tid, binding, body):
        proof = body.worker_idle
        if proof is None:
            return
        if not binding["business"].get("worker_pool") or not body.entries:
            raise HTTPException(403, "Worker idle observation requires its original journal")
        entries = [json.loads(line) for line in body.entries.splitlines()]
        index = proof.seq - body.previous.seq - 1
        if not 0 <= index < len(entries):
            raise HTTPException(409, "Worker idle observation is outside the journal delta")
        entry = entries[index]
        context = proof.context.model_dump(mode="json")
        if (
            entry.get("id") != proof.entry_id
            or entry.get("customType") != "bbx.worker-idle"
            or entry.get("data") != {"context": context}
        ):
            raise HTTPException(409, "Worker idle journal evidence changed")
        task = await row(conn, s.tasks, tid)
        aid = binding["binding"]["agent_id"]
        if context != work_context(task, aid):
            return
        if await self._model_after_idle(conn, tid, binding["id"], proof.seq):
            return
        commands = (
            await conn.execute(
                select(s.runner_commands).where(
                    s.runner_commands.c.task_id == tid,
                    s.runner_commands.c.binding_id == binding["id"],
                    s.runner_commands.c.kind == "input",
                )
            )
        ).mappings()
        if any(
            command["status"] in {"queued", "accepted"}
            or ((command.get("native_proof") or {}).get("seq", 0) > proof.seq)
            for command in commands
        ):
            return
        if await conn.scalar(
            select(s.pi_model_calls.c.call_id)
            .where(
                s.pi_model_calls.c.task_id == tid,
                s.pi_model_calls.c.agent_id == aid,
                s.pi_model_calls.c.state.in_(["prepared", "dispatched", "unknown"]),
            )
            .limit(1)
        ):
            return
        current = await row(conn, s.runner_bindings, tid, binding["id"])
        await conn.execute(
            update(s.runner_bindings)
            .where(
                s.runner_bindings.c.task_id == tid,
                s.runner_bindings.c.id == binding["id"],
            )
            .values(
                business={
                    **current["business"],
                    "worker_idle": {"context": context, "seq": proof.seq},
                    "last_worker_idle_seq": proof.seq,
                }
            )
        )
        await self.ctf._emit(
            conn,
            tid,
            "native_worker.idle",
            "system",
            {
                "agent_id": aid,
                "binding_id": binding["id"],
                "context": context,
                "seq": proof.seq,
            },
        )

    @staticmethod
    async def _model_after_idle(conn, tid, bid, sequence):
        return (
            await conn.scalar(
                select(s.pi_model_calls.c.call_id)
                .where(
                    s.pi_model_calls.c.task_id == tid,
                    s.pi_model_calls.c.intent["proof"]["binding"]["binding_id"].astext == bid,
                    s.pi_model_calls.c.intent["proof"]["journal"]["seq"].as_integer() >= sequence,
                )
                .limit(1)
            )
            is not None
        )

    async def _challenges(self, conn, tid):
        return list(
            (
                await conn.scalars(
                    select(s.ctf_challenges.c.data).where(s.ctf_challenges.c.task_id == tid)
                )
            ).all()
        )

    async def _new_session(self, conn, tid, member, owner, previous=None):
        aid = member["id"]
        generation = member["generation"] + 1
        registration = worker_id(tid, aid, owner["epoch"], generation, "session")
        session = {
            "binding_id": str(uuid5(NAMESPACE_URL, f"runner-binding:{tid}:{registration}")),
            "turn_id": worker_id(registration, "turn"),
            "generation": generation,
            "epoch": owner["epoch"],
            "work_revision": (previous or {}).get("work_revision", 0),
            "registration_id": registration,
        }
        turn = {
            "id": session["turn_id"],
            "agent_id": aid,
            "generation": generation,
            "runtime_instance": "native-pool:" + registration,
            "status": "running",
            "purpose": "execution",
            "assignment_message_ids": [],
            "explicit_reply_ids": [],
            "checkpoint_revision": 0,
            "usage": {},
            "usage_requests": [],
            "started_at": datetime.now(UTC),
        }
        await conn.execute(insert(s.ctf_turns).values(task_id=tid, **turn))
        await self.ctf._emit(conn, tid, "turn.started", "system", turn)
        return session

    async def _register_session(self, conn, tid, task, aid, session, spec, owner):
        await self.control._register_binding(
            conn,
            tid,
            runner_json_text(
                {
                    "request_id": session["registration_id"],
                    "expected_epoch": owner["epoch"],
                    "agent_id": aid,
                    "challenge_id": None,
                    "model": spec["model"],
                    "model_credential_ref": "native-model-" + session["binding_id"],
                    "mcp_credential_ref": "native-mcp-" + session["binding_id"],
                    "allowed_tools": spec["allowed_tools"],
                    "instructions": spec["instructions"][aid],
                    "profile_revision": task["agent_profile_version"],
                    "prompt_revision": 1,
                }
            ).encode(),
            pool_worker=True,
        )

    async def claim(self, conn, tid, task, member, challenge, *, first=False, restore=None):
        choices = available_work(task, await self._challenges(conn, tid))
        if restore is None:
            try:
                require_claimable(task, challenge, member, choices)
            except RuleViolation as error:
                raise HTTPException(
                    409, {"code": error.code, "message": str(error), **choices}
                ) from error
        elif (
            restore["assignment"]["agent_id"] != member["id"]
            or restore["assignment"]["work_id"] != challenge["id"]
        ):
            raise HTTPException(409, "Worker recovery cannot choose another challenge")
        if restore is None and bool(member["has_worked"]) == first:
            raise HTTPException(409, "Worker first-assignment state changed")
        record = (
            (
                await conn.execute(
                    select(s.task_runner_resources).where(s.task_runner_resources.c.task_id == tid)
                )
            )
            .mappings()
            .one_or_none()
        )
        if record is None:
            raise HTTPException(409, "Worker execution resources are unavailable")
        spec = record["data"].get("worker_spec")
        if not spec or member["id"] not in spec["instructions"]:
            raise HTTPException(409, "Worker execution profile is unavailable")
        owner = await self.control._current_owner(conn, tid)
        aid, cid = member["id"], challenge["id"]
        state = dict(task["ctf_control"])
        sessions = dict(state.get("native_worker_sessions", {}))
        session = sessions.get(aid)
        created = session is None
        if created:
            session = await self._new_session(
                conn, tid, member, owner, restore.get("session") if restore else None
            )
        if session["epoch"] != owner["epoch"]:
            raise HTTPException(409, "Worker session requires original epoch recovery")
        session = {**session, "work_revision": session["work_revision"] + 1}
        sessions[aid] = session
        reference = worker_id(tid, aid, owner["epoch"], session["work_revision"], cid)
        assignment = {
            "assignment_ref": reference,
            "work_id": cid,
            "agent_id": aid,
            "generation": session["generation"],
            "epoch": session["epoch"],
            "turn_id": session["turn_id"],
            "binding_id": session["binding_id"],
            "work_revision": session["work_revision"],
            "scope_revision": challenge.get("scope_revision", 1),
            "role": restore["assignment"]["role"]
            if restore
            else "primary"
            if challenge.get("owner_id") is None
            else "assistant",
            "input_id": worker_id(reference, "input"),
        }
        if restore:
            assignment["previous_assignment_id"] = restore["assignment"]["assignment_ref"]
        claimed = (
            {**challenge, "revision": challenge["revision"] + 1}
            if restore
            else claim_challenge(task, challenge, aid, datetime.now(UTC))
        )
        ready = dict(state.get("native_worker_ready", {}))
        ready.pop(cid, None)
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **state,
                    "native_worker_sessions": sessions,
                    "native_worker_ready": ready,
                    "native_worker_active": {
                        **state.get("native_worker_active", {}),
                        aid: assignment,
                    },
                }
            )
        )
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid)
            .values(
                generation=session["generation"],
                current_turn_id=session["turn_id"],
                run_state="running",
                has_worked=True,
            )
        )
        await self.ctf._board_event(
            conn, tid, "system", "ctf.challenge.updated", reference, reference, claimed
        )
        if created:
            await self._register_session(conn, tid, task, aid, session, spec, owner)
        if first or restore and claimed["work_status"] == "in_progress":
            instruction = (
                "Resume your existing challenge with the original team. "
                if restore
                else "The platform assigned your first work. "
            ) + "Read the challenge board and solve it with your peers."
            content = {
                "challenge": claimed,
                "assignment": assignment,
                **(
                    {
                        "previous_session": restore["session"],
                        "known_results": restore.get("known_results", []),
                    }
                    if restore
                    else {}
                ),
                "instruction": instruction,
            }
            await self.ctf._message(
                conn,
                tid,
                "system",
                aid,
                worker_message(
                    content,
                    {
                        "assignment": assignment,
                        "challenge_id": cid,
                        "revision": claimed["revision"],
                        "title": claimed["title"],
                        "workspace": f"/workspace/shared/challenges/{cid}/workers/{aid}",
                        "instruction": (
                            instruction + " This is a summary. Use board_challenges for the full "
                            "challenge and board_read for facts/intents. Confirmed old calls "
                            "stay in the execution ledger and must not be repeated."
                        ),
                    },
                ),
                assignment["input_id"],
                "instruction",
                challenge_id=cid,
            )
        await self.ctf._emit(conn, tid, "native_worker.assigned", "system", assignment)
        board = await self.repo.load(conn, tid, include_tool_calls=False, challenge_id=cid)
        return jsonable_encoder(
            {
                "assignment": assignment,
                "challenge": claimed,
                "board": {
                    "facts": board.facts,
                    "intents": board.intents,
                    "version": board.task["version"],
                },
                "workspace": f"/workspace/shared/challenges/{cid}/workers/{aid}",
            }
        )

    async def restore(self, conn, tid, task, record, owner):
        pending = task["ctf_control"].get("native_worker_continuations", {})
        for aid, original in pending.items():
            task = await row(conn, s.tasks, tid)
            member = await row(conn, s.ctf_members, tid, aid)
            if original.get("assignment"):
                challenge = await self.ctf._challenge(conn, tid, original["assignment"]["work_id"])
                restored = await self.claim(conn, tid, task, member, challenge, restore=original)
                await self._restore_intents(
                    conn, tid, aid, restored["assignment"], original["assignment"]
                )
            else:
                session = await self._new_session(conn, tid, member, owner, original["session"])
                session["work_revision"] += 1
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(
                        ctf_control={
                            **task["ctf_control"],
                            "native_worker_sessions": {
                                **task["ctf_control"].get("native_worker_sessions", {}),
                                aid: session,
                            },
                        }
                    )
                )
                await conn.execute(
                    update(s.ctf_members)
                    .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid)
                    .values(
                        generation=session["generation"],
                        current_turn_id=session["turn_id"],
                        run_state="idle",
                    )
                )
                await self._register_session(
                    conn, tid, task, aid, session, record["data"]["worker_spec"], owner
                )
            current = (await row(conn, s.tasks, tid))["ctf_control"]
            remaining = dict(current["native_worker_continuations"])
            remaining.pop(aid)
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={
                        **current,
                        "native_worker_continuations": remaining,
                    }
                )
            )

    async def _restore_intents(self, conn, tid, aid, assignment, previous):
        from bbx_contracts.models import KnowledgeAssignment

        original = await self.ctf._challenge(conn, tid, assignment["work_id"])
        claim = KnowledgeAssignment(
            agent_id=aid,
            generation=assignment["generation"],
            epoch=assignment["epoch"],
            turn_id=assignment["turn_id"],
            owner_id=original["owner_id"],
            scope_revision=assignment["scope_revision"],
            assignment_id=assignment["assignment_ref"],
        ).model_dump(mode="json")
        intents = (
            (
                await conn.execute(
                    select(s.intents).where(
                        s.intents.c.task_id == tid,
                        s.intents.c.challenge_id == assignment["work_id"],
                        s.intents.c.status == "claimed",
                        s.intents.c.holder == aid,
                        s.intents.c.claim_assignment_ref["assignment_id"].astext
                        == previous["assignment_ref"],
                    )
                )
            )
            .mappings()
            .all()
        )
        if intents:
            written = await self.repo.append(
                conn,
                tid,
                [
                    {
                        "type": "intent.claimed",
                        "actor": "system",
                        "object_id": item["id"],
                        "addressed_to": [aid],
                        "payload": {
                            "intent_id": item["id"],
                            "holder": aid,
                            "challenge_id": assignment["work_id"],
                            "claim_assignment_ref": claim,
                            "recovered_from": previous["assignment_ref"],
                        },
                    }
                    for item in intents
                ],
            )
            await self.repo.notify(tid, written[-1]["version"], connection=conn)

    async def _enqueue(self, conn, tid, owner, binding, message, context):
        payload = runner_json_text(
            {
                "kind": "input",
                "binding_id": binding["id"],
                "input_id": message["id"],
                "content": message["body"],
                "mode": "steer",
                "worker_context": context,
            }
        )
        await self.control._enqueue(
            conn,
            tid,
            owner,
            worker_id(message["id"], "deliver"),
            payload,
            runner_payload_digest(payload),
        )

    async def _deliver(self, conn, tid, task, owner):
        for aid, session in task["ctf_control"].get("native_worker_sessions", {}).items():
            binding = await row(conn, s.runner_bindings, tid, session["binding_id"])
            if binding["status"] != "prepared" or not binding["cursor"]:
                continue
            context = work_context(task, aid)
            messages = (
                await conn.execute(
                    select(s.ctf_messages)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.recipient_id == aid,
                        s.ctf_messages.c.status == "queued",
                        s.ctf_messages.c.deferred.is_(False),
                        s.ctf_messages.c.purpose == "execution",
                    )
                    .order_by(s.ctf_messages.c.recipient_sequence)
                )
            ).mappings()
            for message in messages:
                if message["challenge_id"] not in {None, context["challenge_id"]}:
                    continue
                await self._enqueue(conn, tid, owner, binding, message, context)

    async def _notify_available(self, conn, tid, task, idle):
        choices = available_work(task, await self._challenges(conn, tid))
        if not (choices["new_challenges"] or choices["assistance"]):
            return
        compact = {
            key: [{k: v for k, v in item.items() if k != "description"} for item in values]
            for key, values in choices.items()
        }
        signature = runner_payload_digest(runner_json_text(compact))
        state = dict(task["ctf_control"])
        sessions = dict(state.get("native_worker_sessions", {}))
        for member in idle:
            aid = member["id"]
            session = sessions.get(aid)
            if not member["has_worked"] or not session:
                continue
            binding = await row(conn, s.runner_bindings, tid, session["binding_id"])
            if not binding["business"].get("worker_idle"):
                continue
            settled = binding["business"].get("last_worker_idle_seq", 0)
            notice_key = f"{session['work_revision']}:{settled}:{signature}"
            if session.get("available_notice") == notice_key:
                continue
            await self.ctf._message(
                conn,
                tid,
                "system",
                aid,
                worker_message(
                    {
                        **compact,
                        "instruction": (
                            "Choose and claim a new challenge first; otherwise choose assistance. "
                            "If another Worker wins, choose again. "
                            "Wait only when no work is available."
                        ),
                    },
                    {
                        "counts": {key: len(values) for key, values in compact.items()},
                        "instruction": (
                            "Available work exceeds this notification. Read board_available_work "
                            "for all choices. Claim a new challenge first, otherwise assistance. "
                            "If another Worker wins, choose again; wait only when no work remains."
                        ),
                    },
                ),
                worker_id(tid, aid, "available", notice_key),
                "instruction",
            )
            sessions[aid] = {**session, "available_notice": notice_key}
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(ctf_control={**state, "native_worker_sessions": sessions})
        )

    async def _release(self, conn, tid, task, record):
        for aid, assignment in list(task["ctf_control"].get("native_worker_active", {}).items()):
            challenge = await self.ctf._challenge(conn, tid, assignment["work_id"])
            if challenge["work_status"] != "completed":
                continue
            binding = await row(conn, s.runner_bindings, tid, assignment["binding_id"])
            idle = binding["business"].get("worker_idle")
            if not idle or idle["context"] != work_context(task, aid):
                continue
            if await self._model_after_idle(conn, tid, binding["id"], idle["seq"]):
                continue
            if await conn.scalar(
                select(s.pi_model_calls.c.call_id)
                .where(
                    s.pi_model_calls.c.task_id == tid,
                    s.pi_model_calls.c.agent_id == aid,
                    s.pi_model_calls.c.state.in_(["prepared", "dispatched", "unknown"]),
                )
                .limit(1)
            ):
                continue
            if await conn.scalar(
                select(s.runner_calls.c.id)
                .where(
                    s.runner_calls.c.task_id == tid,
                    s.runner_calls.c.data["binding"]["agent_id"].astext == aid,
                    s.runner_calls.c.state.in_(["admitted", "dispatched", "unknown"]),
                )
                .limit(1)
            ):
                continue
            state = dict((await row(conn, s.tasks, tid))["ctf_control"])
            active = dict(state["native_worker_active"])
            if not assignment.get("releasing"):
                active[aid] = {**assignment, "releasing": True}
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(ctf_control={**state, "native_worker_active": active})
                )
                await self.ctf._emit(
                    conn,
                    tid,
                    "native_worker.cleanup_changed",
                    "system",
                    {"binding_id": binding["id"]},
                )
            if await conn.scalar(
                select(s.sandbox_command_records.c.id)
                .where(
                    s.sandbox_command_records.c.task_id == tid,
                    s.sandbox_command_records.c.assignment_id == assignment["assignment_ref"],
                    s.sandbox_command_records.c.data["scope_state"].astext.is_distinct_from(
                        "sealed"
                    ),
                )
                .limit(1)
            ):
                continue
            active.pop(aid)
            await conn.execute(
                update(s.ctf_messages)
                .where(
                    s.ctf_messages.c.task_id == tid,
                    s.ctf_messages.c.recipient_id == aid,
                    s.ctf_messages.c.challenge_id == assignment["work_id"],
                    s.ctf_messages.c.status == "queued",
                    s.ctf_messages.c.sender_kind == "system",
                )
                .values(status="cancelled")
            )
            sessions = dict(state["native_worker_sessions"])
            sessions[aid] = {**sessions[aid], "work_revision": sessions[aid]["work_revision"] + 1}
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={
                        **state,
                        "native_worker_active": active,
                        "native_worker_sessions": sessions,
                    }
                )
            )
            await conn.execute(
                update(s.ctf_members)
                .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid)
                .values(run_state="idle")
            )
            await self.ctf._emit(conn, tid, "native_worker.released", "system", assignment)

    async def reconcile(self, conn, tid, task, record, owner, *, allow_new):
        from bbx_blackboard.worker_board_updates import clock_changes, notify_changes

        if allow_new and task["ctf_control"].get("native_worker_continuations"):
            await self.restore(conn, tid, task, record, owner)
            task = await row(conn, s.tasks, tid)
        if task["ctf_control"].get("native_worker_continuations"):
            return []
        await clock_changes(self.ctf, conn, tid, task)
        task = await row(conn, s.tasks, tid)
        await self._release(conn, tid, task, record)
        task = await row(conn, s.tasks, tid)
        if allow_new:
            idle = list(
                (
                    await conn.execute(
                        select(s.ctf_members)
                        .where(
                            s.ctf_members.c.task_id == tid,
                            s.ctf_members.c.role == "teammate",
                            s.ctf_members.c.lifecycle == "active",
                            s.ctf_members.c.run_state == "idle",
                            s.ctf_members.c.pending_operation.is_(None),
                        )
                        .order_by(s.ctf_members.c.id)
                    )
                ).mappings()
            )
            for member in idle:
                if member["has_worked"]:
                    continue
                task = await row(conn, s.tasks, tid)
                choices = available_work(task, await self._challenges(conn, tid))
                preferred = choices["new_challenges"] or choices["assistance"]
                if not preferred:
                    break
                challenge = await self.ctf._challenge(conn, tid, preferred[0]["challenge_id"])
                await self.claim(conn, tid, task, member, challenge, first=True)
            task = await row(conn, s.tasks, tid)
            await notify_changes(self.ctf, conn, tid)
            task = await row(conn, s.tasks, tid)
            await self._notify_available(conn, tid, task, idle)
            await self._deliver(conn, tid, await row(conn, s.tasks, tid), owner)
        return sorted(
            item["binding_id"]
            for item in task["ctf_control"].get("native_worker_sessions", {}).values()
        )
