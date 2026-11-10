"""Per-binding capabilities and the durable business MCP invocation ledger."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from bbx_contracts.mcp_catalog import FrozenNativeMcpTool
from bbx_contracts.native_gateway import GatewayCredentialsRequest
from bbx_contracts.runner import (
    BACKGROUND_HELPER_TOOLS,
    MAX_MODEL_METADATA_BYTES,
    PROTOCOL,
    ToolInvocation,
    ToolResultConfirmation,
    ToolUnknownSubmission,
    runner_json_text,
    runner_payload_digest,
)
from bbx_contracts.runner_bootstrap import native_resume_pending
from bbx_contracts.runner_vpn import NETWORK_USE_DEFAULT, VPN_TOOLS
from bbx_contracts.sandbox_tools import HELPER_SANDBOX_TOOLS, SANDBOX_CONTROLS, SANDBOX_TOOLS
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, insert, select, update

from bbx_blackboard.auth import Principal
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.gateway_auth import (
    DIRECT_MCP,
    RuntimeBusinessContext,
    issue_gateway_credentials,
    require_mcp_transport,
    task_direct_mcp,
    validate_gateway_role,
)
from bbx_blackboard.native_vpn import NativeVpn, vpn_result, vpn_solver_deadline
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
        from bbx_blackboard.sandbox_tools import SandboxTools

        self.sandbox = SandboxTools(self)
        self.vpn: NativeVpn | None = None

    async def _scope(self, conn, tid, identity, *, sdk=None, journal=None, metadata=False):
        owner = await self.control._identity(
            conn, tid, RunnerIdentity(identity.fence, identity.token_version), live=True
        )
        binding = await self.control._binding(
            conn, tid, identity.binding_id, owner, live=True, permit=not metadata
        )
        await validate_gateway_role(conn, tid, identity, binding)
        if identity.expires_at <= datetime.now(UTC).timestamp():
            raise HTTPException(401, "Gateway capability expired")
        task = await self.control._task(conn, tid, live=True)
        if not metadata and native_resume_pending(task):
            raise HTTPException(409, "Replacement Runner session is not ready")
        if task["ctf_control"].get("failure_hold"):
            raise HTTPException(409, "Task admission is paused")
        if not metadata and (
            (binding["business"].get("native_child_id") and binding["status"] != "prepared")
            or binding["sdk_session_id"] is None
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
            binding = await self.control._binding(conn, tid, bid, owner, live=True)
            if binding["business"].get("native_child_id"):
                raise HTTPException(403, "Helper credentials require the original launch claim")
            if not await task_direct_mcp(conn, tid):
                raise HTTPException(409, "Original execution cannot safely migrate to Pi")
            spec = json.loads(binding["payload_json"])["spec"]
            targets = spec.get("mcp_target_credential_refs")
            if not targets:
                raise HTTPException(409, "Direct MCP binding is missing its target identities")
            member = await row(conn, s.ctf_members, tid, binding["binding"]["agent_id"])
            config = await conn.scalar(
                select(s.task_runner_resources.c.config).where(
                    s.task_runner_resources.c.task_id == tid
                )
            )
            if not config or config.get("mcp_transport") != "direct-v2":
                raise HTTPException(409, "Direct MCP deployment is not frozen")
            return {
                **issue_gateway_credentials(
                    self.settings, owner, bid, member["role"], targets=targets
                ),
                "mcp_targets": {
                    "business": config["runner_blackboard_url"].rstrip("/")
                    + f"/api/tasks/{tid}/business-mcp-v2/",
                    "sandbox": "http://127.0.0.1:8766/mcp/",
                },
            }

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

    async def _external_tools(self, conn, tid, identity, binding) -> dict:
        if identity.audience != "business-mcp" or identity.role not in {"lead", "teammate"}:
            return {}
        frozen = await conn.scalar(
            select(s.task_runner_resources.c.data["native_mcp"][identity.role]).where(
                s.task_runner_resources.c.task_id == tid
            )
        )
        spec = json.loads(binding["payload_json"])["spec"]
        return {
            name: grant for name, grant in (frozen or {}).items() if name in spec["allowed_tools"]
        }

    async def external_catalog(self, tid, identity) -> dict:
        async with self.repo.engine.connect() as conn:
            _, _, binding = await self._scope(conn, tid, identity, metadata=True)
            return await self._external_tools(conn, tid, identity, binding)

    async def catalog(self, tid, identity):
        async with self.repo.engine.connect() as conn:
            task, _, binding = await self._scope(conn, tid, identity, metadata=True)
            member = await row(conn, s.ctf_members, tid, binding["binding"]["agent_id"])
            roles = task["ctf_control"].get("business_tools", {}).get("roles", {})
            allowed = (
                set(HELPER_SANDBOX_TOOLS)
                if binding["business"].get("native_child_id")
                else set(roles.get(member["role"], [])) | set(SANDBOX_TOOLS)
            )
            spec = json.loads(binding["payload_json"])["spec"]
            declared = {
                name
                for name, route in spec.get("mcp_tool_routes", {}).items()
                if DIRECT_MCP[route["target"]][0] == identity.audience
            }
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
        await require_mcp_transport(conn, tid, identity)
        spec = json.loads(binding["payload_json"])["spec"]
        route = spec.get("mcp_tool_routes", {}).get(invocation.tool_name)
        target = route["target"] if route else None
        if invocation.tool_name in {"delegate_helper", *BACKGROUND_HELPER_TOOLS} and (
            spec.get("helper_foreground") or spec.get("helper_background")
        ):
            target = "business"
        if target is None or DIRECT_MCP[target][0] != identity.audience:
            raise HTTPException(403, "MCP capability does not match the frozen tool target")
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
        if invocation.tool_name in {"delegate_helper", *BACKGROUND_HELPER_TOOLS}:
            raise HTTPException(403, "Helper delegation requires the local Runner control route")
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
            member = await row(conn, s.ctf_members, tid, binding["binding"]["agent_id"])
            task = await self.control._task(conn, tid, live=True)
            roles = task["ctf_control"].get("business_tools", {}).get("roles", {})
            spec = json.loads(binding["payload_json"])["spec"]
            child = bool(binding["business"].get("native_child_id"))
            external = (await self._external_tools(conn, tid, identity, binding)).get(
                invocation.tool_name
            )
            role_tools = (
                set(HELPER_SANDBOX_TOOLS)
                if child
                else {*roles.get(member["role"], []), *SANDBOX_TOOLS}
            )
            if invocation.tool_name not in spec["allowed_tools"] or (
                invocation.tool_name not in role_tools and external is None
            ):
                raise HTTPException(403, "Tool is not published for this role")
            reconciling = invocation.tool_name in SANDBOX_CONTROLS
            original_args = exact_json(invocation.arguments_json)
            if not isinstance(original_args, dict):
                raise HTTPException(422, "Tool arguments must be an object")
            if external is None and "request_id" in original_args:
                raise HTTPException(422, "Platform mutation identity is supplied by the host")
            if (
                external is None
                and member["role"] == "teammate"
                and invocation.tool_name
                not in {*SANDBOX_TOOLS, "board_announcements", "board_challenges"}
                and str(original_args.get("challenge_id")) != binding["business"]["challenge_id"]
            ):
                raise HTTPException(403, "Worker tool arguments exceed the current Assignment")
            if invocation.tool_name in SANDBOX_TOOLS:
                self.sandbox.validate_arguments(invocation.tool_name, original_args)
                if (
                    child
                    and invocation.tool_name == "exec_command"
                    and original_args.get("background")
                ):
                    raise HTTPException(403, "Helper command must use foreground mode")
                scoped = await self.sandbox.binding(conn, tid, binding, owner)
                if reconciling:
                    await self.sandbox.resolve_command(
                        conn, tid, original_args["command_id"], scoped
                    )
            if external is not None and await self._count(
                conn,
                s.runner_calls.c.task_id == tid,
                s.runner_calls.c.provider_key == "external-mcp",
                self._unresolved(),
            ):
                raise HTTPException(409, "Task has an unresolved external MCP call")
            unrelated = (
                [s.runner_calls.c.id != "tool:" + original_args["command_id"]]
                if reconciling
                else []
            )
            if await self._count(
                conn,
                s.runner_calls.c.task_id == tid,
                s.runner_calls.c.binding_id == binding["id"],
                self._unresolved(),
                *unrelated,
            ):
                raise HTTPException(409, "Binding has an unresolved call")
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
                **(
                    {"external_mcp": runner_payload_digest(runner_json_text(external))}
                    if external is not None
                    else {}
                ),
            }
            value = dict(
                task_id=tid,
                id="tool:" + invocation.invocation_id,
                kind="tool",
                binding_id=binding["id"],
                owner_epoch=owner["epoch"],
                state="admitted",
                concurrency_active=True,
                provider_key=(
                    "external-mcp"
                    if external is not None
                    else "sandbox"
                    if invocation.tool_name in SANDBOX_TOOLS
                    else "business"
                ),
                data=data,
                created_at=datetime.now(UTC),
            )
            await conn.execute(insert(s.runner_calls).values(**value))
            if invocation.tool_name == "exec_command" and self.vpn is not None:
                resource = await self.vpn.bootstrap._get(conn, tid)
                try:
                    if resource:
                        vpn_solver_deadline(resource)
                except HTTPException as error:
                    result = vpn_result({"error": error.detail}, error=True)
                    await self._save_tool_result(conn, tid, value, result)
                    return self._permit(await self._call(conn, tid, value["id"]))
            return self._permit(value)

    async def claim_business_tool(
        self, tid, identity, invocation, name, arguments, *, target="business"
    ):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call, owner, binding = await self._tool(conn, tid, identity, invocation)
            if call is None:
                raise HTTPException(403, "MCP call has no admitted invocation")
            data = call["data"]
            external = None
            if target == "business" and call["provider_key"] == "external-mcp":
                external = await self._matching_external(conn, tid, identity, binding, call)
            elif call["provider_key"] != target:
                raise HTTPException(403, "MCP target does not own this tool invocation")
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
            # Enqueue VPN management and complete the original MCP call in one Task transaction.
            if name in VPN_TOOLS or name == NETWORK_USE_DEFAULT:
                if target != "business" or self.vpn is None:
                    raise HTTPException(403, "Original VPN management is unavailable")
                value = await self.vpn.tool(conn, tid, owner, binding, name, original)
                await self._save_tool_result(conn, tid, call, value)
                return None, self._permit(await self._call(conn, tid, call["id"]))
            if target == "sandbox" and name == "exec_command":
                resource = await self.sandbox.resource(conn, tid)
                try:
                    vpn_solver_deadline(resource)
                except HTTPException as error:
                    value = vpn_result({"error": error.detail}, error=True)
                    await self._save_tool_result(conn, tid, call, value)
                    return None, self._permit(await self._call(conn, tid, call["id"]))
            await conn.execute(
                update(s.runner_calls)
                .where(s.runner_calls.c.task_id == tid, s.runner_calls.c.id == call["id"])
                .values(state="dispatched")
            )
            if target == "sandbox":
                return await self.sandbox.dispatch_claim(
                    conn, tid, call, owner, binding, original
                ), None
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
            if external is not None:
                data = {**data, "external_grant": external}
            return (principal, data, original), None

    async def _matching_external(self, conn, tid, identity, binding, call) -> FrozenNativeMcpTool:
        original = call["data"]
        grant = (await self._external_tools(conn, tid, identity, binding)).get(
            original["tool_name"]
        )
        if grant is None or runner_payload_digest(runner_json_text(grant)) != original.get(
            "external_mcp"
        ):
            raise HTTPException(403, "Original external MCP grant is unavailable")
        return FrozenNativeMcpTool.model_validate(grant)

    async def verify_external_send(self, tid, identity, invocation):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call, owner, binding = await self._tool(conn, tid, identity, invocation)
            if (
                call is None
                or call["provider_key"] != "external-mcp"
                or call["owner_epoch"] != owner["epoch"]
                or call["state"] != "dispatched"
                or call["data"]["authorized_wait_until_ms"]
                <= int(datetime.now(UTC).timestamp() * 1000)
            ):
                raise HTTPException(409, "Original external MCP permit expired or changed")
            await self._matching_external(conn, tid, identity, binding, call)

    async def save_business_result(self, tid, invocation_id, value):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call = await self._call(conn, tid, "tool:" + invocation_id)
            if call is None or call["state"] not in {"dispatched", "unknown"}:
                raise HTTPException(409, "Business result has no original dispatch")
            await self._save_tool_result(conn, tid, call, value)
            return value

    async def _save_tool_result(self, conn, tid, call, value):
        result_json = runner_json_text(value)
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
