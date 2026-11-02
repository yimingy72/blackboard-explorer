"""Boot-fenced CTF commands using one official MCP request per attempt."""

import asyncio
import base64
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from agent_framework import FunctionInvocationContext
from bbx_contracts.ctf import CtfDrainProof, execution_agent_id
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bbx_runtime.clients.blackboard import RemoteError, is_control_transport_error
from bbx_runtime.clients.envd import EnvdClient
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.failures import CtfToolFailure
from bbx_runtime.ctf.telemetry import observe
from bbx_runtime.session import SessionCheckpoint


class DrainPending(RemoteError):
    """The known generation is stopping, but process reaping is not confirmed."""

    def __init__(self) -> None:
        super().__init__(409, "Execution drain is still pending")


class UnknownCommandOutcome(RuntimeError):
    """An unknown boot or command outcome forbids automatic replay."""


class CtfExecutionAdapter:
    def __init__(self, service: CtfClient, envd: EnvdClient) -> None:
        self.service, self.envd = service, envd
        self.pending_registrations: dict[tuple[str, str], dict[str, Any]] = {}
        self.job_locks: dict[tuple[str, str], asyncio.Lock] = {}

    @staticmethod
    def pending_jobs(checkpoint: SessionCheckpoint, turn: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            entry
            for entry in checkpoint.session.state.get("ctf_commands", {}).values()
            if entry.get("mode") == "async"
            and entry.get("turn_id") == str(turn["id"])
            and (
                entry.get("state") != "finished"
                or (
                    entry.get("delivery") == "notification" and not entry.get("completion_notified")
                )
            )
        ]

    @staticmethod
    def job_entry(checkpoint: SessionCheckpoint, command_id: str) -> dict[str, Any]:
        matches = [
            entry
            for entry in checkpoint.session.state.get("ctf_commands", {}).values()
            if entry.get("mode") == "async" and entry.get("command_id") == command_id
        ]
        if len(matches) != 1:
            raise ValueError("Command is not in this member's saved session")
        return matches[0]

    async def _registration(self, identity: dict[str, Any]) -> dict[str, Any]:
        status = await self.envd.ctf_status(agent_id=identity["agent_id"])
        if any(status.get(key) != identity[key] for key in ("task_id", "boot_id", "agent_id")):
            raise UnknownCommandOutcome("Execution registration identity changed")
        if status.get("state") == "unregistered":
            return status
        if (
            status.get("state") not in {"registered", "running", "stopping", "drained"}
            or status.get("member_id") != identity["member_id"]
            or type(status.get("generation")) is not int
            or status["generation"] < 0
        ):
            raise UnknownCommandOutcome(
                "Execution registration is unknown or belongs to another member"
            )
        return status

    async def _register(self, identity: dict[str, Any]) -> None:
        for attempt in range(2):
            try:
                await self.envd.ctf_operation("register", identity)
                return
            except (httpx.TransportError, ConnectionError, RemoteError) as error:
                if not is_control_transport_error(error):
                    raise
                try:
                    status = await self._registration(identity)
                except Exception as read_error:
                    raise error from read_error
                if status.get("generation") == identity["generation"] and status["state"] in {
                    "registered",
                    "running",
                }:
                    return
                # Registration is idempotent for this exact boot/member/generation.
                # A stopped or different generation must never be reopened.
                if status["state"] != "unregistered" or attempt:
                    raise

    async def _record_binding(self, identity: dict[str, Any], turn_id: str | None) -> None:
        for attempt in range(2):
            try:
                await self.service.runtime(
                    identity["task_id"],
                    "record_execution",
                    agent_id=identity["member_id"],
                    turn_id=turn_id,
                    generation=identity["generation"],
                    boot_id=identity["boot_id"],
                )
                self.pending_registrations.pop((identity["task_id"], identity["member_id"]), None)
                return
            except (httpx.TransportError, ConnectionError, RemoteError) as error:
                if not is_control_transport_error(error):
                    raise
                try:
                    state = await self.service.runtime_state(identity["task_id"])
                except Exception as read_error:
                    raise error from read_error
                member = next(
                    (m for m in state["members"] if m["id"] == identity["member_id"]), None
                )
                binding = (member or {}).get("execution") or {}
                if all(binding.get(key) == identity[key] for key in ("boot_id", "generation")):
                    self.pending_registrations.pop(
                        (identity["task_id"], identity["member_id"]), None
                    )
                    return
                if (
                    attempt
                    or member is None
                    or member.get("generation") != identity["generation"]
                    or member.get("current_turn_id") != turn_id
                ):
                    raise
                # Retry only the same fenced binding after authoritative readback.

    async def _drain_identity(self, identity: dict[str, Any]) -> dict[str, Any]:
        proof = await self.envd.ctf_operation("drain", identity)
        return self._drain_proof(proof, identity)

    @staticmethod
    def _drain_proof(proof: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
        if any(proof.get(key) != identity[key] for key in identity):
            raise UnknownCommandOutcome("Execution drain identity changed")
        if proof.get("drained") is not True:
            raise DrainPending()
        # Status may gain capabilities; the backend accepts only its drain contract.
        return {key: value for key, value in proof.items() if key in CtfDrainProof.model_fields}

    async def identity(
        self, task_id: str, member_id: str, generation: int, boot_id: str | None = None
    ) -> dict[str, Any]:
        status = await self.envd.ctf_status()
        if status.get("state") == "unknown" or str(status.get("task_id")) != task_id:
            raise UnknownCommandOutcome("Execution registry is unknown or belongs to another task")
        if boot_id is not None and status["boot_id"] != boot_id:
            raise UnknownCommandOutcome("Execution boot changed; old process drain is unproven")
        return {
            "task_id": task_id,
            "member_id": member_id,
            "agent_id": execution_agent_id(member_id),
            "generation": generation,
            "boot_id": status["boot_id"],
        }

    async def prepare(
        self, task_id: str, member: dict[str, Any], turn: dict[str, Any] | None
    ) -> dict[str, Any]:
        generation = turn["generation"] if turn else member["generation"]
        previous = member.get("execution") or {}
        pending = self.pending_registrations.get((task_id, member["id"]), {})
        identity = await self.identity(
            task_id,
            member["id"],
            generation,
            previous.get("replacement_boot_id")
            if previous.get("container_destroyed")
            else previous.get("boot_id", pending.get("boot_id")),
        )
        turn_id = turn["id"] if turn else member.get("current_turn_id")
        status = await self._registration(identity)
        if status.get("generation", generation) > generation:
            raise UnknownCommandOutcome("Execution belongs to a newer generation")
        if status.get("generation", generation) < generation:
            current = next(
                (
                    m
                    for m in (await self.service.runtime_state(task_id))["members"]
                    if m["id"] == member["id"]
                ),
                None,
            )
            if (
                current is None
                or current["generation"] != generation
                or current.get("current_turn_id") != turn_id
            ):
                raise UnknownCommandOutcome("Execution handoff is not owned by this turn")
            # A lost binding from a previous runtime must be stopped on its real
            # registered generation before this generation can be installed.
            old_identity = {**identity, "generation": status["generation"]}
            proof = (
                self._drain_proof(status, old_identity)
                if status.get("drained") is True
                else await self._drain_identity(old_identity)
            )
            old_binding = current.get("execution") or {}
            if not old_binding.get("drained") and all(
                old_binding.get(key) == old_identity[key] for key in ("boot_id", "generation")
            ):
                await self.service.runtime(
                    task_id, "record_execution_drained", agent_id=member["id"], proof=proof
                )
        await self.envd.create_user(identity["agent_id"])
        self.pending_registrations[(task_id, member["id"])] = identity.copy()
        await self._register(identity)
        await self._record_binding(identity, turn_id)
        return identity

    async def stop_member(self, task_id: str, member: dict[str, Any]) -> dict[str, Any]:
        binding = member.get("execution") or {}
        generation = binding.get("generation", member["generation"])
        if binding.get("container_destroyed") and binding.get("drained"):
            # This proof was durably recorded only after the manager verified the
            # exact old container was destroyed; do not re-register it on new boot.
            return {
                "task_id": task_id,
                "agent_id": execution_agent_id(member["id"]),
                "member_id": member["id"],
                "boot_id": binding["boot_id"],
                "generation": generation,
                "state": "drained",
                "drained": True,
            }
        if not binding:
            pending = self.pending_registrations.get((task_id, member["id"]), {})
            identity = await self.identity(
                task_id, member["id"], generation, pending.get("boot_id")
            )
            status = await self._registration(identity)
            if status.get("generation") == generation:
                # Recover a registered (possibly already stopped) handshake using
                # the current turn identity rather than an invalid idle binding.
                self.pending_registrations[(task_id, member["id"])] = identity.copy()
                await self._record_binding(identity, member.get("current_turn_id"))
            else:
                identity = await self.prepare(task_id, member, None)
        else:
            identity = await self.identity(task_id, member["id"], generation, binding["boot_id"])
            status = await self._registration(identity)
            if status.get("generation", generation) != generation:
                if not binding.get("drained") or status.get("generation") != member["generation"]:
                    raise UnknownCommandOutcome(
                        "Execution registration does not match the saved binding"
                    )
                identity = {**identity, "generation": member["generation"]}
                self.pending_registrations[(task_id, member["id"])] = identity.copy()
                await self._record_binding(identity, member.get("current_turn_id"))
            elif status["state"] == "unregistered":
                await self._register(identity)
        proof = await self._drain_identity(identity)
        await self.service.runtime(
            task_id, "record_execution_drained", agent_id=member["id"], proof=proof
        )
        return proof

    async def drain(self, task_id: str) -> bool:
        state = await self.service.runtime_state(task_id)
        # Root status is checked even for an empty roster: a restarted registry
        # must never masquerade as an empty, drained environment.
        root = await self.envd.ctf_status()
        if root.get("state") == "unknown" or str(root.get("task_id")) != task_id:
            return False
        for member in state["members"]:
            identity = {
                "task_id": task_id,
                "member_id": member["id"],
                "agent_id": execution_agent_id(member["id"]),
                "boot_id": root["boot_id"],
                "generation": member["generation"],
            }
            registration = await self._registration(identity)
            if (
                member.get("execution")
                or member.get("current_turn_id")
                or (task_id, member["id"]) in self.pending_registrations
                or registration["state"] != "unregistered"
            ):
                await self.stop_member(task_id, member)
        return True

    async def execute_call(
        self,
        task_id: str,
        member_id: str,
        turn: dict[str, Any],
        checkpoint: SessionCheckpoint,
        context: FunctionInvocationContext,
        arguments: dict[str, Any],
    ) -> str:
        if self.pending_jobs(checkpoint, turn):
            return json.dumps(
                {
                    "state": "busy",
                    "message": "An asynchronous command is pending; wait for its result.",
                }
            )
        await self.service.runtime(
            task_id,
            "authorize_member",
            agent_id=member_id,
            generation=turn["generation"],
            turn_id=turn["id"],
        )
        member = next(
            m
            for m in (await self.service.runtime_state(task_id))["members"]
            if m["id"] == member_id
        )
        binding = member.get("execution") or {}
        identity = await self.identity(
            task_id, member_id, turn["generation"], binding.get("boot_id")
        )
        if not binding or binding.get("generation") != turn["generation"] or binding.get("drained"):
            raise UnknownCommandOutcome("Execution generation is not registered")
        occurrence = context.metadata.setdefault("ctf_command_occurrence", str(uuid4()))
        occurrence = context.metadata.get("function_call_occurrence_id") or occurrence
        key = f"{turn['id']}:{occurrence}"
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
        commands = checkpoint.session.state.setdefault("ctf_commands", {})
        entry = commands.setdefault(
            key,
            {
                "command_id": str(uuid4()),
                "digest": digest,
                "identity": identity,
                "state": "prepared",
            },
        )
        if entry["digest"] != digest or entry["identity"] != identity:
            raise UnknownCommandOutcome("Command identity or arguments changed")
        if entry["state"] == "finished":
            return entry["result"]
        await checkpoint.save()
        metadata = {"ctf": {**identity, "command_id": entry["command_id"]}}
        try:
            result = await self._call(identity, arguments, metadata)
            result = await self.preserve_output(
                task_id, member_id, turn, entry["command_id"], result
            )
        except Exception as error:
            # Dispatch or output preservation can fail after the command's side effects.
            raise CtfToolFailure(error) from error
        entry.update(state="finished", result=result)
        await checkpoint.save()
        return result

    async def start_call(
        self,
        task_id: str,
        member_id: str,
        turn: dict[str, Any],
        checkpoint: SessionCheckpoint,
        context: FunctionInvocationContext,
        arguments: dict[str, Any],
    ) -> str:
        async with self.job_locks.setdefault((task_id, member_id), asyncio.Lock()):
            await self.service.runtime(
                task_id,
                "authorize_member",
                agent_id=member_id,
                generation=turn["generation"],
                turn_id=turn["id"],
            )
            capabilities = await self.envd.ctf_status()
            if "ctf_async_commands_v1" not in capabilities.get("capabilities", []):
                return json.dumps(
                    {
                        "error": {
                            "code": "async_commands_unavailable",
                            "message": "This image does not support asynchronous commands. "
                            "Use execute_command or select a compatible image for a new task.",
                        }
                    }
                )
            occurrence = context.metadata.setdefault("ctf_command_occurrence", str(uuid4()))
            occurrence = context.metadata.get("function_call_occurrence_id") or occurrence
            key = f"{turn['id']}:{occurrence}"
            commands = checkpoint.session.state.setdefault("ctf_commands", {})
            digest = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
            entry = commands.get(key)
            if entry is None:
                if self.pending_jobs(checkpoint, turn):
                    return json.dumps(
                        {
                            "state": "busy",
                            "message": "This member already has a pending asynchronous command.",
                        }
                    )
                member = next(
                    m
                    for m in (await self.service.runtime_state(task_id))["members"]
                    if m["id"] == member_id
                )
                binding = member.get("execution") or {}
                identity = await self.identity(
                    task_id, member_id, turn["generation"], binding.get("boot_id")
                )
                if (
                    not binding
                    or binding.get("generation") != turn["generation"]
                    or binding.get("drained")
                ):
                    raise UnknownCommandOutcome("Execution generation is not registered")
                entry = commands[key] = {
                    "command_id": str(uuid4()),
                    "digest": digest,
                    "identity": identity,
                    "turn_id": str(turn["id"]),
                    "mode": "async",
                    "state": "prepared",
                    "delivery": "notification",
                    "completion_notified": False,
                }
                await checkpoint.save()
                try:
                    response = await self._request_tool(
                        identity,
                        arguments,
                        {"ctf": {**identity, "command_id": entry["command_id"]}},
                        "start_command",
                    )
                    status = json.loads(response).get("structuredContent") or {}
                    if status.get("state") == "busy":
                        # An explicit rejection proves this command was never dispatched.
                        del commands[key]
                        await checkpoint.save()
                        return json.dumps(status)
                except Exception as error:
                    # Only read the same ID after uncertain dispatch; never send it again.
                    try:
                        status = await self.envd.ctf_status(
                            **identity, command_id=entry["command_id"]
                        )
                        if status.get("state") not in {
                            "running",
                            "completed",
                            "timed_out",
                            "cancelled",
                            "failed",
                        }:
                            raise UnknownCommandOutcome("Command dispatch could not be confirmed")
                    except Exception:
                        entry["state"] = "unknown"
                        await checkpoint.save()
                        raise CtfToolFailure(error) from error
            else:
                if entry.get("digest") != digest or entry.get("turn_id") != str(turn["id"]):
                    raise UnknownCommandOutcome("Command occurrence arguments changed")
                if entry["state"] == "finished":
                    return json.dumps(
                        {
                            "command_id": entry["command_id"],
                            "state": entry["terminal_status"],
                            "result": json.loads(entry["result"]),
                        }
                    )
                status = await self.envd.ctf_status(
                    **entry["identity"], command_id=entry["command_id"]
                )
            await self._save_job_status(
                task_id, member_id, turn, checkpoint, entry, status, initial=True
            )
            if entry["state"] == "finished":
                return json.dumps(
                    {
                        "command_id": entry["command_id"],
                        "state": entry["terminal_status"],
                        "result": json.loads(entry["result"]),
                    }
                )
            return json.dumps(
                {
                    "command_id": entry["command_id"],
                    "state": "running",
                    "message": "Continue coordination or finish this response to wait. "
                    "Completion will be delivered; do not poll.",
                }
            )

    async def _save_job_status(
        self,
        task_id: str,
        member_id: str,
        turn: dict[str, Any],
        checkpoint: SessionCheckpoint,
        entry: dict[str, Any],
        status: dict[str, Any],
        *,
        initial: bool = False,
    ) -> None:
        expected = {**entry["identity"], "command_id": entry["command_id"]}
        if any(status.get(key) != value for key, value in expected.items()):
            raise UnknownCommandOutcome("Command status identity changed")
        state = status.get("state")
        if state == "running":
            if entry["state"] != "running":
                entry["state"] = "running"
                await checkpoint.save()
            return
        if state not in {"completed", "timed_out", "cancelled", "failed"} or not isinstance(
            status.get("result"), dict
        ):
            entry["state"] = "unknown"
            await checkpoint.save()
            raise UnknownCommandOutcome("Command outcome is unknown")
        result = await self.preserve_output(
            task_id,
            member_id,
            turn,
            entry["command_id"],
            json.dumps({"structuredContent": status["result"]}, ensure_ascii=False),
        )
        entry.update(state="finished", terminal_status=state, result=result)
        if initial:
            entry.update(delivery="tool_result", completion_notified=True)
        await checkpoint.save()

    async def reconcile_jobs(
        self,
        task_id: str,
        member_id: str,
        turn: dict[str, Any],
        checkpoint: SessionCheckpoint,
        *,
        notify: bool = True,
    ) -> None:
        entries = self.pending_jobs(checkpoint, turn)
        if notify:
            entries += [
                entry
                for entry in checkpoint.session.state.get("ctf_commands", {}).values()
                if entry.get("mode") == "async"
                and entry.get("turn_id") != str(turn["id"])
                and entry.get("state") == "finished"
                and entry.get("delivery") == "notification"
                and not entry.get("completion_notified")
            ]
        for entry in entries:
            if entry["state"] != "finished":
                status = await self.envd.ctf_status(
                    **entry["identity"], command_id=entry["command_id"]
                )
                await self._save_job_status(task_id, member_id, turn, checkpoint, entry, status)
            if notify and entry["state"] == "finished" and entry["delivery"] == "notification":
                confirmed = False
                for attempt in range(2):
                    try:
                        receipt = await self.service.runtime(
                            task_id,
                            "notify_command_result",
                            agent_id=member_id,
                            turn_id=entry["turn_id"],
                            generation=entry["identity"]["generation"],
                            command_id=entry["command_id"],
                        )
                        notice_id = str(
                            uuid5(
                                NAMESPACE_URL,
                                f"ctf-command-result:{task_id}:{member_id}:{entry['turn_id']}:{entry['identity']['generation']}:{entry['command_id']}",
                            )
                        )
                        if (
                            not isinstance(receipt, dict)
                            or receipt.get("id") != notice_id
                            or receipt.get("recipient_id") != member_id
                        ):
                            raise httpx.RemoteProtocolError(
                                "Command notification lacks write proof"
                            )
                        confirmed = True
                        break
                    except Exception as error:
                        if (
                            isinstance(error, RemoteError)
                            and error.code == "ctf_command_notice_unavailable"
                            and entry["turn_id"] != str(turn["id"])
                        ):
                            break
                        if attempt or not is_control_transport_error(error):
                            raise
                        # The service re-reads this deterministic ID, not the command.
                if confirmed:
                    entry["completion_notified"] = True
                    await checkpoint.save()

    async def cancel_call(
        self,
        task_id: str,
        member_id: str,
        turn: dict[str, Any],
        checkpoint: SessionCheckpoint,
        command_id: str,
    ) -> str:
        await self.service.runtime(
            task_id,
            "authorize_member",
            agent_id=member_id,
            turn_id=turn["id"],
            generation=turn["generation"],
        )
        entry = self.job_entry(checkpoint, command_id)
        if entry.get("turn_id") != str(turn["id"]):
            raise ValueError("Only this turn's command can be cancelled")
        status = await self.envd.ctf_operation(
            "cancel-command", {**entry["identity"], "command_id": command_id}
        )
        await self._save_job_status(task_id, member_id, turn, checkpoint, entry, status)
        return json.dumps({"command_id": command_id, "state": status["state"]})

    async def preserve_output(
        self, task_id: str, member_id: str, turn: dict[str, Any], command_id: str, result: str
    ) -> str:
        decoded = json.loads(result)
        output = decoded.get("structuredContent") or {}
        path = output.get("full_output_path")
        if not path:
            return result
        candidate = PurePosixPath(path)
        parent = PurePosixPath("/workspace/agents") / execution_agent_id(member_id) / ".outputs"
        if candidate.parent != parent or ".." in candidate.parts:
            raise UnknownCommandOutcome("Command output path does not belong to this member")
        info = await self.envd.stat(path)
        if not info.get("is_file") or info.get("size", 0) > 8 * 1024 * 1024:
            output["full_output_registration"] = "unregistered: file missing or exceeds 8 MiB"
        else:
            content = await self.envd.read_file(path)
            if len(content) > 8 * 1024 * 1024:
                raise UnknownCommandOutcome("Command output changed during registration")
            saved = await observe(
                self.service,
                task_id,
                member_id,
                turn,
                "tool_result",
                {
                    "tool": "execute_command_full_output",
                    "call_id": command_id,
                    "path": path,
                    "content_base64": base64.b64encode(content).decode(),
                    "size": len(content),
                },
                f"{command_id}:full-output",
            )
            output["full_output_uri"] = saved["uri"]
        decoded["structuredContent"] = output
        return json.dumps(decoded, ensure_ascii=False)

    async def _call(
        self, identity: dict[str, Any], arguments: dict[str, Any], metadata: dict[str, Any]
    ) -> str:
        return await self._request_tool(identity, arguments, metadata, "execute_command")

    async def _request_tool(
        self,
        identity: dict[str, Any],
        arguments: dict[str, Any],
        metadata: dict[str, Any],
        tool_name: str,
    ) -> str:
        # No MCPStreamableHTTPTool retry layer: a transport error is an unknown
        # outcome, and only an explicit subsequent same-ID call may query/replay.
        headers = {**self.envd.headers, "X-Agent-Id": identity["agent_id"]}
        read_timeout = (
            int(arguments.get("timeout_sec", 120)) + 30 if tool_name == "execute_command" else 30
        )
        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(30, read=read_timeout, connect=10),
            trust_env=False,
        ) as http:
            async with streamable_http_client(f"{self.envd.base_url}/mcp", http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments, meta=metadata)
                    if result.isError:
                        raise UnknownCommandOutcome(str(result.content))
                    return result.model_dump_json()

    async def close(self) -> None:
        self.job_locks.clear()
        await self.envd.close()
