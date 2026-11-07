"""The Task's trusted MCP sidecar; only Blackboard admits dispatch and saves results."""

import argparse
import asyncio
import hmac
import json
import os
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
from bbx_contracts.runner import ToolInvocation, runner_json_text
from bbx_contracts.sandbox_tools import sandbox_schemas
from bbx_sandbox_execution.connect import connect_bound_execd, read_execution_context
from bbx_sandbox_execution.execution import AssignmentBinding
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from bbx_runtime.sandbox_tools import BoundedExecdLogTransport, assignment_control, dispatch

request_scope: ContextVar[tuple[str, str]] = ContextVar("bbx_sandbox_mcp_scope")


def load_configuration(path):
    config = json.loads(path.read_bytes())
    task = str(UUID(config["task_id"]))
    if (
        config.get("version") != 1
        or config.get("runner_pod_uid") != os.environ.get("BBX_POD_UID")
        or task != config["task_id"]
        or config.get("inbound_key") == config.get("report_key")
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
                sandbox = self.sandbox
                if sandbox is None:
                    raise ValueError("Original Sandbox is unavailable")

                async def admit():
                    if datetime.fromisoformat(self.config["hard_deadline"]) <= datetime.now(UTC):
                        return False
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

    def check_binding(self, binding):
        expected = {
            "task_id": self.config["task_id"],
            "sandbox_id": self.config["sandbox_id"],
            "pod_uid": self.config["sandbox_pod_uid"],
            "environment_revision": self.config["environment_revision"],
            "execution_context_id": self.config["execution_context_id"],
            **self.config["network"],
        }
        if any(getattr(binding, key) != value for key, value in expected.items()):
            raise ValueError("Original Task execution binding changed")

    async def control(self, method, path, body=None, *, authorization=None, sdk=None):
        headers = {"x-bbx-sandbox-report": self.config["report_key"]}
        if authorization:
            headers["authorization"] = authorization
        if sdk:
            headers["x-bbx-sdk-session-id"] = sdk
        response = await self.http.request(
            method,
            f"{self.config['blackboard_url'].rstrip('/')}/api/tasks/{self.config['task_id']}/sandbox-host/{path}",
            headers=headers,
            json=body,
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
                self.ready = True
                cleanup = asyncio.create_task(self.scopes())
                try:
                    yield
                finally:
                    self.ready = False
                    cleanup.cancel()
                    await asyncio.gather(cleanup, return_exceptions=True)
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
    uvicorn.run(host.app, host="0.0.0.0", port=8766, access_log=False)


if __name__ == "__main__":
    main()
