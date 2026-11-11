"""One stateless, capability-bound MCP adapter over the existing business transactions."""

import asyncio
import json
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import mcp.types as types
from bbx_contracts.runner import ToolInvocation, runner_json_text
from bbx_contracts.runner_vpn import VPN_DYNAMIC_PROFILE
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import ValidationError
from starlette.responses import JSONResponse

from bbx_blackboard.board_runner_tools import invoke as invoke_board_tool
from bbx_blackboard.board_runner_tools import schemas as board_schemas
from bbx_blackboard.business_tools import MUTATIONS, BusinessTools, schemas
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.gateway_auth import EXPLORATION_ROLES, require_gateway_identity
from bbx_blackboard.mcp_catalog import invoke_native_tool

request_scope = ContextVar("bbx_business_mcp_scope")


class BusinessMCP:
    def __init__(self, application):
        self.application = application
        self.server = Server("bbx-business-v1")
        self.manager = StreamableHTTPSessionManager(self.server, json_response=True, stateless=True)

        @self.server.list_tools()
        async def list_tools():
            tid, identity, _, dynamic = request_scope.get()
            gateway = self.application.state.runner_gateway
            allowed = await gateway.catalog(tid, identity)
            catalog = (
                board_schemas()
                if identity.role in EXPLORATION_ROLES
                else schemas(dynamic_vpn=dynamic)
            )
            builtins = [
                types.Tool(
                    name=name,
                    description=name.replace("board_", "").replace("_", " "),
                    inputSchema=catalog[name],
                )
                for name in allowed
                if name in catalog
            ]
            external = await gateway.external_catalog(tid, identity)
            return [
                *builtins,
                *(
                    types.Tool(
                        name=name,
                        description=grant["tool"]["description"],
                        inputSchema=grant["tool"]["input_schema"],
                        outputSchema=grant["tool"]["output_schema"],
                    )
                    for name, grant in external.items()
                ),
            ]

        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            tid, identity, sdk, _ = request_scope.get()
            meta = self.server.request_context.meta
            envelope = getattr(meta, "bbx", None)
            if not isinstance(envelope, dict):
                raise HTTPException(403, "Trusted invocation metadata required")
            metadata = dict(envelope)
            permit_ref = metadata.pop("permit_ref", None)
            invocation = ToolInvocation.model_validate(metadata)
            if permit_ref != invocation.invocation_id or sdk != invocation.sdk_session_id:
                raise HTTPException(403, "MCP invocation scope changed")
            gateway = self.application.state.runner_gateway
            claimed, saved = await gateway.claim_business_tool(
                tid, identity, invocation, name, arguments
            )
            if saved:
                return types.CallToolResult.model_validate(json.loads(saved["result_json"]))
            principal, data, original = claimed
            if grant := data.get("external_grant"):
                started = False

                async def before_send():
                    nonlocal started
                    await gateway.verify_external_send(tid, identity, invocation)
                    started = True

                try:
                    remaining = (
                        data["authorized_wait_until_ms"] / 1000 - datetime.now(UTC).timestamp()
                    )
                    async with asyncio.timeout(max(0, min(45, remaining))):
                        payload = await invoke_native_tool(
                            self.application, grant, identity.role, original, before_send
                        )
                except asyncio.CancelledError:
                    await asyncio.shield(
                        gateway.unknown_business_result(tid, invocation.invocation_id)
                    )
                    raise
                except Exception:
                    if started:
                        await gateway.unknown_business_result(tid, invocation.invocation_id)
                        raise HTTPException(409, "External MCP outcome is unconfirmed") from None
                    payload = {
                        "content": [
                            {
                                "type": "text",
                                "text": "External MCP was not sent; connection or scope failed.",
                            }
                        ],
                        "isError": True,
                    }
                try:
                    await gateway.save_business_result(tid, invocation.invocation_id, payload)
                except BaseException:
                    await asyncio.shield(
                        gateway.unknown_business_result(tid, invocation.invocation_id)
                    )
                    raise
                return types.CallToolResult.model_validate(payload)
            args = dict(original)
            if name in MUTATIONS:
                args["request_id"] = data["mutation_request_id"]
            try:
                if principal.mode == "blackboard":
                    result = await invoke_board_tool(
                        self.application, tid, principal, name, original
                    )
                else:
                    result = await BusinessTools(
                        self.application.state.ctf_service,
                        self.application.state.settings.agent_token_secret.get_secret_value(),
                    ).invoke(tid, principal, name, args)
                value = jsonable_encoder(result, custom_encoder={Decimal: str})
                payload = {
                    "content": [{"type": "text", "text": runner_json_text(value)}],
                    "structuredContent": {
                        "result": value,
                        **(
                            {"bbx_board_stop": True}
                            if principal.mode == "blackboard" and name == "submit_close"
                            else {}
                        ),
                    },
                    "isError": False,
                }
            except (HTTPException, RuleViolation, ValidationError) as error:
                if principal.mode != "blackboard" and not isinstance(error, HTTPException):
                    await asyncio.shield(
                        gateway.unknown_business_result(tid, invocation.invocation_id)
                    )
                    raise
                status = error.status_code if isinstance(error, HTTPException) else 422
                detail = (
                    error.detail
                    if isinstance(error, HTTPException)
                    else "Blackboard tool input rejected"
                )
                payload = {
                    "content": [
                        {
                            "type": "text",
                            "text": runner_json_text({"status": status, "detail": detail}),
                        }
                    ],
                    "isError": True,
                }
            except BaseException:
                await asyncio.shield(gateway.unknown_business_result(tid, invocation.invocation_id))
                raise
            try:
                await gateway.save_business_result(tid, invocation.invocation_id, payload)
            except BaseException:
                await asyncio.shield(gateway.unknown_business_result(tid, invocation.invocation_id))
                raise
            return types.CallToolResult.model_validate(payload)

    def run(self):
        return self.manager.run()

    async def __call__(self, scope, receive, send):
        request = Request({**scope, "app": self.application}, receive)
        try:
            tid = UUID(scope["path_params"]["task_id"])
            if request.headers.getlist("origin") or request.method != "POST":
                raise HTTPException(
                    403, "Direct business MCP requires the private Runner transport"
                )
            identity = require_gateway_identity(request, tid, "business-mcp")
            if identity.role == "helper":
                raise HTTPException(403, "Helper capability is scoped to the Sandbox target")
            gateway = self.application.state.runner_gateway
            if gateway is None:
                raise HTTPException(503, "Business MCP is unavailable")
            async with gateway.repo.engine.connect() as conn:
                task, _, binding = await gateway._scope(conn, tid, identity, metadata=True)
                sdk_headers = request.headers.getlist("x-bbx-sdk-session-id")
                sdk = sdk_headers[0] if len(sdk_headers) == 1 else ""
                if not sdk or (
                    binding["sdk_session_id"] is not None and sdk != binding["sdk_session_id"]
                ):
                    raise HTTPException(403, "MCP SDK identity changed")
            dynamic = (task.get("ctf_control") or {}).get("vpn_profile") == VPN_DYNAMIC_PROFILE
            token = request_scope.set((tid, identity, sdk, dynamic))
            try:
                await self.manager.handle_request(scope, receive, send)
            finally:
                request_scope.reset(token)
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, status_code=error.status_code)(
                scope, receive, send
            )
