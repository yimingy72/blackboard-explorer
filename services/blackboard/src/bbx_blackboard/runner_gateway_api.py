"""Independent model/MCP capability routes and private Runtime capture boundary."""

from uuid import UUID

from bbx_contracts.native_gateway import (
    GatewayCredentialsRequest,
)
from bbx_contracts.runner import (
    ToolInvocation,
    ToolPermit,
    ToolResultConfirmation,
    ToolResultReceipt,
    ToolUnknownSubmission,
)
from fastapi import APIRouter, HTTPException, Request, Response

from bbx_blackboard.auth import require_service
from bbx_blackboard.gateway_auth import mcp_identity

tool_router = APIRouter(
    prefix="/api/tasks/{task_id}/runner-control/bindings/{binding_id}/tools", tags=["runner-tools"]
)
private_router = APIRouter(
    prefix="/api/tasks/{task_id}/ctf/runtime/runner-gateway", tags=["runner-gateway-private"]
)


def service(request):
    gateway = request.app.state.runner_gateway
    if gateway is None:
        raise HTTPException(503, "Runner gateway is unavailable")
    return gateway


def tool_identity(request, task_id, binding_id):
    identity = mcp_identity(request, task_id)
    if identity.binding_id != binding_id:
        raise HTTPException(403, "Tool binding scope denied")
    return identity


@private_router.post("/bindings/{binding_id}/credentials")
async def credentials(
    request: Request,
    response: Response,
    task_id: UUID,
    binding_id: str,
    body: GatewayCredentialsRequest,
):
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await service(request).credentials(task_id, binding_id, await request.body())


@tool_router.post("/admit", response_model=ToolPermit)
async def admit_tool(
    request: Request, response: Response, task_id: UUID, binding_id: str, body: ToolInvocation
):
    response.headers["Cache-Control"] = "no-store"
    return await service(request).admit_tool(
        task_id, tool_identity(request, task_id, binding_id), await request.body()
    )


@tool_router.post("/{invocation_id}/result", response_model=ToolResultReceipt)
async def tool_result(
    request: Request,
    response: Response,
    task_id: UUID,
    binding_id: str,
    invocation_id: str,
    body: ToolResultConfirmation,
):
    response.headers["Cache-Control"] = "no-store"
    return await service(request).confirm_tool_result(
        task_id, invocation_id, tool_identity(request, task_id, binding_id), await request.body()
    )


@tool_router.post("/{invocation_id}/unknown", response_model=ToolPermit)
async def tool_unknown(
    request: Request,
    task_id: UUID,
    binding_id: str,
    invocation_id: str,
    body: ToolUnknownSubmission,
):
    return await service(request).observe_tool_unknown(
        task_id, invocation_id, tool_identity(request, task_id, binding_id), await request.body()
    )
