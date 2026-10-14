"""CTF challenge CAS, append-only records and trusted artifact registration."""

from __future__ import annotations

import hashlib
import json
import posixpath
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from bbx_contracts.ctf import (
    CtfAssignmentInstruction,
    CtfProgress,
    CtfSolution,
    execution_agent_id,
    validate_ctf_delivery,
)
from bbx_contracts.tool_errors import CTF_BOARD_ERROR_CODES, validation_issues
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import literal_column, or_, select

from bbx_blackboard.store import schema as s

SOLUTION_DELIVERY_ERROR = {
    "code": "invalid_solution_delivery",
    "message": "Owner or Lead must supply a valid solution when completing work; "
    "solution is forbidden for other statuses",
}


def ensure(condition, message):
    if not condition:
        code = CTF_BOARD_ERROR_CODES.get(message)
        raise HTTPException(409, {"code": code, "message": message} if code else message)


class CtfBoardMixin:
    repo: Any
    objects: Any
    _task: Any
    _actor: Any
    _message: Any
    _fence: Any

    async def _board_write(self, conn, tid, actor, generation, turn_id):
        task = await self._task(conn, tid)
        ensure(task["ctf_control"]["phase"] == "running", "Task is not running")
        ensure(
            task["ctf_control"].get("replacement", {}).get("phase", "ready") == "ready",
            "Execution replacement is in progress",
        )
        await self._actor(conn, tid, actor, generation, turn_id)

    async def _challenge(self, conn, tid, challenge_id):
        result = (
            (
                await conn.execute(
                    select(s.ctf_challenges).where(
                        s.ctf_challenges.c.task_id == tid,
                        s.ctf_challenges.c.id == str(challenge_id),
                    )
                )
            )
            .mappings()
            .first()
        )
        if result is None:
            raise HTTPException(404, "Challenge not found")
        return dict(result["data"])

    async def _existing(self, conn, tid, actor, request_id, command):
        digest = hashlib.sha256(
            json.dumps(command, sort_keys=True, default=str).encode()
        ).hexdigest()
        previous = (
            (
                await conn.execute(
                    select(s.events)
                    .where(
                        s.events.c.task_id == tid,
                        s.events.c.actor == actor,
                        s.events.c.type.in_(
                            [
                                "ctf.challenge.created",
                                "ctf.challenge.updated",
                                "ctf.record.appended",
                            ]
                        ),
                        s.events.c.payload["request_id"].astext == str(request_id),
                    )
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        if previous:
            ensure(previous["payload"]["request_hash"] == digest, "Request ID conflict")
            result = dict(previous["payload"]["result"])
            if previous["type"] == "ctf.record.appended":
                result["created_version"] = previous["version"]
                result["created_at"] = previous["created_at"].isoformat()
                if result.get("kind") == "artifact_registration":
                    result["artifact"] = {
                        **result["artifact"],
                        "created_version": previous["version"],
                    }
            return result, digest
        return None, digest

    async def _board_event(self, conn, tid, actor, kind, request_id, digest, result):
        events = await self.repo.append(
            conn,
            tid,
            [
                {
                    "type": kind,
                    "actor": actor,
                    "object_id": result["id"],
                    "addressed_to": None,
                    "payload": {
                        "request_id": str(request_id),
                        "request_hash": digest,
                        "result": result,
                    },
                }
            ],
        )
        if kind == "ctf.record.appended":
            result = {
                **result,
                "created_version": events[0]["version"],
                "created_at": events[0]["created_at"].isoformat(),
            }
        await self.repo.notify(tid, events[-1]["version"], connection=conn)
        return result

    async def list_challenges(self, tid, *, limit=None, offset=0):
        async with self.repo.engine.connect() as conn:
            query = (
                select(s.ctf_challenges.c.data)
                .where(s.ctf_challenges.c.task_id == tid)
                .order_by(s.ctf_challenges.c.id)
            )
            if limit is not None:
                query = query.offset(offset).limit(limit + 1)
            values = [dict(item) for item in (await conn.execute(query)).scalars()]
            if limit is None:
                return {"challenges": values}
            return {
                "challenges": values[:limit],
                "next_offset": offset + limit if len(values) > limit else None,
            }

    async def get_challenge(self, tid, challenge_id):
        async with self.repo.engine.connect() as conn:
            return await self._challenge(conn, tid, challenge_id)

    async def get_record(self, tid, challenge_id, record_id):
        async with self.repo.engine.connect() as conn:
            await self._challenge(conn, tid, challenge_id)
            record = (
                await conn.execute(
                    select(s.ctf_records.c.data).where(
                        s.ctf_records.c.task_id == tid,
                        s.ctf_records.c.id == str(record_id),
                        s.ctf_records.c.data[literal_column("'challenge_id'")].astext
                        == str(challenge_id),
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise HTTPException(404, "Challenge record not found")
            return dict(record)

    async def create_challenge(
        self,
        tid,
        actor,
        request_id,
        title,
        description="",
        connection=None,
        external_id=None,
        requirements=None,
        generation=None,
        turn_id=None,
    ):
        ensure(isinstance(title, str) and 0 < len(title.strip()) <= 200, "Invalid title")
        content = dict(
            title=title,
            description=description,
            connection=connection,
            external_id=external_id,
            requirements=requirements,
        )
        async with self.repo.engine.begin() as conn:
            await self._board_write(conn, tid, actor, generation, turn_id)
            previous, digest = await self._existing(
                conn, tid, actor, request_id, {"operation": "create", **content}
            )
            if previous:
                return previous
            result = {
                "id": str(uuid5(NAMESPACE_URL, f"ctf-challenge:{tid}:{actor}:{request_id}")),
                "task_id": str(tid),
                **content,
                "owner_id": None,
                "collaborator_ids": [],
                "work_status": "pending",
                "revision": 1,
                "verification": {},
                "target": {},
                "tombstone": False,
            }
            return await self._board_event(
                conn, tid, actor, "ctf.challenge.created", request_id, digest, result
            )

    @staticmethod
    def _revision(challenge, expected_revision):
        if challenge["revision"] != expected_revision:
            raise HTTPException(
                409, {"code": "revision_conflict", "current_revision": challenge["revision"]}
            )

    async def _active_member(self, conn, tid, member_id):
        member = (
            (
                await conn.execute(
                    select(s.ctf_members).where(
                        s.ctf_members.c.task_id == tid, s.ctf_members.c.id == member_id
                    )
                )
            )
            .mappings()
            .first()
        )
        ensure(
            member is not None
            and member["lifecycle"] == "active"
            and member["run_state"] != "stopping",
            "Member is unavailable",
        )

    @staticmethod
    def _delivery_solution(completed, solution):
        try:
            value = CtfSolution.model_validate(solution) if solution is not None else None
            validate_ctf_delivery(completed, value)
            return value.model_dump(mode="json") if value is not None else None
        except (ValidationError, ValueError) as exc:
            detail: dict[str, Any] = dict(SOLUTION_DELIVERY_ERROR)
            if isinstance(exc, ValidationError):
                detail["fields"] = validation_issues(exc, CtfSolution.model_json_schema())
            raise HTTPException(422, detail) from exc

    async def _complete_solution(
        self,
        conn,
        tid,
        actor,
        challenge,
        request_id,
        solution,
        artifact_ids=None,
        generation=None,
        turn_id=None,
    ):
        ensure(
            actor in {"lead", "user", challenge["owner_id"]},
            "Only the owner or Lead can deliver a solution",
        )
        refs = list(artifact_ids or [])
        record_ids = set()
        for step in solution["steps"]:
            refs.extend(step["artifact_ids"])
            record_ids.update(step["record_ids"])
        if record_ids:
            records = (
                (
                    await conn.execute(
                        select(
                            s.ctf_records.c.id,
                            s.ctf_records.c.data["challenge_id"].astext.label("challenge_id"),
                        ).where(
                            s.ctf_records.c.task_id == tid,
                            s.ctf_records.c.id.in_(record_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
            ensure(
                len(records) == len(record_ids)
                and all(record["challenge_id"] == challenge["id"] for record in records),
                "Solution records must belong to this challenge",
            )
        artifacts = await self._artifact_refs(conn, tid, refs, challenge["id"])
        task = await self._task(conn, tid)
        result = dict(
            id=str(uuid5(NAMESPACE_URL, f"ctf-solution:{tid}:{actor}:{request_id}")),
            task_id=str(tid),
            challenge_id=challenge["id"],
            author_id=actor,
            kind="solution",
            body=solution["conclusion"],
            artifact_refs=artifacts,
            solution=solution,
            solution_run_number=task["run_number"],
            solution_challenge_revision=challenge["revision"] + 1,
            solution_generation=generation,
            solution_turn_id=str(turn_id) if turn_id is not None else None,
        )
        digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        return await self._board_event(
            conn,
            tid,
            actor,
            "ctf.record.appended",
            f"solution:{request_id}",
            digest,
            result,
        )

    async def update_challenge(
        self,
        tid,
        actor,
        challenge_id,
        request_id,
        expected_revision,
        action,
        owner_id=None,
        collaborator_ids=None,
        work_status=None,
        generation=None,
        turn_id=None,
        instructions=None,
        solution=None,
    ):
        solution = self._delivery_solution(
            action == "set_status" and work_status == "completed", solution
        )
        if instructions is not None:
            if not isinstance(instructions, list) or not 1 <= len(instructions) <= 32:
                raise HTTPException(422, "Instructions must contain 1 to 32 recipients")
            try:
                instructions = sorted(
                    [
                        CtfAssignmentInstruction.model_validate(item).model_dump(mode="json")
                        for item in instructions
                    ],
                    key=lambda item: item["recipient_id"],
                )
            except ValidationError as exc:
                raise HTTPException(422, "Invalid assignment instruction") from exc
            if len({item["recipient_id"] for item in instructions}) != len(instructions):
                raise HTTPException(422, "Assignment recipients must be unique")
        async with self.repo.engine.begin() as conn:
            await self._board_write(conn, tid, actor, generation, turn_id)
            command = dict(
                operation="update",
                challenge_id=str(challenge_id),
                expected_revision=expected_revision,
                action=action,
                owner_id=owner_id,
                collaborator_ids=collaborator_ids,
                work_status=work_status,
            )
            if solution is not None:
                command["solution"] = solution
            if instructions is not None:
                ensure(
                    actor in {"lead", "user"} and action in {"assign", "collaborators"},
                    "Only Lead may dispatch with assignment or collaborator changes",
                )
                command["instructions"] = instructions
            previous, digest = await self._existing(conn, tid, actor, request_id, command)
            if previous:
                return previous
            challenge = await self._challenge(conn, tid, challenge_id)
            self._revision(challenge, expected_revision)
            ensure(not challenge["tombstone"], "Challenge is deleted")
            lead = actor in {"lead", "user"}
            owner = actor == challenge["owner_id"]
            result = dict(challenge)
            if action == "claim":
                ensure(
                    challenge["owner_id"] is None and challenge["work_status"] == "pending",
                    "Challenge is not available",
                )
                await self._active_member(conn, tid, actor)
                result.update(owner_id=actor, work_status="in_progress")
            elif action == "assign":
                ensure(lead and owner_id is not None, "Only Lead can assign an owner")
                await self._active_member(conn, tid, owner_id)
                ensure(
                    challenge["work_status"] not in {"completed", "cancelled"},
                    "Reopen the challenge first",
                )
                result.update(owner_id=owner_id, work_status="in_progress")
            elif action == "collaborators":
                ensure(lead and collaborator_ids is not None, "Only Lead can set collaborators")
                assert collaborator_ids is not None
                for member in collaborator_ids:
                    await self._active_member(conn, tid, member)
                result["collaborator_ids"] = list(dict.fromkeys(collaborator_ids))
            elif action == "set_status":
                ensure(lead or owner, "Only the owner or Lead can update work status")
                ensure(
                    work_status in {"in_progress", "blocked", "completed", "cancelled"},
                    "Invalid work status",
                )
                ensure(
                    challenge["work_status"] not in {"completed", "cancelled"},
                    "Reopen the challenge first",
                )
                ensure(
                    work_status == "cancelled" or challenge["owner_id"] is not None,
                    "Challenge needs an owner",
                )
                result["work_status"] = work_status
            elif action == "release":
                ensure(lead or owner, "Only the owner or Lead can release")
                ensure(
                    challenge["work_status"] not in {"completed", "cancelled"},
                    "Reopen the challenge first",
                )
                result.update(owner_id=None, work_status="pending")
            elif action == "reopen":
                ensure(lead or owner, "Only the owner or Lead can reopen")
                ensure(
                    challenge["work_status"] in {"completed", "cancelled"},
                    "Challenge is not closed",
                )
                if challenge["owner_id"]:
                    previous_owner = (
                        (
                            await conn.execute(
                                select(s.ctf_members).where(
                                    s.ctf_members.c.task_id == tid,
                                    s.ctf_members.c.id == challenge["owner_id"],
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if (
                        previous_owner is None
                        or previous_owner["lifecycle"] != "active"
                        or previous_owner["run_state"] == "stopping"
                    ):
                        result.update(owner_id=None, previous_owner_id=challenge["owner_id"])
                result["work_status"] = "in_progress" if result["owner_id"] else "pending"
                result["solution_record_id"] = None
            elif action == "delete":
                ensure(lead, "Only Lead can delete a challenge")
                result.update(tombstone=True, work_status="cancelled")
            else:
                raise HTTPException(422, "Unknown challenge action")
            for instruction in instructions or []:
                ensure(
                    instruction["recipient_id"]
                    in {result["owner_id"], *result["collaborator_ids"]},
                    "Instruction recipient must own or collaborate on the updated challenge",
                )
                await self._active_member(conn, tid, instruction["recipient_id"])
            if solution is not None:
                record = await self._complete_solution(
                    conn,
                    tid,
                    actor,
                    challenge,
                    request_id,
                    solution,
                    generation=generation,
                    turn_id=turn_id,
                )
                result["solution_record_id"] = record["id"]
            result["revision"] += 1
            result = await self._board_event(
                conn, tid, actor, "ctf.challenge.updated", request_id, digest, result
            )
            for instruction in instructions or []:
                await self._message(
                    conn,
                    tid,
                    actor,
                    instruction["recipient_id"],
                    instruction["body"],
                    str(
                        uuid5(
                            NAMESPACE_URL,
                            f"ctf-assignment:{tid}:{actor}:{challenge_id}:{request_id}:"
                            f"{instruction['recipient_id']}",
                        )
                    ),
                    "instruction",
                )
            if (
                action in {"release", "reopen"}
                and actor != "lead"
                and result["owner_id"] is None
                and result["work_status"] == "pending"
                and (
                    challenge["owner_id"] is not None
                    or challenge["work_status"] in {"completed", "cancelled"}
                )
            ):
                await self._message(
                    conn,
                    tid,
                    "system",
                    "lead",
                    json.dumps(
                        {
                            "challenge_id": result["id"],
                            "revision": result["revision"],
                            "reason": action,
                        },
                        sort_keys=True,
                    ),
                    str(
                        uuid5(
                            NAMESPACE_URL,
                            f"ctf-challenge-available:{tid}:{challenge_id}:"
                            f"{result['revision']}:{request_id}",
                        )
                    ),
                    notification_purpose="challenge_available",
                )
            return result

    async def list_records(self, tid, challenge_id, *, limit=None, offset=0, after_version=None):
        if after_version is not None and (type(after_version) is not int or after_version < 0):
            raise HTTPException(422, "Invalid after_version")
        async with self.repo.engine.connect() as conn:
            await self._challenge(conn, tid, challenge_id)
            query = (
                select(s.ctf_records.c.data)
                .where(
                    s.ctf_records.c.task_id == tid,
                    s.ctf_records.c.data[literal_column("'challenge_id'")].astext
                    == str(challenge_id),
                )
                .order_by(s.ctf_records.c.version)
            )
            if after_version is not None:
                query = query.where(s.ctf_records.c.version > after_version)
            if limit is not None:
                query = query.offset(offset).limit(limit + 1)
            values = [dict(item) for item in (await conn.execute(query)).scalars()]
            if limit is None:
                return {"records": values}
            return {
                "records": values[:limit],
                "next_offset": offset + limit if len(values) > limit else None,
            }

    async def _artifact_refs(self, conn, tid, artifact_ids, challenge_id=None):
        ids = list(dict.fromkeys(str(item) for item in (artifact_ids or [])))
        if not ids:
            return []
        registered = (
            (
                await conn.execute(
                    select(
                        s.ctf_records.c.id,
                        s.ctf_records.c.data["kind"].astext.label("kind"),
                        s.ctf_records.c.data["artifact"].label("artifact"),
                    ).where(
                        s.ctf_records.c.task_id == tid,
                        s.ctf_records.c.id.in_(ids),
                    )
                )
            )
            .mappings()
            .all()
        )
        ensure(
            len(registered) == len(ids)
            and all(item["kind"] == "artifact_registration" for item in registered),
            "Artifact is not registered in this task",
        )
        by_id = {item["id"]: item["artifact"] for item in registered}
        if challenge_id is not None:
            records = (
                (
                    await conn.execute(
                        select(
                            s.ctf_records.c.data["challenge_id"].astext.label("challenge_id"),
                            s.ctf_records.c.data["artifact_refs"].label("artifact_refs"),
                        ).where(
                            s.ctf_records.c.task_id == tid,
                            or_(
                                *[
                                    s.ctf_records.c.data["artifact_refs"].contains([{"id": aid}])
                                    for aid in ids
                                ]
                            ),
                        )
                    )
                )
                .mappings()
                .all()
            )
            associations = {aid: set() for aid in ids}
            for record in records:
                if record["challenge_id"] is not None:
                    for artifact in record["artifact_refs"]:
                        if artifact["id"] in associations:
                            associations[artifact["id"]].add(record["challenge_id"])
            ensure(
                all(
                    not targets or str(challenge_id) in targets for targets in associations.values()
                ),
                "Artifacts must not belong exclusively to another challenge",
            )
        return [by_id[aid] for aid in ids]

    async def _append_record(
        self,
        conn,
        tid,
        actor,
        challenge_id,
        request_id,
        body,
        kind,
        artifact_ids,
        help_fields=None,
        expected_revision=None,
        progress=None,
    ):
        command = dict(
            operation="record",
            expected_revision=expected_revision,
            challenge_id=str(challenge_id),
            body=body,
            kind=kind,
            artifact_ids=[str(item) for item in (artifact_ids or [])],
            **(help_fields or {}),
        )
        if progress is not None:
            command["progress"] = progress
        previous, digest = await self._existing(conn, tid, actor, request_id, command)
        if previous:
            return previous, True
        challenge = await self._challenge(conn, tid, challenge_id)
        ensure(not challenge["tombstone"], "Challenge is deleted")
        ensure(
            actor in {"lead", "user", challenge["owner_id"], *challenge["collaborator_ids"]},
            "Only Lead, owner or collaborators may append records. Read get_challenge; "
            "ask Lead to add you to collaborator_ids, or send_message the existing owner "
            "to relay your findings. record_candidate requires the same assignment.",
        )
        refs = await self._artifact_refs(conn, tid, artifact_ids, challenge_id)
        ensure(isinstance(body, str) and bool(body.strip()), "Record body is required")
        result = dict(
            id=str(uuid5(NAMESPACE_URL, f"ctf-record:{tid}:{actor}:{request_id}")),
            task_id=str(tid),
            challenge_id=str(challenge_id),
            author_id=actor,
            kind=kind,
            body=body,
            artifact_refs=refs,
            **(help_fields or {}),
        )
        if progress is not None:
            result["progress"] = progress
        return await self._board_event(
            conn, tid, actor, "ctf.record.appended", request_id, digest, result
        ), False

    async def append_record(
        self,
        tid,
        actor,
        challenge_id,
        request_id,
        body,
        kind="note",
        artifact_ids=None,
        generation=None,
        turn_id=None,
        progress=None,
    ):
        if progress is not None:
            try:
                progress = CtfProgress.model_validate(progress).model_dump(mode="json")
            except ValidationError as exc:
                raise HTTPException(422, "Invalid progress") from exc
        ensure(kind in {"note", "correction"}, "Reserved record kind")
        async with self.repo.engine.begin() as conn:
            await self._board_write(conn, tid, actor, generation, turn_id)
            result, _ = await self._append_record(
                conn,
                tid,
                actor,
                challenge_id,
                request_id,
                body,
                kind,
                artifact_ids,
                progress=progress,
            )
            return result

    async def request_help(
        self,
        tid,
        actor,
        challenge_id,
        request_id,
        expected_revision,
        body,
        attempted_routes,
        observations_and_basis,
        failure_conditions,
        current_blocker,
        help_needed,
        artifact_ids=None,
        no_artifacts_reason=None,
        generation=None,
        turn_id=None,
    ):
        fields = dict(
            attempted_routes=attempted_routes,
            observations_and_basis=observations_and_basis,
            failure_conditions=failure_conditions,
            current_blocker=current_blocker,
            help_needed=help_needed,
        )
        ensure(
            all(isinstance(value, str) and value.strip() for value in fields.values()),
            "All five help fields are required",
        )
        ensure(
            bool(artifact_ids)
            or (isinstance(no_artifacts_reason, str) and bool(no_artifacts_reason.strip())),
            "Explain why no artifacts are needed",
        )
        fields["no_artifacts_reason"] = no_artifacts_reason
        async with self.repo.engine.begin() as conn:
            await self._board_write(conn, tid, actor, generation, turn_id)
            challenge = await self._challenge(conn, tid, challenge_id)
            # Idempotence is checked before CAS; the first transaction updates revision itself.
            record, duplicate = await self._append_record(
                conn,
                tid,
                actor,
                challenge_id,
                request_id,
                body,
                "help_request",
                artifact_ids,
                fields,
                expected_revision=expected_revision,
            )
            if duplicate:
                return record
            ensure(
                actor == challenge["owner_id"] and actor != "lead",
                "Only the teammate owner can request Lead assistance",
            )
            ensure(
                challenge["work_status"] not in {"completed", "cancelled"},
                "Reopen the challenge before requesting help",
            )
            self._revision(challenge, expected_revision)
            updated = {**challenge, "work_status": "blocked", "revision": challenge["revision"] + 1}
            await self._board_event(
                conn, tid, actor, "ctf.challenge.updated", f"help:{request_id}", "help", updated
            )
            await self._message(
                conn,
                tid,
                actor,
                "lead",
                json.dumps(
                    {
                        "challenge_id": str(challenge_id),
                        "record_id": record["id"],
                        "summary": "Owner requests assistance; read the shared record.",
                    }
                ),
                str(uuid5(NAMESPACE_URL, f"ctf-help:{tid}:{actor}:{request_id}")),
                "help_request",
            )
            return record

    async def list_artifacts(self, tid):
        async with self.repo.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(s.ctf_records.c.data)
                    .where(
                        s.ctf_records.c.task_id == tid,
                        s.ctf_records.c.data["kind"].astext == "artifact_registration",
                    )
                    .order_by(s.ctf_records.c.version)
                )
            ).scalars()
            return {"artifacts": [item["artifact"] for item in rows]}

    async def authorize_shared_artifact(self, tid, uri):
        async with self.repo.engine.connect() as conn:
            return (
                await conn.execute(
                    select(s.ctf_records.c.id)
                    .where(
                        s.ctf_records.c.task_id == tid,
                        s.ctf_records.c.data["kind"].astext == "artifact_registration",
                        s.ctf_records.c.data["artifact"]["uri"].astext == uri,
                    )
                    .limit(1)
                )
            ).first() is not None

    async def register_artifact(
        self, tid, agent_id, turn_id, generation, request_id, path, uri, sha256, size, filename
    ):
        ensure(
            posixpath.normpath(path) == path
            and ".." not in path.split("/")
            and path.startswith("/workspace/"),
            "Artifact path is not canonical",
        )
        ensure(
            path.startswith("/workspace/shared/")
            or path.startswith(f"/workspace/agents/{execution_agent_id(agent_id)}/"),
            "Artifact path is outside the workspace",
        )
        ensure(
            uri.startswith(f"evidence/{tid}/")
            and len(uri.split("/")) == 4
            and ".." not in uri.split("/"),
            "Artifact URI belongs to another task",
        )
        ensure(filename == posixpath.basename(path), "Artifact filename mismatch")
        metadata = dict(path=path, uri=uri, sha256=sha256, size=size, filename=filename)
        async with self.repo.engine.begin() as conn:
            await self._board_write(conn, tid, agent_id, generation, turn_id)
            previous, digest = await self._existing(
                conn, tid, agent_id, request_id, {"operation": "artifact", **metadata}
            )
            if previous:
                return previous["artifact"]
        ensure(self.objects is not None, "Object storage is unavailable")
        content = await self.objects.get(uri)
        ensure(
            len(content) == size and hashlib.sha256(content).hexdigest() == sha256,
            "Artifact content integrity mismatch",
        )
        async with self.repo.engine.begin() as conn:
            await self._board_write(conn, tid, agent_id, generation, turn_id)
            previous, digest = await self._existing(
                conn, tid, agent_id, request_id, {"operation": "artifact", **metadata}
            )
            if previous:
                return previous["artifact"]
            aid = str(uuid5(NAMESPACE_URL, f"ctf-artifact:{tid}:{agent_id}:{request_id}"))
            result = {
                "id": aid,
                "task_id": str(tid),
                "kind": "artifact_registration",
                "author_id": agent_id,
                "artifact": {"id": aid, "task_id": str(tid), **metadata},
            }
            saved = await self._board_event(
                conn, tid, agent_id, "ctf.record.appended", request_id, digest, result
            )
            return {**saved["artifact"], "created_version": saved["created_version"]}

    async def lookup_artifact(self, tid, agent_id, request_id, turn_id=None, generation=None):
        async with self.repo.engine.begin() as conn:
            await self._task(conn, tid)
            if generation is not None:
                await self._fence(conn, tid, agent_id, generation, turn_id)
            aid = str(uuid5(NAMESPACE_URL, f"ctf-artifact:{tid}:{agent_id}:{request_id}"))
            result = (
                await conn.execute(
                    select(s.ctf_records.c.data).where(
                        s.ctf_records.c.task_id == tid, s.ctf_records.c.id == aid
                    )
                )
            ).scalar_one_or_none()
            return result["artifact"] if result else None
