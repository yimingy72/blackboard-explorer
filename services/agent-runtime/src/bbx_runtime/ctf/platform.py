"""Pinned platform tools with checkpointed, single-dispatch MCP calls."""

import asyncio
import hashlib
import io
import json
from typing import Any
from uuid import uuid4

import httpx
from agent_framework import FunctionInvocationContext, tool
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class PlatformAdapter:
    def __init__(self, service: Any, objects_factory: Any, transport: Any = None) -> None:
        self.service = service
        self.objects_factory = objects_factory
        self.transport = transport

    async def request(self, server: dict, headers: dict, name: str | None, arguments: dict) -> Any:
        if self.transport is not None:
            return await self.transport(server, headers, name, arguments)
        async with httpx.AsyncClient(headers=headers, timeout=120) as http:
            async with streamable_http_client(server["url"], http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if name is None:
                        return (await session.list_tools()).model_dump(mode="json")
                    # Intentionally bypass MCPTool's automatic transport retry layer.
                    return (await session.call_tool(name, arguments)).model_dump(mode="json")

    async def build(
        self,
        profile: Any,
        role: str,
        task_id: str,
        member_id: str,
        turn: dict,
        checkpoint_provider: Any,
    ) -> list[Any]:
        result = []
        for grant in profile.worker_tools[role].mcp_servers:
            selected = [
                binding
                for binding in profile.platform_tools
                if binding.server_name == grant.name
                and binding.server_version == grant.version
                and binding.tool_name in (grant.allowed_tools or [])
                and binding.purpose != "unknown"
                and (role == "lead" or binding.purpose in {"submit", "status"})
            ]
            if not selected:
                continue
            server = await self.service.client.get_mcp_server(grant.name, grant.version)
            if (
                server.get("name") != grant.name
                or server.get("version") != grant.version
                or not server.get("enabled")
            ):
                raise ValueError("Pinned MCP server is unavailable")
            headers = {}
            if server.get("has_secret"):
                credentials = await self.service.client.get_mcp_credentials(
                    grant.name, grant.version
                )
                secret = credentials.get("secret")
                if not isinstance(secret, str) or not secret:
                    raise ValueError("MCP credential is unavailable")
                headers[server["auth_header"]] = f"{server['auth_scheme']} {secret}".strip()
            catalog = await self.request(server, headers, None, {})
            catalog = {item["name"]: item for item in catalog["tools"]}
            for binding in selected:
                if binding.tool_name not in catalog:
                    raise ValueError("Configured MCP tool is unavailable")
                result.append(
                    self.wrapper(
                        binding,
                        server,
                        headers,
                        catalog[binding.tool_name],
                        task_id,
                        member_id,
                        turn,
                        checkpoint_provider,
                    )
                )
        return result

    def wrapper(
        self,
        binding: Any,
        server: dict,
        headers: dict,
        definition: dict,
        task_id: str,
        member_id: str,
        turn: dict,
        checkpoint_provider: Any,
    ) -> Any:
        @tool(
            name=f"platform_{binding.server_name}_{binding.tool_name}",
            description=f"Configured {binding.purpose} tool. Remote arguments schema: "
            f"{json.dumps(definition['inputSchema'])}. "
            "Unknown outcomes require status query; never resubmit blindly.",
        )
        async def invoke(
            challenge_id: str,
            expected_revision: int,
            arguments: dict[str, Any],
            ctx: FunctionInvocationContext,
        ) -> str:
            cancelled = False
            checkpoint = checkpoint_provider()
            identity = dict(
                agent_id=member_id,
                turn_id=turn["id"],
                generation=turn["generation"],
                server_name=binding.server_name,
                server_version=binding.server_version,
                tool_name=binding.tool_name,
                challenge_id=challenge_id,
                expected_revision=expected_revision,
            )
            calls: dict[str, Any] = checkpoint.session.state.setdefault("ctf_platform_calls", {})
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "server": binding.server_name,
                        "version": binding.server_version,
                        "tool": binding.tool_name,
                        "challenge": challenge_id,
                        "arguments": arguments,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            occurrence = ctx.metadata.setdefault("ctf_platform_occurrence", str(uuid4()))
            occurrence = ctx.metadata.get("function_call_occurrence_id") or occurrence
            key = f"{turn['id']}:{occurrence}"
            entry: Any = calls.get(key)
            if entry is None:
                await self.service.runtime(task_id, "authorize_platform_call", **identity)
                # A changed model occurrence must not blindly repeat an uncertain write.
                if not binding.read_only and any(
                    item["digest"] == digest
                    and (item["state"] in {"dispatched", "unknown"} or "record" not in item)
                    for item in calls.values()
                ):
                    return json.dumps(
                        {
                            "status": "unknown",
                            "message": "Prior call outcome unknown; query platform status.",
                        }
                    )
                entry = calls[key] = {
                    "digest": digest,
                    "call_id": str(uuid4()),
                    "state": "dispatched",
                }
                await checkpoint.save()
                await self.service.runtime(
                    task_id, "begin_platform_call", **identity, call_id=entry["call_id"]
                )
                try:
                    entry["response"] = await self.request(
                        server, headers, binding.tool_name, arguments
                    )
                    entry["state"] = "received"
                except asyncio.CancelledError:
                    cancelled = True
                    entry.update(
                        state="unknown",
                        response={"status": "unknown", "error": "cancelled call outcome unknown"},
                    )
                except Exception:
                    entry.update(
                        state="unknown",
                        response={"status": "unknown", "error": "transport outcome unknown"},
                    )
                await checkpoint.save()
            elif entry["digest"] != digest:
                raise ValueError("Platform occurrence arguments changed")
            if "response" not in entry:
                entry.update(
                    state="unknown",
                    response={"status": "unknown", "error": "interrupted call outcome unknown"},
                )
            if "record" not in entry:
                payload = json.dumps(entry["response"], ensure_ascii=False).encode()
                uri = entry.setdefault("uri", f"evidence/{task_id}/{uuid4()}/platform-result.json")
                await checkpoint.save()
                await self.objects_factory().put(uri, io.BytesIO(payload), length=len(payload))
                entry["record"] = await self.service.runtime(
                    task_id,
                    "record_platform_result",
                    **identity,
                    request_id=entry["call_id"],
                    call_id=entry["call_id"],
                    response_uri=uri,
                    response_sha256=hashlib.sha256(payload).hexdigest(),
                )
                await checkpoint.save()
            if cancelled:
                raise asyncio.CancelledError
            return json.dumps(
                {"result": entry["response"], "evidence": entry["record"]}, ensure_ascii=False
            )

        return invoke
