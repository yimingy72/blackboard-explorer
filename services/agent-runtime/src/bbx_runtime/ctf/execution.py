"""Boot-fenced CTF commands using one official MCP request per attempt."""

import base64
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

import httpx
from agent_framework import FunctionInvocationContext
from bbx_contracts.ctf import execution_agent_id
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.clients.envd import EnvdClient
from bbx_runtime.ctf.client import CtfClient
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
        identity = await self.identity(
            task_id,
            member["id"],
            generation,
            previous.get("replacement_boot_id")
            if previous.get("container_destroyed")
            else previous.get("boot_id"),
        )
        await self.envd.create_user(identity["agent_id"])
        await self.envd.ctf_operation("register", identity)
        await self.service.runtime(
            task_id,
            "record_execution",
            agent_id=member["id"],
            turn_id=turn["id"] if turn else None,
            generation=generation,
            boot_id=identity["boot_id"],
        )
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
            identity = await self.prepare(task_id, member, None)
        else:
            identity = await self.identity(task_id, member["id"], generation, binding["boot_id"])
        proof = await self.envd.ctf_operation("drain", identity)
        if any(proof.get(key) != identity[key] for key in ("boot_id", "generation", "member_id")):
            raise UnknownCommandOutcome("Execution drain identity changed")
        if not proof.get("drained"):
            raise DrainPending()
        await self.service.runtime(
            task_id, "record_execution_drained", agent_id=member["id"], proof=proof
        )
        return proof

    async def drain(self, task_id: str) -> bool:
        state = await self.service.state(task_id)
        # Root status is checked even for an empty roster: a restarted registry
        # must never masquerade as an empty, drained environment.
        root = await self.envd.ctf_status()
        if root.get("state") == "unknown" or str(root.get("task_id")) != task_id:
            return False
        for member in state["members"]:
            if member.get("execution"):
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
        await self.service.runtime(
            task_id,
            "authorize_member",
            agent_id=member_id,
            generation=turn["generation"],
            turn_id=turn["id"],
        )
        member = next(
            m for m in (await self.service.state(task_id))["members"] if m["id"] == member_id
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
        result = await self._call(identity, arguments, metadata)
        result = await self.preserve_output(task_id, member_id, turn, entry["command_id"], result)
        entry.update(state="finished", result=result)
        await checkpoint.save()
        return result

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
        # No MCPStreamableHTTPTool retry layer: a transport error is an unknown
        # outcome, and only an explicit subsequent same-ID call may query/replay.
        headers = {**self.envd.headers, "X-Agent-Id": identity["agent_id"]}
        async with httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(650, connect=10), trust_env=False
        ) as http:
            async with streamable_http_client(f"{self.envd.base_url}/mcp", http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("execute_command", arguments, meta=metadata)
                    if result.isError:
                        raise UnknownCommandOutcome(str(result.content))
                    return result.model_dump_json()

    async def close(self) -> None:
        await self.envd.close()
