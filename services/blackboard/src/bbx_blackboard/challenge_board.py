"""Assignment-scoped knowledge using the existing event and projection transaction."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from bbx_contracts.challenge_board import (
    BOARD_CONTRACT,
    ChallengeFactRequest,
    ChallengeIntentRequest,
    business_contract,
    native_dispatch_enabled,
)
from bbx_contracts.models import Fact, Intent, KnowledgeAssignment
from fastapi import HTTPException
from sqlalchemy import select

from bbx_blackboard.auth import Principal
from bbx_blackboard.ctf_review import TERMINAL
from bbx_blackboard.domain import RuleViolation, dispute_fields
from bbx_blackboard.domain.rules import fact_events, intent_events
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import row


def current_assignment(task, challenge, member):
    if member["role"] != "lead" and native_dispatch_enabled(task):
        snapshot = task["ctf_control"].get("native_worker_active", {}).get(member["id"], {})
        if (
            snapshot.get("work_id") != challenge["id"]
            or snapshot.get("scope_revision") != challenge.get("scope_revision", 1)
            or snapshot.get("turn_id") != member["current_turn_id"]
            or snapshot.get("generation") != member["generation"]
            or challenge["work_status"] != "in_progress"
        ):
            raise HTTPException(403, "Current native Worker Assignment is unavailable")
        return snapshot
    if (
        member["role"] == "lead"
        or business_contract(task) == BOARD_CONTRACT
        or (task.get("ctf_options") or {}).get("dispatch_mode", "manual") != "automatic"
    ):
        return None
    control = task["ctf_control"]
    reference = control.get("dispatch_members", {}).get(member["id"])
    snapshot = control.get("dispatch_assignments", {}).get(reference)
    if (
        not snapshot
        or snapshot.get("work_id") != challenge["id"]
        or snapshot.get("scope_revision") != challenge.get("scope_revision", 1)
        or snapshot.get("assignment_ref", reference) != reference
    ):
        raise HTTPException(403, "Current Assignment binding is unavailable")
    return {**snapshot, "assignment_ref": reference}


async def knowledge_event_scope(repo, conn, tid, aid):
    task = await row(conn, s.tasks, tid)
    member = (
        (
            await conn.execute(
                select(s.ctf_members).where(
                    s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid
                )
            )
        )
        .mappings()
        .first()
    )
    if member is None or member["role"] not in {"lead", "teammate"}:
        return set()
    review = task["status"] in TERMINAL
    if review:
        turn = (
            await conn.execute(
                select(s.ctf_turns).where(
                    s.ctf_turns.c.task_id == tid,
                    s.ctf_turns.c.id == member["current_turn_id"],
                    s.ctf_turns.c.agent_id == aid,
                    s.ctf_turns.c.generation == member["generation"],
                    s.ctf_turns.c.purpose == "review",
                    s.ctf_turns.c.status == "running",
                )
            )
        ).first()
        if turn is None:
            return set()
    elif member["lifecycle"] != "active":
        return set()
    challenges = (
        await conn.execute(select(s.ctf_challenges.c.data).where(s.ctf_challenges.c.task_id == tid))
    ).scalars()
    history = await repo.historical_challenges(conn, tid, aid) if review else set()
    scope = set()
    for challenge in challenges:
        if challenge.get("tombstone"):
            continue
        if member["role"] == "lead":
            scope.add(challenge["id"])
        elif review and (challenge.get("owner_id") == aid or challenge["id"] in history):
            scope.add(challenge["id"])
        elif not review and challenge.get("owner_id") == aid:
            try:
                current_assignment(task, challenge, member)
            except HTTPException:
                continue
            scope.add(challenge["id"])
    return scope


class ChallengeBoardService:
    def __init__(self, ctf: Any) -> None:
        self.ctf = ctf
        self.repo = ctf.repo

    @staticmethod
    def _agent(identity: Principal, tid: UUID) -> str:
        if (
            identity.kind != "agent"
            or identity.task_id != tid
            or identity.mode != "ctf"
            or identity.purpose != "solver"
            or not identity.agent_id
            or identity.generation is None
        ):
            raise HTTPException(403, "Task-bound CTF agent identity required")
        return identity.agent_id

    async def _task_access(self, conn, tid, identity, *, write=False, lead=False):
        if getattr(identity, "runtime_context", None) is not None:
            from bbx_blackboard.gateway_auth import validate_runtime_context

            await validate_runtime_context(conn, tid, identity.runtime_context)
        task = await row(conn, s.tasks, tid)
        if task["mode"] != "ctf" or business_contract(task) != BOARD_CONTRACT:
            raise HTTPException(409, "Published challenge-board-v1 task required")
        if task["deleting"]:
            raise HTTPException(409, "Task is deleting")
        if identity.kind in {"user", "service"} and not write:
            return task, None
        if write and native_dispatch_enabled(task) and identity.runtime_context is None:
            raise HTTPException(403, "Native Runner context is required for this Task")
        aid = self._agent(identity, tid)
        member = (
            (
                await conn.execute(
                    select(s.ctf_members).where(
                        s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid
                    )
                )
            )
            .mappings()
            .first()
        )
        if member is None or member["role"] not in {"lead", "teammate"}:
            raise HTTPException(403, "Unknown member; helper ChildRun binding is not ready")
        if member["generation"] != identity.generation:
            raise HTTPException(403, "Stale board identity")
        if lead and member["role"] != "lead":
            raise HTTPException(403, "Only Lead may perform this operation")
        if write:
            if task["status"] != "running" or identity.turn_id is None:
                raise HTTPException(409, "Task execution is not running")
            await self.ctf._board_write(conn, tid, aid, identity.generation, identity.turn_id)
        elif task["status"] in TERMINAL:
            if identity.turn_id is None:
                raise HTTPException(403, "Fenced review required")
            _, turn = await self.ctf._fence(conn, tid, aid, identity.generation, identity.turn_id)
            self.ctf._authorize_review(task, turn)
        elif member["lifecycle"] != "active":
            raise HTTPException(403, "Inactive board identity")
        return task, dict(member)

    async def _authority(self, conn, tid, cid, identity, *, write=False):
        if getattr(identity, "runtime_context", None) is not None:
            from bbx_blackboard.gateway_auth import validate_runtime_context

            await validate_runtime_context(conn, tid, identity.runtime_context)
        task = await row(conn, s.tasks, tid)
        if task["mode"] != "ctf":
            raise HTTPException(409, "mode_mismatch")
        if task["deleting"]:
            raise HTTPException(409, "Task is deleting")
        if write and task["status"] != "running":
            raise HTTPException(409, "Task is not running")
        challenge = await self.ctf._challenge(conn, tid, cid)
        if challenge.get("tombstone"):
            raise HTTPException(404, "Challenge not found")
        if identity.kind in {"user", "service"} and not write:
            return task, challenge, None
        if write and native_dispatch_enabled(task) and identity.runtime_context is None:
            raise HTTPException(403, "Native Runner context is required for this Task")
        aid = self._agent(identity, tid)
        member = (
            (
                await conn.execute(
                    select(s.ctf_members).where(
                        s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid
                    )
                )
            )
            .mappings()
            .first()
        )
        if member is None or member["role"] not in {"lead", "teammate"}:
            raise HTTPException(403, "Unknown member; helper ChildRun binding is not ready")
        if member["generation"] != identity.generation:
            raise HTTPException(403, "Stale knowledge identity")
        if not write and task["status"] in TERMINAL:
            if identity.turn_id is None:
                raise HTTPException(403, "Fenced review identity required")
            _, turn = await self.ctf._fence(conn, tid, aid, identity.generation, identity.turn_id)
            self.ctf._authorize_review(task, turn)
            if (
                member["role"] != "lead"
                and challenge.get("owner_id") != aid
                and str(cid) not in await self.repo.historical_challenges(conn, tid, aid)
            ):
                raise HTTPException(403, "Challenge is outside this Agent's review history")
            return task, challenge, None
        if member["lifecycle"] != "active":
            raise HTTPException(403, "Inactive knowledge identity")
        if member["role"] != "lead":
            if challenge.get("owner_id") != aid:
                raise HTTPException(403, "Challenge is outside the current Assignment")
        snapshot = current_assignment(task, challenge, member)
        if write:
            if identity.turn_id is None:
                raise HTTPException(403, "Execution turn identity required")
            await self.ctf._board_write(conn, tid, aid, identity.generation, identity.turn_id)
            if snapshot is not None and snapshot.get("readonly_intake"):
                raise HTTPException(403, "Read-only legacy intake cannot write challenge knowledge")
        return task, challenge, snapshot

    async def _evidence(self, conn, tid, cid, aid, task, data):
        uris, native_refs = [], {}
        assignment = (
            task["ctf_control"].get("native_worker_active", {}).get(aid)
            if native_dispatch_enabled(task)
            else None
        )
        for evidence in data["evidence"]:
            uri = evidence.get("uri")
            call_id = evidence.get("call_id")
            if not uri and assignment and evidence["type"] == "command_output" and call_id:
                commands, calls = s.sandbox_command_records, s.runner_calls
                if any(evidence.get(key) is not None for key in ("path", "size", "sha256")):
                    raise HTTPException(422, "Command evidence references the saved result only")
                original = (
                    await conn.execute(
                        select(commands.c.id)
                        .join(
                            calls,
                            (calls.c.task_id == commands.c.task_id)
                            & (calls.c.binding_id == commands.c.binding_id),
                        )
                        .where(
                            commands.c.task_id == tid,
                            commands.c.id == call_id,
                            commands.c.agent_id == aid,
                            commands.c.challenge_id == cid,
                            commands.c.assignment_id == assignment["assignment_ref"],
                            commands.c.binding_id == assignment["binding_id"],
                            commands.c.epoch == assignment["epoch"],
                            commands.c.state.in_(["completed", "failed"]),
                            commands.c.data["receipt"].astext == "foreground_terminal",
                            commands.c.execution_id.is_not(None),
                            commands.c.exit_code.is_not(None),
                            calls.c.id == "tool:" + call_id,
                            calls.c.provider_key == "sandbox",
                            calls.c.state == "completed",
                            calls.c.data["tool_name"].astext == "exec_command",
                        )
                    )
                ).scalar_one_or_none()
                if not original:
                    raise HTTPException(
                        403, "Command evidence is outside the original Assignment or unconfirmed"
                    )
                native_refs[call_id] = {"id": call_id, "agent_id": aid}
                continue
            if not uri:
                raise RuleViolation("evidence_missing", "证据尚未持久化，请先登记材料。")
            initial = next(
                (item for item in task.get("initial_attachments", []) if item["uri"] == uri), None
            )
            if initial is not None:
                if aid != "lead" and initial["id"] not in task["ctf_control"].get(
                    "public_input_ids", []
                ):
                    raise HTTPException(403, "Initial attachment is not shared with this Agent")
                material = initial
            else:
                registered = (
                    (
                        await conn.execute(
                            select(
                                s.ctf_records.c.id,
                                s.ctf_records.c.data["artifact"].label("artifact"),
                            )
                            .where(
                                s.ctf_records.c.task_id == tid,
                                s.ctf_records.c.data["kind"].astext == "artifact_registration",
                                s.ctf_records.c.data["artifact"]["uri"].astext == uri,
                            )
                            .limit(1)
                        )
                    )
                    .mappings()
                    .first()
                )
                if registered is None:
                    raise HTTPException(403, "Evidence must be registered in this task")
                try:
                    await self.ctf._artifact_refs(conn, tid, [registered["id"]], cid)
                except HTTPException as error:
                    raise HTTPException(
                        403, "Evidence is not shared with this challenge"
                    ) from error
                material = registered["artifact"]
            for key in ("path", "size", "sha256"):
                if evidence.get(key) is not None and evidence[key] != material.get(key):
                    raise RuleViolation(
                        "evidence_metadata_conflict", "证据元数据与登记材料不一致。"
                    )
            uris.append(uri)
        return list(dict.fromkeys(uris)), native_refs

    async def _objects_exist(self, uris):
        if not uris:
            return
        if self.ctf.objects is None:
            raise RuleViolation("evidence_missing", "证据存储不可用。")
        try:
            async with asyncio.timeout(10):
                for uri in uris:
                    if not await self.ctf.objects.exists(uri):
                        raise RuleViolation("evidence_missing", "证据尚未持久化，请先登记材料。")
        except TimeoutError as error:
            raise HTTPException(503, "Evidence storage check timed out") from error

    async def _previous(self, conn, tid, aid, request_id, digest):
        events = [
            dict(item)
            for item in (
                await conn.execute(
                    select(s.events)
                    .where(
                        s.events.c.task_id == tid,
                        s.events.c.payload["request_id"].astext == request_id,
                        s.events.c.payload["request_actor"].astext == aid,
                    )
                    .order_by(s.events.c.version)
                )
            ).mappings()
        ]
        if events and events[0]["payload"]["request_hash"] != digest:
            raise HTTPException(409, "Request ID conflict")
        return events

    @staticmethod
    def _response(events):
        first = events[0]
        model = Fact if first["type"] == "fact.posted" else Intent
        data = {key: value for key, value in first["payload"].items() if key in model.model_fields}
        if model is Fact:
            data.update(status="proposed", relied_by=0)
        obj = model.model_validate({**data, "version": first["version"]})
        return {
            "id": first["object_id"],
            "object": obj.model_dump(mode="json"),
            "challenge_revision": first["payload"]["challenge_revision"],
            "board_version": events[-1]["version"],
            "events": events,
        }

    async def write(self, tid: UUID, cid: UUID, identity: Principal, request, *, kind: str):
        model = ChallengeFactRequest if kind == "fact" else ChallengeIntentRequest
        body = model.model_validate(request).model_dump(mode="json")
        aid = self._agent(identity, tid)
        digest = hashlib.sha256(
            json.dumps({"kind": kind, "challenge_id": str(cid), **body}, sort_keys=True).encode()
        ).hexdigest()
        if kind == "fact":
            async with self.repo.engine.begin() as conn:
                await self.repo.lock(conn, tid)
                task, challenge, _ = await self._authority(conn, tid, cid, identity, write=True)
                previous = await self._previous(conn, tid, aid, body["request_id"], digest)
                if previous:
                    return self._response(previous)
                self.ctf._revision(challenge, body["expected_revision"])
                uris, _ = await self._evidence(conn, tid, cid, aid, task, body)
            # Registered objects are immutable; external I/O must not hold the Task lock.
            await self._objects_exist(uris)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, challenge, snapshot = await self._authority(conn, tid, cid, identity, write=True)
            previous = await self._previous(conn, tid, aid, body["request_id"], digest)
            if previous:
                return self._response(previous)
            self.ctf._revision(challenge, body["expected_revision"])
            state = await self.repo.load(conn, tid, include_tool_calls=False, challenge_id=str(cid))
            assert identity.generation is not None and identity.turn_id is not None
            origin = KnowledgeAssignment(
                agent_id=aid,
                generation=identity.generation,
                turn_id=identity.turn_id,
                epoch=snapshot.get("epoch", task["ctf_control"]["epoch"])
                if snapshot
                else task["ctf_control"]["epoch"],
                owner_id=challenge.get("owner_id"),
                scope_revision=challenge.get("scope_revision", 1),
                assignment_id=snapshot.get("assignment_ref") if snapshot else None,
            ).model_dump(mode="json")
            data = {
                **{
                    key: value
                    for key, value in body.items()
                    if key not in {"request_id", "expected_revision"}
                },
                "challenge_id": str(cid),
                "assignment_ref": origin,
            }
            if kind == "fact":
                iid = data.get("resolves")
                if iid:
                    if challenge.get("owner_id") != aid:
                        raise HTTPException(
                            403, "Only the current Assignment may close its directions"
                        )
                    if iid not in state.intents:
                        raise RuleViolation("invalid_reference", "意图不在当前题目范围内。")
                    if state.intents[iid]["status"] == "closed":
                        raise RuleViolation("intent_closed", "意图已关闭，请刷新题内知识。")
                _, native_refs = await self._evidence(conn, tid, cid, aid, task, data)
                state.tool_calls = await self.repo.tool_call_refs(
                    conn, tid, [item["call_id"] for item in data["evidence"] if item.get("call_id")]
                )
                state.tool_calls.update(native_refs)
                planned = fact_events(
                    state,
                    aid,
                    data,
                    resolvable_intents={iid} if iid else set(),
                )
            else:
                planned = intent_events(state, aid, {**data, "relates_to": [], "claim": False})
            recipients = sorted({"lead", aid, challenge.get("owner_id")} - {None})
            for event in planned:
                event["addressed_to"] = recipients
                event["payload"].update(
                    challenge_id=str(cid),
                    challenge_revision=challenge["revision"],
                    request_id=body["request_id"],
                    request_actor=aid,
                    request_hash=digest,
                )
            saved = await self.repo.append(conn, tid, planned)
            if kind == "fact" and snapshot and native_dispatch_enabled(task):
                fact_id = saved[0]["object_id"]
                await self.ctf._message(
                    conn,
                    tid,
                    "system",
                    "lead",
                    json.dumps(
                        {
                            "challenge_id": str(cid),
                            "challenge_revision": challenge["revision"],
                            "assignment_id": snapshot["assignment_ref"],
                            "fact_id": fact_id,
                            "instruction": "A Worker recorded a fact. Read the challenge board.",
                        },
                        ensure_ascii=False,
                    ),
                    str(uuid5(NAMESPACE_URL, f"native-worker-fact:{tid}:{fact_id}")),
                    "message",
                )
            await self.repo.notify(tid, saved[-1]["version"], connection=conn)
            return self._response(saved)

    async def board(self, tid: UUID, cid: UUID, identity: Principal):
        async with self.repo.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            _, challenge, _ = await self._authority(conn, tid, cid, identity)
            state = await self.repo.load(conn, tid, include_tool_calls=False, challenge_id=str(cid))
        fields = dispute_fields(state.facts)
        facts = {
            fid: Fact.model_validate(
                {
                    **{key: value for key, value in item.items() if key in Fact.model_fields},
                    "status": fields[fid]["status"],
                    "relied_by": fields[fid]["relied_by"],
                }
            ).model_dump(mode="json")
            for fid, item in state.facts.items()
        }
        intents = {
            iid: Intent.model_validate(
                {key: value for key, value in item.items() if key in Intent.model_fields}
            ).model_dump(mode="json")
            for iid, item in state.intents.items()
        }
        return {
            "task_id": str(tid),
            "challenge_id": str(cid),
            "challenge_revision": challenge["revision"],
            "board_version": state.task["version"],
            "facts": facts,
            "intents": intents,
        }

    async def get_object(self, tid, cid, identity, oid, depth=0):
        board = await self.board(tid, cid, identity)
        objects = {**board["facts"], **board["intents"]}
        if oid not in objects:
            raise HTTPException(404, "Knowledge object not found in this challenge")
        obj = objects[oid]
        keys = set()
        if depth:
            keys.update(
                obj.get("derived_from", []) + obj.get("disputes", []) + obj.get("based_on", [])
            )
            keys.update(value for key in ("resolves", "retry_of") if (value := obj.get(key)))
            keys.update(
                key
                for key, value in objects.items()
                if oid
                in value.get("derived_from", [])
                + value.get("disputes", [])
                + value.get("based_on", [])
                or oid in (value.get("resolves"), value.get("retry_of"))
            )
        return {"object": obj, "related": {key: objects[key] for key in keys if key in objects}}

    async def search(self, tid, cid, identity, query, limit):
        board = await self.board(tid, cid, identity)
        hits = []
        for kind, objects in (("fact", board["facts"]), ("intent", board["intents"])):
            for item in objects.values():
                text = " ".join(
                    str(item.get(key) or "") for key in ("statement", "expected", "method")
                )
                if query.casefold() in text.casefold():
                    hits.append(
                        (
                            item["version"],
                            {"id": item["id"], "type": kind, "statement": item["statement"]},
                        )
                    )
        return [hit for _, hit in sorted(hits, key=lambda item: item[0], reverse=True)[:limit]]
