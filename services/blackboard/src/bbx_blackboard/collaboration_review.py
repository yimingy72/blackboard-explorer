"""A peer result opens review; only a Lead decision resumes or completes the team."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from bbx_contracts.ctf_collaboration import ReviewChallenge, SubmitChallengeReview
from bbx_contracts.models import KnowledgeAssignment, PostFactRequest
from fastapi import HTTPException
from sqlalchemy import select

from bbx_blackboard.board_events import digest, prior
from bbx_blackboard.collaboration_board import CollaborationBoardService
from bbx_blackboard.domain.challenge_collaboration import clock_state, collaboration, participants
from bbx_blackboard.domain.rules import fact_events
from bbx_blackboard.store import schema as s


def require_result_revision(challenge, expected):
    current = challenge["revision"]
    state = collaboration(challenge)
    if expected == current or (
        challenge["work_status"] == "awaiting_review"
        and state.review_base_revision <= expected < current
    ):
        return
    raise HTTPException(409, {"code": "revision_conflict", "current_revision": current})


class CollaborationReview(CollaborationBoardService):
    async def _references(self, conn, tid, cid, facts, candidates):
        found_facts = set(
            (
                await conn.scalars(
                    select(s.facts.c.id).where(
                        s.facts.c.task_id == tid,
                        s.facts.c.challenge_id == str(cid),
                        s.facts.c.id.in_(facts),
                    )
                )
            ).all()
        )
        found_candidates = set(
            (
                await conn.scalars(
                    select(s.flag_candidates.c.id).where(
                        s.flag_candidates.c.task_id == tid,
                        s.flag_candidates.c.challenge_id == str(cid),
                        s.flag_candidates.c.id.in_([str(value) for value in candidates]),
                    )
                )
            ).all()
        )
        if found_facts != set(facts) or found_candidates != {str(value) for value in candidates}:
            raise HTTPException(403, "Review evidence is outside this challenge")

    async def mark_review(
        self, conn, tid, task, challenge, aid, request_id, summary, facts, candidates
    ):
        candidates = [UUID(str(value)) for value in candidates]
        if challenge["work_status"] not in {"in_progress", "awaiting_review"}:
            raise HTTPException(409, "Challenge is not accepting another review")
        await self._references(conn, tid, challenge["id"], facts, candidates)
        state = clock_state(challenge, datetime.now(UTC), running=False)
        if challenge["work_status"] == "in_progress":
            state.review_revision += 1
            state.review_base_revision = challenge["revision"]
            state.review_fact_refs = []
            state.review_candidate_ids = []
        state.review_summary, state.review_author = summary, aid
        state.review_fact_refs = list(dict.fromkeys([*state.review_fact_refs, *facts]))
        state.review_candidate_ids = list(dict.fromkeys([*state.review_candidate_ids, *candidates]))
        value = {
            **challenge,
            "work_status": "awaiting_review",
            "revision": challenge["revision"] + 1,
            "collaboration": state.model_dump(mode="json"),
        }
        hashed = digest({"review": value, "request_id": str(request_id)})
        await self.ctf._board_event(
            conn,
            tid,
            aid,
            "ctf.record.appended",
            request_id,
            hashed,
            {
                "id": str(uuid5(NAMESPACE_URL, f"peer-review-result:{tid}:{aid}:{request_id}")),
                "task_id": str(tid),
                "challenge_id": challenge["id"],
                "author_id": aid,
                "kind": "note",
                "body": summary,
                "verification": {
                    "status": "awaiting_review",
                    "review_revision": state.review_revision,
                    "candidate_ids": [str(value) for value in candidates],
                    "fact_refs": facts,
                },
            },
        )
        await self.ctf._board_event(
            conn, tid, aid, "ctf.challenge.updated", request_id, hashed, value
        )
        return value

    async def submit(self, tid, identity, request):
        body = SubmitChallengeReview.model_validate(request)
        aid = self._agent(identity, tid)
        hashed = digest({"operation": "submit_peer_review", **body.model_dump(mode="json")})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, challenge, assignment = await self._authority(
                conn, tid, body.challenge_id, identity, write=True
            )
            if assignment is None:
                raise HTTPException(403, "Only current Workers submit challenge results")
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            require_result_revision(challenge, body.expected_revision)
            value = await self.mark_review(
                conn,
                tid,
                task,
                challenge,
                aid,
                body.request_id,
                body.summary,
                body.fact_refs,
                body.candidate_ids,
            )
            from bbx_blackboard.board_events import emit

            return await emit(
                self.repo,
                conn,
                tid,
                aid,
                "board.request.linked",
                body.request_id,
                hashed,
                value,
                extra={"challenge_id": str(body.challenge_id)},
            )

    async def _check_decision(self, conn, tid, identity, body):
        from bbx_blackboard.flag_board import verification_round

        task, _ = await self.access(conn, tid, identity, lead=True)
        challenge = await self.ctf._challenge(conn, tid, body.challenge_id)
        self.ctf._revision(challenge, body.expected_revision)
        state = collaboration(challenge)
        if (
            challenge["work_status"] != "awaiting_review"
            or state.review_revision != body.review_revision
        ):
            raise HTTPException(409, "The review round or challenge state changed")
        await self._references(conn, tid, body.challenge_id, body.fact_refs, body.candidate_ids)
        if state.review_candidate_ids and not body.candidate_ids:
            raise HTTPException(422, "Identify the Flag candidates covered by this decision")
        if not set(body.candidate_ids) <= set(state.review_candidate_ids):
            raise HTTPException(409, "Candidate is outside this review round")
        submissions = list(
            (
                await conn.scalars(
                    select(s.flag_submissions.c.data).where(
                        s.flag_submissions.c.task_id == tid,
                        s.flag_submissions.c.challenge_id == str(body.challenge_id),
                        s.flag_submissions.c.id.in_([str(value) for value in body.submission_ids]),
                    )
                )
            ).all()
        )
        if {value["id"] for value in submissions} != {str(value) for value in body.submission_ids}:
            raise HTTPException(403, "Verification receipt is outside this challenge")
        if any(
            value["candidate_id"] not in {str(cid) for cid in body.candidate_ids}
            for value in submissions
        ):
            raise HTTPException(409, "Receipt does not verify the selected candidates")
        if any(
            value["verification_round"] != verification_round(task, challenge)
            for value in submissions
        ):
            raise HTTPException(409, "Verification receipt belongs to an earlier challenge scope")
        if body.decision == "accepted":
            platform = (challenge.get("target") or {}).get("source") == "platform"
            if platform or body.submission_ids:
                if not any(
                    value["status"] in {"accepted", "duplicate"}
                    and value.get("progress_applied")
                    and (value.get("result") or {}).get("total_count") is not None
                    and value["result"]["correct_count"] == value["result"]["total_count"]
                    for value in submissions
                ):
                    raise HTTPException(409, "Complete trusted platform verification is required")
            elif not body.fact_refs and not body.candidate_ids:
                raise HTTPException(422, "Accepted work requires saved result evidence")
        return task, challenge

    async def review(self, tid, identity, request):
        body = ReviewChallenge.model_validate(request)
        aid = self._agent(identity, tid)
        hashed = digest({"operation": "review_peer_challenge", **body.model_dump(mode="json")})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self.access(conn, tid, identity, lead=True)
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            await self._check_decision(conn, tid, identity, body)
        raw = json.dumps(body.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode()
        checksum = hashlib.sha256(raw).hexdigest()
        uri = f"evidence/{tid}/lead/review-{checksum}.json"
        if self.ctf.objects is None:
            raise HTTPException(503, "Review evidence storage is unavailable")
        try:
            async with asyncio.timeout(10):
                await self.ctf.objects.put(uri, raw, content_type="application/json")
        except TimeoutError as error:
            raise HTTPException(503, "Review evidence upload timed out") from error
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self.access(conn, tid, identity, lead=True)
            if previous := await prior(conn, tid, aid, body.request_id, hashed):
                return previous
            task, challenge = await self._check_decision(conn, tid, identity, body)
            state = await self.repo.load(
                conn, tid, include_tool_calls=False, challenge_id=str(body.challenge_id)
            )
            assert identity.generation is not None and identity.turn_id is not None
            origin = KnowledgeAssignment(
                agent_id=aid,
                generation=identity.generation,
                turn_id=identity.turn_id,
                epoch=task["ctf_control"]["epoch"],
                owner_id=challenge["owner_id"],
                scope_revision=challenge.get("scope_revision", 1),
            ).model_dump(mode="json")
            data = PostFactRequest.model_validate(
                {
                    "kind": "observation",
                    "statement": body.reason,
                    "derived_from": body.fact_refs,
                    "evidence": [
                        {
                            "type": "text",
                            "uri": uri,
                            "sha256": checksum,
                            "size": len(raw),
                            "summary": "Persisted Lead verification decision",
                            "auto": True,
                        }
                    ],
                }
            ).model_dump(mode="json")
            data.update(challenge_id=str(body.challenge_id), assignment_ref=origin)
            events = fact_events(state, aid, data, resolvable_intents=set())
            for event in events:
                event["addressed_to"] = sorted({"lead", *participants(task, body.challenge_id)})
                event["payload"].update(
                    challenge_id=str(body.challenge_id),
                    challenge_revision=challenge["revision"],
                    scope_revision=challenge.get("scope_revision", 1),
                )
            saved = await self.repo.append(conn, tid, events)
            feedback = saved[0]["object_id"]
            config = collaboration(challenge)
            config.feedback_fact_id = feedback
            config.solve_started_at = datetime.now(UTC) if body.decision == "rejected" else None
            value = {
                **challenge,
                "work_status": "completed" if body.decision == "accepted" else "in_progress",
                "revision": challenge["revision"] + 1,
                "collaboration": config.model_dump(mode="json"),
                "verification": {
                    **challenge.get("verification", {}),
                    "status": body.decision,
                    "source": "platform" if body.submission_ids else "lead",
                    "summary": body.reason,
                    "response_uri": uri,
                    "submission_id": str(body.submission_ids[0]) if body.submission_ids else None,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            }
            return await self._save(conn, tid, aid, body, hashed, value)
