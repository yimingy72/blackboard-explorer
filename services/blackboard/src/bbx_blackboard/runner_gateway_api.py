"""Independent model/MCP capability routes and private Runtime capture boundary."""

from uuid import UUID

from bbx_contracts.native_gateway import (
    GatewayCredentialsRequest,
    NativeBudgetBinding,
    NativeBudgetPrepare,
    NativeFeeView,
    NativeModelDispatchGrant,
    NativeModelDispatchRequest,
    NativeModelSelection,
    NativeTransportObservation,
    NativeTransportReceipt,
)
from bbx_contracts.runner import (
    ModelAdmissionReceipt,
    ModelAttemptRequest,
    ModelOutcomeReceipt,
    ModelOutcomeSubmission,
    ToolInvocation,
    ToolPermit,
    ToolResultConfirmation,
    ToolResultReceipt,
    ToolUnknownSubmission,
)
from fastapi import APIRouter, HTTPException, Request, Response

from bbx_blackboard.auth import require_service, require_task_reader
from bbx_blackboard.gateway_auth import require_gateway_identity

model_router = APIRouter(prefix="/api/tasks/{task_id}/runner-model", tags=["runner-model"])
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


def private_identity(request, task_id):
    require_service(request)
    return require_gateway_identity(
        request,
        task_id,
        "model",
        authorization=request.headers.get("x-bbx-runner-authorization", ""),
    )


def tool_identity(request, task_id, binding_id):
    identity = require_gateway_identity(request, task_id, "mcp")
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


@private_router.post("/native-budget/prepare", response_model=NativeBudgetBinding)
async def prepare_budget(
    request: Request, response: Response, task_id: UUID, body: NativeBudgetPrepare
):
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await service(request).native.prepare(task_id, await request.body())


@private_router.post("/native-budget/publish", response_model=NativeBudgetBinding)
async def publish_budget(request: Request, response: Response, task_id: UUID):
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await service(request).native.publish(task_id)


@private_router.get("/native-budget/model", response_model=NativeModelSelection)
async def native_model(request: Request, response: Response, task_id: UUID, model_alias: str):
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await service(request).native.selection(task_id, model_alias)


@model_router.get("/fees", response_model=NativeFeeView)
async def fees(request: Request, task_id: UUID):
    from bbx_blackboard.task_board import TaskBoardService

    gateway = service(request)
    identity = require_task_reader(request, task_id)
    async with gateway.repo.engine.connect() as conn:
        await TaskBoardService(gateway.ctf)._task_access(conn, task_id, identity)
    return await gateway.native.view(task_id)


@model_router.post("/attempts/admit", response_model=ModelAdmissionReceipt)
async def admit_model(request: Request, task_id: UUID, body: ModelAttemptRequest):
    return await service(request).native.admit_model(
        task_id, require_gateway_identity(request, task_id, "model"), await request.body()
    )


@private_router.post("/model/{attempt_id}/dispatch", response_model=NativeModelDispatchGrant)
async def dispatch(
    request: Request,
    response: Response,
    task_id: UUID,
    attempt_id: str,
    body: NativeModelDispatchRequest,
):
    response.headers["Cache-Control"] = "no-store"
    return await service(request).native.dispatch_model(
        task_id, attempt_id, private_identity(request, task_id), await request.body()
    )


@private_router.post("/model/{attempt_id}/transport", response_model=NativeTransportReceipt)
async def transport(
    request: Request, task_id: UUID, attempt_id: str, body: NativeTransportObservation
):
    require_service(request)
    return await service(request).native.transport(task_id, attempt_id, await request.body())


@model_router.post("/attempts/{attempt_id}/observation", response_model=ModelOutcomeReceipt)
async def observation(
    request: Request, task_id: UUID, attempt_id: str, body: ModelOutcomeSubmission
):
    return await service(request).native.observe_model(
        task_id,
        attempt_id,
        require_gateway_identity(request, task_id, "model"),
        await request.body(),
    )


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
