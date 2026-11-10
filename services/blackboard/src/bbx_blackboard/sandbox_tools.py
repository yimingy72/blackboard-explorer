"""Task-bound Sandbox dispatch and command audit over the existing Runner ledger."""

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from bbx_contracts.mcp_catalog import SandboxMcpAuthorize
from bbx_contracts.runner_inputs import INPUT_PROFILE
from bbx_contracts.sandbox_tools import (
    MAX_COMMAND_OUTPUT_BYTES,
    SANDBOX_TOOLS,
    SandboxCommandDetail,
    SandboxCommandPage,
    SandboxCommandSummary,
    SandboxHostEvent,
    SandboxHostResult,
    SandboxToolClaim,
    sandbox_schemas,
)
from fastapi import APIRouter, HTTPException, Query, Request, Response
from jsonschema import ValidationError, validate
from sqlalchemy import insert, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB

from bbx_blackboard.auth import require_task_reader
from bbx_blackboard.gateway_auth import mcp_identity, require_gateway_identity
from bbx_blackboard.native_mcp_routes import NativeMCPRoutes
from bbx_blackboard.native_traffic import (
    PART_LIMIT,
    CaptureReady,
    CaptureSeal,
    NativeTraffic,
    opted_in,
)
from bbx_blackboard.native_vpn import vpn_enabled, vpn_solver_deadline
from bbx_blackboard.runner_control import row
from bbx_blackboard.runner_gateway import decode
from bbx_blackboard.store import schema as s


def now():
    return datetime.now(UTC)


class SandboxTools:
    def __init__(self, gateway):
        self.gateway, self.repo = gateway, gateway.repo
        self.traffic = NativeTraffic(self)

    async def resource(self, conn, tid):
        found = (
            (
                await conn.execute(
                    select(s.task_runner_resources).where(s.task_runner_resources.c.task_id == tid)
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            raise HTTPException(503, "Task Sandbox host is unavailable")
        return found

    async def host(self, conn, tid, key, *, purpose="report"):
        resource = await self.resource(conn, tid)
        host = resource["sandbox_host"]
        expected = host.get(purpose + "_key_hash", "")
        if not expected or not hmac.compare_digest(
            hashlib.sha256(key.encode()).hexdigest(), expected
        ):
            raise HTTPException(401, "Task Sandbox host identity required")
        return resource, host

    def private_host(self, host):
        return {
            **host["context"],
            "context_digest": self.context_digest(host["context"]),
            **{
                purpose + "_key": self.gateway.cipher.decrypt(
                    host[purpose + "_key_ciphertext"].encode()
                ).decode()
                for purpose in ("inbound", "report")
            },
        }

    @staticmethod
    def context_digest(context):
        return hashlib.sha256(
            json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def host_material(self, conn, tid, resource, context):
        if resource["config"].get("runner_profile") != "node-sandbox-host-v1":
            raise HTTPException(409, "Task does not use the Sandbox host profile")
        observed = resource["observation"]
        expected = {
            "sandbox_id": observed.get("sandbox", {}).get("id"),
            "sandbox_pod_uid": observed.get("sandbox", {}).get("pod_uid"),
            "environment_revision": resource["config"]["environment_revision"],
        }
        if (
            not isinstance(context, dict)
            or set(context) != {*expected, "execution_context_id", "execd_endpoint", "host_url"}
            or any(not isinstance(value, str) or not value for value in context.values())
            or any(context.get(name) != value for name, value in expected.items())
        ):
            raise HTTPException(409, "Task Sandbox context does not match original resources")
        from urllib.parse import urlsplit

        if re.fullmatch(r"[0-9a-f]{64}", context.get("execution_context_id", "")) is None:
            raise HTTPException(422, "Task execution context nonce is invalid")
        try:
            target = urlsplit(context["host_url"])
            port = target.port
        except ValueError as error:
            raise HTTPException(422, "Task Sandbox host endpoint is invalid") from error
        if (
            target.scheme != "http"
            or port != 8766
            or target.path
            or target.query
            or target.fragment
            or target.username
            or not target.hostname
            or target.hostname != observed.get("runner", {}).get("pod_ip")
        ):
            raise HTTPException(422, "Task Sandbox host endpoint is invalid")
        if any(c in context["execd_endpoint"] for c in ("\r", "\n", "@", "?", "#")):
            raise HTTPException(422, "Task execution endpoint is invalid")
        current = resource["sandbox_host"]
        frozen = {
            **context,
            "task_id": str(tid),
            "operation_id": resource["operation_id"],
            "runner_pod_uid": observed.get("runner", {}).get("pod_uid"),
            "network": resource["config"]["sandbox_network"],
            **(
                {"traffic_targets": resource["config"]["traffic_targets"]}
                if opted_in(resource)
                else {}
            ),
            **(
                {"inputs_profile": INPUT_PROFILE}
                if resource["request"].get("shared_inputs")
                else {}
            ),
            **({"vpn_profile": resource["data"]["vpn_profile"]} if vpn_enabled(resource) else {}),
            "blackboard_url": resource["config"]["runner_blackboard_url"],
            "hard_deadline": resource["hard_deadline"].isoformat(),
        }
        if current:
            if current["context"] != frozen:
                raise HTTPException(409, "Original Task Sandbox host context changed")
        else:
            current = {"context": frozen, "ready": False}
            for purpose in ("inbound", "report"):
                key = secrets.token_urlsafe(48)
                current[purpose + "_key_hash"] = hashlib.sha256(key.encode()).hexdigest()
                current[purpose + "_key_ciphertext"] = self.gateway.cipher.encrypt(
                    key.encode()
                ).decode()
            await conn.execute(
                update(s.task_runner_resources)
                .where(s.task_runner_resources.c.task_id == tid)
                .values(sandbox_host=current)
            )
        return {
            **self.private_host(current),
            **(
                {"mcp_transport": "direct-v2"}
                if resource["data"].get("mcp_transport") == "direct-v2"
                else {}
            ),
        }

    @staticmethod
    def validate_arguments(name, arguments):
        if name not in SANDBOX_TOOLS:
            raise HTTPException(403, "Sandbox tool is not published")
        try:
            validate(arguments, sandbox_schemas()[name])
        except ValidationError as error:
            raise HTTPException(
                422, "Sandbox tool arguments do not match the published schema"
            ) from error
        for key in ("path", "cwd"):
            if key in arguments:
                path = PurePosixPath(arguments[key])
                if (
                    not path.is_absolute()
                    or not path.is_relative_to("/workspace")
                    or ".." in path.parts
                    or "\x00" in arguments[key]
                ):
                    raise HTTPException(
                        403, "Sandbox file and working paths require the shared workspace"
                    )
        if name == "exec_command" and (
            not arguments["command"].strip() or "\x00" in arguments["command"]
        ):
            raise HTTPException(422, "A Sandbox command is required")
        if name == "write_file":
            mode = arguments.get("mode", 644)
            if not 0 <= mode <= 777 or any(c > "7" for c in str(mode)):
                raise HTTPException(422, "Only ordinary file permission bits are allowed")

    async def require_ready(self, conn, tid):
        resource = await self.resource(conn, tid)
        if not resource["sandbox_host"].get("ready"):
            raise HTTPException(503, "Task Sandbox host is not ready")
        return resource

    async def binding(self, conn, tid, runner_binding, owner):
        resource = await self.require_ready(conn, tid)
        context = resource["sandbox_host"]["context"]
        original_pod = resource["observation"].get("runner", {}).get("pod_uid")
        if (
            resource["data"].get("owner_epoch") != owner["epoch"]
            or owner["pod_uid"] != original_pod
            or context["runner_pod_uid"] != original_pod
        ):
            raise HTTPException(403, "Runner owner does not match the original Sandbox host")
        if entry := self.traffic.binding(resource, runner_binding["id"]):
            return entry["binding"]
        member = await row(conn, s.ctf_members, tid, runner_binding["binding"]["agent_id"])
        business = runner_binding["business"]
        assignment = runner_binding["binding"].get("assignment_ref") or str(
            uuid5(
                NAMESPACE_URL,
                f"sandbox-lead:{tid}:{owner['epoch']}:{runner_binding['binding']['generation']}",
            )
        )
        return {
            "task_id": str(tid),
            "agent_id": member["id"],
            "role": "helper"
            if business.get("native_child_id")
            else "lead"
            if member["role"] == "lead"
            else "worker",
            "assignment_id": assignment,
            "generation": runner_binding["binding"]["generation"],
            "epoch": owner["epoch"],
            "sandbox_id": context["sandbox_id"],
            "pod_uid": context["sandbox_pod_uid"],
            "environment_revision": context["environment_revision"],
            **context["network"],
            "challenge_id": business.get("challenge_id"),
            "parent_agent_id": business.get("parent_agent_id"),
            "execution_profile": "basic",
            "process_accounting": "cgroup-v2",
            "execution_context_id": context["execution_context_id"],
        }

    async def audit(self, conn, tid, command_id):
        found = (
            (
                await conn.execute(
                    select(s.sandbox_command_records).where(
                        s.sandbox_command_records.c.task_id == tid,
                        s.sandbox_command_records.c.id == command_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            raise HTTPException(404, "Original Sandbox command is unavailable")
        return found

    async def resolve_command(self, conn, tid, command_id, binding):
        command = await self.audit(conn, tid, command_id)
        if any(
            command[name] != binding[key]
            for name, key in (
                ("agent_id", "agent_id"),
                ("assignment_id", "assignment_id"),
                ("epoch", "epoch"),
                ("generation", "generation"),
            )
        ):
            raise HTTPException(403, "Sandbox command exceeds the current Assignment")
        if not command["execution_id"]:
            raise HTTPException(409, "Original execution ID is unknown; redispatch is forbidden")
        return command

    async def claim(self, tid, identity, invocation, name, arguments):
        self.validate_arguments(name, arguments)
        claimed, saved = await self.gateway.claim_business_tool(
            tid, identity, invocation, name, arguments, target="sandbox"
        )
        return {"saved": json.loads(saved["result_json"])} if saved else claimed

    async def dispatch_claim(self, conn, tid, call, owner, runner_binding, arguments):
        binding = await self.binding(conn, tid, runner_binding, owner)
        args = dict(arguments)
        network = None
        if call["data"]["tool_name"] == "exec_command":
            binding, network = await self.traffic.allocate(conn, tid, binding, runner_binding)
        data = {
            **call["data"],
            "sandbox_binding": binding,
            **({"network_source_id": network["scope"]["source_id"]} if network else {}),
        }
        if call["data"]["tool_name"] == "exec_command":
            remaining = int((owner["hard_deadline"] - now()).total_seconds())
            if binding["role"] == "helper":
                if args.get("background"):
                    raise HTTPException(403, "Helper command must use foreground mode")
                remaining = min(
                    remaining,
                    int(runner_binding["business"]["child_deadline_ms"] / 1000 - now().timestamp()),
                )
            if remaining <= 0:
                raise HTTPException(409, "Task command deadline has elapsed")
            args["timeout_seconds"] = min(
                args.get("timeout_seconds", 30), remaining, 300 if args.get("background") else 45
            )
            args.update(
                command_id=call["data"]["invocation_id"],
                invocation_id=call["data"]["invocation_id"],
            )
            options = {
                "background": args.get("background", False),
                "cwd": args.get("cwd", "/workspace"),
            }
            if network:
                data["command_options"] = options
            else:
                await self._record_command(conn, tid, call, runner_binding, binding, options)
        elif call["data"]["tool_name"] in {
            "execution_status",
            "execution_output",
            "interrupt_execution",
        }:
            command = await self.resolve_command(conn, tid, args.pop("command_id"), binding)
            data["sandbox_command_id"] = command["id"]
            args["execution_id"] = command["execution_id"]
        await conn.execute(
            update(s.runner_calls)
            .where(s.runner_calls.c.task_id == tid, s.runner_calls.c.id == call["id"])
            .values(data=data)
        )
        return {
            "binding": binding,
            "arguments": args,
            **({"network": network} if network else {}),
        }

    @staticmethod
    async def _record_command(conn, tid, call, runner_binding, binding, options):
        await conn.execute(
            insert(s.sandbox_command_records).values(
                task_id=tid,
                id=call["data"]["invocation_id"],
                binding_id=runner_binding["id"],
                agent_id=binding["agent_id"],
                role=binding["role"],
                challenge_id=UUID(binding["challenge_id"]) if binding["challenge_id"] else None,
                assignment_id=binding["assignment_id"],
                generation=binding["generation"],
                epoch=binding["epoch"],
                state="dispatched",
                created_at=now(),
                data={
                    "binding": binding,
                    **options,
                    "stdout": "",
                    "stderr": "",
                    "stdout_bytes": 0,
                    "stderr_bytes": 0,
                    "output_truncated": False,
                    "scope_state": "active",
                },
            )
        )

    @staticmethod
    def _host_epoch_floor(resource):
        history = resource["data"].get("recovery_history", [])
        return history[-1]["replacement_epoch"] if history else 0

    async def settle_unsent(self, conn, tid, call):
        await self.gateway._save_tool_result(
            conn,
            tid,
            call,
            {
                "content": [{"type": "text", "text": "The original command was not sent."}],
                "isError": True,
            },
        )

    async def event(self, tid, invocation_id, key, event, payload):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            resource, _ = await self.host(conn, tid, key)
            call = await self.gateway._call(conn, tid, "tool:" + invocation_id)
            if (
                not call
                or call["owner_epoch"] < self._host_epoch_floor(resource)
                or call["provider_key"] != "sandbox"
                or not call["data"].get("sandbox_binding")
                or call["state"] not in {"dispatched", "unknown", "completed"}
            ):
                raise HTTPException(403, "Host callbacks require an original Sandbox dispatch")
            name = call["data"]["tool_name"]
            events = {
                "exec_command": {"execution_bound", "output", "returned"},
                "execution_status": {"status"},
                "execution_output": {"logs", "logs_unavailable"},
                "interrupt_execution": {"cancel"},
            }
            if event != "unknown" and event not in events.get(name, set()):
                raise HTTPException(403, "Callback does not match the original Sandbox tool")
            if event != "unknown" and call["data"].get("sdk_admitted") is not True:
                raise HTTPException(403, "Original SDK dispatch was not admitted")
            if call["state"] == "completed":
                return {"saved": True}
            if (
                event == "unknown"
                and call["data"].get("network_source_id")
                and call["data"].get("sdk_admitted") is not True
            ):
                await self.settle_unsent(conn, tid, call)
                return {"saved": True}
            command_id = (
                invocation_id if name == "exec_command" else call["data"].get("sandbox_command_id")
            )
            if event == "unknown":
                await conn.execute(
                    update(s.runner_calls)
                    .where(
                        s.runner_calls.c.task_id == tid,
                        s.runner_calls.c.id == call["id"],
                        s.runner_calls.c.state == "dispatched",
                    )
                    .values(state="unknown")
                )
            if command_id is None:
                return {"saved": True}
            command = await self.audit(conn, tid, command_id)
            data, values = dict(command["data"]), {}
            terminal = command["state"] in {"completed", "failed"}
            execution_id = payload.get("execution_id")
            if event == "execution_bound":
                if (
                    not isinstance(execution_id, str)
                    or re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", execution_id) is None
                ):
                    raise HTTPException(422, "An actual execution ID is required")
                if command["execution_id"] not in {None, execution_id}:
                    raise HTTPException(409, "Original command execution ID changed")
                if not command["execution_id"]:
                    values.update(execution_id=execution_id, started_at=now(), state="running")
            elif event == "unknown":
                if name == "exec_command" and not terminal:
                    values["state"] = "unknown"
            elif not command["execution_id"] or execution_id != command["execution_id"]:
                raise HTTPException(409, "Original execution identity is required")
            elif event == "output":
                self._output(data, payload)
            elif event == "returned":
                evidence = payload.get("evidence")
                code = payload.get("exit_code")
                if (
                    payload.get("background") != data["background"]
                    or evidence
                    not in (
                        {"background_start_ack", "background_start_error"}
                        if data["background"]
                        else {"foreground_terminal"}
                    )
                    or (evidence != "background_start_ack" and type(code) is not int)
                ):
                    raise HTTPException(409, "Official command completion evidence is incomplete")
                if terminal and code != command["exit_code"]:
                    raise HTTPException(409, "Original command exit code changed")
                if evidence != "background_start_ack" and not terminal:
                    values.update(
                        state="completed" if code == 0 else "failed",
                        exit_code=code,
                        finished_at=now(),
                    )
                elif not terminal:
                    values["state"] = "running"
                data["receipt"] = evidence
                for stream in ("stdout", "stderr"):
                    total = payload.get("observed_output_bytes", {}).get(
                        stream, data[stream + "_bytes"]
                    )
                    if type(total) is not int or total < data[stream + "_bytes"]:
                        raise HTTPException(409, "Original output byte total moved backwards")
                    data[stream + "_bytes"] = total
                    data["output_truncated"] |= (
                        payload.get("output_truncated", {}).get(stream) is True
                    )
            elif event == "status":
                code = payload.get("exit_code")
                if payload.get("running") is False and type(code) is int:
                    if terminal and command["exit_code"] != code:
                        raise HTTPException(409, "Original command exit code changed")
                    if not terminal:
                        values.update(
                            state="completed" if code == 0 else "failed",
                            exit_code=code,
                            finished_at=now(),
                        )
                elif payload.get("running") is True and not terminal:
                    values["state"] = "running"
            elif event == "logs":
                self._logs(data, payload)
            elif event == "logs_unavailable":
                data["output_truncated"] = True
            elif event == "cancel":
                phase = payload.get("phase")
                if phase not in {"requested", "acknowledged"}:
                    raise HTTPException(422, "Invalid interrupt receipt phase")
                if data.get("cancel_phase") != "acknowledged":
                    data["cancel_phase"] = phase
            await conn.execute(
                update(s.sandbox_command_records)
                .where(
                    s.sandbox_command_records.c.task_id == tid,
                    s.sandbox_command_records.c.id == command_id,
                )
                .values(data=data, **values)
            )
            return {"saved": True}

    @staticmethod
    def _retain(text, chunk):
        remaining = MAX_COMMAND_OUTPUT_BYTES - len(text.encode())
        # Do not expand a truncated UTF-8 sequence past the persisted byte budget.
        return text + chunk[: max(0, remaining)].decode("utf-8", errors="ignore"), len(
            chunk
        ) > remaining

    def _output(self, data, payload):
        stream, offset = payload.get("stream"), payload.get("offset")
        if stream not in {"stdout", "stderr"} or type(offset) is not int or offset < 0:
            raise HTTPException(422, "Invalid command byte cursor")
        try:
            chunk = base64.b64decode(payload.get("data", ""), validate=True)
        except (ValueError, TypeError) as error:
            raise HTTPException(422, "Invalid command output bytes") from error
        size = payload.get("size")
        if type(size) is not int or size < len(chunk) or len(chunk) > MAX_COMMAND_OUTPUT_BYTES:
            raise HTTPException(422, "Command output exceeds its bound")
        if offset != data[stream + "_bytes"]:
            raise HTTPException(409, "Original command byte cursor changed")
        data[stream], truncated = self._retain(data[stream], chunk)
        data[stream + "_bytes"] = offset + size
        data["output_truncated"] |= (
            truncated or payload.get("truncated") is True or size != len(chunk)
        )

    def _logs(self, data, payload):
        content, cursor, start = (
            payload.get("content"),
            payload.get("cursor"),
            payload.get("from_cursor"),
        )
        if (
            not data["background"]
            or not isinstance(content, str)
            or type(cursor) is not int
            or type(start) is not int
            or not 0 <= start <= cursor
        ):
            raise HTTPException(422, "Invalid original background byte cursor")
        previous = data.get("log_cursor", 0)
        if cursor <= previous:
            return
        if start != previous:
            data["output_truncated"] = True
            return
        data["stdout"], truncated = self._retain(data["stdout"], content.encode())
        data["stdout_bytes"] = max(data["stdout_bytes"], cursor)
        data["log_cursor"] = cursor
        data["output_truncated"] |= (
            truncated
            or payload.get("truncated") is True
            or payload.get("text_integrity") == "loss_possible"
        )

    async def complete(self, tid, invocation_id, key, result):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            resource, _ = await self.host(conn, tid, key)
            call = await self.gateway._call(conn, tid, "tool:" + invocation_id)
            if (
                call is None
                or call["owner_epoch"] < self._host_epoch_floor(resource)
                or call["provider_key"] != "sandbox"
                or not call["data"].get("sandbox_binding")
            ):
                raise HTTPException(403, "Host result requires an original Sandbox dispatch")
            if call["state"] == "completed":
                return {"result": json.loads(self.gateway._permit(call)["result_json"])}
            if (
                call["state"] not in {"dispatched", "unknown"}
                or call["data"].get("sdk_admitted") is not True
            ):
                raise HTTPException(409, "Original SDK dispatch was not admitted")
            if call["data"]["tool_name"] == "exec_command":
                record = await self.audit(conn, tid, invocation_id)
                value = result.get("structuredContent", {}).get("result", {})
                if (
                    not record["data"].get("receipt")
                    or not record["execution_id"]
                    or value.get("execution_id") != record["execution_id"]
                    or value.get("command_id") != invocation_id
                ):
                    raise HTTPException(409, "Original command completion evidence is incomplete")
            await self.gateway._save_tool_result(conn, tid, call, result)
            return {"result": result}

    async def _scope_live(self, conn, tid, binding_id):
        try:
            task = await self.gateway.control._task(conn, tid)
            if NativeMCPRoutes.stopping(task):
                return False
            owner = await self.gateway.control._current_owner(conn, tid)
            binding = await row(conn, s.runner_bindings, tid, binding_id)
            if binding["owner_epoch"] != owner["epoch"] or binding["status"] not in {
                "registered",
                "prepared",
            }:
                return False
            await self.gateway.control._mapping(conn, tid, binding, allow_pending=True)
            return True
        except HTTPException as error:
            if error.status_code not in {403, 404, 409}:
                raise
            return False

    async def pending_scopes(self, tid, key):
        from bbx_blackboard.native_recovery import exited_scopes

        records = s.sandbox_command_records
        async with self.repo.engine.connect() as conn:
            resource, _ = await self.host(conn, tid, key)
            pending = (
                (
                    await conn.execute(
                        select(records)
                        .where(
                            records.c.task_id == tid,
                            ~exited_scopes(resource, records),
                            records.c.data["scope_state"].astext != "sealed",
                        )
                        .distinct(records.c.binding_id)
                        .order_by(records.c.binding_id, records.c.sequence)
                    )
                )
                .mappings()
                .all()
            )
            result = []
            for record in pending:
                if not await self._scope_live(conn, tid, record["binding_id"]):
                    result.append(
                        {"command_id": record["id"], "binding": record["data"]["binding"]}
                    )
                    if len(result) >= 8:
                        break
            return result

    async def _scope_patch(self, conn, tid, binding_id, patch):
        records = s.sandbox_command_records
        await conn.execute(
            update(records)
            .where(records.c.task_id == tid, records.c.binding_id == binding_id)
            .values(data=records.c.data.op("||")(literal(patch, type_=JSONB)))
        )

    async def claim_scope(self, tid, command_id, key):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            resource, _ = await self.host(conn, tid, key)
            command = await self.audit(conn, tid, command_id)
            if command["epoch"] < self._host_epoch_floor(resource):
                raise HTTPException(403, "Sandbox scope belongs to an exited epoch")
            if command["data"].get("scope_state") == "sealed" or await self._scope_live(
                conn, tid, command["binding_id"]
            ):
                raise HTTPException(409, "Original Assignment does not need a stop")
            first = command["data"]["scope_state"] == "active"
            if first:
                await self._scope_patch(
                    conn, tid, command["binding_id"], {"scope_state": "stopping"}
                )
            return {"stop": first}

    async def scope_event(self, tid, command_id, key, event, payload):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            resource, _ = await self.host(conn, tid, key)
            command = await self.audit(conn, tid, command_id)
            if command["epoch"] < self._host_epoch_floor(resource):
                raise HTTPException(403, "Sandbox scope belongs to an exited epoch")
            data = command["data"]
            if data["scope_state"] == "sealed":
                return {"saved": True}
            binding = data["binding"]
            scope_id = hashlib.sha256(
                json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if data["scope_state"] != "stopping" or payload.get("scope_id") != scope_id:
                raise HTTPException(409, "Original Assignment cleanup identity changed")
            execution_id = payload.get("execution_id")
            if (
                event == "scope_bound"
                and isinstance(execution_id, str)
                and re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", execution_id)
            ):
                await self._scope_patch(
                    conn, tid, command["binding_id"], {"scope_execution_id": execution_id}
                )
            elif event == "scope_observed":
                receipt = payload.get("receipt", {})
                if (
                    not execution_id
                    or execution_id != data.get("scope_execution_id")
                    or receipt.get("scope_id") != scope_id
                    or receipt.get("execution_context_id") != binding["execution_context_id"]
                    or receipt.get("state") != "confirmed"
                    or receipt.get("reason") != "observed_empty"
                    or receipt.get("sealed") is not True
                    or type(receipt.get("populated")) is not int
                    or receipt["populated"] != 0
                ):
                    raise HTTPException(
                        409, "Original Assignment is not confirmed sealed and empty"
                    )
                await self._scope_patch(
                    conn,
                    tid,
                    command["binding_id"],
                    {"scope_state": "sealed", "scope_id": scope_id},
                )
                await self.gateway.control.native_worker_cleanup_changed(
                    conn, tid, command["binding_id"]
                )
            elif event != "scope_unknown":
                raise HTTPException(422, "Invalid Assignment cleanup callback")
            return {"saved": True}

    def record_query(self, tid, scope):
        records = s.sandbox_command_records
        return (
            select(
                records,
                s.runner_calls.c.data["arguments_ciphertext"].astext.label("arguments_ciphertext"),
            )
            .join(
                s.runner_calls,
                (s.runner_calls.c.task_id == records.c.task_id)
                & (s.runner_calls.c.id == literal("tool:") + records.c.id),
            )
            .where(
                records.c.task_id == tid,
                *(records.c[name] == value for name, value in scope.items()),
            )
        )

    async def page(self, tid, *, scope, before=None, limit=50):
        async with self.repo.engine.connect() as conn:
            query = self.record_query(tid, scope)
            if before is not None:
                query = query.where(s.sandbox_command_records.c.sequence < before)
            rows = (
                (
                    await conn.execute(
                        query.order_by(s.sandbox_command_records.c.sequence.desc()).limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            items = [self.summary(command) for command in rows[:limit]]
            return SandboxCommandPage(
                items=items, next_before=items[-1].sequence if len(rows) > limit else None
            )

    def summary(self, command):
        data = command["data"]
        arguments = json.loads(
            self.gateway.cipher.decrypt(command["arguments_ciphertext"].encode())
        )
        return SandboxCommandSummary(
            **{
                name: command[name]
                for name in SandboxCommandSummary.model_fields
                if name
                not in {"command", "cwd", "background", "output_truncated", "parent_agent_id"}
            },
            parent_agent_id=data.get("binding", {}).get("parent_agent_id"),
            command=arguments["command"],
            cwd=data["cwd"],
            background=data["background"],
            output_truncated=data["output_truncated"],
        )

    async def detail(self, tid, command_id, *, scope):
        async with self.repo.engine.connect() as conn:
            command = (
                (
                    await conn.execute(
                        self.record_query(tid, scope).where(
                            s.sandbox_command_records.c.id == command_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if command is None:
                raise HTTPException(404, "Original Sandbox command is unavailable")
            return SandboxCommandDetail(
                **self.summary(command).model_dump(),
                **{
                    name: command["data"][name]
                    for name in ("stdout", "stderr", "stdout_bytes", "stderr_bytes")
                },
            )


router = APIRouter(tags=["sandbox-commands"])


def _tools(request):
    gateway = request.app.state.runner_gateway
    if gateway is None:
        raise HTTPException(503, "Task Sandbox tools are unavailable")
    return gateway.sandbox


def _report_key(request):
    values = request.headers.getlist("x-bbx-sandbox-report")
    if len(values) != 1 or not values[0] or len(values[0]) > 128:
        raise HTTPException(401, "Task Sandbox host identity required")
    return values[0]


async def _host_body(request, model, maximum):
    key = _report_key(request)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > maximum:
            raise HTTPException(413, "Sandbox host request exceeds its byte bound")
        raw.extend(chunk)
    body, _ = decode(model, raw, maximum)
    return body, key


def sandbox_mcp_identity(request: Request, tid: UUID):
    identity = mcp_identity(request, tid)
    if identity.audience not in {"mcp", "sandbox-mcp"}:
        raise HTTPException(403, "A Sandbox target capability is required")
    return identity


@router.post("/api/tasks/{task_id}/sandbox-host/authorize", include_in_schema=False)
async def host_authorize(request: Request, task_id: UUID):
    body, key = await _host_body(request, SandboxMcpAuthorize, 16 * 1024)
    identity = require_gateway_identity(request, task_id, "sandbox-mcp")
    service = _tools(request)
    async with service.repo.engine.connect() as conn:
        resource, _ = await service.host(conn, task_id, key)
        if resource["data"].get("mcp_transport") != "direct-v2":
            raise HTTPException(403, "The original Sandbox target uses a different transport")
        _, _, binding = await service.gateway._scope(conn, task_id, identity, metadata=True)
        sdk = binding["sdk_session_id"]
        if (sdk is not None and sdk != body.sdk_session_id) or (
            sdk is None and body.method in {"tools/call", "notifications/cancelled"}
        ):
            raise HTTPException(403, "Original Sandbox SDK identity changed")
    return {"authorized": True}


@router.get("/api/tasks/{task_id}/sandbox-host/catalog")
async def host_catalog(request: Request, task_id: UUID):
    service = _tools(request)
    async with service.repo.engine.connect() as conn:
        await service.host(conn, task_id, _report_key(request))
    identity = sandbox_mcp_identity(request, task_id)
    return [
        name for name in await service.gateway.catalog(task_id, identity) if name in SANDBOX_TOOLS
    ]


@router.post("/api/tasks/{task_id}/sandbox-host/claim")
async def host_claim(request: Request, task_id: UUID):
    service = _tools(request)
    async with service.repo.engine.connect() as conn:
        await service.host(conn, task_id, _report_key(request))
    identity = sandbox_mcp_identity(request, task_id)
    body, _ = await _host_body(request, SandboxToolClaim, 2 * 1024 * 1024)
    if request.headers.getlist("x-bbx-sdk-session-id") != [body.invocation.sdk_session_id]:
        raise HTTPException(403, "Original Sandbox SDK identity changed")
    return await service.claim(task_id, identity, body.invocation, body.name, body.arguments)


@router.post("/api/tasks/{task_id}/sandbox-host/admit")
async def host_admit(request: Request, task_id: UUID):
    service = _tools(request)
    identity = sandbox_mcp_identity(request, task_id)
    body, key = await _host_body(request, SandboxToolClaim, 2 * 1024 * 1024)
    async with service.repo.engine.begin() as conn:
        await service.repo.lock(conn, task_id)
        resource, _ = await service.host(conn, task_id, key)
        call, owner, runner_binding = await service.gateway._tool(
            conn, task_id, identity, body.invocation
        )
        current = await service.binding(conn, task_id, runner_binding, owner)
        if (
            not call
            or call["provider_key"] != "sandbox"
            or call["state"] != "dispatched"
            or call["data"].get("sandbox_binding") != current
            or call["data"].get("sdk_admitted") is True
            or call["data"]["authorized_wait_until_ms"] <= int(now().timestamp() * 1000)
        ):
            raise HTTPException(409, "Original Sandbox dispatch has no current SDK admission")
        if call["data"]["tool_name"] == "exec_command":
            vpn_solver_deadline(resource)
        if opted_in(resource) and call["data"]["tool_name"] == "exec_command":
            entry = service.traffic.binding(resource, runner_binding["id"])
            grant = (entry or {}).get("grant", {})
            if (
                not entry
                or entry["state"] != "ready"
                or entry.get("loss")
                or entry["scope"]["source_id"] != call["data"].get("network_source_id")
                or body.network_grant_revision is None
                or grant.get("grant_revision") != body.network_grant_revision
                or grant.get("authority_until_ms", 0) <= int(now().timestamp() * 1000)
            ):
                raise HTTPException(409, "Original Sandbox command lacks its current capture grant")
            await service._record_command(
                conn, task_id, call, runner_binding, current, call["data"]["command_options"]
            )
        await conn.execute(
            update(s.runner_calls)
            .where(s.runner_calls.c.task_id == task_id, s.runner_calls.c.id == call["id"])
            .values(data={**call["data"], "sdk_admitted": True})
        )
        return {"admitted": True}


@router.post("/api/tasks/{task_id}/sandbox-host/calls/{invocation_id}/events")
async def host_event(request: Request, task_id: UUID, invocation_id: str):
    body, key = await _host_body(request, SandboxHostEvent, 128 * 1024)
    return await _tools(request).event(task_id, invocation_id, key, body.event, body.payload)


@router.post("/api/tasks/{task_id}/sandbox-host/calls/{invocation_id}/result")
async def host_result(request: Request, task_id: UUID, invocation_id: str):
    body, key = await _host_body(request, SandboxHostResult, 2 * 1024 * 1024)
    return await _tools(request).complete(task_id, invocation_id, key, body.result)


async def _read_scope(request, tid):
    service = _tools(request)
    try:
        identity = require_task_reader(request, tid)
    except HTTPException:
        identity = None
    async with service.repo.engine.connect() as conn:
        if identity and identity.kind in {"user", "service"}:
            await row(conn, s.tasks, tid)
            return {}
        capability = sandbox_mcp_identity(request, tid)
        _, owner, runner_binding = await service.gateway._scope(
            conn, tid, capability, metadata=True
        )
        binding = await service.binding(conn, tid, runner_binding, owner)
        return (
            {}
            if binding["role"] == "lead"
            else {
                "agent_id": binding["agent_id"],
                "assignment_id": binding["assignment_id"],
                "generation": binding["generation"],
                "epoch": binding["epoch"],
            }
        )


@router.get("/api/tasks/{task_id}/sandbox/commands", response_model=SandboxCommandPage)
async def command_records(
    request: Request,
    response: Response,
    task_id: UUID,
    agent_id: str | None = None,
    before: Annotated[int | None, Query(gt=0, le=9223372036854775807)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    scope = await _read_scope(request, task_id)
    if agent_id and scope and agent_id != scope["agent_id"]:
        raise HTTPException(403, "Command records exceed the current Assignment")
    if agent_id:
        scope["agent_id"] = agent_id
    response.headers["Cache-Control"] = "no-store"
    return await _tools(request).page(task_id, scope=scope, before=before, limit=limit)


@router.get(
    "/api/tasks/{task_id}/sandbox/commands/{command_id}", response_model=SandboxCommandDetail
)
async def command_record(request: Request, response: Response, task_id: UUID, command_id: str):
    scope = await _read_scope(request, task_id)
    response.headers["Cache-Control"] = "no-store"
    return await _tools(request).detail(task_id, command_id, scope=scope)


@router.get("/api/tasks/{task_id}/sandbox-host/scopes")
async def host_scopes(request: Request, task_id: UUID):
    return await _tools(request).pending_scopes(task_id, _report_key(request))


@router.post("/api/tasks/{task_id}/sandbox-host/scopes/{command_id}/claim")
async def scope_claim(request: Request, task_id: UUID, command_id: str):
    return await _tools(request).claim_scope(task_id, command_id, _report_key(request))


@router.post("/api/tasks/{task_id}/sandbox-host/scopes/{command_id}/events")
async def scope_event(request: Request, task_id: UUID, command_id: str):
    body, key = await _host_body(request, SandboxHostEvent, 32768)
    return await _tools(request).scope_event(task_id, command_id, key, body.event, body.payload)


_NETWORK = "/api/tasks/{task_id}/sandbox-host/network"


@router.get(_NETWORK)
async def network_scopes(request: Request, task_id: UUID):
    return await _tools(request).traffic.list(task_id, _report_key(request))


@router.post(_NETWORK + "/{source_id}/observe")
async def network_observe(request: Request, task_id: UUID, source_id: str):
    body, key = await _host_body(request, CaptureReady, 4096)
    return await _tools(request).traffic.observe(task_id, key, source_id, body)


@router.post(_NETWORK + "/{source_id}/permit")
async def network_permit(request: Request, task_id: UUID, source_id: str):
    return await _tools(request).traffic.permit(task_id, _report_key(request), source_id)


@router.post(_NETWORK + "/{source_id}/seal")
async def network_seal(request: Request, task_id: UUID, source_id: str):
    body, key = await _host_body(request, CaptureSeal, 4096)
    return await _tools(request).traffic.seal(task_id, key, source_id, body)


@router.post(_NETWORK + "/{source_id}/bytes")
async def network_bytes(request: Request, task_id: UUID, source_id: str):
    service = _tools(request)
    key = _report_key(request)
    async with service.repo.engine.connect() as conn:
        await service.host(conn, task_id, key)
    objects = request.app.state.objects
    if objects is None:
        raise HTTPException(503, "Original capture storage is unavailable")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > PART_LIMIT:
            raise HTTPException(413, "Original capture exceeds its sealed byte bound")
        data.extend(chunk)
    return await service.traffic.upload(task_id, key, source_id, bytes(data), objects)
