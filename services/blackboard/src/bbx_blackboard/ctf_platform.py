"""Explicit platform capabilities and provenance-aware CTF verification."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from bbx_contracts.ctf import CtfFakePlatformResult, CtfVerification
from sqlalchemy import select

from bbx_blackboard.ctf_board import ensure
from bbx_blackboard.profiles import ctf_profile
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import now, row


class CtfPlatformMixin:
    repo: Any
    objects: Any
    _board_write: Any
    _task: Any
    _fence: Any
    _challenge: Any
    _revision: Any
    _existing: Any
    _artifact_refs: Any
    _board_event: Any

    async def _verification_record(
        self,
        conn,
        tid,
        actor,
        request_id,
        digest,
        challenge,
        verification,
        body,
        artifact_refs=None,
        target=None,
        platform_call=None,
        expected_revision=None,
    ):
        applied = expected_revision is None or challenge["revision"] == expected_revision
        result = {
            "id": str(uuid5(NAMESPACE_URL, f"ctf-verification:{tid}:{actor}:{request_id}")),
            "task_id": str(tid),
            "challenge_id": challenge["id"],
            "author_id": actor,
            "kind": "target" if target is not None and verification is None else "verification",
            "body": body,
            "artifact_refs": artifact_refs or [],
            "verification": verification,
            "target": target,
            "platform_call": platform_call,
            "summary_applied": applied,
        }
        record = await self._board_event(
            conn, tid, actor, "ctf.record.appended", request_id, digest, result
        )
        if applied:
            updated = {**challenge, "revision": challenge["revision"] + 1}
            if verification is not None:
                updated["verification"] = verification
            if target is not None:
                updated["target"] = target
            await self._board_event(
                conn,
                tid,
                actor,
                "ctf.challenge.updated",
                f"verification:{request_id}",
                digest,
                updated,
            )
        return record

    async def set_verification_required(
        self,
        tid,
        actor,
        challenge_id,
        request_id,
        expected_revision,
        required,
        basis,
        generation=None,
        turn_id=None,
    ):
        ensure(
            actor in {"lead", "user"} and isinstance(required, bool) and bool(basis.strip()),
            "Lead must provide the verification requirement basis",
        )
        async with self.repo.engine.begin() as conn:
            await self._board_write(conn, tid, actor, generation, turn_id)
            previous, digest = await self._existing(
                conn,
                tid,
                actor,
                request_id,
                dict(
                    operation="verification_required",
                    challenge_id=str(challenge_id),
                    expected_revision=expected_revision,
                    required=required,
                    basis=basis,
                ),
            )
            if previous:
                return previous
            challenge = await self._challenge(conn, tid, challenge_id)
            self._revision(challenge, expected_revision)
            ensure(not challenge["tombstone"], "Challenge is deleted")
            verification = CtfVerification.model_validate(challenge["verification"]).model_dump(
                mode="json"
            )
            verification.update(required=required, basis=basis, updated_at=now().isoformat())
            return await self._verification_record(
                conn, tid, actor, request_id, digest, challenge, verification, basis
            )

    async def record_candidate(
        self,
        tid,
        actor,
        challenge_id,
        request_id,
        expected_revision,
        status,
        summary,
        evidence_refs=None,
        generation=None,
        turn_id=None,
    ):
        ensure(status in {"candidate", "unknown"}, "Agents can only declare candidate or unknown")
        return await self._verification_write(
            tid,
            actor,
            challenge_id,
            request_id,
            expected_revision,
            status,
            summary,
            evidence_refs,
            "agent",
            generation,
            turn_id,
        )

    async def manual_verification(
        self,
        tid,
        user_id,
        challenge_id,
        request_id,
        expected_revision,
        status,
        summary,
        evidence_refs=None,
    ):
        ensure(
            bool(user_id) and status in {"accepted", "rejected", "unknown"},
            "Invalid authenticated user confirmation",
        )
        return await self._verification_write(
            tid,
            "user",
            challenge_id,
            request_id,
            expected_revision,
            status,
            summary,
            evidence_refs,
            "user",
            None,
            None,
            user_id,
        )

    async def _verification_write(
        self,
        tid,
        actor,
        challenge_id,
        request_id,
        expected_revision,
        status,
        summary,
        evidence_refs,
        source,
        generation,
        turn_id,
        user_id=None,
    ):
        async with self.repo.engine.begin() as conn:
            await self._board_write(conn, tid, actor, generation, turn_id)
            previous, digest = await self._existing(
                conn,
                tid,
                actor,
                request_id,
                dict(
                    operation="verification",
                    challenge_id=str(challenge_id),
                    expected_revision=expected_revision,
                    status=status,
                    summary=summary,
                    evidence_refs=[str(item) for item in (evidence_refs or [])],
                    source=source,
                    user_id=user_id,
                ),
            )
            if previous:
                return previous
            challenge = await self._challenge(conn, tid, challenge_id)
            self._revision(challenge, expected_revision)
            ensure(not challenge["tombstone"], "Challenge is deleted")
            ensure(
                actor in {"lead", "user", challenge["owner_id"], *challenge["collaborator_ids"]},
                "Only assigned members may declare a candidate",
            )
            refs = await self._artifact_refs(conn, tid, evidence_refs)
            previous_state = challenge["verification"]
            verification = CtfVerification(
                required=previous_state.get("required", False),
                basis=previous_state.get("basis"),
                status=status,
                source=source,
                summary=summary,
                user_id=user_id,
                updated_at=now(),
            ).model_dump(mode="json")
            return await self._verification_record(
                conn, tid, actor, request_id, digest, challenge, verification, summary, refs
            )

    async def _platform_binding(
        self,
        conn,
        tid,
        agent_id,
        turn_id,
        generation,
        server_name,
        server_version,
        tool_name,
        allow_settlement=False,
    ):
        if allow_settlement:
            await self._task(conn, tid)
            await self._fence(conn, tid, agent_id, generation, turn_id)
        else:
            await self._board_write(conn, tid, agent_id, generation, turn_id)
        task = await row(conn, s.tasks, tid)
        member = await row(conn, s.ctf_members, tid, agent_id)
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
            .one()
        )
        profile = ctf_profile(dict(profile_row))
        allowed = next(
            (
                item
                for item in profile.worker_tools[member["role"]].mcp_servers
                if item.name == server_name and item.version == server_version
            ),
            None,
        )
        ensure(
            allowed is not None
            and (allowed.allowed_tools is None or tool_name in allowed.allowed_tools),
            "Platform tool is not in this role's fixed allowlist",
        )
        binding = next(
            (
                item
                for item in profile.platform_tools
                if item.server_name == server_name
                and item.server_version == server_version
                and item.tool_name == tool_name
            ),
            None,
        )
        ensure(binding is not None, "Platform tool has no explicit purpose binding")
        assert binding is not None
        ensure(
            binding.purpose not in {"management", "unknown"} or member["role"] == "lead",
            "Only Lead can manage targets or call unclassified tools",
        )
        return binding

    async def authorize_platform_call(
        self,
        tid,
        agent_id,
        turn_id,
        generation,
        challenge_id,
        expected_revision,
        server_name,
        server_version,
        tool_name,
    ):
        async with self.repo.engine.begin() as conn:
            binding = await self._platform_binding(
                conn, tid, agent_id, turn_id, generation, server_name, server_version, tool_name
            )
            challenge = await self._challenge(conn, tid, challenge_id)
            self._revision(challenge, expected_revision)
            ensure(not challenge["tombstone"], "Challenge is deleted")
            if binding.purpose == "submit":
                ensure(
                    agent_id in {"lead", challenge["owner_id"], *challenge["collaborator_ids"]},
                    "Submission requires assignment to this challenge",
                )
            return binding.model_dump(mode="json")

    async def begin_platform_call(
        self,
        tid,
        agent_id,
        turn_id,
        generation,
        challenge_id,
        expected_revision,
        server_name,
        server_version,
        tool_name,
        call_id,
    ):
        async with self.repo.engine.begin() as conn:
            await self._platform_binding(
                conn, tid, agent_id, turn_id, generation, server_name, server_version, tool_name
            )
            challenge = await self._challenge(conn, tid, challenge_id)
            self._revision(challenge, expected_revision)
            ensure(not challenge["tombstone"], "Challenge is deleted")
            binding = await self._platform_binding(
                conn, tid, agent_id, turn_id, generation, server_name, server_version, tool_name
            )
            if binding.purpose == "submit":
                ensure(
                    agent_id in {"lead", challenge["owner_id"], *challenge["collaborator_ids"]},
                    "Submission requires assignment",
                )
            payload = dict(
                agent_id=agent_id,
                turn_id=str(turn_id),
                generation=generation,
                challenge_id=str(challenge_id),
                expected_revision=expected_revision,
                server_name=server_name,
                server_version=server_version,
                tool_name=tool_name,
                call_id=call_id,
            )
            previous = await self._dispatch(conn, tid, call_id)
            if previous:
                ensure(previous == payload, "Platform call ID conflict")
                return previous
            await self.repo.append(
                conn,
                tid,
                [
                    {
                        "type": "ctf.platform.dispatched",
                        "actor": agent_id,
                        "object_id": call_id,
                        "payload": payload,
                        "addressed_to": [agent_id],
                    }
                ],
            )
            return payload

    async def _dispatch(self, conn, tid, call_id):
        return (
            await conn.execute(
                select(s.events.c.payload)
                .where(
                    s.events.c.task_id == tid,
                    s.events.c.type == "ctf.platform.dispatched",
                    s.events.c.object_id == call_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _settlement_binding(self, conn, tid, agent_id, turn_id, generation, command):
        dispatch = await self._dispatch(conn, tid, command["call_id"])
        expected = {
            key: command[key]
            for key in (
                "call_id",
                "challenge_id",
                "expected_revision",
                "server_name",
                "server_version",
                "tool_name",
            )
        }
        expected.update(agent_id=agent_id, turn_id=str(turn_id), generation=generation)
        ensure(dispatch == expected, "Platform result has no matching authorized dispatch")
        return await self._platform_binding(
            conn,
            tid,
            agent_id,
            turn_id,
            generation,
            command["server_name"],
            command["server_version"],
            command["tool_name"],
            allow_settlement=True,
        )

    async def record_platform_result(
        self,
        tid,
        agent_id,
        turn_id,
        generation,
        request_id,
        challenge_id,
        expected_revision,
        server_name,
        server_version,
        tool_name,
        call_id,
        response_uri,
        response_sha256,
    ):
        ensure(
            response_uri.startswith(f"evidence/{tid}/")
            and len(response_uri.split("/")) == 4
            and ".." not in response_uri.split("/"),
            "Platform result must belong to this task",
        )
        command = dict(
            operation="platform_result",
            challenge_id=str(challenge_id),
            expected_revision=expected_revision,
            server_name=server_name,
            server_version=server_version,
            tool_name=tool_name,
            call_id=call_id,
            response_uri=response_uri,
            response_sha256=response_sha256,
        )
        async with self.repo.engine.begin() as conn:
            await self._settlement_binding(conn, tid, agent_id, turn_id, generation, command)
            previous, digest = await self._existing(conn, tid, agent_id, request_id, command)
            if previous:
                return previous
        ensure(self.objects is not None, "Object storage is unavailable")
        raw = await self.objects.get(response_uri)
        ensure(
            hashlib.sha256(raw).hexdigest() == response_sha256, "Platform result integrity mismatch"
        )
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = {}
        async with self.repo.engine.begin() as conn:
            binding = await self._settlement_binding(
                conn, tid, agent_id, turn_id, generation, command
            )
            previous, digest = await self._existing(conn, tid, agent_id, request_id, command)
            if previous:
                return previous
            challenge = await self._challenge(conn, tid, challenge_id)
            parsed = None
            if (
                binding.result_adapter == "fake_ctf_v1"
                and binding.purpose != "unknown"
                and isinstance(payload, dict)
                and not payload.get("isError", False)
            ):
                try:
                    parsed = CtfFakePlatformResult.model_validate(
                        payload.get("structuredContent") or payload
                    )
                except ValueError:
                    pass
            stamp = now().isoformat()
            call = dict(
                call_id=call_id,
                server_name=server_name,
                server_version=server_version,
                tool_name=tool_name,
                adapter=binding.result_adapter,
                response_uri=response_uri,
                response_sha256=response_sha256,
                test_only=binding.result_adapter == "fake_ctf_v1",
            )
            target = None
            verification = None
            if binding.purpose in {"management", "connect", "status", "unknown"}:
                target = {
                    **challenge["target"],
                    "provider": server_name,
                    "status": parsed.target_status
                    if parsed and parsed.target_status
                    else "unknown",
                    "updated_at": stamp,
                    "source": "platform",
                    "call_id": call_id,
                    "response_uri": response_uri,
                    "test_only": call["test_only"],
                }
                if parsed:
                    target["external_target_id"] = parsed.target_id
                    if parsed.connection:
                        target["connection"] = parsed.connection
            if binding.purpose in {"submit", "status", "unknown"}:
                verification = CtfVerification(
                    required=challenge["verification"].get("required", False),
                    basis=challenge["verification"].get("basis"),
                    status=parsed.status if parsed else "unknown",
                    source="platform",
                    submission_id=parsed.submission_id if parsed else None,
                    external_ref=parsed.target_id if parsed else None,
                    response_uri=response_uri,
                    call_id=call_id,
                    summary="Structured test adapter result"
                    if parsed
                    else "Result requires verification",
                    updated_at=now(),
                    test_only=binding.result_adapter == "fake_ctf_v1",
                ).model_dump(mode="json")
            return await self._verification_record(
                conn,
                tid,
                agent_id,
                request_id,
                digest,
                challenge,
                verification,
                "Platform call result",
                target=target,
                platform_call=call,
                expected_revision=expected_revision,
            )

    async def authorize_platform_object(self, tid, uri):
        async with self.repo.engine.connect() as conn:
            return (
                await conn.execute(
                    select(s.ctf_records.c.id)
                    .where(
                        s.ctf_records.c.task_id == tid,
                        s.ctf_records.c.data["platform_call"]["response_uri"].astext == uri,
                    )
                    .limit(1)
                )
            ).first() is not None
