"""Task-wide immutable announcements and the versioned Lead business boundary."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from bbx_contracts.challenge_board import (
    DISPATCH_TOOL,
    UPDATE_CHALLENGE_TOOL,
    AnnouncementRequest,
    BoardChallengeRequest,
    BoardChallengeUpdateRequest,
    BoardDispatchChallengeRequest,
    business_manifest,
    native_scope_updates_enabled,
)
from bbx_contracts.ctf import CtfChallenge, CtfVerification
from fastapi import HTTPException
from sqlalchemy import select, update

from bbx_blackboard.board_events import digest, emit, prior
from bbx_blackboard.challenge_board import ChallengeBoardService, knowledge_event_scope
from bbx_blackboard.flag_board import verification_round
from bbx_blackboard.store import schema as s


class TaskBoardService(ChallengeBoardService):
    async def manifest(self, tid, identity):
        async with self.repo.engine.connect() as conn:
            task, member = await self._task_access(conn, tid, identity)
            declared = task["ctf_control"].get("business_tools")
            if declared is None or declared["contract"] != business_manifest()["contract"]:
                raise HTTPException(409, "Business tool manifest is unavailable")
            return {
                **declared,
                "roles": declared["roles"]
                if member is None
                else {member["role"]: declared["roles"][member["role"]]},
            }

    async def publish_announcement(self, tid, identity, request):
        body = AnnouncementRequest.model_validate(request)
        command = {"operation": "announcement", **body.model_dump(mode="json")}
        hashed = digest(command)
        aid = self._agent(identity, tid)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, _ = await self._task_access(conn, tid, identity, write=True, lead=True)
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            latest = (
                (
                    await conn.execute(
                        select(s.board_announcements)
                        .where(s.board_announcements.c.task_id == tid)
                        .order_by(s.board_announcements.c.revision.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            revision = latest["revision"] if latest else 0
            if body.expected_revision != revision:
                raise HTTPException(
                    409, {"code": "announcement_revision_conflict", "current_revision": revision}
                )
            value = {
                "id": str(
                    uuid5(NAMESPACE_URL, f"board-announcement:{tid}:{aid}:{body.request_id}")
                ),
                "task_id": str(tid),
                "revision": revision + 1,
                "body": body.body,
                "author_id": aid,
                "environment_revision": str(task["ctf_control"]["environment_revision"])
                if task["ctf_control"].get("environment_revision") is not None
                else None,
                "supersedes": latest["id"] if latest else None,
                "published_at": datetime.now(UTC).isoformat(),
                "created_version": 0,
            }
            return await emit(
                self.repo,
                conn,
                tid,
                aid,
                "board.announcement.published",
                body.request_id,
                hashed,
                value,
            )

    async def announcements(self, tid, identity, after=0, limit=100):
        async with self.repo.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            await self._task_access(conn, tid, identity)
            return list(
                (
                    await conn.execute(
                        select(s.board_announcements.c.data)
                        .where(
                            s.board_announcements.c.task_id == tid,
                            s.board_announcements.c.revision > after,
                        )
                        .order_by(s.board_announcements.c.revision)
                        .limit(limit)
                    )
                ).scalars()
            )

    async def challenges(self, tid, identity, limit=100, offset=0):
        async with self.repo.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            _, member = await self._task_access(conn, tid, identity)
            scope = (
                await knowledge_event_scope(self.repo, conn, tid, identity.agent_id)
                if identity.kind == "agent"
                else None
            )
            runtime = getattr(identity, "runtime_context", None)
            if (
                scope is not None
                and runtime is not None
                and member is not None
                and member["role"] == "teammate"
            ):
                business = (
                    await conn.execute(
                        select(s.runner_bindings.c.business).where(
                            s.runner_bindings.c.task_id == tid,
                            s.runner_bindings.c.id == runtime.identity.binding_id,
                        )
                    )
                ).scalar_one()
                scope &= {business["challenge_id"]}
            query = select(s.ctf_challenges.c.data).where(s.ctf_challenges.c.task_id == tid)
            if scope is not None:
                query = query.where(s.ctf_challenges.c.id.in_(scope))
            return list(
                (
                    await conn.execute(
                        query.order_by(s.ctf_challenges.c.id).limit(limit).offset(offset)
                    )
                ).scalars()
            )

    async def create_challenge(self, tid, identity, request):
        body = BoardChallengeRequest.model_validate(request)
        if not body.title.strip():
            raise HTTPException(422, "Challenge title is required")
        aid = self._agent(identity, tid)
        hashed = digest({"operation": "lead_create_challenge", **body.model_dump(mode="json")})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task_access(conn, tid, identity, write=True, lead=True)
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            if body.external_id is not None:
                existing = await conn.execute(
                    select(s.ctf_challenges.c.id)
                    .where(
                        s.ctf_challenges.c.task_id == tid,
                        s.ctf_challenges.c.data["external_id"].astext == body.external_id,
                    )
                    .limit(1)
                )
                if existing.first() is not None:
                    raise HTTPException(409, "External challenge is already registered")
            value = CtfChallenge(
                id=uuid5(NAMESPACE_URL, f"board-challenge:{tid}:{aid}:{body.request_id}"),
                task_id=tid,
                title=body.title,
                description=body.description,
                external_id=body.external_id,
                connection=body.connection,
                requirements=body.requirements,
                revision=1,
                verification=CtfVerification(
                    required=True, basis="Trusted flag verification"
                ).model_dump(mode="json"),
            ).model_dump(mode="json")
            await self.ctf._board_event(
                conn, tid, aid, "ctf.challenge.created", body.request_id, hashed, value
            )
            return await emit(
                self.repo,
                conn,
                tid,
                aid,
                "board.request.linked",
                body.request_id,
                hashed,
                value,
                extra={"challenge_id": value["id"]},
            )

    async def _worker_environment(self, conn, tid):
        resources = (
            (
                await conn.execute(
                    select(s.task_runner_resources.c.config, s.task_runner_resources.c.data).where(
                        s.task_runner_resources.c.task_id == tid
                    )
                )
            )
            .mappings()
            .first()
        )
        if (
            not resources
            or (resources["config"] or {}).get("runner_profile") != "node-sandbox-host-v1"
            or not resources["data"].get("worker_spec")
        ):
            raise HTTPException(409, "Original native Worker environment is unavailable")

    @staticmethod
    def _ready_challenge(control, queue, challenge, readiness, priority):
        cid = challenge["id"]
        if readiness == "no_external_materials" and (
            not (challenge.get("description") or "").strip()
            or any(challenge.get(name) for name in ("external_id", "connection", "target"))
        ):
            raise HTTPException(
                409, "A self-contained challenge with no external target is required"
            )
        old = queue.get(cid)
        if not old:
            control["native_worker_ready_sequence"] = (
                control.get("native_worker_ready_sequence", 0) + 1
            )
        queue[cid] = {
            "readiness": readiness,
            "priority": priority,
            "sequence": old["sequence"] if old else control["native_worker_ready_sequence"],
            "scope_revision": challenge.get("scope_revision", 1),
        }
        return "ready" if readiness == "no_external_materials" else "materials_wait"

    async def dispatch_challenge(self, tid, identity, request):
        body = BoardDispatchChallengeRequest.model_validate(request)
        aid = self._agent(identity, tid)
        if identity.runtime_context is None:
            raise HTTPException(403, "Native Runner context required for dispatch")
        hashed = digest({"operation": DISPATCH_TOOL, **body.model_dump(mode="json")})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, _ = await self._task_access(conn, tid, identity, write=True, lead=True)
            control = dict(task["ctf_control"])
            if DISPATCH_TOOL not in control.get("business_tools", {}).get("roles", {}).get(
                "lead", []
            ):
                raise HTTPException(403, "Native dispatch is not published for this Task")
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            await self._worker_environment(conn, tid)
            cid = str(body.challenge_id)
            challenge = await self.ctf._challenge(conn, tid, cid)
            self.ctf._revision(challenge, body.expected_revision)
            if challenge["tombstone"] or challenge["work_status"] in {"completed", "cancelled"}:
                raise HTTPException(409, "Closed challenges cannot be dispatched or reopened")
            queue = dict(control.get("native_worker_ready", {}))
            value = {**challenge, "revision": challenge["revision"] + 1}
            if body.action == "ready":
                if challenge["owner_id"] is not None or challenge["work_status"] != "pending":
                    raise HTTPException(409, "Only unassigned challenges can change readiness")
                status = self._ready_challenge(
                    control, queue, challenge, body.readiness, body.priority
                )
            else:
                queue.pop(cid, None)
                value["work_status"] = "cancelled"
                status = "draining" if challenge["owner_id"] else "cancelled"
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(ctf_control={**control, "native_worker_ready": queue})
            )
            await self.ctf._board_event(
                conn, tid, aid, "ctf.challenge.updated", body.request_id, hashed, value
            )
            return await emit(
                self.repo,
                conn,
                tid,
                aid,
                "board.request.linked",
                body.request_id,
                hashed,
                {"challenge": value, "dispatch_status": status},
                extra={"challenge_id": cid},
            )

    async def update_challenge(self, tid, identity, request):
        body = BoardChallengeUpdateRequest.model_validate(request)
        aid = self._agent(identity, tid)
        if identity.runtime_context is None:
            raise HTTPException(403, "Native Runner context required for challenge updates")
        supplied = body.model_dump(mode="json", exclude_unset=True)
        hashed = digest({"operation": UPDATE_CHALLENGE_TOOL, **supplied})
        cid = str(body.challenge_id)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, _ = await self._task_access(conn, tid, identity, write=True, lead=True)
            if not native_scope_updates_enabled(task):
                raise HTTPException(403, "Challenge updates are not published for this Task")
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            await self._worker_environment(conn, tid)
            challenge = await self.ctf._challenge(conn, tid, cid)
            self.ctf._revision(challenge, body.expected_revision)
            if challenge["tombstone"]:
                raise HTTPException(404, "Challenge not found")
            changes = {
                key: supplied[key]
                for key in ("title", "description", "requirements")
                if key in supplied and supplied[key] != challenge.get(key)
            }
            scope_changed = bool(changes.keys() & {"description", "requirements"})
            if body.readiness is not None and not scope_changed:
                raise HTTPException(422, "Readiness on an update requires a changed scope")
            value = {**challenge, **changes}
            status = "updated"
            if scope_changed:
                control = dict(task["ctf_control"])
                scope = challenge.get("scope_revision", 1)
                if (
                    challenge["work_status"] in {"completed", "cancelled"}
                    or (challenge.get("verification") or {}).get("status")
                    in {"accepted", "unknown"}
                    or any(challenge.get(name) for name in ("external_id", "connection", "target"))
                ):
                    raise HTTPException(
                        409, "Scope can only change for an open self-contained challenge"
                    )
                owner = challenge.get("owner_id")
                assignments = control.get("native_worker_active", {})
                if owner is None and any(item["work_id"] == cid for item in assignments.values()):
                    raise HTTPException(409, "Original Assignment cleanup is still pending")
                if owner is not None:
                    active = assignments.get(owner, {})
                    if active.get("work_id") != cid or active.get("scope_revision") != scope:
                        raise HTTPException(409, "Original Assignment cleanup is still pending")
                await self._retire_flag_round(conn, tid, cid, task, challenge, body.request_id)
                verification = challenge.get("verification") or {}
                value.update(
                    scope_revision=scope + 1,
                    work_status="blocked" if owner else "pending",
                    solution_record_id=None,
                    verification=CtfVerification(
                        required=verification.get("required", True), basis=verification.get("basis")
                    ).model_dump(mode="json"),
                )
                queue = dict(control.get("native_worker_ready", {}))
                queue.pop(cid, None)
                status = "pending"
                if body.readiness is not None:
                    status = self._ready_challenge(
                        control, queue, value, body.readiness, body.priority
                    )
                if owner is not None:
                    status = "draining"
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(ctf_control={**control, "native_worker_ready": queue})
                )
            if changes:
                value["revision"] += 1
                await self.ctf._board_event(
                    conn, tid, aid, "ctf.challenge.updated", body.request_id, hashed, value
                )
            return await emit(
                self.repo,
                conn,
                tid,
                aid,
                "board.request.linked",
                body.request_id,
                hashed,
                {"challenge": value, "dispatch_status": status},
                extra={"challenge_id": cid},
            )

    async def _retire_flag_round(self, conn, tid, cid, task, challenge, edit_id):
        flags = s.flag_submissions
        unresolved = (
            await conn.execute(
                select(flags.c.id)
                .where(
                    flags.c.task_id == tid,
                    flags.c.challenge_id == cid,
                    flags.c.status.in_(["dispatched", "unknown"]),
                )
                .limit(1)
            )
        ).first()
        if unresolved:
            raise HTTPException(409, "Original Flag submission still requires reconciliation")
        old_round = verification_round(task, challenge)
        prepared = (
            (
                await conn.execute(
                    select(flags.c.data, flags.c.platform_call_id).where(
                        flags.c.task_id == tid,
                        flags.c.challenge_id == cid,
                        flags.c.verification_round == old_round,
                        flags.c.status == "prepared",
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in prepared:
            if row["platform_call_id"] is not None:
                raise HTTPException(409, "Original Flag submission has conflicting send evidence")
            submission = row["data"]
            request_id = uuid5(NAMESPACE_URL, f"scope-cancel:{tid}:{edit_id}:{submission['id']}")
            await emit(
                self.repo,
                conn,
                tid,
                "system",
                "board.flag.cancelled",
                request_id,
                digest({"scope_edit": str(edit_id), "submission": submission["id"]}),
                {**submission, "status": "cancelled", "updated_version": 0},
            )

    async def finish(self, tid, identity, request_id: UUID, conclusion):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task_access(conn, tid, identity, write=True, lead=True)
        return await self.ctf.request_finish(
            tid,
            identity.agent_id,
            request_id,
            conclusion,
            identity.generation,
            identity.turn_id,
            runtime_context=identity.runtime_context,
        )
