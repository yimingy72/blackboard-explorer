"""The Task's trusted MCP sidecar; only Blackboard admits dispatch and saves results."""

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import ssl
import time
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import mcp.types as types
import uvicorn
from bbx_contracts.runner import ToolInvocation, runner_json_text, validate_runner_json_object
from bbx_contracts.runner_inputs import INPUT_PROFILE
from bbx_contracts.runner_vpn import VPN_DYNAMIC_PROFILE, VPN_PROFILE, NativeTaskRoutePolicy
from bbx_contracts.sandbox_tools import sandbox_schemas
from bbx_sandbox_execution.connect import connect_bound_execd, read_execution_context
from bbx_sandbox_execution.execution import AssignmentBinding
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from bbx_runtime.sandbox_inputs import initial_inputs_ready
from bbx_runtime.sandbox_network import SEAL_FIELDS, ready_identity, root_network, sealed_bytes
from bbx_runtime.sandbox_tools import BoundedExecdLogTransport, assignment_control, dispatch
from bbx_runtime.sandbox_vpn import supervise_sandbox_vpn

request_scope: ContextVar[tuple[str, str]] = ContextVar("bbx_sandbox_mcp_scope")
DIRECT_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "notifications/cancelled",
        "ping",
        "tools/list",
        "tools/call",
    }
)
MAX_DIRECT_MESSAGE = 2 * 1024 * 1024


def load_configuration(path):
    config = json.loads(path.read_bytes())
    task = str(UUID(config["task_id"]))
    if (
        config.get("version") != 1
        or config.get("mcp_transport") not in {None, "direct-v2"}
        or config.get("runner_pod_uid") != os.environ.get("BBX_POD_UID")
        or task != config["task_id"]
        or config.get("inbound_key") == config.get("report_key")
        or config.get("inputs_profile") not in {None, INPUT_PROFILE}
        or config.get("vpn_profile") not in {None, VPN_PROFILE, VPN_DYNAMIC_PROFILE}
        or any(
            not isinstance(config.get(key), str) or not config[key]
            for key in ("inbound_key", "report_key", "execd_token")
        )
    ):
        raise ValueError("Original Task Sandbox host mount is invalid")
    headers = config["execd_headers"]
    if (
        set(headers) != {"OpenSandbox-Ingress-To", "OpenSandbox-Secure-Access"}
        or headers["OpenSandbox-Ingress-To"] != config["sandbox_id"] + "-44772"
        or not headers["OpenSandbox-Secure-Access"]
    ):
        raise ValueError("Original official execution access is unavailable")
    return config


class SandboxMCP:
    def __init__(self, configuration):
        self.config = configuration
        self.ready = False
        self.server = Server("bbx-sandbox-v1")
        self.manager = StreamableHTTPSessionManager(self.server, json_response=True, stateless=True)
        self.http: httpx.AsyncClient
        self.sandbox = None
        self.default_network: dict | None = None
        self.vpn_network: dict | None = None
        self.vpn_link: dict | None = None
        self.network_locks: dict[str, asyncio.Lock] = {}
        self.acknowledged_networks: set[str] = set()
        self.app = Starlette(
            routes=[Route("/ready", self.health), Mount("/mcp", app=self.handle)],
            lifespan=self.run,
        )
        schemas = sandbox_schemas()

        @self.server.list_tools()
        async def list_tools():
            authorization, sdk = request_scope.get()
            names = await self.control("GET", "catalog", authorization=authorization, sdk=sdk)
            return [
                types.Tool(name=name, description=name.replace("_", " "), inputSchema=schemas[name])
                for name in names
                if name in schemas
            ]

        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            authorization, sdk = request_scope.get()
            meta = getattr(self.server.request_context.meta, "bbx", None)
            invocation = None
            claimed = False
            try:
                if not isinstance(meta, dict):
                    raise ValueError("Trusted invocation metadata is required")
                envelope = dict(meta)
                permit = envelope.pop("permit_ref", None)
                invocation = ToolInvocation.model_validate(envelope)
                if permit != invocation.invocation_id or sdk != invocation.sdk_session_id:
                    raise ValueError("Original Sandbox invocation changed")
                body = {
                    "invocation": invocation.model_dump(mode="json"),
                    "name": name,
                    "arguments": arguments,
                }
                claim = await self.control(
                    "POST", "claim", body, authorization=authorization, sdk=sdk
                )
                if "saved" in claim:
                    return types.CallToolResult.model_validate(claim["saved"])
                claimed = True
                binding = AssignmentBinding(**claim["binding"])
                self.check_binding(binding)
                network = claim.get("network")
                if self.binding_network and name == "exec_command" and not network:
                    raise ValueError("An original per-Assignment network is required")
                if (
                    network
                    and self.config.get("vpn_profile") == VPN_DYNAMIC_PROFILE
                    and not network["scope"].get("vpn_operation_id")
                ):
                    self.observe_default_network(network["scope"])
                sandbox = self.sandbox
                if sandbox is None:
                    raise ValueError("Original Sandbox is unavailable")

                async def admit():
                    if datetime.fromisoformat(self.config["hard_deadline"]) <= datetime.now(UTC):
                        return False
                    if network:
                        source = self.network_scope(network, binding)
                        if network["scope"]["binding_id"] != invocation.binding.binding_id:
                            raise ValueError("Original Sandbox binding changed")
                        async with self.network_locks.setdefault(source, asyncio.Lock()):
                            revision = await self.grant(sandbox, network, prepare=True)
                            result = await self.control(
                                "POST",
                                "admit",
                                {**body, "network_grant_revision": revision},
                                authorization=authorization,
                                sdk=sdk,
                            )
                    else:
                        result = await self.control(
                            "POST", "admit", body, authorization=authorization, sdk=sdk
                        )
                    return result.get("admitted") is True

                async def persist(event, payload):
                    await self.control(
                        "POST",
                        f"calls/{invocation.invocation_id}/events",
                        {"event": event, "payload": payload},
                    )

                value = await dispatch(
                    sandbox,
                    binding,
                    name,
                    claim["arguments"],
                    admit=admit,
                    persist=persist,
                    hard_deadline=datetime.fromisoformat(self.config["hard_deadline"]),
                )
                payload = {
                    "content": [{"type": "text", "text": runner_json_text(value)}],
                    "structuredContent": {"result": value},
                    "isError": value.get("output_unavailable") is True,
                }
                saved = await self.control(
                    "POST", f"calls/{invocation.invocation_id}/result", {"result": payload}
                )
                return types.CallToolResult.model_validate(saved["result"])
            except BaseException as error:
                if claimed and invocation is not None:
                    with suppress(Exception):
                        await asyncio.shield(
                            self.control(
                                "POST",
                                f"calls/{invocation.invocation_id}/events",
                                {
                                    "event": "unknown",
                                    "payload": {"reason": "host_result_unconfirmed"},
                                },
                            )
                        )
                if not isinstance(error, Exception):
                    raise
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text="Sandbox tool was denied or its original outcome is unconfirmed.",
                        )
                    ],
                    isError=True,
                )

    @property
    def binding_network(self):
        return bool(
            self.config.get("traffic_targets")
            or self.config.get("vpn_profile") == VPN_DYNAMIC_PROFILE
        )

    def observe_default_network(self, original):
        value = {
            "network_digest": original["network_digest"],
            "route_policy": NativeTaskRoutePolicy.model_validate(
                original["route_policy"]
            ).model_dump(mode="json"),
        }
        if (
            not re.fullmatch(r"[a-f0-9]{64}", value["network_digest"])
            or self.vpn_network is not None
            or self.default_network not in (None, value)
        ):
            raise ValueError("Original Task default network identity changed")
        self.default_network = value

    def check_binding(self, binding):
        expected = {
            "task_id": self.config["task_id"],
            "sandbox_id": self.config["sandbox_id"],
            "pod_uid": self.config["sandbox_pod_uid"],
            "environment_revision": self.config["environment_revision"],
            "execution_context_id": self.config["execution_context_id"],
            **(self.config["network"] if not self.binding_network else {}),
        }
        if any(getattr(binding, key) != value for key, value in expected.items()):
            raise ValueError("Original Task execution binding changed")

    def network_scope(self, item, binding):
        scope = item["scope"]
        source = scope["source_id"]
        ordinal = int(scope["host_interface"][2:], 16)
        if self.config.get("vpn_profile") == VPN_DYNAMIC_PROFILE:
            vpn = bool(scope.get("vpn_operation_id"))
            network = self.vpn_network if vpn else self.default_network
            if (
                network is None
                or network["network_digest"]
                != scope.get("vpn_network_digest" if vpn else "network_digest")
                or network["route_policy"] != scope.get("route_policy")
            ):
                raise ValueError("Original Sandbox network digest or policy changed")
        else:
            if self.config.get("traffic_targets") != scope.get("targets"):
                raise ValueError("Original Sandbox traffic target changed")
        if (
            item["binding"] != json.loads(binding.scope_request()[1])
            or scope["task_id"] != binding.task_id
            or scope["agent_id"] != binding.agent_id
            or scope["role"] != binding.role
            or scope.get("parent_agent_id") != binding.parent_agent_id
            or scope["assignment_id"] != binding.assignment_id
            or scope["generation"] != binding.generation
            or scope["epoch"] != binding.epoch
            or scope["sandbox_id"] != binding.sandbox_id
            or scope["sandbox_pod_uid"] != binding.pod_uid
            or scope["runner_pod_uid"] != self.config["runner_pod_uid"]
            or scope["execution_context_id"] != binding.execution_context_id
            or scope["namespace"] != binding.namespace
            or scope["ipv4"] != binding.ipv4
            or scope["host_ipv4"] != str(ipaddress.IPv4Address(binding.ipv4) - 1)
            or scope["host_interface"] != f"bv{ordinal:03x}"
        ):
            raise ValueError("Original Sandbox traffic scope changed")
        return source

    async def grant(self, sandbox, item, *, prepare=False):
        scope, binding = item["scope"], AssignmentBinding(**item["binding"])
        source = self.network_scope(item, binding)
        path = f"network/{source}"
        vpn = self.config.get("vpn_profile") == VPN_PROFILE or bool(scope.get("vpn_operation_id"))
        if vpn and (
            not self.vpn_link
            or self.vpn_link["vpn_operation_id"] != scope.get("vpn_operation_id")
            or self.vpn_link.get("network_digest") != scope.get("vpn_network_digest")
        ):
            raise ValueError("Original Task VPN link is unavailable")
        if prepare and item.get("prepare_allowed"):
            await root_network(
                sandbox,
                "prepare",
                source,
                {
                    "source_id": source,
                    "ordinal": int(scope["host_interface"][2:], 16),
                    "namespace": binding.namespace,
                    "network_identity": binding.network_identity,
                    "ipv4": binding.ipv4,
                    **(
                        {"route_policy": scope["route_policy"]}
                        if "route_policy" in scope
                        else {"targets": scope["targets"]}
                    ),
                    "scope_id": binding.scope_request()[0],
                    **({"vpn": self.vpn_link} if vpn else {}),
                },
            )
        observed = await root_network(sandbox, "read", source)
        if prepare:
            for _ in range(8):
                if observed.get("capture_ready"):
                    break
                await asyncio.sleep(0.1)
                observed = await root_network(sandbox, "read", source)
        if not observed.get("capture_ready"):
            raise ValueError("Original Sandbox capture is not listening")
        identity = ready_identity(observed)
        if item.get("ready") is None:
            await self.control("POST", path + "/observe", identity)
        elif item["ready"] != identity:
            raise ValueError("Original capture process changed")
        permit = await self.control("POST", path + "/permit", {})
        installed = await root_network(
            sandbox, "renew", source, {**permit, "boot_ms": observed["boot_ms"]}
        )
        if (
            permit["source_id"] != source
            or installed.get("grant_revision") != permit["grant_revision"]
            or not installed.get("gate_live")
            or ready_identity(installed) != identity
        ):
            raise ValueError("Original Sandbox kernel grant was not installed")
        return permit["grant_revision"]

    async def archive_network(self, sandbox, item, source):
        path = f"network/{source}"
        if not item.get("retire"):
            await self.grant(sandbox, item)
            return
        fenced = await root_network(sandbox, "fence", source)
        if fenced["state"] not in {"fenced", "sealed", "acked"}:
            raise ValueError("Original Sandbox network fence is unconfirmed")
        if not item.get("can_seal"):
            if item.get("ready") is None and fenced.get("capture_ready"):
                # A root prepare may have succeeded even if its first Host receipt was lost.
                await self.control("POST", path + "/observe", ready_identity(fenced))
            return
        observed = await root_network(sandbox, "seal", source)
        if observed.get("state") not in {"sealed", "acked"}:
            raise ValueError("Original capture has no sealed bytes")
        original = {
            **{k: observed[k] for k in SEAL_FIELDS if k in observed},
            **ready_identity(observed),
        }
        if not item["archive_confirmed"]:
            await self.control("POST", path + "/seal", original)
            data = await sealed_bytes(sandbox, source, original)
            response = await self.control("POST", path + "/bytes", raw=data)
            if response.get("archive_confirmed") is not True:
                raise ValueError("Original archive acknowledgement is missing")
        await root_network(sandbox, "ack", source, {"sha256": original["sha256"]})
        self.acknowledged_networks.add(source)

    async def networks(self):
        sandbox = self.sandbox
        if sandbox is None:
            return
        grants = asyncio.Semaphore(8)
        archives = asyncio.Semaphore(3)
        work: dict[str, asyncio.Task] = {}

        async def reconcile(item, source):
            try:
                limit = archives if item.get("retire") else grants
                async with limit, self.network_locks.setdefault(source, asyncio.Lock()):
                    await self.archive_network(sandbox, item, source)
            except Exception:
                # No capture, permit or archive result is inferred from missing evidence.
                return

        try:
            while True:
                try:
                    batch = await self.control("GET", "network")
                    for item in batch["scopes"]:
                        try:
                            binding = AssignmentBinding(**item["binding"])
                            self.check_binding(binding)
                            source = self.network_scope(item, binding)
                            if source not in work and source not in self.acknowledged_networks:
                                task = asyncio.create_task(reconcile(item, source))
                                work[source] = task
                                task.add_done_callback(lambda _, key=source: work.pop(key, None))
                        except (ValueError, KeyError, TypeError):
                            continue
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
                await asyncio.sleep(1.5)
        finally:
            pending = list(work.values())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def control(self, method, path, body=None, *, authorization=None, sdk=None, raw=None):
        headers = {"x-bbx-sandbox-report": self.config["report_key"]}
        if authorization:
            headers["authorization"] = authorization
        if sdk:
            headers["x-bbx-sdk-session-id"] = sdk
        if raw is not None:
            headers["content-type"] = "application/octet-stream"
        response = await self.http.request(
            method,
            f"{self.config['blackboard_url'].rstrip('/')}/api/tasks/{self.config['task_id']}/sandbox-host/{path}",
            headers=headers,
            json=body,
            content=raw,
        )
        response.raise_for_status()
        return response.json()

    def authorized(self, request):
        values = request.headers.getlist("x-bbx-sandbox-inbound")
        return len(values) == 1 and hmac.compare_digest(
            values[0].encode(), self.config["inbound_key"].encode()
        )

    async def health(self, request):
        if not self.authorized(request):
            return JSONResponse(
                {"detail": "Task Sandbox gateway identity required"}, status_code=401
            )
        return JSONResponse(
            {
                key: self.config[key]
                for key in (
                    "task_id",
                    "operation_id",
                    "runner_pod_uid",
                    "sandbox_id",
                    "sandbox_pod_uid",
                    "execution_context_id",
                )
            }
            | {"ready": self.ready},
            status_code=200 if self.ready else 503,
        )

    async def handle(self, scope, receive, send):
        request = Request(scope, receive)
        if self.config.get("mcp_transport") == "direct-v2":
            await self.handle_direct(request, scope, receive, send)
            return
        if not self.ready or not self.authorized(request):
            await JSONResponse(
                {"detail": "Task Sandbox gateway identity required"}, status_code=401
            )(scope, receive, send)
            return
        token = request_scope.set(
            (
                request.headers.get("authorization", ""),
                request.headers.get("x-bbx-sdk-session-id", ""),
            )
        )
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            request_scope.reset(token)

    async def handle_direct(self, request, scope, receive, send):
        async def deny(status):
            await JSONResponse(
                {"detail": "The scoped Sandbox MCP request was refused"}, status_code=status
            )(scope, receive, send)

        authorizations = request.headers.getlist("authorization")
        sessions = request.headers.getlist("x-bbx-sdk-session-id")
        if (
            not self.ready
            or request.method != "POST"
            or request.url.query
            or request.client is None
            or request.client.host != "127.0.0.1"
            or "origin" in request.headers
            or "x-bbx-sandbox-inbound" in request.headers
            or len(authorizations) != 1
            or not authorizations[0].startswith("Bearer ")
            or len(sessions) != 1
            or not sessions[0]
        ):
            await deny(401)
            return
        raw = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_DIRECT_MESSAGE:
                    await deny(413)
                    return
                raw.extend(chunk)
            value = validate_runner_json_object(
                raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()
            )
            method = value.get("method")
            if not isinstance(method, str) or method not in DIRECT_METHODS:
                raise ValueError("Unsupported direct MCP method")
        except (UnicodeError, ValueError):
            await deny(422)
            return
        try:
            result = await self.control(
                "POST",
                "authorize",
                {"method": method, "sdk_session_id": sessions[0]},
                authorization=authorizations[0],
                sdk=sessions[0],
            )
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            await deny(status if status in {401, 403, 409, 422} else 503)
            return
        except (httpx.RequestError, ValueError):
            await deny(503)
            return
        if not isinstance(result, dict) or result.get("authorized") is not True:
            await deny(503)
            return
        # The official MCP SDK still parses and executes the exact original message.
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(raw), "more_body": False}
            return await receive()

        token = request_scope.set((authorizations[0], sessions[0]))
        try:
            await self.manager.handle_request(scope, replay, send)
        finally:
            request_scope.reset(token)

    async def scopes(self):
        sandbox = self.sandbox
        if sandbox is None:
            return
        while True:
            try:
                pending = await self.control("GET", "scopes")
                for item in pending:
                    binding = AssignmentBinding(**item["binding"])
                    self.check_binding(binding)
                    path = f"scopes/{item['command_id']}"
                    claimed = await self.control("POST", path + "/claim")

                    async def persist(event, payload, path=path):
                        await self.control(
                            "POST", path + "/events", {"event": event, "payload": payload}
                        )

                    try:
                        await assignment_control(
                            sandbox, binding, stop=claimed["stop"], persist=persist
                        )
                    except Exception:
                        # The durable stop claim prevents a second kill after an uncertain response.
                        continue
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(3)

    async def prepare_inputs(self, sandbox):
        if await initial_inputs_ready(self, sandbox):
            self.ready = True

    @asynccontextmanager
    async def run(self, app):
        tls = ssl.create_default_context()
        ca_file = Path(self.config["ca_file"])
        if ca_file.is_file() and ca_file.stat().st_size:
            tls.load_verify_locations(cafile=ca_file)
        async with (
            httpx.AsyncClient(
                verify=tls,
                trust_env=False,
                follow_redirects=False,
                timeout=15,
                transport=httpx.AsyncHTTPTransport(verify=tls, retries=0),
            ) as client,
            BoundedExecdLogTransport() as transport,
            self.manager.run(),
        ):
            self.http = client
            self.sandbox = connect_bound_execd(
                self.config["sandbox_id"],
                self.config["execd_endpoint"],
                self.config["execd_token"],
                endpoint_headers=self.config["execd_headers"],
                transport=transport,
            )
            try:
                identity = await read_execution_context(self.sandbox)
                if identity != {
                    "pod_uid": self.config["sandbox_pod_uid"],
                    "environment_revision": self.config["environment_revision"],
                    "execution_context_id": self.config["execution_context_id"],
                }:
                    raise ValueError("Original Sandbox execution context changed")
                self.ready = self.config.get("inputs_profile") is None
                cleanup = [asyncio.create_task(self.scopes())]
                if not self.ready:
                    cleanup.append(asyncio.create_task(self.prepare_inputs(self.sandbox)))
                if self.binding_network:
                    cleanup.append(asyncio.create_task(self.networks()))
                if self.config.get("vpn_profile") in {VPN_PROFILE, VPN_DYNAMIC_PROFILE}:
                    cleanup.append(asyncio.create_task(supervise_sandbox_vpn(self, self.sandbox)))
                try:
                    yield
                finally:
                    self.ready = False
                    for job in cleanup:
                        job.cancel()
                    await asyncio.gather(*cleanup, return_exceptions=True)
            finally:
                await self.sandbox.close()


def main():
    parser = argparse.ArgumentParser(description="Run the original Task Sandbox MCP host")
    parser.add_argument("--config", type=Path, required=True)
    options = parser.parse_args()
    # Kubernetes projects the immutable Task host configuration after the waiting Pod has a UID.
    until = time.monotonic() + 180
    while not options.config.is_file():
        if time.monotonic() >= until:
            raise RuntimeError("Original Sandbox host mount is unavailable")
        time.sleep(0.25)
    host = SandboxMCP(load_configuration(options.config))
    direct = host.config.get("mcp_transport") == "direct-v2"
    uvicorn.run(
        host.app,
        host="127.0.0.1" if direct else "0.0.0.0",
        port=8766,
        access_log=False,
        proxy_headers=not direct,
    )


if __name__ == "__main__":
    main()
