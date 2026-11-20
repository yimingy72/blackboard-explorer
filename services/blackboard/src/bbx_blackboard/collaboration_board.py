"""Peer challenge writes share the Task lock and durable board event transaction."""

from uuid import NAMESPACE_URL, uuid5

from bbx_contracts.ctf import CtfChallenge, CtfVerification
from bbx_contracts.ctf_collaboration import (
    MAX_CHALLENGE_WORKERS,
    ChallengeCollaboration,
    ClaimChallengeRequest,
    CollaborativeChallengeRequest,
    CollaborativeChallengeUpdate,
    RequestAssistance,
    collaboration_enabled,
)
from fastapi import HTTPException
from sqlalchemy import func, select, update

from bbx_blackboard.board_events import digest, emit, prior
from bbx_blackboard.challenge_board import ChallengeBoardService
from bbx_blackboard.domain.challenge_collaboration import available_work, collaboration
from bbx_blackboard.store import schema as s


class CollaborationBoardService(ChallengeBoardService):
    async def access(self, conn, tid, identity, *, lead=False, write=True):
        task, member = await self._task_access(conn, tid, identity, write=write, lead=lead)
        if not collaboration_enabled(task):
            raise HTTPException(409, "Peer collaboration is not published for this Task")
        return task, member

    @staticmethod
    async def all_challenges(conn, tid):
        return list(
            (
                await conn.scalars(
                    select(s.ctf_challenges.c.data).where(s.ctf_challenges.c.task_id == tid)
                )
            ).all()
        )

    async def available(self, tid, identity):
        async with self.repo.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            task, member = await self.access(conn, tid, identity, write=False)
            return {
                **available_work(task, await self.all_challenges(conn, tid)),
                "board_version": int(
                    await conn.scalar(
                        select(func.max(s.events.c.version)).where(s.events.c.task_id == tid)
                    )
                    or 0
                ),
                "current_assignment": task["ctf_control"]
                .get("native_worker_active", {})
                .get(member["id"] if member else ""),
            }

    async def _save(self, conn, tid, aid, body, hashed, value, *, created=False):
        await self.ctf._board_event(
            conn,
            tid,
            aid,
            "ctf.challenge.created" if created else "ctf.challenge.updated",
            body.request_id,
            hashed,
            value,
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

    @staticmethod
    async def _ready(conn, tid, task, challenge, readiness, priority):
        control = dict(task["ctf_control"])
        queue = dict(control.get("native_worker_ready", {}))
        sequence = control.get("native_worker_ready_sequence", 0) + 1
        current = queue.get(challenge["id"], {})
        queue[challenge["id"]] = {
            "readiness": readiness,
            "priority": priority,
            "sequence": current.get("sequence", sequence),
            "scope_revision": challenge.get("scope_revision", 1),
        }
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **control,
                    "native_worker_ready": queue,
                    "native_worker_ready_sequence": sequence if not current else sequence - 1,
                }
            )
        )

    async def create_challenge(self, tid, identity, request):
        body = CollaborativeChallengeRequest.model_validate(request)
        aid = self._agent(identity, tid)
        hashed = digest({"operation": "create_peer_challenge", **body.model_dump(mode="json")})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, _ = await self.access(conn, tid, identity, lead=True)
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            if body.external_id is not None and await conn.scalar(
                select(s.ctf_challenges.c.id)
                .where(
                    s.ctf_challenges.c.task_id == tid,
                    s.ctf_challenges.c.data["external_id"].astext == body.external_id,
                )
                .limit(1)
            ):
                raise HTTPException(409, "External challenge is already registered")
            value = CtfChallenge(
                id=uuid5(NAMESPACE_URL, f"board-challenge:{tid}:{aid}:{body.request_id}"),
                task_id=tid,
                revision=1,
                title=body.title,
                description=body.description,
                connection=body.connection,
                external_id=body.external_id,
                requirements=body.requirements,
                dispatch_status="materials_pending"
                if body.readiness == "materials_pending"
                else "ready",
                verification=CtfVerification(required=True, basis="Lead verification").model_dump(
                    mode="json"
                ),
                collaboration=ChallengeCollaboration(
                    difficulty=body.difficulty or "medium",
                    multi_stage=body.multi_stage,
                    assistance_target=1 if body.difficulty == "hard" else 0,
                ),
            ).model_dump(mode="json")
            await self._ready(conn, tid, task, value, body.readiness, body.priority)
            return await self._save(conn, tid, aid, body, hashed, value, created=True)

    async def update_challenge(self, tid, identity, request):
        body = CollaborativeChallengeUpdate.model_validate(request)
        aid = self._agent(identity, tid)
        patch = body.model_dump(mode="json", exclude_unset=True)
        hashed = digest({"operation": "update_peer_challenge", **patch})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, _ = await self.access(conn, tid, identity, lead=True)
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            challenge = await self.ctf._challenge(conn, tid, body.challenge_id)
            self.ctf._revision(challenge, body.expected_revision)
            if challenge["work_status"] in {"completed", "cancelled"}:
                raise HTTPException(409, "Challenge work is already complete")
            state = collaboration(challenge)
            for key in ("difficulty", "multi_stage"):
                if key in patch:
                    setattr(state, key, patch[key])
            if patch.get("multi_stage") and "difficulty" not in patch:
                state.difficulty = "hard"
            if state.difficulty == "hard":
                state.assistance_target = max(1, state.assistance_target)
            value = {
                **challenge,
                **{
                    key: patch[key]
                    for key in ("title", "description", "connection", "requirements")
                    if key in patch
                },
                "revision": challenge["revision"] + 1,
                "collaboration": state.model_dump(mode="json"),
            }
            if challenge["work_status"] == "awaiting_review":
                value["collaboration"]["review_base_revision"] = value["revision"]
            queued = task["ctf_control"].get("native_worker_ready", {}).get(challenge["id"], {})
            if challenge["work_status"] == "pending" and ({"readiness", "priority"} & patch.keys()):
                readiness = body.readiness or queued.get("readiness", "ready")
                priority = body.priority if body.priority is not None else queued.get("priority", 1)
                await self._ready(conn, tid, task, value, readiness, priority)
                value["dispatch_status"] = (
                    "materials_pending" if readiness == "materials_pending" else "ready"
                )
            return await self._save(conn, tid, aid, body, hashed, value)

    async def request_assistance(self, tid, identity, request):
        body = RequestAssistance.model_validate(request)
        aid = self._agent(identity, tid)
        hashed = digest({"operation": "request_assistance", **body.model_dump(mode="json")})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, challenge, assignment = await self._authority(
                conn, tid, body.challenge_id, identity, write=True
            )
            if not collaboration_enabled(task) or assignment is None:
                raise HTTPException(403, "Only current Workers may request assistance")
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            self.ctf._revision(challenge, body.expected_revision)
            if challenge["work_status"] != "in_progress":
                raise HTTPException(409, "Only solving challenges recruit assistance")
            state = collaboration(challenge)
            if state.assistance_target + body.additional_workers >= MAX_CHALLENGE_WORKERS:
                raise HTTPException(
                    409, "Each challenge supports at most 10 Workers including its owner"
                )
            state.assistance_target += body.additional_workers
            await self.ctf._board_event(
                conn,
                tid,
                aid,
                "ctf.record.appended",
                body.request_id,
                hashed,
                {
                    "id": str(uuid5(NAMESPACE_URL, f"peer-help:{tid}:{body.request_id}")),
                    "task_id": str(tid),
                    "challenge_id": str(body.challenge_id),
                    "kind": "help_request",
                    "author_id": aid,
                    "body": body.reason,
                },
            )
            return await self._save(
                conn,
                tid,
                aid,
                body,
                hashed,
                {
                    **challenge,
                    "revision": challenge["revision"] + 1,
                    "collaboration": state.model_dump(mode="json"),
                },
            )

    async def claim(self, tid, identity, request):
        from bbx_blackboard.native_worker_pool import NativeWorkerPool
        from bbx_blackboard.runner_control import RunnerControl

        body = ClaimChallengeRequest.model_validate(request)
        aid = self._agent(identity, tid)
        hashed = digest({"operation": "claim_peer_challenge", **body.model_dump(mode="json")})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, member = await self.access(conn, tid, identity)
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            assert member is not None
            if not member["has_worked"]:
                raise HTTPException(409, "First work is assigned by the platform")
            challenge = await self.ctf._challenge(conn, tid, body.challenge_id)
            self.ctf._revision(challenge, body.expected_revision)
            pool = NativeWorkerPool(self.ctf, RunnerControl(self.repo.engine))
            result = await pool.claim(conn, tid, task, member, challenge)
            return await emit(
                self.repo,
                conn,
                tid,
                aid,
                "board.request.linked",
                body.request_id,
                hashed,
                result,
                extra={"challenge_id": str(body.challenge_id)},
            )
