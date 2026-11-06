"""Task-wide immutable announcements and the versioned Lead business boundary."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from bbx_contracts.challenge_board import (
    AnnouncementRequest,
    BoardChallengeRequest,
    business_manifest,
)
from bbx_contracts.ctf import CtfChallenge, CtfVerification
from fastapi import HTTPException
from sqlalchemy import select

from bbx_blackboard.board_events import digest, emit, prior
from bbx_blackboard.challenge_board import ChallengeBoardService, knowledge_event_scope
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
            await self._task_access(conn, tid, identity)
            scope = (
                await knowledge_event_scope(self.repo, conn, tid, identity.agent_id)
                if identity.kind == "agent"
                else None
            )
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

    async def finish(self, tid, identity, request_id: UUID, conclusion):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self._task_access(conn, tid, identity, write=True, lead=True)
        return await self.ctf.request_finish(
            tid, identity.agent_id, request_id, conclusion, identity.generation, identity.turn_id
        )
