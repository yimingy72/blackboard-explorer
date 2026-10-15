"""Pinned platform tools with checkpointed, single-dispatch MCP calls."""

import asyncio
import hashlib
import io
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import uuid4

from agent_framework import FunctionInvocationContext, MCPStreamableHTTPTool, tool
from mcp import types
from pydantic import Field

from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.tool_results import ToolJsonResult, recoverable_tool_error

logger = logging.getLogger(__name__)


class PlatformCatalogCache:
    """Task-scoped pinned metadata; connection credentials never live in this cache."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, tuple[asyncio.Lock, dict[str, Any]]]] = {}

    def clear(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)

    async def get(
        self, task_id: str, identity: str, load: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        entries = self.tasks.setdefault(task_id, {})
        lock, value = entries.setdefault(identity, (asyncio.Lock(), {}))
        async with lock:
            if not value:
                value.update(await load())
            return value


class PlatformAdapter:
    def __init__(
        self,
        service: Any,
        objects_factory: Any,
        transport: Any = None,
        catalog_cache: PlatformCatalogCache | None = None,
    ) -> None:
        self.service = service
        self.objects_factory = objects_factory
        self.transport = transport
        self.catalog_cache = catalog_cache or PlatformCatalogCache()
        self.connections: dict[str, MCPStreamableHTTPTool] = {}
        self.connection_locks: dict[str, asyncio.Lock] = {}
        self.observation_secrets: list[str] = []

    @staticmethod
    def identity(server: dict, headers: dict, **scope: Any) -> str:
        return hashlib.sha256(
            json.dumps({"server": server, "headers": headers, **scope}, sort_keys=True).encode()
        ).hexdigest()

    async def _close_connection(self, connection: MCPStreamableHTTPTool) -> None:
        primary = sys.exception()
        cleanup = None
        try:
            if primary is None:
                await connection.close()
            else:
                cleanup = asyncio.create_task(connection.close())
                await asyncio.wait_for(asyncio.shield(cleanup), timeout=2)
        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, asyncio.CancelledError) and primary is None:
                raise
            if cleanup is not None and not cleanup.done():
                if isinstance(error, asyncio.CancelledError):
                    try:
                        await asyncio.wait({cleanup}, timeout=2)
                    except asyncio.CancelledError:
                        pass
                if not cleanup.done():
                    cleanup.cancel()
                    try:
                        await asyncio.wait({cleanup}, timeout=2)
                    except asyncio.CancelledError:
                        pass
                # Retrieve any late cleanup error, including after a bounded join expires.
                cleanup.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )
            logger.warning("CTF MCP cleanup failed error_type=%s", type(error).__name__)

    async def close(self) -> None:
        connections, self.connections = self.connections, {}
        for connection in connections.values():
            await self._close_connection(connection)

    async def _connection(
        self, identity: str, server: dict, headers: dict
    ) -> MCPStreamableHTTPTool:
        async with self.connection_locks.setdefault(identity, asyncio.Lock()):
            if identity not in self.connections:
                connection = MCPStreamableHTTPTool(
                    name=server["name"],
                    url=server["url"],
                    static_headers=headers,
                    request_timeout=120,
                    allowed_tools=[],
                    load_tools=False,
                    load_prompts=False,
                    sampling_max_requests=0,
                )
                try:
                    await connection.connect()
                except BaseException:
                    await self._close_connection(connection)
                    raise
                self.connections[identity] = connection
            return self.connections[identity]

    async def request(self, server: dict, headers: dict, name: str | None, arguments: dict) -> Any:
        if self.transport is not None:
            return await self.transport(server, headers, name, arguments)
        identity = self.identity(server, headers)
        connection = await self._connection(identity, server, headers)
        try:
            session = connection.session
            if session is None:
                raise RuntimeError("MCP session is unavailable")
            if name is None:
                catalog = []
                params = None
                while True:
                    page = await session.list_tools(params=params)
                    catalog.extend(item.model_dump(mode="json") for item in page.tools)
                    if not page.nextCursor:
                        return {"tools": catalog}
                    params = types.PaginatedRequestParams(cursor=page.nextCursor)
            # MCPTool retries writes; the public session API sends once and validates results.
            return (await session.call_tool(name, arguments)).model_dump(mode="json")
        except BaseException:
            if self.connections.get(identity) is connection:
                self.connections.pop(identity)
                await self._close_connection(connection)
            raise

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
                self.observation_secrets.append(secret)
                headers[server["auth_header"]] = f"{server['auth_scheme']} {secret}".strip()

            async def load_catalog(server=server, headers=headers):
                catalog = await self.request(server, headers, None, {})
                return {"tools": {item["name"]: item for item in catalog["tools"]}}

            catalog = (
                await self.catalog_cache.get(
                    task_id,
                    self.identity(
                        server,
                        headers,
                        role=role,
                        allowed_tools=sorted(grant.allowed_tools or []),
                    ),
                    load_catalog,
                )
            )["tools"]
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
            expected_revision: Annotated[int, Field(ge=1)],
            arguments: dict[str, Any],
            ctx: FunctionInvocationContext,
        ) -> ToolJsonResult:
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
            if entry is not None and entry["digest"] != digest:
                raise ValueError("Platform occurrence arguments changed")
            if entry is None or entry["state"] == "rejected":
                try:
                    await self.service.runtime(task_id, "authorize_platform_call", **identity)
                except RemoteError as error:
                    if feedback := recoverable_tool_error(error):
                        return feedback
                    raise
                # A changed model occurrence must not blindly repeat an uncertain write.
                if not binding.read_only and any(
                    item["digest"] == digest
                    and item["state"] != "rejected"
                    and (item["state"] in {"dispatched", "unknown"} or "record" not in item)
                    for item in calls.values()
                ):
                    return json.dumps(
                        {
                            "status": "unknown",
                            "message": "Prior call outcome unknown; query platform status.",
                        }
                    )
                if entry is None:
                    entry = calls[key] = {
                        "digest": digest,
                        "call_id": str(uuid4()),
                        "state": "prepared",
                    }
                else:
                    entry["state"] = "prepared"
                await checkpoint.save()
                try:
                    await self.service.runtime(
                        task_id, "begin_platform_call", **identity, call_id=entry["call_id"]
                    )
                except RemoteError as error:
                    if 400 <= error.status < 500:
                        # An explicit preflight rejection proves the MCP request never started.
                        entry["state"] = "rejected"
                        await checkpoint.save()
                        if feedback := recoverable_tool_error(error):
                            return feedback
                    raise
                entry["state"] = "dispatched"
                await checkpoint.save()
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
            elif entry["state"] == "prepared":
                return json.dumps(
                    {
                        "status": "unknown",
                        "message": "Prior call preflight outcome unknown; query platform status.",
                    }
                )
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
