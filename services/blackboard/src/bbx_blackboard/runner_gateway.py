"""Per-binding capabilities and the durable business MCP invocation ledger."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from bbx_contracts.native_gateway import GatewayCredentialsRequest
from bbx_contracts.runner import (
    MAX_MODEL_METADATA_BYTES,
    PROTOCOL,
    ToolInvocation,
    ToolResultConfirmation,
    ToolUnknownSubmission,
    runner_json_text,
    runner_payload_digest,
)
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, insert, select, update

from bbx_blackboard.auth import Principal
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.gateway_auth import RuntimeBusinessContext, issue_gateway_credentials
from bbx_blackboard.runner_auth import RunnerIdentity
from bbx_blackboard.runner_control import RunnerControl, fence, row
from bbx_blackboard.store import schema as s


def exact_json(value: str):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON property")
            result[key] = item
        return result

    def invalid(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def decode(model, raw, maximum=MAX_MODEL_METADATA_BYTES):
    if len(raw) > maximum:
        raise HTTPException(413, "Gateway request exceeds its byte bound")
    try:
        value = raw.decode("utf-8")
        parsed = exact_json(value)
        return model.model_validate(parsed), hashlib.sha256(raw).hexdigest()
    except (UnicodeError, ValueError, ValidationError) as error:
        raise HTTPException(422, "Invalid gateway request") from error


async def cancel_admitted(conn, tid, binding_id=None, through=None, epoch=None):
    """Only calls that never received a dispatch claim are proven not sent."""
    query = update(s.runner_calls).where(
        s.runner_calls.c.task_id == tid, s.runner_calls.c.state == "admitted"
    )
    if binding_id is not None:
        query = query.where(s.runner_calls.c.binding_id == binding_id)
    if through is not None:
        query = query.where(s.runner_calls.c.created_at <= through)
    if epoch is not None:
        query = query.where(s.runner_calls.c.owner_epoch == epoch)
    await conn.execute(query.values(state="not_sent", concurrency_active=False))


class RunnerGateway:
    def __init__(self, engine, settings):
        self.control = RunnerControl(engine)
        self.repo = self.control.repo
        self.ctf = CtfService(engine)
        self.settings = settings
        self.clock = lambda: datetime.now(UTC)
        key = hashlib.sha256(
            b"bbx-runner-call-private-v1\0"
            + settings.agent_token_secret.get_secret_value().encode()
        ).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        from bbx_blackboard.native_fees import NativeFees

        self.native = NativeFees(self)

    async def _scope(self, conn, tid, identity, *, sdk=None, journal=None, metadata=False):
        owner = await self.control._identity(
            conn, tid, RunnerIdentity(identity.fence, identity.token_version), live=True
        )
        binding = await self.control._binding(
            conn, tid, identity.binding_id, owner, live=True, permit=not metadata
        )
        if identity.expires_at <= datetime.now(UTC).timestamp():
            raise HTTPException(401, "Gateway capability expired")
        task = await self.control._task(conn, tid, live=True)
        if task["ctf_control"].get("failure_hold"):
            raise HTTPException(409, "Task admission is paused")
        if not metadata and (
            binding["sdk_session_id"] is None
            or binding["sdk_session_id"] != sdk
            or journal is None
            or not await self.control._confirmed_cursor(conn, tid, binding, journal)
        ):
            raise HTTPException(403, "Confirmed SDK journal is required")
        return task, owner, binding

    async def credentials(self, tid, bid, raw):
        body, _ = decode(GatewayCredentialsRequest, raw, 65536)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await self.control._task(conn, tid, live=True)
            owner = await self.control._current_owner(conn, tid)
            if body.expected_epoch != owner["epoch"]:
                raise HTTPException(409, "Runner owner changed")
            await self.control._binding(conn, tid, bid, owner, live=True)
            return issue_gateway_credentials(self.settings, owner, bid)

    async def _call(self, conn, tid, key):
        return (
            (
                await conn.execute(
                    select(s.runner_calls).where(
                        s.runner_calls.c.task_id == tid, s.runner_calls.c.id == key
                    )
                )
            )
            .mappings()
            .first()
        )

    async def _count(self, conn, *conditions):
        return (
            await conn.execute(select(func.count()).select_from(s.runner_calls).where(*conditions))
        ).scalar_one()

    def _unresolved(self):
        return s.runner_calls.c.state.in_(["admitted", "dispatched", "unknown"]) & (
            (s.runner_calls.c.kind != "model") | s.runner_calls.c.concurrency_active.is_(True)
        )

    async def catalog(self, tid, identity):
        async with self.repo.engine.connect() as conn:
            _, _, binding = await self._scope(conn, tid, identity, metadata=True)
            member = await row(conn, s.ctf_members, tid, binding["binding"]["agent_id"])
            from bbx_contracts.challenge_board import business_manifest

            allowed = set(business_manifest()["roles"][member["role"]])
            declared = set(json.loads(binding["payload_json"])["spec"]["allowed_tools"])
            return sorted(allowed & declared)

    async def _tool(self, conn, tid, identity, invocation, *, live=True):
        if (
            identity.binding_id != invocation.binding.binding_id
            or identity.fence != invocation.fence
        ):
            raise HTTPException(403, "Tool capability scope changed")
        key = "tool:" + invocation.invocation_id
        call = await self._call(conn, tid, key)
        if live:
            _, owner, binding = await self._scope(
                conn,
                tid,
                identity,
                sdk=invocation.sdk_session_id,
                journal=invocation.journal.model_dump(mode="json"),
            )
            if invocation.binding.model_dump(mode="json") != binding["binding"]:
                raise HTTPException(403, "Tool binding changed")
        else:
            owner = await self.control._identity(
                conn, tid, RunnerIdentity(identity.fence, identity.token_version)
            )
            binding = await row(conn, s.runner_bindings, tid, identity.binding_id)
        identity_fields = [
            str(tid),
            identity.binding_id,
            invocation.sdk_session_id,
            invocation.assistant_entry_id,
            invocation.tool_call_id,
            invocation.tool_name,
        ]
        expected_id = runner_payload_digest(runner_json_text(identity_fields))
        if invocation.invocation_id != expected_id:
            raise HTTPException(403, "Tool invocation identity is not platform-derived")
        if call:
            original = call["data"]
            checks = {
                "sdk_session_id": invocation.sdk_session_id,
                "binding": invocation.binding.model_dump(mode="json"),
                "assistant_entry_id": invocation.assistant_entry_id,
                "tool_call_id": invocation.tool_call_id,
                "tool_name": invocation.tool_name,
                "arguments_digest": invocation.arguments_digest,
            }
            if call["binding_id"] != identity.binding_id or any(
                original[key] != value for key, value in checks.items()
            ):
                raise HTTPException(409, "Original tool identity or arguments changed")
            adopted_completed = (
                call["state"] == "completed"
                and owner["current"]
                and binding["owner_epoch"] == owner["epoch"]
                and binding["sdk_session_id"] == invocation.sdk_session_id
            )
            if not live and call["owner_epoch"] != owner["epoch"] and not adopted_completed:
                raise HTTPException(403, "Old evidence capability does not own this invocation")
        return dict(call) if call else None, owner, binding

    def _permit(self, call):
        data = call["data"]
        completed = call["state"] == "completed"
        return {
            "protocol": PROTOCOL,
            "confirmed": True,
            "invocation_id": data["invocation_id"],
            "disposition": "completed"
            if completed
            else ("admitted" if call["state"] == "admitted" else "unknown"),
            "arguments_digest": data["arguments_digest"],
            "result_digest": data.get("result_digest") if completed else None,
            "result_json": self.cipher.decrypt(data["result_ciphertext"].encode()).decode()
            if completed
            else None,
            "authorized_wait_until_ms": data["authorized_wait_until_ms"],
        }

    async def admit_tool(self, tid, identity, raw):
        invocation, _ = decode(ToolInvocation, raw, 2 * 1024 * 1024)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call, owner, binding = await self._tool(conn, tid, identity, invocation)
            if call:
                if call["owner_epoch"] != owner["epoch"] and call["state"] != "completed":
                    return {
                        **self._permit(call),
                        "disposition": "unknown",
                        "result_json": None,
                        "result_digest": None,
                    }
                return self._permit(call)
            from bbx_contracts.challenge_board import business_manifest

            member = await row(conn, s.ctf_members, tid, binding["binding"]["agent_id"])
            spec = json.loads(binding["payload_json"])["spec"]
            if (
                invocation.tool_name not in spec["allowed_tools"]
                or invocation.tool_name not in business_manifest()["roles"][member["role"]]
            ):
                raise HTTPException(403, "Tool is not published for this role")
            if await self._count(
                conn,
                s.runner_calls.c.task_id == tid,
                s.runner_calls.c.binding_id == binding["id"],
                self._unresolved(),
            ):
                raise HTTPException(409, "Binding has an unresolved call")
            original_args = exact_json(invocation.arguments_json)
            if not isinstance(original_args, dict):
                raise HTTPException(422, "Tool arguments must be an object")
            if "request_id" in original_args:
                raise HTTPException(422, "Platform mutation identity is supplied by the host")
            if (
                member["role"] == "teammate"
                and invocation.tool_name not in {"board_announcements", "board_challenges"}
                and str(original_args.get("challenge_id")) != binding["business"]["challenge_id"]
            ):
                raise HTTPException(403, "Worker tool arguments exceed the current Assignment")
            wait = min(
                owner["hard_deadline"].timestamp(),
                identity.expires_at,
                datetime.now(UTC).timestamp() + 60,
            )
            data = {
                "invocation_id": invocation.invocation_id,
                "fence": fence(owner),
                "binding": binding["binding"],
                "business": binding["business"],
                "sdk_session_id": invocation.sdk_session_id,
                "journal": invocation.journal.model_dump(mode="json"),
                "assistant_entry_id": invocation.assistant_entry_id,
                "tool_call_id": invocation.tool_call_id,
                "tool_name": invocation.tool_name,
                "arguments_digest": invocation.arguments_digest,
                "arguments_ciphertext": self.cipher.encrypt(
                    invocation.arguments_json.encode()
                ).decode(),
                "mutation_request_id": str(
                    uuid5(NAMESPACE_URL, "runner-business:" + invocation.invocation_id)
                ),
                "authorized_wait_until_ms": int(wait * 1000),
            }
            value = dict(
                task_id=tid,
                id="tool:" + invocation.invocation_id,
                kind="tool",
                binding_id=binding["id"],
                owner_epoch=owner["epoch"],
                state="admitted",
                concurrency_active=True,
                provider_key="business",
                data=data,
                created_at=datetime.now(UTC),
            )
            await conn.execute(insert(s.runner_calls).values(**value))
            return self._permit(value)

    async def claim_business_tool(self, tid, identity, invocation, name, arguments):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call, owner, binding = await self._tool(conn, tid, identity, invocation)
            if call is None:
                raise HTTPException(403, "MCP call has no admitted invocation")
            data = call["data"]
            original = exact_json(
                self.cipher.decrypt(data["arguments_ciphertext"].encode()).decode()
            )
            if name != data["tool_name"] or arguments != original:
                raise HTTPException(403, "MCP metadata does not match the admitted request")
            if call["state"] == "completed":
                return None, self._permit(call)
            if call["state"] != "admitted" or call["owner_epoch"] != owner["epoch"]:
                raise HTTPException(409, "Tool outcome is unknown; redispatch is forbidden")
            if data["authorized_wait_until_ms"] <= int(datetime.now(UTC).timestamp() * 1000):
                raise HTTPException(409, "Tool permit expired")
            await conn.execute(
                update(s.runner_calls)
                .where(s.runner_calls.c.task_id == tid, s.runner_calls.c.id == call["id"])
                .values(state="dispatched")
            )
            context = RuntimeBusinessContext(identity, invocation.sdk_session_id, data["journal"])
            principal = Principal(
                "agent",
                binding["binding"]["agent_id"],
                task_id=tid,
                agent_id=binding["binding"]["agent_id"],
                mode="ctf",
                generation=binding["business"]["generation"],
                turn_id=UUID(binding["business"]["turn_id"]),
                runtime_context=context,
            )
            return (principal, data, original), None

    async def save_business_result(self, tid, invocation_id, value):
        result_json = runner_json_text(value)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call = await self._call(conn, tid, "tool:" + invocation_id)
            if call is None or call["state"] not in {"dispatched", "unknown"}:
                raise HTTPException(409, "Business result has no original dispatch")
            data = {
                **call["data"],
                "result_digest": runner_payload_digest(result_json),
                "result_ciphertext": self.cipher.encrypt(result_json.encode()).decode(),
            }
            await conn.execute(
                update(s.runner_calls)
                .where(s.runner_calls.c.task_id == tid, s.runner_calls.c.id == call["id"])
                .values(state="completed", concurrency_active=False, data=data)
            )
            return value

    async def unknown_business_result(self, tid, invocation_id):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            await conn.execute(
                update(s.runner_calls)
                .where(
                    s.runner_calls.c.task_id == tid,
                    s.runner_calls.c.id == "tool:" + invocation_id,
                    s.runner_calls.c.state == "dispatched",
                )
                .values(state="unknown")
            )

    async def confirm_tool_result(self, tid, iid, identity, raw):
        body, sha = decode(ToolResultConfirmation, raw, 2 * 1024 * 1024)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call, _, _ = await self._tool(conn, tid, identity, body.invocation, live=False)
            if call is None or iid != body.invocation.invocation_id:
                raise HTTPException(404, "Original tool invocation is unavailable")
            if call["state"] != "completed":
                raise HTTPException(409, "Host has no saved completed result")
            permit = self._permit(call)
            receipt = {
                "protocol": PROTOCOL,
                "confirmed": True,
                "invocation_id": iid,
                "result_digest": permit["result_digest"],
                "result_json": permit["result_json"],
            }
            await self.control._receipt(
                conn,
                tid,
                identity.fence.epoch,
                "sdk_tool_confirmation",
                iid,
                raw.decode("utf-8"),
                sha,
                {"confirmed": True, "result_digest": permit["result_digest"]},
                identity.binding_id,
            )
            return receipt

    async def observe_tool_unknown(self, tid, iid, identity, raw):
        body, sha = decode(ToolUnknownSubmission, raw, 2 * 1024 * 1024)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call, _, _ = await self._tool(conn, tid, identity, body.invocation, live=False)
            if call is None or iid != body.invocation.invocation_id:
                raise HTTPException(404, "Original tool invocation is unavailable")
            await self.control._receipt(
                conn,
                tid,
                identity.fence.epoch,
                "sdk_tool_unknown_observation",
                iid,
                raw.decode("utf-8"),
                sha,
                {"confirmed": True},
                identity.binding_id,
            )
            if call["state"] == "admitted":
                await conn.execute(
                    update(s.runner_calls)
                    .where(
                        s.runner_calls.c.task_id == tid,
                        s.runner_calls.c.id == call["id"],
                        s.runner_calls.c.state == "admitted",
                    )
                    .values(state="unknown")
                )
                call["state"] = "unknown"
            permit = self._permit(call)
            try:
                await self._scope(
                    conn,
                    tid,
                    identity,
                    sdk=body.invocation.sdk_session_id,
                    journal=body.invocation.journal.model_dump(mode="json"),
                )
            except HTTPException:
                permit = {
                    **permit,
                    "disposition": "unknown",
                    "result_json": None,
                    "result_digest": None,
                }
            return permit
