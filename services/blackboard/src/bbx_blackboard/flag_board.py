"""Protected Flag candidates and single-dispatch business submissions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from bbx_contracts.challenge_board import (
    BOARD_CONTRACT,
    FlagCandidateRequest,
    FlagCaptureRequest,
    FlagDispatchRequest,
    FlagReceipt,
    FlagReceiptRequest,
    FlagResult,
    FlagSubmissionRequest,
    FlagUnknownRequest,
    business_contract,
)
from bbx_contracts.models import KnowledgeAssignment
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import insert, select

from bbx_blackboard.auth import Principal
from bbx_blackboard.board_events import digest, emit, prior
from bbx_blackboard.challenge_board import ChallengeBoardService
from bbx_blackboard.profiles import ctf_profile
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import row


def target_reference(challenge):
    target = challenge.get("target") or {}
    registered = (
        {key: target.get(key) for key in ("provider", "external_target_id", "resource_version")}
        if target.get("source") == "platform"
        else None
    )
    return {"external_id": challenge.get("external_id"), "registered_target": registered}


def verification_round(task, challenge):
    return "v1:" + digest(
        {
            "task_run": task["run_number"],
            "scope_revision": challenge.get("scope_revision", 1),
            "target_ref": target_reference(challenge),
        }
    )


class FlagBoardService(ChallengeBoardService):
    def __init__(self, ctf, signing_key: str):
        super().__init__(ctf)
        material = signing_key.encode()
        self.cipher = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(b"bbx-board-values-v1\0" + material).digest())
        )
        self.receipt_key = hashlib.sha256(b"bbx-board-receipts-v1\0" + material).digest()

    @staticmethod
    def _v1(task):
        if task["mode"] != "ctf" or business_contract(task) != BOARD_CONTRACT or task["deleting"]:
            raise HTTPException(409, "Available challenge-board-v1 task required")

    @staticmethod
    def _service(identity):
        if identity.kind != "service":
            raise HTTPException(403, "Trusted service identity required")

    async def _candidate(self, conn, tid, candidate_id):
        found = (
            (
                await conn.execute(
                    select(s.flag_candidates).where(
                        s.flag_candidates.c.task_id == tid,
                        s.flag_candidates.c.id == str(candidate_id),
                    )
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            raise HTTPException(404, "Flag candidate not found")
        return dict(found)

    async def _submission(self, conn, tid, submission_id):
        found = (
            (
                await conn.execute(
                    select(s.flag_submissions).where(
                        s.flag_submissions.c.task_id == tid,
                        s.flag_submissions.c.id == str(submission_id),
                    )
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            raise HTTPException(404, "Flag submission not found")
        return dict(found["data"])

    def _origin(self, task, challenge, identity, snapshot):
        assert (
            identity.agent_id and identity.generation is not None and identity.turn_id is not None
        )
        return KnowledgeAssignment(
            agent_id=identity.agent_id,
            generation=identity.generation,
            turn_id=identity.turn_id,
            epoch=snapshot.get("epoch", task["ctf_control"]["epoch"])
            if snapshot
            else task["ctf_control"]["epoch"],
            owner_id=challenge.get("owner_id"),
            scope_revision=challenge.get("scope_revision", 1),
            assignment_id=snapshot.get("assignment_ref") if snapshot else None,
        ).model_dump(mode="json")

    async def candidate(self, tid, cid, identity, request):
        body = FlagCandidateRequest.model_validate(request)
        value = body.value.get_secret_value()
        value_hash = hashlib.sha256(value.encode()).hexdigest()
        aid = self._agent(identity, tid)
        command = {
            "operation": "flag_candidate",
            "challenge_id": str(cid),
            **body.model_dump(mode="json", exclude={"value"}),
            "value_digest": value_hash,
        }
        hashed = digest(command)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, challenge, snapshot = await self._authority(conn, tid, cid, identity, write=True)
            self._v1(task)
            member = await row(conn, s.ctf_members, tid, aid)
            if challenge.get("owner_id") != aid or member["role"] != "teammate":
                raise HTTPException(403, "Only the current Worker Assignment may record a Flag")
            if old := await prior(conn, tid, aid, body.request_id, hashed):
                return old
            self.ctf._revision(challenge, body.expected_revision)
            if challenge["work_status"] in {"completed", "cancelled"}:
                raise HTTPException(409, "Challenge is not accepting candidates")
            refs = await self.ctf._artifact_refs(conn, tid, body.evidence_refs, cid)
            facts = set(
                (
                    await conn.execute(
                        select(s.facts.c.id).where(
                            s.facts.c.task_id == tid,
                            s.facts.c.challenge_id == str(cid),
                            s.facts.c.id.in_(body.fact_refs),
                        )
                    )
                ).scalars()
            )
            if facts != set(body.fact_refs):
                raise HTTPException(403, "Fact evidence is outside this challenge")
            round_id = verification_round(task, challenge)
            candidate_id = str(
                uuid5(NAMESPACE_URL, f"board-flag:{tid}:{cid}:{round_id}:{value_hash}")
            )
            existing = (
                await conn.execute(
                    select(s.flag_candidates.c.data).where(
                        s.flag_candidates.c.task_id == tid, s.flag_candidates.c.id == candidate_id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return await emit(
                    self.repo,
                    conn,
                    tid,
                    aid,
                    "board.request.linked",
                    body.request_id,
                    hashed,
                    existing,
                )
            public = {
                "id": candidate_id,
                "task_id": str(tid),
                "challenge_id": str(cid),
                "verification_round": round_id,
                "value_digest": value_hash,
                "author_id": aid,
                "assignment_ref": self._origin(task, challenge, identity, snapshot),
                "evidence_refs": refs,
                "fact_refs": body.fact_refs,
                "created_at": datetime.now(UTC).isoformat(),
                "created_version": 0,
            }
            await conn.execute(
                insert(s.flag_candidates).values(
                    task_id=tid,
                    id=candidate_id,
                    challenge_id=str(cid),
                    verification_round=round_id,
                    value_digest=value_hash,
                    value_ciphertext=self.cipher.encrypt(value.encode()).decode(),
                    data=public,
                    version=0,
                )
            )
            saved = await emit(
                self.repo, conn, tid, aid, "board.flag.candidate", body.request_id, hashed, public
            )
            await self.ctf._message(
                conn,
                tid,
                "system",
                "lead",
                json.dumps(
                    {
                        "candidate_id": candidate_id,
                        "challenge_id": str(cid),
                        "value_digest": value_hash,
                        "created_version": saved["created_version"],
                    }
                ),
                str(uuid5(NAMESPACE_URL, f"flag-candidate-notice:{tid}:{candidate_id}")),
                "notification",
                notification_purpose="flag_candidate",
                challenge_id=str(cid),
            )
            return saved

    async def value(self, tid, cid, candidate_id, identity):
        async with self.repo.engine.connect() as conn:
            _, member = await self._task_access(conn, tid, identity)
            candidate = await self._candidate(conn, tid, candidate_id)
            if candidate["challenge_id"] != str(cid):
                raise HTTPException(404, "Flag candidate not found")
            allowed = identity.kind == "user" or (member is not None and member["role"] == "lead")
            if identity.kind == "agent" and not allowed:
                await self._authority(conn, tid, cid, identity)
                allowed = candidate["data"]["author_id"] == identity.agent_id
            if not allowed:
                raise HTTPException(403, "Protected Flag value access denied")
            return {
                "candidate_id": str(candidate_id),
                "value": self.cipher.decrypt(candidate["value_ciphertext"].encode()).decode(),
            }

    async def _source(self, conn, task, source):
        profile_row = (
            (
                await conn.execute(
                    select(s.agent_profiles).where(
                        s.agent_profiles.c.name == task["agent_profile"],
                        s.agent_profiles.c.version == task["agent_profile_version"],
                    )
                )
            )
            .mappings()
            .first()
        )
        if profile_row is None:
            raise HTTPException(501, "Submission source is unsupported")
        profile = ctf_profile(dict(profile_row))
        binding = next(
            (
                item
                for item in profile.platform_tools
                if item.server_name == source.server_name
                and item.server_version == source.server_version
                and item.tool_name == source.tool_name
                and item.purpose == "submit"
            ),
            None,
        )
        server = (
            (
                await conn.execute(
                    select(s.platform_configs.c.enabled, s.platform_configs.c.deleted).where(
                        s.platform_configs.c.kind == "mcp-servers",
                        s.platform_configs.c.name == source.server_name,
                        s.platform_configs.c.version == source.server_version,
                    )
                )
            )
            .mappings()
            .first()
        )
        if (
            binding is None
            or binding.result_adapter != "flag_receipt_v1"
            or server is None
            or not server["enabled"]
            or server["deleted"]
        ):
            raise HTTPException(501, "Verifiable flag_receipt_v1 source is unsupported or disabled")

    async def prepare(self, tid, cid, identity, request):
        body = FlagSubmissionRequest.model_validate(request)
        aid = self._agent(identity, tid)
        hashed = digest(
            {
                "operation": "prepare_flag_submission",
                "challenge_id": str(cid),
                **body.model_dump(mode="json"),
            }
        )
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task, challenge, snapshot = await self._authority(conn, tid, cid, identity, write=True)
            self._v1(task)
            member = await row(conn, s.ctf_members, tid, aid)
            if member["role"] != "lead":
                raise HTTPException(403, "Only Lead may initiate submission")
            if old := await prior(conn, tid, aid, body.request_id, hashed):
                return old
            self.ctf._revision(challenge, body.expected_revision)
            candidate = await self._candidate(conn, tid, body.candidate_id)
            if candidate["challenge_id"] != str(cid) or candidate[
                "verification_round"
            ] != verification_round(task, challenge):
                raise HTTPException(
                    409, "Candidate target version is stale or outside this challenge"
                )
            await self._source(conn, task, body.source)
            existing = (
                await conn.execute(
                    select(s.flag_submissions.c.data).where(
                        s.flag_submissions.c.task_id == tid,
                        s.flag_submissions.c.candidate_id == str(body.candidate_id),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing["source"] != body.source.model_dump(mode="json"):
                    raise HTTPException(409, "Candidate already has a submission source")
                return await emit(
                    self.repo,
                    conn,
                    tid,
                    aid,
                    "board.request.linked",
                    body.request_id,
                    hashed,
                    existing,
                )
            if challenge["work_status"] in {"completed", "cancelled"}:
                raise HTTPException(409, "Challenge is not accepting submissions")
            other_source = (
                await conn.execute(
                    select(s.flag_submissions.c.data)
                    .where(
                        s.flag_submissions.c.task_id == tid,
                        s.flag_submissions.c.challenge_id == str(cid),
                        s.flag_submissions.c.verification_round == candidate["verification_round"],
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if other_source is not None and other_source["source"] != body.source.model_dump(
                mode="json"
            ):
                raise HTTPException(409, "Verification round is bound to another source")
            submission_id = str(uuid5(NAMESPACE_URL, f"flag-submission:{tid}:{body.candidate_id}"))
            operation_id = str(uuid5(NAMESPACE_URL, f"flag-operation:{tid}:{body.candidate_id}"))
            public = {
                "id": submission_id,
                "operation_id": operation_id,
                "task_id": str(tid),
                "challenge_id": str(cid),
                "candidate_id": str(body.candidate_id),
                "verification_round": candidate["verification_round"],
                "source": body.source.model_dump(mode="json"),
                "target_ref": target_reference(challenge),
                "author_id": aid,
                "assignment_ref": self._origin(task, challenge, identity, snapshot),
                "status": "prepared",
                "platform_call_id": None,
                "result": None,
                "response_uri": None,
                "response_sha256": None,
                "upstream_response_uri": None,
                "upstream_response_sha256": None,
                "progress_applied": False,
                "late": False,
                "created_at": datetime.now(UTC).isoformat(),
                "created_version": 0,
                "updated_version": 0,
            }
            public["request_digest"] = digest(
                {
                    key: public[key]
                    for key in (
                        "task_id",
                        "challenge_id",
                        "candidate_id",
                        "verification_round",
                        "source",
                        "target_ref",
                        "operation_id",
                    )
                }
                | {"value_digest": candidate["value_digest"]}
            )
            return await emit(
                self.repo, conn, tid, aid, "board.flag.submission", body.request_id, hashed, public
            )

    async def dispatch(self, tid, submission_id, identity, request):
        self._service(identity)
        body = FlagDispatchRequest.model_validate(request)
        hashed = digest(
            {"operation": "dispatch", "id": str(submission_id), **body.model_dump(mode="json")}
        )
        if not body.platform_call_id.strip() or "\x00" in body.platform_call_id:
            raise HTTPException(422, "Platform call ID is empty")
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            self._v1(task)
            if old := await prior(conn, tid, "service", body.request_id, hashed):
                return {"submission": old, "send_allowed": False}
            submission = await self._submission(conn, tid, submission_id)
            if submission["status"] != "prepared":
                return {"submission": submission, "send_allowed": False}
            if task["status"] != "running" or task["ctf_control"]["phase"] != "running":
                saved = await emit(
                    self.repo,
                    conn,
                    tid,
                    "service",
                    "board.flag.cancelled",
                    body.request_id,
                    hashed,
                    {**submission, "status": "cancelled", "updated_version": 0},
                )
                return {"submission": saved, "send_allowed": False}
            if (
                task["ctf_control"].get("failure_hold")
                or task["ctf_control"].get("replacement", {}).get("phase", "ready") != "ready"
            ):
                return {"submission": submission, "send_allowed": False}
            # A challenge's cumulative receipt must settle before its next external submission.
            unresolved = await conn.execute(
                select(s.flag_submissions.c.id)
                .where(
                    s.flag_submissions.c.task_id == tid,
                    s.flag_submissions.c.challenge_id == submission["challenge_id"],
                    s.flag_submissions.c.verification_round == submission["verification_round"],
                    s.flag_submissions.c.id != submission["id"],
                    s.flag_submissions.c.status.in_(("dispatched", "unknown")),
                )
                .limit(1)
            )
            if unresolved.first() is not None:
                return {"submission": submission, "send_allowed": False}
            challenge = await self.ctf._challenge(conn, tid, submission["challenge_id"])
            if (
                challenge["work_status"] in {"cancelled", "completed"}
                or challenge.get("tombstone")
                or submission["verification_round"] != verification_round(task, challenge)
                or submission["target_ref"] != target_reference(challenge)
                or submission["assignment_ref"]["epoch"] != task["ctf_control"]["epoch"]
            ):
                saved = await emit(
                    self.repo,
                    conn,
                    tid,
                    "service",
                    "board.flag.cancelled",
                    body.request_id,
                    hashed,
                    {**submission, "status": "cancelled", "updated_version": 0},
                )
                return {"submission": saved, "send_allowed": False}
            from bbx_contracts.challenge_board import FlagSource

            await self._source(conn, task, FlagSource.model_validate(submission["source"]))
            occupied = await conn.execute(
                select(s.flag_submissions.c.id)
                .where(
                    s.flag_submissions.c.task_id == tid,
                    s.flag_submissions.c.platform_call_id == body.platform_call_id,
                )
                .limit(1)
            )
            if occupied.first() is not None:
                raise HTTPException(409, "Platform call ID is already bound")
            candidate = await self._candidate(conn, tid, submission["candidate_id"])
            value = self.cipher.decrypt(candidate["value_ciphertext"].encode()).decode()
            if await self.ctf._dispatch(conn, tid, body.platform_call_id) is not None:
                raise HTTPException(409, "Platform call ID has another immutable dispatch")
            await self.repo.append(
                conn,
                tid,
                [
                    {
                        "type": "ctf.platform.dispatched",
                        "actor": submission["author_id"],
                        "object_id": body.platform_call_id,
                        "addressed_to": [submission["author_id"]],
                        "payload": {
                            "call_id": body.platform_call_id,
                            "agent_id": submission["author_id"],
                            "turn_id": submission["assignment_ref"]["turn_id"],
                            "generation": submission["assignment_ref"]["generation"],
                            "challenge_id": submission["challenge_id"],
                            "expected_revision": challenge["revision"],
                            **submission["source"],
                            "business_operation_id": submission["operation_id"],
                            "submission_id": submission["id"],
                            "candidate_id": submission["candidate_id"],
                            "request_digest": submission["request_digest"],
                            "verification_round": submission["verification_round"],
                            "target_ref": submission["target_ref"],
                        },
                    }
                ],
            )
            updated = {
                **submission,
                "status": "dispatched",
                "platform_call_id": body.platform_call_id,
                "updated_version": 0,
            }
            saved = await emit(
                self.repo,
                conn,
                tid,
                "service",
                "board.flag.dispatched",
                body.request_id,
                hashed,
                updated,
            )
            return {
                "submission": saved,
                "send_allowed": True,
                "payload": {
                    "operation_id": saved["operation_id"],
                    "candidate_id": saved["candidate_id"],
                    "source": saved["source"],
                    "target_ref": saved["target_ref"],
                    "request_digest": saved["request_digest"],
                    "value": value,
                },
            }

    async def unknown(self, tid, submission_id, identity, request):
        self._service(identity)
        body = FlagUnknownRequest.model_validate(request)
        hashed = digest(
            {"operation": "unknown", "id": str(submission_id), **body.model_dump(mode="json")}
        )
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            self._v1(task)
            if old := await prior(conn, tid, "service", body.request_id, hashed):
                return old
            value = await self._submission(conn, tid, submission_id)
            if value["platform_call_id"] != body.platform_call_id or value["status"] == "prepared":
                raise HTTPException(409, "Unknown outcome has no matching dispatch")
            if value["status"] not in {"dispatched", "unknown"}:
                return value
            return await emit(
                self.repo,
                conn,
                tid,
                "service",
                "board.flag.unknown",
                body.request_id,
                hashed,
                {**value, "status": "unknown", "updated_version": 0},
            )

    async def _capture_record(self, conn, tid, call_id):
        found = (
            await conn.execute(
                select(s.events.c.payload)
                .where(
                    s.events.c.task_id == tid,
                    s.events.c.type == "ctf.platform.result_captured",
                    s.events.c.object_id == call_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return found["result"] if found else None

    async def capture(self, tid, identity, request):
        """The trusted transport passes its own call context and raw response, never a URI."""
        self._service(identity)
        body = FlagCaptureRequest.model_validate(request)
        raw = body.response_text.encode()
        if len(raw) > 65536:
            raise HTTPException(413, "Raw platform result exceeds size limit")
        sha = hashlib.sha256(raw).hexdigest()
        hashed = digest(
            {
                "operation": "capture",
                **body.model_dump(mode="json", exclude={"response_text"}),
                "response_sha256": sha,
            }
        )
        async with self.repo.engine.connect() as conn:
            self._v1(await row(conn, s.tasks, tid))
            dispatch = await self.ctf._dispatch(conn, tid, body.platform_call_id)
            if (
                dispatch is None
                or dispatch.get("request_digest") != body.request_digest
                or any(
                    dispatch.get(key) != value
                    for key, value in body.source.model_dump(mode="json").items()
                )
            ):
                raise HTTPException(409, "Capture does not match the immutable platform dispatch")
            if old := await prior(conn, tid, "service", body.request_id, hashed):
                return old
            existing = await self._capture_record(conn, tid, body.platform_call_id)
            if existing:
                if existing["response_sha256"] != sha:
                    raise HTTPException(409, "Platform call capture is immutable")
                return existing
        capture_key = digest(
            {"call_id": body.platform_call_id, "request_digest": body.request_digest}
        )
        uri = f"evidence/{tid}/platform/{capture_key}-{sha}.json"
        if self.ctf.objects is None:
            raise HTTPException(503, "Receipt storage is unavailable")
        try:
            async with asyncio.timeout(10):
                await self.ctf.objects.put(uri, raw, content_type="application/json")
        except TimeoutError as error:
            raise HTTPException(503, "Platform result capture timed out") from error
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            self._v1(await row(conn, s.tasks, tid))
            if old := await prior(conn, tid, "service", body.request_id, hashed):
                return old
            confirmed = await self.ctf._dispatch(conn, tid, body.platform_call_id)
            if confirmed != dispatch:
                raise HTTPException(409, "Platform dispatch changed during capture")
            existing = await self._capture_record(conn, tid, body.platform_call_id)
            if existing:
                if existing["response_sha256"] != sha:
                    raise HTTPException(409, "Platform call capture is immutable")
                return existing
            return await emit(
                self.repo,
                conn,
                tid,
                "service",
                "ctf.platform.result_captured",
                body.request_id,
                hashed,
                {
                    "id": body.platform_call_id,
                    "platform_call_id": body.platform_call_id,
                    "submission_id": dispatch["submission_id"],
                    "operation_id": dispatch["business_operation_id"],
                    "candidate_id": dispatch["candidate_id"],
                    "source": body.source.model_dump(mode="json"),
                    "request_digest": body.request_digest,
                    "response_uri": uri,
                    "response_sha256": sha,
                },
            )

    async def _seal_verified_receipt(self, tid, submission_id):
        """Trusted adapter hook, deliberately absent from every HTTP/model tool route."""
        async with self.repo.engine.connect() as conn:
            value = await self._submission(conn, tid, submission_id)
            if value["platform_call_id"] is None:
                raise HTTPException(409, "Receipt requires a registered dispatch")
            captured = await self._capture_record(conn, tid, value["platform_call_id"])
            if (
                captured is None
                or captured["submission_id"] != value["id"]
                or captured["request_digest"] != value["request_digest"]
            ):
                raise HTTPException(409, "Receipt has no matching immutable transport capture")
        upstream_uri, upstream_sha256 = captured["response_uri"], captured["response_sha256"]
        raw_result = await self._read_receipt_bytes(upstream_uri, upstream_sha256)
        try:
            parsed = FlagResult.model_validate_json(raw_result)
        except ValidationError as error:
            await self.unknown(
                tid,
                submission_id,
                Principal("service", "flag-receipt-adapter"),
                {
                    "request_id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"board-invalid-capture:{tid}:{submission_id}:{upstream_sha256}",
                        )
                    ),
                    "platform_call_id": value["platform_call_id"],
                },
            )
            raise HTTPException(
                422, "Captured platform result is unsupported; outcome is unknown"
            ) from error
        payload = FlagReceipt(
            operation_id=value["operation_id"],
            submission_id=value["id"],
            candidate_id=value["candidate_id"],
            platform_call_id=value["platform_call_id"],
            source=value["source"],
            target_ref=value["target_ref"],
            request_digest=value["request_digest"],
            verification_round=value["verification_round"],
            upstream_response_uri=upstream_uri,
            upstream_response_sha256=upstream_sha256,
            result=parsed,
        ).model_dump(mode="json")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return json.dumps(
            {
                "payload": payload,
                "signature": hmac.new(self.receipt_key, raw, hashlib.sha256).hexdigest(),
            },
            sort_keys=True,
        ).encode()

    @staticmethod
    def _receipt_uri(tid, uri):
        if (
            not uri.startswith(f"evidence/{tid}/")
            or len(uri.split("/")) != 4
            or ".." in uri.split("/")
        ):
            raise HTTPException(403, "Receipt belongs to another task")

    async def _read_receipt_bytes(self, uri, sha):
        if self.ctf.objects is None:
            raise HTTPException(503, "Receipt storage is unavailable")
        chunks, size = [], 0
        try:
            async with asyncio.timeout(10):
                async for chunk in self.ctf.objects.stream(uri):
                    size += len(chunk)
                    if size > 65536:
                        raise HTTPException(413, "Receipt exceeds size limit")
                    chunks.append(chunk)
        except TimeoutError as error:
            raise HTTPException(503, "Receipt storage check timed out") from error
        raw = b"".join(chunks)
        if hashlib.sha256(raw).hexdigest() != sha:
            raise HTTPException(422, "Receipt integrity mismatch")
        return raw

    async def _load_receipt(self, tid, uri, sha):
        raw = await self._read_receipt_bytes(uri, sha)
        try:
            envelope = json.loads(raw)
            payload = envelope["payload"]
            signed = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            if not hmac.compare_digest(
                envelope["signature"],
                hmac.new(self.receipt_key, signed, hashlib.sha256).hexdigest(),
            ):
                raise ValueError("Invalid attestation")
            receipt = FlagReceipt.model_validate(payload)
            self._receipt_uri(tid, receipt.upstream_response_uri)
            original = await self._read_receipt_bytes(
                receipt.upstream_response_uri, receipt.upstream_response_sha256
            )
            if FlagResult.model_validate_json(original) != receipt.result:
                raise ValueError("Adapter result differs from the saved upstream receipt")
            return receipt
        except (ValueError, KeyError, TypeError, ValidationError) as error:
            raise HTTPException(422, "Verifiable source attestation is missing") from error

    async def receipt(self, tid, submission_id, identity, request):
        self._service(identity)
        body = FlagReceiptRequest.model_validate(request)
        hashed = digest(
            {"operation": "receipt", "id": str(submission_id), **body.model_dump(mode="json")}
        )
        if (
            not body.response_uri.startswith(f"evidence/{tid}/")
            or len(body.response_uri.split("/")) != 4
            or ".." in body.response_uri.split("/")
        ):
            raise HTTPException(403, "Receipt belongs to another task")
        async with self.repo.engine.connect() as conn:
            task = await row(conn, s.tasks, tid)
            self._v1(task)
            if old := await prior(conn, tid, "service", body.request_id, hashed):
                return old
            stored = await self._submission(conn, tid, submission_id)
            if stored["platform_call_id"] is None:
                raise HTTPException(409, "No registered dispatch")
        try:
            receipt = await self._load_receipt(tid, body.response_uri, body.response_sha256)
        except HTTPException as error:
            if error.status_code not in {413, 422}:
                raise
            async with self.repo.engine.begin() as conn:
                await self.repo.lock(conn, tid)
                self._v1(await row(conn, s.tasks, tid))
                if old := await prior(conn, tid, "service", body.request_id, hashed):
                    return old
                current = await self._submission(conn, tid, submission_id)
                if current["status"] not in {"dispatched", "unknown"}:
                    raise HTTPException(
                        409, "Unverifiable receipt cannot replace a final result"
                    ) from error
                return await emit(
                    self.repo,
                    conn,
                    tid,
                    "service",
                    "board.flag.unknown",
                    body.request_id,
                    hashed,
                    {**current, "status": "unknown", "updated_version": 0},
                )
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            self._v1(task)
            if old := await prior(conn, tid, "service", body.request_id, hashed):
                return old
            stored = await self._submission(conn, tid, submission_id)
            expected = {
                "operation_id": stored["operation_id"],
                "submission_id": stored["id"],
                **{
                    key: stored[key]
                    for key in (
                        "candidate_id",
                        "platform_call_id",
                        "source",
                        "target_ref",
                        "request_digest",
                        "verification_round",
                    )
                },
            }
            if (
                receipt.model_dump(
                    mode="json",
                    exclude={"result", "upstream_response_uri", "upstream_response_sha256"},
                )
                != expected
            ):
                raise HTTPException(409, "Receipt does not match the registered operation")
            captured = await self._capture_record(conn, tid, stored["platform_call_id"])
            if (
                captured is None
                or captured["submission_id"] != stored["id"]
                or captured["response_uri"] != receipt.upstream_response_uri
                or captured["response_sha256"] != receipt.upstream_response_sha256
            ):
                raise HTTPException(
                    409, "Receipt is not from this platform call's immutable capture"
                )
            if stored["status"] in {"accepted", "rejected", "duplicate"}:
                if stored["result"] != receipt.result.model_dump(mode="json"):
                    raise HTTPException(409, "Final receipt conflict")
                return stored
            challenge = await self.ctf._challenge(conn, tid, stored["challenge_id"])
            applicable = (
                task["status"] == "running"
                and task["ctf_control"]["phase"] == "running"
                and challenge["work_status"] != "cancelled"
                and not challenge.get("tombstone")
                and verification_round(task, challenge) == stored["verification_round"]
            )
            lead = (
                (
                    await conn.execute(
                        select(s.ctf_members).where(
                            s.ctf_members.c.task_id == tid,
                            s.ctf_members.c.id == stored["author_id"],
                        )
                    )
                )
                .mappings()
                .first()
            )
            origin = stored["assignment_ref"]
            current_lead = (
                lead is not None
                and lead["generation"] == origin["generation"]
                and lead["current_turn_id"] == origin["turn_id"]
                and lead["lifecycle"] == "active"
                and not lead.get("pending_operation")
            )
            updated = {
                **stored,
                "status": receipt.result.status,
                "result": receipt.result.model_dump(mode="json"),
                "response_uri": body.response_uri,
                "response_sha256": body.response_sha256,
                "upstream_response_uri": receipt.upstream_response_uri,
                "upstream_response_sha256": receipt.upstream_response_sha256,
                "progress_applied": applicable and receipt.result.status != "unknown",
                "late": not applicable or not current_lead,
                "updated_version": 0,
            }
            change = None
            if (
                updated["progress_applied"]
                and receipt.result.status in {"accepted", "duplicate"}
                and receipt.result.total_count is not None
                and receipt.result.correct_count == receipt.result.total_count
            ):
                from bbx_contracts.ctf import CtfVerification

                change = {
                    **challenge,
                    "work_status": "completed",
                    "revision": challenge["revision"] + 1,
                    "verification": CtfVerification(
                        required=True,
                        basis="Verified complete Flag receipt",
                        status="accepted",
                        source="platform",
                        submission_id=stored["id"],
                        call_id=stored["platform_call_id"],
                        response_uri=body.response_uri,
                        updated_at=datetime.now(UTC),
                    ).model_dump(mode="json"),
                }
            saved = await emit(
                self.repo,
                conn,
                tid,
                "service",
                "board.flag.receipt",
                body.request_id,
                hashed,
                updated,
                extra={"challenge_update": change},
            )
            if applicable and current_lead:
                await self.ctf._message(
                    conn,
                    tid,
                    "system",
                    stored["author_id"],
                    json.dumps(
                        {
                            "submission_id": stored["id"],
                            "challenge_id": stored["challenge_id"],
                            "status": receipt.result.status,
                        }
                    ),
                    str(
                        uuid5(
                            NAMESPACE_URL,
                            f"flag-result:{tid}:{stored['id']}:{body.response_sha256}",
                        )
                    ),
                    "notification",
                    notification_purpose="flag_result",
                    challenge_id=stored["challenge_id"],
                )
            return saved

    async def board(self, tid, cid, identity):
        async with self.repo.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            task, challenge, _ = await self._authority(conn, tid, cid, identity)
            self._v1(task)
            round_id = verification_round(task, challenge)
            candidates = list(
                (
                    await conn.execute(
                        select(s.flag_candidates.c.data)
                        .where(
                            s.flag_candidates.c.task_id == tid,
                            s.flag_candidates.c.challenge_id == str(cid),
                        )
                        .order_by(s.flag_candidates.c.version)
                    )
                ).scalars()
            )
            submissions = list(
                (
                    await conn.execute(
                        select(s.flag_submissions.c.data)
                        .where(
                            s.flag_submissions.c.task_id == tid,
                            s.flag_submissions.c.challenge_id == str(cid),
                        )
                        .order_by(s.flag_submissions.c.version)
                    )
                ).scalars()
            )
        progress = {"correct_count": None, "total_count": None, "score": None}
        for value in submissions:
            if value["verification_round"] == round_id and value["progress_applied"]:
                for key in progress:
                    if value["result"].get(key) is not None:
                        progress[key] = value["result"][key]
        return {
            "verification_round": round_id,
            "candidates": candidates,
            "submissions": submissions,
            "progress": progress,
        }
