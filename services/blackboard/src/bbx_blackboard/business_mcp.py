"""One stateless, capability-bound MCP adapter over the existing business transactions."""

import asyncio
import json
from contextvars import ContextVar
from decimal import Decimal
from uuid import UUID

import mcp.types as types
from bbx_contracts.runner import ToolInvocation, runner_json_text
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.responses import JSONResponse

from bbx_blackboard.business_tools import MUTATIONS, BusinessTools, schemas
from bbx_blackboard.gateway_auth import require_gateway_identity

request_scope = ContextVar("bbx_business_mcp_scope")


class BusinessMCP:
    def __init__(self, application):
        self.application = application
        self.server = Server("bbx-business-v1")
        self.manager = StreamableHTTPSessionManager(self.server, json_response=True, stateless=True)

        @self.server.list_tools()
        async def list_tools():
            tid, identity, _ = request_scope.get()
            gateway = self.application.state.runner_gateway
            allowed = await gateway.catalog(tid, identity)
            catalog = schemas()
            return [
                types.Tool(
                    name=name,
                    description=name.replace("board_", "").replace("_", " "),
                    inputSchema=catalog[name],
                )
                for name in allowed
                if name in catalog
            ]

        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            tid, identity, sdk = request_scope.get()
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
            args = dict(original)
            if name in MUTATIONS:
                args["request_id"] = data["mutation_request_id"]
            try:
                result = await BusinessTools(
                    self.application.state.ctf_service,
                    self.application.state.settings.agent_token_secret.get_secret_value(),
                ).invoke(tid, principal, name, args)
                value = jsonable_encoder(result, custom_encoder={Decimal: str})
                payload = {
                    "content": [{"type": "text", "text": runner_json_text(value)}],
                    "structuredContent": {"result": value},
                    "isError": False,
                }
            except HTTPException as error:
                payload = {
                    "content": [
                        {
                            "type": "text",
                            "text": runner_json_text(
                                {"status": error.status_code, "detail": error.detail}
                            ),
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
            identity = require_gateway_identity(request, tid, "mcp")
            gateway = self.application.state.runner_gateway
            if gateway is None:
                raise HTTPException(503, "Business MCP is unavailable")
            async with gateway.repo.engine.connect() as conn:
                _, _, binding = await gateway._scope(conn, tid, identity, metadata=True)
                sdk = request.headers.get("x-bbx-sdk-session-id", "")
                if not sdk or (
                    binding["sdk_session_id"] is not None and sdk != binding["sdk_session_id"]
                ):
                    raise HTTPException(403, "MCP SDK identity changed")
            token = request_scope.set((tid, identity, sdk))
            try:
                await self.manager.handle_request(scope, receive, send)
            finally:
                request_scope.reset(token)
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, status_code=error.status_code)(
                scope, receive, send
            )
