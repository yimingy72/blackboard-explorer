"""Independent Runner control routes and trusted service registration."""

from uuid import UUID

from bbx_contracts.runner import (
    CommandReceipt,
    JournalConfirmation,
    JournalReceipt,
    JournalState,
    OperationAck,
    PublishedPrompt,
    RunnerPoll,
    SessionLeaseReceipt,
    SessionLeaseRequest,
    ToolInvocation,
    ToolResultReceipt,
)
from bbx_contracts.runner_bootstrap import (
    HelperAccepted,
    HelperLaunch,
    HelperResult,
    HelperStatus,
    HelperTerminal,
)
from bbx_contracts.runner_control import (
    RunnerBindingRegistration,
    RunnerCommandSubmission,
    RunnerOwnerRegistration,
    RunnerOwnerUpdate,
    RunnerPromptPublication,
)
from fastapi import APIRouter, HTTPException, Query, Request, Response

from bbx_blackboard.auth import require_service
from bbx_blackboard.gateway_auth import require_gateway_identity
from bbx_blackboard.native_helpers import BACKGROUND_STATUS, CANCEL
from bbx_blackboard.runner_auth import (
    issue_runner_control_token,
    require_runner_control,
    runner_credential_expiry,
)
from bbx_blackboard.runner_control import RunnerControl

router = APIRouter(prefix="/api/tasks/{task_id}/runner-control", tags=["runner-control"])
management = APIRouter(
    prefix="/api/tasks/{task_id}/ctf/runtime/runner-management", tags=["runner-management"]
)


def service(request: Request) -> RunnerControl:
    control = request.app.state.runner_control
    if control is None:
        raise HTTPException(503, "Runner storage is unavailable")
    return control


@management.post("/owners")
async def register_owner(
    request: Request, response: Response, task_id: UUID, body: RunnerOwnerRegistration
):
    require_service(request)
    owner = await service(request).register_owner(task_id, await request.body())
    response.headers["Cache-Control"] = "no-store"
    credential_expires_at = runner_credential_expiry(owner)
    return {
        "fence": {
            "task_id": str(task_id),
            **{key: owner[key] for key in ("runner_id", "pod_uid", "epoch")},
        },
        "expires_at": owner["expires_at"],
        "hard_deadline": owner["hard_deadline"],
        "token": issue_runner_control_token(
            request.app.state.settings, owner, expires_at=credential_expires_at
        ),
        "credential_expires_at": credential_expires_at,
        "execution_enabled": False,
    }


@management.post("/owners/renew")
async def renew_owner(request: Request, response: Response, task_id: UUID, body: RunnerOwnerUpdate):
    require_service(request)
    owner = await service(request).update_owner(task_id, await request.body())
    response.headers["Cache-Control"] = "no-store"
    credential_expires_at = runner_credential_expiry(owner)
    return {
        **{key: owner[key] for key in ("epoch", "expires_at", "hard_deadline", "revoked")},
        "token": issue_runner_control_token(
            request.app.state.settings, owner, expires_at=credential_expires_at
        ),
        "credential_expires_at": credential_expires_at,
    }


@management.post("/owners/revoke")
async def revoke_owner(request: Request, task_id: UUID, body: RunnerOwnerUpdate):
    require_service(request)
    owner = await service(request).update_owner(task_id, await request.body(), revoke=True)
    return {key: owner[key] for key in ("epoch", "expires_at", "hard_deadline", "revoked")}


@management.post("/bindings")
async def register_binding(request: Request, task_id: UUID, body: RunnerBindingRegistration):
    require_service(request)
    value = await service(request).register_binding(task_id, await request.body())
    return {
        "binding": value["binding"],
        "prepare_request_id": value["prepare_request_id"],
        "payload_digest": value["payload_digest"],
        "execution_enabled": False,
    }


@management.post("/bindings/{binding_id}/attach")
async def attach(request: Request, task_id: UUID, binding_id: str, body: RunnerOwnerUpdate):
    require_service(request)
    value = await service(request).attach(task_id, binding_id, await request.body())
    return {
        "binding": value["binding"],
        "owner_epoch": value["owner_epoch"],
        "execution_enabled": False,
    }


@management.post("/commands")
async def enqueue(request: Request, task_id: UUID, body: RunnerCommandSubmission):
    require_service(request)
    value = await service(request).enqueue(task_id, await request.body())
    return {
        key: value[key]
        for key in ("request_id", "owner_epoch", "command_seq", "status", "payload_digest")
    }


@management.put("/bindings/{binding_id}/prompt")
async def publish_prompt(
    request: Request, task_id: UUID, binding_id: str, body: RunnerPromptPublication
):
    require_service(request)
    return await service(request).publish_prompt(task_id, binding_id, await request.body())


@router.get("/commands", response_model=RunnerPoll)
async def poll(
    request: Request,
    task_id: UUID,
    after_seq: int = Query(default=0, ge=0),
    runner_id: str = "",
    pod_uid: str = "",
    epoch: int = 0,
    wait_seconds: float = Query(default=15, ge=0, le=15),
):
    identity = require_runner_control(request, task_id)
    if (
        runner_id != identity.fence.runner_id
        or pod_uid != identity.fence.pod_uid
        or epoch != identity.fence.epoch
    ):
        raise HTTPException(403, "Poll identity does not match credential")
    return await service(request).poll(
        task_id, identity, after_seq, wait_seconds, dispatcher=request.app.state.dispatcher
    )


@router.post("/bindings/{binding_id}/acquire", response_model=SessionLeaseReceipt)
async def acquire(request: Request, task_id: UUID, binding_id: str, body: SessionLeaseRequest):
    identity = require_runner_control(request, task_id)
    return await service(request).acquire(task_id, binding_id, identity, await request.body())


@router.post("/commands/{request_id}/receipts", response_model=OperationAck)
async def acknowledge(request: Request, task_id: UUID, request_id: str, body: CommandReceipt):
    identity = require_runner_control(request, task_id)
    return await service(request).acknowledge(task_id, request_id, identity, await request.body())


@router.post("/bindings/{binding_id}/journal/confirm", response_model=JournalReceipt)
async def confirm_journal(
    request: Request, task_id: UUID, binding_id: str, body: JournalConfirmation
):
    identity = require_runner_control(request, task_id)
    return await service(request).confirm_journal(
        task_id, binding_id, identity, await request.body()
    )


@router.get("/bindings/{binding_id}/journal", response_model=JournalState)
async def journal(request: Request, task_id: UUID, binding_id: str):
    identity = require_runner_control(request, task_id)
    return await service(request).journal_state(task_id, binding_id, identity)


@router.get("/bindings/{binding_id}/prompt", response_model=PublishedPrompt)
async def prompt(
    request: Request, task_id: UUID, binding_id: str, known_revision: int = Query(default=0, ge=0)
):
    identity = require_runner_control(request, task_id)
    value = await service(request).prompt(task_id, binding_id, identity, known_revision)
    return Response(status_code=304) if value is None else value


HELPERS = "/bindings/{binding_id}/helpers"


def _helpers(request, response):
    native = request.app.state.native_runner_bootstrap
    if native is None:
        raise HTTPException(503, "Native helper control is unavailable")
    response.headers["Cache-Control"] = "no-store"
    return native.helpers


@router.post(HELPERS + "/reserve", response_model=HelperStatus)
async def reserve_helper(
    request: Request, response: Response, task_id: UUID, binding_id: str, body: ToolInvocation
):
    identity = require_gateway_identity(request, task_id, "mcp")
    return await _helpers(request, response).reserve(
        task_id, binding_id, identity, await request.body()
    )


@router.post(
    HELPERS + "/{invocation_id}/launch",
    response_model=HelperLaunch,
    response_model_exclude_none=True,
)
async def launch_helper(
    request: Request,
    response: Response,
    task_id: UUID,
    binding_id: str,
    invocation_id: str,
    body: ToolInvocation,
):
    identity = require_gateway_identity(request, task_id, "mcp")
    return await _helpers(request, response).launch(
        task_id, binding_id, invocation_id, identity, await request.body()
    )


@router.get(HELPERS + "/{invocation_id}", response_model=HelperStatus)
async def helper_status(
    request: Request, response: Response, task_id: UUID, binding_id: str, invocation_id: str
):
    identity = require_runner_control(request, task_id)
    return await _helpers(request, response).status(task_id, binding_id, invocation_id, identity)


@router.post(HELPERS + "/{invocation_id}/cancel", response_model=HelperStatus)
async def cancel_helper(
    request: Request, response: Response, task_id: UUID, binding_id: str, invocation_id: str
):
    identity = require_runner_control(request, task_id)
    return await _helpers(request, response).report(
        task_id, binding_id, invocation_id, identity, "cancel"
    )


@router.post(HELPERS + "/{invocation_id}/terminal", response_model=HelperStatus)
async def helper_terminal(
    request: Request,
    response: Response,
    task_id: UUID,
    binding_id: str,
    invocation_id: str,
    body: HelperTerminal,
):
    identity = require_runner_control(request, task_id)
    return await _helpers(request, response).report(
        task_id, binding_id, invocation_id, identity, "terminal", body
    )


@router.post(HELPERS + "/{invocation_id}/result", response_model=HelperStatus)
async def helper_result(
    request: Request,
    response: Response,
    task_id: UUID,
    binding_id: str,
    invocation_id: str,
    body: HelperResult,
):
    identity = require_runner_control(request, task_id)
    return await _helpers(request, response).report(
        task_id, binding_id, invocation_id, identity, "result", body
    )


@router.post(HELPERS + "/{invocation_id}/accepted", response_model=HelperStatus)
async def accept_background_helper(
    request: Request,
    response: Response,
    task_id: UUID,
    binding_id: str,
    invocation_id: str,
    body: HelperAccepted,
):
    identity = require_runner_control(request, task_id)
    return await _helpers(request, response).accept(
        task_id, binding_id, invocation_id, identity, body
    )


@router.post(HELPERS + "/status", response_model=ToolResultReceipt)
async def background_helper_status(
    request: Request,
    response: Response,
    task_id: UUID,
    binding_id: str,
    body: ToolInvocation,
):
    identity = require_gateway_identity(request, task_id, "mcp")
    return await _helpers(request, response).parent_tool(
        task_id, binding_id, identity, await request.body(), BACKGROUND_STATUS
    )


@router.post(HELPERS + "/cancel", response_model=ToolResultReceipt)
async def cancel_background_helper(
    request: Request,
    response: Response,
    task_id: UUID,
    binding_id: str,
    body: ToolInvocation,
):
    identity = require_gateway_identity(request, task_id, "mcp")
    return await _helpers(request, response).parent_tool(
        task_id, binding_id, identity, await request.body(), CANCEL
    )
