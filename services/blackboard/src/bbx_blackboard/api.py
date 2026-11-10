"""FastAPI boundary for the blackboard service."""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

import anyio
from bbx_contracts.challenge_board import (
    BOARD_CONTRACT,
    BOARD_LEGACY,
    business_contract,
    native_dispatch_enabled,
)
from bbx_contracts.ctf import (
    CtfAgentProfile,
    CtfEvent,
    CtfTaskCreate,
    resolve_ctf_model,
    resolve_ctf_options,
)
from bbx_contracts.gateway_models import GatewayModelSnapshot, GatewayModelView
from bbx_contracts.heartbeat import HeartbeatRequest, ReceiptHeartbeatRequest
from bbx_contracts.legacy_network import LegacyNetworkView
from bbx_contracts.models import (
    AgentProfile,
    Event,
    InitialAttachment,
    ModelConfig,
    PostFactRequest,
    PostIntentRequest,
    SearchHit,
    SubmitCloseRequest,
    TaskSpec,
    TaskStatus,
)
from bbx_contracts.pi_models import PiModelRef, PiModelVersion, PiReviewSelection
from bbx_contracts.runner import runner_json_text, runner_payload_digest
from bbx_contracts.runner_bootstrap import NativeRunnerLaunch, TaskCreationCapabilities
from bbx_contracts.runner_inputs import INPUT_PROFILE
from bbx_contracts.runner_vpn import NativeTaskNetworkStatus
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Numeric, Text, func, select, tuple_
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from bbx_blackboard.auth import (
    COOKIE_NAME,
    check_user_password,
    ctf_review_tool_request,
    issue_agent_token,
    issue_user_token,
    principal,
    require_agent_writer,
    require_service,
    require_task_reader,
    require_user_or_service,
)
from bbx_blackboard.billing import router as billing_router
from bbx_blackboard.business_mcp import BusinessMCP
from bbx_blackboard.challenge_board_api import router as challenge_board_router
from bbx_blackboard.conversations import Conversations
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.ctf_api import public_member
from bbx_blackboard.ctf_api import router as ctf_router
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.gateway_tasks import (
    TERMINAL_TASKS,
    native_ctf_review_closed,
    review_source,
    task_gateway_model,
    validate_task_threshold,
)
from bbx_blackboard.historical_fees import router as historical_fees_router
from bbx_blackboard.input_groups import InputGroups, download_headers, owner_key, request_hash
from bbx_blackboard.input_groups import router as inputs_router
from bbx_blackboard.legacy_network import (
    deployment_snapshot,
    protected_task,
    public_network,
    require_runtime_version,
    runtime_compatible,
)
from bbx_blackboard.legacy_network import (
    router as legacy_network_router,
)
from bbx_blackboard.legacy_network import (
    run_receipt as legacy_run_receipt,
)
from bbx_blackboard.legacy_network import (
    task_snapshot as legacy_task_snapshot,
)
from bbx_blackboard.mcp_catalog import router as mcp_catalog_router
from bbx_blackboard.native import NativeAnalysisCache
from bbx_blackboard.native_api import router as native_router
from bbx_blackboard.native_ctf_publication import (
    NativeCtfPublication,
    PublicationState,
    load_publication,
)
from bbx_blackboard.native_inputs import router as native_inputs_router
from bbx_blackboard.native_runner_bootstrap import NativeRunnerBootstrap, native_role_thinking
from bbx_blackboard.native_runner_bootstrap import router as native_runner_bootstrap_router
from bbx_blackboard.native_vpn import router as native_vpn_router
from bbx_blackboard.pi_ledger import pi_pending, pi_totals, task_pi_model
from bbx_blackboard.pi_ledger import public as pi_fees_router
from bbx_blackboard.pi_ledger import router as pi_ledger_router
from bbx_blackboard.pi_playground import router as pi_playground_router
from bbx_blackboard.pi_registry import PiRegistry, display_pi_model
from bbx_blackboard.pi_registry import router as pi_registry_router
from bbx_blackboard.platform import PlatformStore
from bbx_blackboard.platform import router as platform_router
from bbx_blackboard.profiles import ProfileStore, ctf_profile, require_ctf_tool_capabilities
from bbx_blackboard.progress import router as progress_router
from bbx_blackboard.runner_control import RunnerControl
from bbx_blackboard.runner_control_api import management as runner_management_router
from bbx_blackboard.runner_control_api import router as runner_control_router
from bbx_blackboard.runner_gateway import RunnerGateway
from bbx_blackboard.runner_gateway_api import private_router as runner_gateway_private_router
from bbx_blackboard.runner_gateway_api import tool_router as runner_tool_router
from bbx_blackboard.sandbox_tools import router as sandbox_tools_router
from bbx_blackboard.service import BoardService, ObjectStore
from bbx_blackboard.settings import Settings
from bbx_blackboard.store import schema as s
from bbx_blackboard.traffic import router as traffic_router
from bbx_blackboard.worker_settings import (
    model_context_threshold,
)
from bbx_blackboard.worker_settings import router as settings_router
from bbx_blackboard.workspace import (
    ArchiveTooLarge,
    InvalidArchive,
    InvalidPath,
    NotPreviewable,
    WorkspaceArchiveCache,
)


class LoginBody(BaseModel):
    username: str
    password: str


class TaskCreateBody(TaskSpec):
    pi_model: PiModelRef | None = Field(default=None, exclude_if=lambda value: value is None)
    input_group_id: UUID | None = None
    input_file_ids: list[UUID] | None = Field(default=None, max_length=20)
    model_id: str | None = Field(default=None, min_length=1)
    model_version: int | None = Field(default=None, ge=1)
    profile_version: int | None = Field(default=None, ge=1)
    reasoning_effort: str | None = Field(
        default=None, min_length=1, description="任务思考强度；省略或 null 沿用模型配置"
    )

    @model_validator(mode="after")
    def check_input_selection(self) -> TaskCreateBody:
        if self.input_file_ids is not None:
            if self.input_group_id is None:
                raise ValueError("Input file selection requires an input group")
            self.input_file_ids = sorted(set(self.input_file_ids), key=str)
        return self


class TaskCreated(BaseModel):
    id: UUID
    agent_profile: str
    agent_profile_version: int


class GatewayFeeScope(BaseModel):
    confirmed_usd: str | None = None
    pending: int = 0


class GatewayTaskFees(BaseModel):
    solver: GatewayFeeScope
    progress: GatewayFeeScope


class TaskView(BaseModel):
    execution_blocked_reason: Literal["legacy_execution_unmapped"] | None = None
    pi_model: PiModelRef | None = Field(default=None, exclude_if=lambda value: value is None)
    gateway_fees: GatewayTaskFees | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    gateway_model: GatewayModelView | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    business_contract: str | None = None
    name: str | None = None
    initial_attachments: list[InitialAttachment] = Field(default_factory=list)
    legacy_network: LegacyNetworkView | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    native_network: NativeTaskNetworkStatus | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    initial_attachment_state: Literal["pending", "ready", "blocked"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    cost_currency: str | None = None
    cost_breakdown: dict[str, str] = Field(default_factory=dict)
    id: UUID
    mode: Literal["blackboard", "ctf"] = "blackboard"
    goal: str
    status: str
    deleting: bool
    acceptance_state: dict[str, Any]
    usage: dict[str, Any]
    agents: list[dict[str, Any]]
    report_uri: str | None
    workspace_uri: str | None
    agent_profile: str
    agent_profile_version: int
    created_at: datetime
    budget: dict[str, Any] = Field(default_factory=dict)
    run_number: int = 1
    active_seconds: int = 0
    active_since: datetime | None = None
    cleanup_ready: bool = False
    runs: list[dict[str, Any]] = Field(default_factory=list)
    members: list[dict[str, Any]] = Field(default_factory=list)
    ctf_options: dict[str, Any] | None = None
    ctf_phase: str | None = None
    ctf_conclusion: dict[str, Any] | None = None


class TaskPage(BaseModel):
    items: list[TaskView]
    total: int
    status_counts: dict[str, int]
    limit: int
    offset: int


class ResumeBody(BaseModel):
    currency: Literal["USD"] | None = None
    request_id: UUID
    additional_cost: Decimal = Field(ge=0)
    additional_minutes: int = Field(ge=0)
    refresh_tools: bool = False


class BudgetAdjustBody(BaseModel):
    currency: Literal["USD"] | None = None
    request_id: UUID | None = None
    additional_cost: Decimal = Field(default=Decimal(0), ge=0)
    max_minutes: int | None = Field(default=None, ge=1)


class StatusBody(BaseModel):
    status: Literal["running", "closing", "finished", "failed", "stopped"]
    reason: str | None = None


class StatusResult(BaseModel):
    status: str
    events: list[dict[str, Any]] = Field(default_factory=list)


class ArchiveBody(BaseModel):
    uri: str = Field(min_length=1)
    size: int = Field(ge=0)
    fallback: Literal["none", "agents-only"]


class AgentRegisterBody(BaseModel):
    task_type: Literal["explore", "derive", "close"]
    is_seed: bool = False
    intent_id: str | None = None
    close_mode: Literal["judge", "final"] | None = None
    derive_parallel: bool | None = None
    derive_review: bool = False

    @model_validator(mode="after")
    def check_derive_mode(self) -> AgentRegisterBody:
        if self.derive_review and (self.task_type != "derive" or self.derive_parallel is True):
            raise ValueError("derive_review requires a nonparallel derive agent")
        if self.intent_id is not None and self.task_type != "explore":
            raise ValueError("intent_id requires an explore agent")
        return self


class AgentRegistered(BaseModel):
    agent_id: str
    token: str
    derive_round: int | None = None
    started_at: AwareDatetime | None = None


class UserMessageBody(BaseModel):
    id: UUID
    content: str = Field(min_length=1, max_length=20000)


class DeliveryBody(BaseModel):
    id: UUID
    claim_token: UUID


class SessionBody(BaseModel):
    session: dict[str, Any]
    opening_instructions: str
    origin: Literal["native", "legacy"]
    expected_revision: int = Field(ge=0)
    expected_derive_round: int | None = Field(default=None, ge=1)
    expected_started_at: AwareDatetime | None = None
    deliveries: list[DeliveryBody] = Field(default_factory=list)
    review_claim: DeliveryBody | None = None


class ClaimBody(BaseModel):
    mode: Literal["active", "review"]


class CompleteBody(SessionBody):
    claim_token: UUID
    content: str = Field(min_length=1)
    usage: dict[str, Any] = Field(default_factory=dict)


class FailBody(BaseModel):
    claim_token: UUID
    error: str = Field(min_length=1, max_length=200)


class HeartbeatBody(HeartbeatRequest):
    model_config = ConfigDict(extra="ignore")


class ConcludeBody(BaseModel):
    reason: str = Field(min_length=1)
    expected_derive_round: int | None = Field(default=None, ge=1)


class GraceBody(BaseModel):
    expected_derive_round: int | None = Field(default=None, ge=1)


class FinishBody(BaseModel):
    receipt: dict[str, Any]
    end_reason: str = Field(min_length=1)
    expected_derive_round: int | None = Field(default=None, ge=1)
    expected_started_at: AwareDatetime | None = None
    expected_run_number: int | None = Field(default=None, ge=1)
    expected_agent_run_id: str | None = Field(default=None, min_length=1)


class ClaimForBody(BaseModel):
    intent_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)


class ReleaseBody(BaseModel):
    note: str = Field(min_length=1)


class CloseBody(SubmitCloseRequest):
    report_uri: str | None = None


class ToolCallBody(BaseModel):
    agent_id: str = Field(min_length=1)
    expected_derive_round: int | None = Field(default=None, ge=1)
    id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    result_head: str | None = None
    result_uri: str | None = None


class AgentTraceBody(BaseModel):
    expected_derive_round: int | None = Field(default=None, ge=1)
    kind: Literal["initial_context", "board_update", "model_output", "model_error"]
    step: int = Field(ge=0)
    uri: str = Field(min_length=1)
    summary: str = Field(max_length=240)


class ProfileName(BaseModel):
    name: str
    latest_version: int


class ProfileVersion(BaseModel):
    name: str
    version: int
    created_by: str | None
    created_at: datetime


class ProfileDocument(ProfileVersion):
    profile: AgentProfile | CtfAgentProfile


class UploadResult(BaseModel):
    key: str
    size: int


class WorkspaceEntryView(BaseModel):
    path: str
    kind: Literal["file", "directory", "link"]
    size: int


class WorkspaceTreeView(BaseModel):
    entries: list[WorkspaceEntryView]


class WorkspaceFileView(BaseModel):
    path: str
    size: int
    text: str
    truncated: bool
    binary: bool


def _web_dist() -> Path:
    return Path(__file__).resolve().parents[4] / "web/dist"


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.lstrip("/").split("/", 1)[0] == "api":
                raise
        else:
            if response.status_code != 404:
                return response
        if path.lstrip("/").split("/", 1)[0] == "api" or "." in Path(path).name:
            raise HTTPException(404, "Not found")
        return await super().get_response("index.html", scope)


def _service(request: Request) -> BoardService:
    service: BoardService | None = request.app.state.board_service
    if service is None:
        raise HTTPException(503, "Blackboard is starting")
    return service


def _conversations(request: Request) -> Conversations:
    conversations: Conversations | None = request.app.state.conversations
    if conversations is None:
        raise HTTPException(503, "Blackboard is starting")
    return conversations


def _profiles(request: Request) -> ProfileStore:
    store: ProfileStore | None = request.app.state.profile_store
    if store is None:
        raise HTTPException(503, "Blackboard is starting")
    return store


async def _task(request: Request, task_id: UUID) -> dict[str, Any]:
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        row = (
            (await conn.execute(select(s.tasks).where(s.tasks.c.id == task_id))).mappings().first()
        )
    if row is None:
        raise HTTPException(404, "Task not found")
    return dict(row)


async def _agent_record(request: Request, task_id: UUID, agent_id: str) -> dict[str, Any]:
    async with request.app.state.engine.connect() as conn:
        found = (
            (
                await conn.execute(
                    select(
                        s.agent_runs.c.id,
                        s.agent_runs.c.task_type,
                        s.agent_runs.c.derive_round,
                        s.agent_runs.c.started_at,
                    ).where(s.agent_runs.c.task_id == task_id, s.agent_runs.c.id == agent_id)
                )
            )
            .mappings()
            .first()
        )
    if found is None:
        raise HTTPException(404, "Agent not found")
    return dict(found)


def _run_uri(kind: str, task_id: UUID, run: int) -> str:
    suffix = "md" if kind == "reports" else "tar.zst"
    return f"{kind}/{task_id}.{suffix}" if run == 1 else f"{kind}/{task_id}/run-{run}.{suffix}"


async def _run_record(request: Request, task_id: UUID, run: int) -> dict[str, Any]:
    async with request.app.state.engine.connect() as conn:
        found = (
            (
                await conn.execute(
                    select(s.task_runs).where(
                        s.task_runs.c.task_id == task_id, s.task_runs.c.run_number == run
                    )
                )
            )
            .mappings()
            .first()
        )
    return dict(found) if found else {}


async def _task_runs(request: Request, task_id: UUID) -> list[dict[str, Any]]:
    async with request.app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(s.task_runs)
                    .where(s.task_runs.c.task_id == task_id)
                    .order_by(s.task_runs.c.run_number)
                )
            )
            .mappings()
            .all()
        )
    return [dict(item) for item in rows]


async def _workspace_uri(request: Request, task_id: UUID, run: int | None = None) -> str:
    task = await _task(request, task_id)
    number = run or task.get("run_number", 1)
    uri = (
        (await _run_record(request, task_id, number)).get("workspace_uri")
        if run
        else task.get("workspace_uri")
    )
    if not uri or uri != _run_uri("workspace", task_id, number):
        raise HTTPException(404, "Workspace archive not found")
    if not await request.app.state.objects.exists(uri):
        raise HTTPException(404, "Workspace archive not found")
    return uri


def _key_task(uri: str) -> UUID | None:
    parts = uri.split("/")
    if (
        any(part in {"", ".", ".."} for part in parts)
        or (parts[0] == "evidence" and len(parts) != 4)
        or (parts[0] == "inputs" and len(parts) != 4)
        or (parts[0] == "toolcalls" and (len(parts) != 3 or not parts[2].endswith(".txt")))
        or (
            parts[0] == "traces" and (len(parts) != 4 or not re.fullmatch(r"[^/]+\.json", parts[3]))
        )
        or parts[0] not in {"evidence", "toolcalls", "traces", "inputs"}
    ):
        return None
    try:
        return UUID(parts[1])
    except ValueError:
        return None


def _upload_limit(task_id: UUID, key: str) -> int:
    tid = str(task_id)
    if any(part in {"", ".", ".."} for part in key.split("/")):
        raise HTTPException(422, "Invalid object key for task")
    if re.fullmatch(rf"evidence/{tid}/[^/]+/[^/]+", key):
        return 50 * 1024 * 1024
    if re.fullmatch(rf"toolcalls/{tid}/[^/]+\.txt", key):
        return 10 * 1024 * 1024
    if key == f"reports/{tid}.md":
        return 10 * 1024 * 1024
    if key == f"workspace/{tid}.tar.zst":
        return 2 * 1024 * 1024 * 1024
    raise HTTPException(422, "Invalid object key for task")


def _profile_document(row: dict[str, Any]) -> ProfileDocument:
    return ProfileDocument(
        name=row["name"],
        version=row["version"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        profile=(
            ctf_profile(row)
            if row.get("prompts", {}).get("mode") == "ctf"
            else AgentProfile.model_validate({key: row[key] for key in AgentProfile.model_fields})
        ),
    )


class TaskBoundaryMiddleware:
    """Check task access without wrapping request bodies or cancellation in a task group."""

    def __init__(self, app: ASGIApp, check: Any) -> None:
        self.app, self.check = app, check

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            response = await self.check(Request(scope, receive))
            if response is not None:
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    objects: ObjectStore | None = None,
    dispatcher: Any | None = None,
) -> FastAPI:
    """Construct routes without I/O; initialize owned resources in lifespan."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_engine = app.state.engine is None
        own_dispatcher = app.state.dispatcher is None
        if app.state.native_ctf_publication.publication is None:
            app.state.native_ctf_publication = load_publication(settings)
        if own_engine:
            app.state.engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        if app.state.objects is None:
            from bbx_objects import ObjectStore as MinioStore

            endpoint = urlsplit(settings.minio_endpoint)
            app.state.objects = MinioStore(
                endpoint.netloc,
                settings.minio_root_user,
                settings.minio_root_password.get_secret_value(),
                settings.minio_bucket,
                secure=endpoint.scheme == "https",
            )
        app.state.board_service = BoardService(
            app.state.engine, app.state.objects, pi_site_id=settings.pi_site_id
        )
        app.state.ctf_service = CtfService(app.state.engine, app.state.objects)
        app.state.runner_control = RunnerControl(app.state.engine)
        app.state.runner_gateway = RunnerGateway(app.state.engine, settings)
        app.state.native_runner_bootstrap = NativeRunnerBootstrap(app.state.runner_gateway)
        app.state.conversations = Conversations(app.state.engine)
        app.state.input_groups = InputGroups(app.state.engine, app.state.objects)
        app.state.profile_store = ProfileStore(app.state.engine)
        app.state.platform_store = PlatformStore(
            app.state.engine, settings.agent_token_secret.get_secret_value()
        )
        app.state.pi_registry = PiRegistry(app.state.platform_store, settings)
        await app.state.profile_store.ensure_bundled(settings.profiles_dir)
        if own_dispatcher:
            from bbx_blackboard.sse import SSEDispatcher

            app.state.dispatcher = SSEDispatcher(settings)
        await app.state.dispatcher.start()
        cleanup = asyncio.create_task(app.state.input_groups.run_cleanup())
        try:
            await app.state.native_cache.initialize()
            async with app.state.direct_business_mcp.run():
                yield
        finally:
            cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup
            await app.state.workspace_cache.close()
            await app.state.native_cache.close()
            await app.state.dispatcher.stop()
            if own_engine:
                await app.state.engine.dispose()

    app = FastAPI(title="Blackboard API", version="1.0.0", lifespan=lifespan)
    app.include_router(billing_router)
    app.include_router(inputs_router)
    app.include_router(traffic_router)
    app.include_router(native_router)
    app.include_router(legacy_network_router)
    app.include_router(pi_playground_router)
    app.include_router(pi_ledger_router)
    app.include_router(pi_fees_router)
    app.include_router(historical_fees_router)
    app.include_router(pi_registry_router)
    app.state.settings = settings
    app.state.legacy_network_publication = deployment_snapshot(settings)
    app.state.native_ctf_publication = PublicationState(
        exclusive=settings.native_ctf_publication_file is not None
    )
    app.state.engine = engine
    app.state.objects = objects
    app.state.dispatcher = dispatcher
    app.state.board_service = (
        BoardService(engine, objects, pi_site_id=settings.pi_site_id)
        if engine is not None and objects is not None
        else None
    )
    app.state.ctf_service = CtfService(engine, objects) if engine is not None else None
    app.state.runner_control = RunnerControl(engine) if engine is not None else None
    app.state.runner_gateway = RunnerGateway(engine, settings) if engine is not None else None
    app.state.native_runner_bootstrap = (
        NativeRunnerBootstrap(app.state.runner_gateway)
        if app.state.runner_gateway is not None
        else None
    )
    app.state.direct_business_mcp = BusinessMCP(app)
    app.state.conversations = Conversations(engine) if engine is not None else None
    app.state.input_groups = (
        InputGroups(engine, objects) if engine is not None and objects is not None else None
    )
    app.state.profile_store = ProfileStore(engine) if engine is not None else None
    app.state.platform_store = (
        PlatformStore(engine, settings.agent_token_secret.get_secret_value())
        if engine is not None
        else None
    )
    app.state.pi_registry = (
        PiRegistry(app.state.platform_store, settings) if app.state.platform_store else None
    )
    app.state.workspace_cache = WorkspaceArchiveCache()
    app.state.native_cache = NativeAnalysisCache()

    async def protected_snapshot(request, tid):
        engine = request.app.state.engine
        if engine is None:
            return None
        async with engine.connect() as conn:
            return await legacy_task_snapshot(conn, tid)

    async def require_task_model(request, tid, task):
        async with request.app.state.engine.connect() as conn:
            frozen = await task_pi_model(conn, tid)
            if task.get("mode") == "ctf":
                from bbx_blackboard.gateway_auth import task_direct_mcp

                if not await task_direct_mcp(conn, tid):
                    frozen = None
        if frozen is None or frozen[0] != settings.pi_model_authority:
            raise HTTPException(409, "旧执行无法安全迁移；原记录仍可查看和导出")
        if task.get("mode", "blackboard") == "blackboard":
            prior = await protected_snapshot(request, tid)
            if prior is None:
                raise HTTPException(409, "此历史任务没有原黑板网络配置")
            if prior != request.app.state.legacy_network_publication:
                raise HTTPException(409, "原黑板网络配置不可用，不能改用其他出口")

    async def ctf_boundary(request: Request) -> Response | None:
        # Presentation capabilities never enter a solver endpoint, including future routes.
        if request.headers.get("authorization") or request.cookies.get(COOKIE_NAME):
            try:
                identity = principal(request)
                if identity.purpose == "review":
                    if identity.task_id is not None and ctf_review_tool_request(
                        request, identity.task_id
                    ):
                        return None
                    return JSONResponse(
                        {"detail": "Review cannot access solver APIs"}, status_code=403
                    )
                if identity.purpose == "progress":
                    prefix = f"/api/tasks/{identity.task_id}/progress"
                    path = request.url.path
                    allowed_read = request.method == "GET" and (
                        path == prefix
                        or path in {prefix + "/sources", prefix + "/search", prefix + "/evidence"}
                        or path.startswith(prefix + "/objects/")
                    )
                    allowed_candidate = request.method == "POST" and path == (
                        prefix + f"/jobs/{identity.progress_job_id}/candidate"
                    )
                    if not (allowed_read or allowed_candidate):
                        return JSONResponse(
                            {"detail": "Progress capability cannot access this API"},
                            status_code=403,
                        )
            except HTTPException:
                pass
        if re.fullmatch(r"/api/tasks/[0-9a-fA-F-]{36}/runner-control(?:/.*)?", request.url.path):
            # This fixed router uses an independent audience and never constructs a Principal.
            return None
        if re.fullmatch(
            r"/api/tasks/[0-9a-fA-F-]{36}/(business-mcp-v2|sandbox-host|sandbox/commands|runner-vpn)(?:/.*)?",
            request.url.path,
        ):
            return None
        if request.app.state.engine is None and request.app.state.ctf_service is None:
            return None
        match = re.match(r"^/api/tasks/([0-9a-fA-F-]{36})(/.*)?$", request.url.path)
        evidence_tid = (
            _key_task(request.query_params.get("uri", ""))
            if request.url.path == "/api/evidence"
            else None
        )
        if match or evidence_tid:
            try:
                tid = UUID(match[1]) if match else evidence_tid
                if tid is None:
                    return None
                identity = require_task_reader(request, tid)
                task = await _task(request, tid)
                mode = task.get("mode", "blackboard")
                request.state.task_mode = mode
                request.state.native_worker_dispatch = mode == "ctf" and native_dispatch_enabled(
                    task
                )
                request.state.business_contract = business_contract(task)
                suffix = (match[2] or "") if match else "/evidence"
                if (
                    mode == "blackboard"
                    and identity.kind == "service"
                    and not runtime_compatible(request)
                    and (
                        suffix in {"/state", "/archive-data", "/execution-network"}
                        or request.method != "GET"
                    )
                    and await protected_snapshot(request, tid) is not None
                ):
                    require_runtime_version(request)
                if identity.kind == "agent" and identity.mode != mode:
                    raise HTTPException(409, "mode_mismatch")
                if mode != "ctf" and suffix.startswith("/ctf/"):
                    raise HTTPException(409, "mode_mismatch")
                if mode == "ctf":
                    versioned = business_contract(task) != BOARD_LEGACY
                    if (
                        versioned
                        and identity.kind == "agent"
                        and not suffix.startswith("/ctf/knowledge/")
                        and suffix not in {"/events", "/stream", "/evidence"}
                    ):
                        raise HTTPException(
                            403, "Versioned Agent must use its published business tools"
                        )
                    if (
                        versioned
                        and suffix.startswith("/ctf/")
                        and not suffix.startswith("/ctf/knowledge/")
                    ):
                        if identity.kind == "agent" or (
                            request.method != "GET" and not suffix.startswith("/ctf/runtime/")
                        ):
                            raise HTTPException(
                                403, "Legacy business entry is unavailable for this Task contract"
                            )
                    if identity.kind == "agent":
                        if identity.generation is None:
                            raise HTTPException(403, "CTF token required")
                        knowledge_route = suffix.startswith("/ctf/knowledge/")
                        if versioned and request.method == "GET" and not knowledge_route:
                            from bbx_blackboard.task_board import TaskBoardService

                            board_engine = request.app.state.engine
                            if board_engine is None:
                                raise HTTPException(503, "Board storage is unavailable")
                            async with board_engine.connect() as conn:
                                _, board_member = await TaskBoardService(
                                    request.app.state.ctf_service
                                )._task_access(conn, tid, identity)
                            if board_member is None:
                                raise HTTPException(403, "Agent board identity required")
                            uri = request.query_params.get("uri", "")
                            if (
                                suffix == "/evidence"
                                and uri.startswith("inputs/")
                                and board_member["role"] != "lead"
                            ):
                                public = set(task["ctf_control"].get("public_input_ids", []))
                                if not any(
                                    item["uri"] == uri and item["id"] in public
                                    for item in task.get("initial_attachments", [])
                                ):
                                    raise HTTPException(403, "Input is not shared with this Agent")
                        if request.method == "GET" and not knowledge_route:
                            await request.app.state.ctf_service.authorize_reader(
                                tid, identity.agent_id, identity.generation
                            )
                        elif (
                            request.method != "GET"
                            and not knowledge_route
                            and not suffix.startswith("/ctf/tools/")
                        ):
                            await request.app.state.ctf_service.authorize_member(
                                tid,
                                identity.agent_id,
                                identity.generation,
                                identity.turn_id,
                            )
                        if suffix == "/evidence" and not request.query_params.get(
                            "uri", ""
                        ).startswith("inputs/"):
                            owned = await request.app.state.ctf_service.authorize_object(
                                tid,
                                identity.agent_id,
                                request.query_params.get("uri", ""),
                            )
                            if not owned:
                                raise HTTPException(403, "Private CTF object access denied")
                    common = {
                        "",
                        "/state",
                        "/events",
                        "/stream",
                        "/start",
                        "/stop",
                        "/resume",
                        "/evidence",
                        "/archive-data",
                        "/archive",
                        "/cleanup-ready",
                        "/purge",
                        "/report",
                        "/workspace",
                        "/workspace/tree",
                        "/workspace/file",
                    }
                    conversation = bool(re.fullmatch(r"/agents/[^/]+/(messages|session)", suffix))
                    traffic = suffix == "/traffic" or suffix.startswith("/traffic/")
                    review_gateway = (
                        identity.kind == "service"
                        and request.method == "POST"
                        and (
                            suffix == "/gateway-model/review/claim"
                            or bool(
                                re.fullmatch(
                                    r"/gateway-model/attempts/[0-9a-fA-F-]{36}/result", suffix
                                )
                            )
                        )
                        or identity.kind == "user"
                        and request.method == "GET"
                        and suffix == "/gateway-model/review-costs"
                    )
                    if (
                        not suffix.startswith("/ctf/")
                        and suffix not in common
                        and not conversation
                        and not traffic
                        and not review_gateway
                    ):
                        raise HTTPException(409, "mode_mismatch")
                    if identity.kind == "agent" and suffix in {
                        "/archive-data",
                        "/archive",
                        "/cleanup-ready",
                        "/purge",
                        "/report",
                        "/workspace",
                        "/workspace/tree",
                        "/workspace/file",
                    }:
                        raise HTTPException(403, "User or service access required")
                    if conversation and request.method not in {"GET", "POST"}:
                        raise HTTPException(
                            409, "CTF sessions require generation-fenced checkpoint"
                        )
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code)
            except (ValueError, TypeError):
                return JSONResponse({"detail": "Invalid task identifier"}, status_code=422)
        return None

    app.add_middleware(TaskBoundaryMiddleware, check=ctf_boundary)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> Response:
        # Never echo raw request input: platform forms may include credentials.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()
                ]
            },
        )

    @app.exception_handler(RuleViolation)
    async def rule_error(_request: Request, exc: RuleViolation) -> Response:
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing_error(_request: Request, _exc: KeyError) -> Response:
        return JSONResponse({"code": "not_found", "message": "Object not found"}, status_code=404)

    @app.post("/api/login", tags=["auth"])
    async def login(body: LoginBody, response: Response) -> dict[str, str]:
        if not check_user_password(settings, body.username, body.password):
            raise HTTPException(401, "Invalid username or password")
        response.set_cookie(
            COOKIE_NAME,
            issue_user_token(settings, body.username),
            max_age=12 * 3600,
            httponly=True,
            samesite="lax",
            path="/api",
        )
        return {"username": body.username}

    @app.post("/api/logout", tags=["auth"])
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME, path="/api")
        return {"ok": True}

    async def resolve_ctf_creation(
        request: Request, publication: NativeCtfPublication
    ) -> CtfAgentProfile:
        bootstrap = request.app.state.native_runner_bootstrap
        if bootstrap is None or request.app.state.objects is None:
            raise HTTPException(503, "原生 CTF 控制服务或对象存储不可用")
        bootstrap.require_launch_configuration()
        row = await _profiles(request).get("ctf", publication.ctf_profile_version)
        profile = ctf_profile(row)
        require_ctf_tool_capabilities(profile)
        return profile

    async def selected_task_model(request: Request, body) -> PiModelVersion:
        registry = request.app.state.pi_registry
        if registry is None or not registry.enabled:
            raise HTTPException(503, "平台 Pi 模型目录尚未启用")
        if body.pi_model is None:
            raise HTTPException(422, "请选择平台已启用的 Pi 模型")
        return await registry.selected(body.pi_model)

    @app.get(
        "/api/task-creation-capabilities",
        response_model=TaskCreationCapabilities,
        tags=["tasks"],
    )
    async def task_creation_capabilities(
        request: Request, response: Response
    ) -> TaskCreationCapabilities:
        require_user_or_service(request)
        response.headers["Cache-Control"] = "no-store"
        state: PublicationState = request.app.state.native_ctf_publication
        publication = state.publication
        if publication is None:
            return state.capabilities()
        try:
            await resolve_ctf_creation(request, publication)
        except (HTTPException, ValueError):
            return state.capabilities(available=False)
        capabilities = state.capabilities()
        if (
            capabilities.native_ctf is not None
            and getattr(request.app.state.pi_registry, "enabled", False)
            and settings.pi_model_authority
        ):
            capabilities.native_ctf.external_mcp = True
        return capabilities

    @app.post("/api/tasks", response_model=TaskCreated, tags=["tasks"])
    async def create_task(request: Request, body: TaskCreateBody | CtfTaskCreate) -> TaskCreated:
        identity = require_user_or_service(request)
        # Optional native_environment is omitted when absent, preserving the legacy request hash.
        digest = request_hash(body.model_dump())
        state: PublicationState = request.app.state.native_ctf_publication
        if body.input_group_id is not None:
            store: InputGroups = request.app.state.input_groups
            if store is None:
                raise HTTPException(503, "Blackboard is starting")
            prior = await store.prior_task(body.input_group_id, owner_key(identity), digest)
            if prior is not None:
                return TaskCreated(
                    id=prior["id"],
                    agent_profile=prior["agent_profile"],
                    agent_profile_version=prior["agent_profile_version"],
                )
        native_queue = None
        selected = None
        publication = state.publication if state.exclusive else None
        if isinstance(body, CtfTaskCreate) and not state.exclusive:
            raise HTTPException(409, "请使用已发布原生 CTF 环境创建任务")
        if body.budget.currency != "USD":
            raise HTTPException(422, "新任务预算必须明确使用 USD")
        if (
            body.model_id is not None
            or body.model_version is not None
            or body.reasoning_effort is not None
        ):
            raise HTTPException(422, "新任务只能引用平台 Pi 模型")

        if state.exclusive:
            if publication is None:
                raise HTTPException(503, "当前部署没有可用的原生 CTF 环境")
            if not isinstance(body, CtfTaskCreate) or body.native_environment is None:
                raise HTTPException(409, "当前部署只支持使用已发布环境创建原生 CTF 任务")
            if (
                body.native_environment.id,
                body.native_environment.revision,
                body.native_environment.publication_stamp,
            ) != (publication.id, publication.revision, state.stamp):
                raise HTTPException(409, "所选原生 CTF 环境或版本已变化")
            if body.input_group_id is None:
                raise HTTPException(422, "原生 CTF 任务需要一个本人附件组")
            if publication.inputs_profile != INPUT_PROFILE and (
                body.input_file_ids or body.public_input_ids
            ):
                raise HTTPException(422, "此原生 CTF 发布未启用初始附件")
            if (
                body.agent_profile != "ctf"
                or body.profile_version not in (None, publication.ctf_profile_version)
                or body.reasoning_effort is not None
                or body.egress_allowlist
                or body.ctf_options.model_fields_set - {"dispatch_mode", "max_teammates"}
                or (
                    "dispatch_mode" in body.ctf_options.model_fields_set
                    and body.ctf_options.dispatch_mode != "automatic"
                )
            ):
                raise HTTPException(422, "该原生 CTF 环境要求已发布的模型、配置和自动派工")
            publication.validate_budget(body.budget.max_cost)
            selected = await selected_task_model(request, body)
            bootstrap = request.app.state.native_runner_bootstrap
            if bootstrap is None:
                raise HTTPException(503, "原生 CTF 控制服务不可用")
            launch = NativeRunnerLaunch(
                request_id=body.input_group_id,
                model_alias=selected.id,
                budget_currency="USD",
                traffic_profile="binding-pcap-v1",
            )
            launch_digest = runner_payload_digest(runner_json_text(launch.model_dump(mode="json")))
            frozen_publication = publication.frozen()
            native_grants = None
            if body.native_mcp is not None:
                from bbx_blackboard.mcp_catalog import freeze_native_grants

                native_grants = await freeze_native_grants(request, body.native_mcp)

            async def queue_native(conn, tid):
                await bootstrap.repo.lock(conn, tid)
                await bootstrap.queue_locked(
                    conn,
                    tid,
                    launch,
                    launch_digest,
                    publication=frozen_publication,
                    native_grants=native_grants,
                )

            native_queue = queue_native
        elif isinstance(body, CtfTaskCreate) and body.native_environment is not None:
            raise HTTPException(409, "当前部署未发布原生 CTF 环境")

        if isinstance(body, CtfTaskCreate) and body.native_vpn_source_id is not None:
            from bbx_contracts.runner_vpn import VPN_DYNAMIC_PROFILE

            if publication is None or publication.vpn_profile != VPN_DYNAMIC_PROFILE:
                raise HTTPException(422, "Private VPN source is not supported by this Task")
        if isinstance(body, CtfTaskCreate):
            assert publication is not None and selected is not None
            profile = await resolve_ctf_creation(request, publication)
            try:
                profile = CtfAgentProfile.model_validate(
                    {
                        **profile.model_dump(mode="json"),
                        "model": display_pi_model(selected),
                    }
                )
                for role in ("lead", "teammate"):
                    thinking = native_role_thinking(
                        resolve_ctf_model(profile, role).reasoning_effort
                    )
                    if thinking != "off" and not selected.reasoning:
                        raise ValueError("Selected route lacks reasoning support")
            except ValueError as error:
                raise HTTPException(422, "所选模型不兼容已发布的 CTF 角色配置") from error
            try:
                requested_options = body.ctf_options.model_dump(exclude_unset=True)
                if publication:
                    requested_options["dispatch_mode"] = "automatic"
                options = resolve_ctf_options(profile, requested_options, inherit_model_window=True)
                if publication and not 1 <= options.max_teammates <= 20:
                    raise ValueError("原生 CTF 任务需要 1 至 20 个 Worker 席位")
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            # Native dispatch uses the Task's board contract, not the legacy role tool catalog.
            row = await _profiles(request).create("ctf-task-settings", profile, identity.name)
            spec = body.model_dump(
                exclude={
                    "input_group_id",
                    "input_file_ids",
                    "pi_model",
                    "public_input_ids",
                    "model_id",
                    "model_version",
                    "profile_version",
                    "reasoning_effort",
                    "native_environment",
                    "native_vpn_source_id",
                    "native_mcp",
                }
            )
            spec["agent_profile"] = row["name"]
            spec["ctf_options"] = options.model_dump()
            tid = await request.app.state.ctf_service.create_task(
                spec,
                profile_version=row["version"],
                pi_model=(settings.pi_model_authority, selected),
                **(
                    {
                        "business_contract": BOARD_CONTRACT,
                        "native_queue": native_queue,
                        "native_inputs_profile": publication.inputs_profile
                        if publication
                        else None,
                        "native_vpn_profile": publication.vpn_profile if publication else None,
                        "native_vpn_source_id": body.native_vpn_source_id,
                    }
                    if native_queue is not None
                    else {}
                ),
                **(
                    {
                        "input_group_id": body.input_group_id,
                        "input_file_ids": body.input_file_ids,
                        **(
                            {"public_input_ids": body.public_input_ids}
                            if body.public_input_ids is not None
                            else {}
                        ),
                        "input_owner": owner_key(identity),
                        "create_request_hash": digest,
                    }
                    if body.input_group_id is not None
                    else {}
                ),
            )
            original = await _task(request, tid)
            return TaskCreated(
                id=tid,
                agent_profile=original["agent_profile"],
                agent_profile_version=original["agent_profile_version"],
            )
        legacy_network = request.app.state.legacy_network_publication
        if legacy_network is None:
            raise HTTPException(503, "黑板执行网络配置不可用")
        profiles = _profiles(request)
        selected = await selected_task_model(request, body)
        validate_task_threshold(body.budget.max_cost)
        row = await profiles.get(body.agent_profile, body.profile_version)
        if row.get("prompts", {}).get("mode") == "ctf":
            raise HTTPException(409, "mode_mismatch")
        content = {key: row[key] for key in AgentProfile.model_fields}
        model = display_pi_model(selected)
        content["models"] = {role: model for role in ("explore", "derive", "close")}
        threshold = model_context_threshold(content["models"])
        if threshold is not None:
            content["params"] = {**content["params"], "context_threshold": threshold}
        frozen_profile = AgentProfile.model_validate(content)
        from bbx_blackboard.mcp_catalog import validate_profile_tools

        await validate_profile_tools(request, frozen_profile)
        row = await profiles.create("task-settings", frozen_profile, "task")
        spec = body.model_dump(
            exclude={
                "profile_version",
                "pi_model",
                "model_id",
                "model_version",
                "reasoning_effort",
                "input_group_id",
                "input_file_ids",
            }
        )
        spec["agent_profile"] = row["name"]
        spec["params"] = {**row["params"], **body.params}
        if threshold is not None:
            spec["params"]["context_threshold"] = threshold
        task_id = await _service(request).create_task(
            spec,
            profile_version=row["version"],
            legacy_network=legacy_network,
            pi_model=(settings.pi_model_authority, selected),
            **(
                {
                    "input_group_id": body.input_group_id,
                    "input_file_ids": body.input_file_ids,
                    "input_owner": owner_key(identity),
                    "create_request_hash": digest,
                }
                if body.input_group_id is not None
                else {}
            ),
        )
        if body.input_group_id is not None:
            # A concurrent retry may have bound this group with an earlier model
            # snapshot. Return the task's real profile, not the retry's new choice.
            row = await _task(request, task_id)
            return TaskCreated(
                id=task_id,
                agent_profile=row["agent_profile"],
                agent_profile_version=row["agent_profile_version"],
            )
        return TaskCreated(
            id=task_id,
            agent_profile=row["name"],
            agent_profile_version=row["version"],
        )

    @app.get("/api/tasks/deletions", tags=["tasks"])
    async def pending_deletions(request: Request) -> list[UUID]:
        require_service(request)
        return await _conversations(request).deletions(
            include_protected=runtime_compatible(request)
        )

    async def task_views(conn: AsyncConnection, tasks: list[dict[str, Any]]) -> list[TaskView]:
        if not tasks:
            return []
        ids = [task["id"] for task in tasks]
        gateway_models = {
            row.task_id: GatewayModelSnapshot.model_validate(row.snapshot).public()
            for row in (
                await conn.execute(
                    select(s.gateway_task_models.c.task_id, s.gateway_task_models.c.snapshot).where(
                        s.gateway_task_models.c.task_id.in_(ids)
                    )
                )
            )
        }
        pi_models = {
            item.task_id: PiModelRef(
                model_id=item.snapshot["id"], model_revision=item.snapshot["model_revision"]
            )
            for item in (
                await conn.execute(
                    select(s.pi_task_models.c.task_id, s.pi_task_models.c.snapshot).where(
                        s.pi_task_models.c.authority == settings.pi_model_authority,
                        s.pi_task_models.c.task_id.in_(ids),
                    )
                )
            )
        }
        ordinary_ids = [task["id"] for task in tasks if task.get("mode") != "ctf"]
        ctf_ids = [task["id"] for task in tasks if task.get("mode") == "ctf"]
        direct_ctf = (
            set(
                (
                    await conn.execute(
                        select(s.task_runner_resources.c.task_id).where(
                            s.task_runner_resources.c.task_id.in_(ctf_ids),
                            s.task_runner_resources.c.data["mcp_transport"].astext == "direct-v2",
                        )
                    )
                ).scalars()
            )
            if ctf_ids
            else set()
        )

        agents: dict[UUID, list[dict[str, Any]]] = {}
        members: dict[UUID, list[dict[str, Any]]] = {}
        runs: dict[UUID, list[dict[str, Any]]] = {}
        profiles = {
            (row["name"], row["version"]): row["models"]
            for row in (
                await conn.execute(
                    select(
                        s.agent_profiles.c.name,
                        s.agent_profiles.c.version,
                        s.agent_profiles.c.models,
                    ).where(
                        tuple_(s.agent_profiles.c.name, s.agent_profiles.c.version).in_(
                            [
                                (task["agent_profile"], task["agent_profile_version"])
                                for task in tasks
                            ]
                        )
                    )
                )
            ).mappings()
        }
        if ordinary_ids:
            for row in (
                await conn.execute(
                    select(s.agent_runs)
                    .where(
                        s.agent_runs.c.task_id.in_(ordinary_ids),
                        s.agent_runs.c.status.in_(("running", "concluding")),
                    )
                    .order_by(s.agent_runs.c.task_id, s.agent_runs.c.id)
                )
            ).mappings():
                agents.setdefault(row["task_id"], []).append(dict(row))
        if ctf_ids:
            for row in (
                await conn.execute(
                    select(s.ctf_members).where(s.ctf_members.c.task_id.in_(ctf_ids))
                )
            ).mappings():
                members.setdefault(row["task_id"], []).append(
                    public_member({**row, "agent_id": row["id"]})
                )
        for row in (
            await conn.execute(
                select(s.task_runs)
                .where(s.task_runs.c.task_id.in_(ids))
                .order_by(s.task_runs.c.run_number)
            )
        ).mappings():
            runs.setdefault(row["task_id"], []).append(dict(row))
        progress_rows = (
            {
                item.task_id: item.data
                for item in (
                    await conn.execute(
                        select(s.task_progress.c.task_id, s.task_progress.c.data).where(
                            s.task_progress.c.task_id.in_(ordinary_ids)
                        )
                    )
                )
                if isinstance(item.data, dict)
            }
            if ordinary_ids
            else {}
        )
        fees: dict[UUID, dict[str, GatewayFeeScope]] = {}
        if gateway_models or ctf_ids:
            attempts, costs = s.gateway_execution_attempts, s.native_request_costs
            attached = attempts.outerjoin(
                costs,
                (costs.c.accounting_source_id == attempts.c.accounting_source_id)
                & (costs.c.native_log_id == attempts.c.native_log_id)
                & (costs.c.state == "known"),
            )
            for tid, purpose, amount, pending in await conn.execute(
                select(
                    attempts.c.task_id,
                    attempts.c.purpose,
                    func.sum(sql_cast(costs.c.cost_usd, Numeric)),
                    func.count().filter(costs.c.cost_usd.is_(None)),
                )
                .select_from(attached)
                .where(
                    attempts.c.task_id.in_(gateway_models),
                    attempts.c.purpose.in_(("ordinary", "progress")),
                )
                .group_by(attempts.c.task_id, attempts.c.purpose)
            ):
                name = "solver" if purpose == "ordinary" else "progress"
                fees.setdefault(tid, {})[name] = GatewayFeeScope(
                    confirmed_usd=str(amount) if amount is not None else None, pending=pending
                )
            native_ids = ctf_ids
            if native_ids:
                for tid, amount in await conn.execute(
                    select(costs.c.task_id, func.sum(sql_cast(costs.c.cost_usd, Numeric)))
                    .where(
                        costs.c.task_id.in_(native_ids),
                        costs.c.state == "known",
                        costs.c.evidence["attributes"]["bbx_purpose"].astext.is_(None),
                    )
                    .group_by(costs.c.task_id)
                ):
                    fees.setdefault(tid, {})["solver"] = GatewayFeeScope(
                        confirmed_usd=str(amount) if amount is not None else None
                    )
                for tid, pending in await conn.execute(
                    select(s.runner_calls.c.task_id, func.count())
                    .where(
                        s.runner_calls.c.task_id.in_(native_ids),
                        s.runner_calls.c.kind == "model",
                        s.runner_calls.c.state.in_(("dispatched", "unknown")),
                    )
                    .group_by(s.runner_calls.c.task_id)
                ):
                    fees.setdefault(tid, {}).setdefault(
                        "solver", GatewayFeeScope()
                    ).pending = pending
        result = []
        for task in tasks:
            models = profiles.get((task["agent_profile"], task["agent_profile_version"]))
            if models is None:
                raise KeyError(task["agent_profile"])
            currencies = {model.get("price", {}).get("currency") for model in models.values()}
            currency = task["budget"].get("currency") or (
                currencies.pop() if len(currencies) == 1 else None
            )
            progress = progress_rows.get(task["id"], {})
            progress_cost = Decimal(str(progress.get("cost", 0)))
            progress_currency = progress.get("budget_currency") or (
                (progress.get("model") or {}).get("price", {}).get("currency")
            )
            solver_cost = Decimal(str(task["usage"].get("cost", 0)))
            common_currency = not progress or (
                currency is not None and currency == progress_currency
            )
            result.append(
                TaskView(
                    gateway_model=gateway_models.get(task["id"]),
                    pi_model=pi_models.get(task["id"]),
                    execution_blocked_reason=(
                        "legacy_execution_unmapped"
                        if task["id"] not in pi_models
                        or (task["id"] in ctf_ids and task["id"] not in direct_ctf)
                        else None
                    ),
                    gateway_fees=GatewayTaskFees(
                        solver=fees.get(task["id"], {}).get("solver", GatewayFeeScope()),
                        progress=fees.get(task["id"], {}).get("progress", GatewayFeeScope()),
                    )
                    if task["id"] in gateway_models or task["id"] in fees
                    else None,
                    mode=task.get("mode", "blackboard"),
                    name=task.get("name"),
                    initial_attachments=task.get("initial_attachments") or [],
                    initial_attachment_state=(task.get("ctf_control") or {}).get(
                        "initial_attachment_state"
                    ),
                    cost_currency=currency,
                    id=task["id"],
                    goal=task["goal"],
                    status=task["status"],
                    deleting=task["deleting"],
                    acceptance_state=task["acceptance_state"],
                    usage=task["usage"],
                    cost_breakdown={
                        "solver": str(solver_cost),
                        "progress": str(progress_cost),
                        **({"total": str(solver_cost + progress_cost)} if common_currency else {}),
                    },
                    agents=agents.get(task["id"], []),
                    report_uri=task.get("report_uri"),
                    workspace_uri=task.get("workspace_uri"),
                    agent_profile=task["agent_profile"],
                    agent_profile_version=task["agent_profile_version"],
                    created_at=task["created_at"],
                    budget=task["budget"],
                    run_number=task["run_number"],
                    active_seconds=task["active_seconds"],
                    active_since=task["active_since"],
                    cleanup_ready=task["cleanup_ready"],
                    runs=runs.get(task["id"], []),
                    members=members.get(task["id"], []),
                    ctf_options=task.get("ctf_options"),
                    business_contract=business_contract(task)
                    if task.get("mode") == "ctf"
                    else None,
                    ctf_phase=(task.get("ctf_control") or {}).get("phase"),
                    ctf_conclusion=task.get("ctf_conclusion"),
                )
            )
        return result

    @app.get("/api/tasks", response_model=list[TaskView], tags=["tasks"])
    async def list_tasks(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[TaskView]:
        identity = require_user_or_service(request)
        async with request.app.state.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            rows = (
                await conn.execute(
                    select(
                        s.tasks,
                        select(s.task_progress.c.data["cost"].astext)
                        .where(s.task_progress.c.task_id == s.tasks.c.id)
                        .scalar_subquery()
                        .label("progress_cost"),
                    )
                    .where(
                        *(
                            [~protected_task()]
                            if identity.kind == "service" and not runtime_compatible(request)
                            else []
                        )
                    )
                    .order_by(s.tasks.c.created_at.desc(), s.tasks.c.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).mappings()
            return await task_views(conn, [dict(row) for row in rows])

    @app.get("/api/tasks/page", response_model=TaskPage, tags=["tasks"])
    async def task_page(
        request: Request,
        q: str = Query(default="", max_length=200),
        status: TaskStatus | Literal["active", "ended"] | None = None,
        mode: Literal["blackboard", "ctf"] | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> TaskPage:
        require_user_or_service(request)
        conditions = []
        if q.strip():
            search = func.concat_ws(
                " ", s.tasks.c.name, s.tasks.c.goal, sql_cast(s.tasks.c.id, Text)
            )
            conditions.append(search.icontains(q.strip(), autoescape=True))
        if mode is not None:
            conditions.append(s.tasks.c.mode == mode)
        async with request.app.state.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            counts = {value.value: 0 for value in TaskStatus}
            for name, count in await conn.execute(
                select(s.tasks.c.status, func.count()).where(*conditions).group_by(s.tasks.c.status)
            ):
                counts[name] = count
            if status is None:
                total = sum(counts.values())
            else:
                states = {
                    "active": ("created", "provisioning", "running", "closing"),
                    "ended": ("finished", "stopped"),
                }.get(status, (status,))
                total = sum(counts.get(value, 0) for value in states)
                conditions.append(s.tasks.c.status.in_(states))
            rows = (
                await conn.execute(
                    select(
                        s.tasks,
                        select(s.task_progress.c.data["cost"].astext)
                        .where(s.task_progress.c.task_id == s.tasks.c.id)
                        .scalar_subquery()
                        .label("progress_cost"),
                    )
                    .where(*conditions)
                    .order_by(s.tasks.c.created_at.desc(), s.tasks.c.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).mappings()
            return TaskPage(
                items=await task_views(conn, [dict(row) for row in rows]),
                total=total,
                status_counts=counts,
                limit=limit,
                offset=offset,
            )

    @app.get("/api/tasks/{task_id}", response_model=TaskView, tags=["tasks"])
    async def task_detail(request: Request, task_id: UUID) -> TaskView:
        require_task_reader(request, task_id)
        async with request.app.state.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            task = (
                (
                    await conn.execute(
                        select(
                            s.tasks,
                            select(s.task_progress.c.data["cost"].astext)
                            .where(s.task_progress.c.task_id == s.tasks.c.id)
                            .scalar_subquery()
                            .label("progress_cost"),
                        ).where(s.tasks.c.id == task_id)
                    )
                )
                .mappings()
                .first()
            )
            if task is None:
                raise HTTPException(404, "Task not found")
            view = (await task_views(conn, [dict(task)]))[0]
            if task["mode"] == "blackboard" and (
                policy := await legacy_task_snapshot(conn, task_id)
            ):
                receipt = await legacy_run_receipt(conn, task_id, task["run_number"])
                view.legacy_network = public_network(policy, receipt)
            return view

    @app.post("/api/tasks/{task_id}/resume", response_model=TaskView, tags=["tasks"])
    async def resume_task(request: Request, task_id: UUID, body: ResumeBody) -> TaskView:
        identity = require_user_or_service(request)
        if identity.kind != "user":
            raise HTTPException(403, "User token required")
        task = await _task(request, task_id)
        if body.additional_cost and body.currency != "USD":
            raise HTTPException(422, "追加金额必须明确使用 USD")
        if task.get("mode") == "ctf" and business_contract(task) == BOARD_LEGACY:
            raise HTTPException(409, "专用原生 CTF 部署不能恢复旧版任务")
        if task.get("mode") == "ctf":
            await require_task_model(request, task_id, task)
            await request.app.state.ctf_service.resume(
                task_id,
                body.request_id,
                body.additional_cost,
                body.additional_minutes,
                body.refresh_tools,
                identity.name,
            )
            return await task_detail(request, task_id)
        await require_task_model(request, task_id, task)
        await _service(request).resume(
            task_id,
            body.request_id,
            body.additional_cost,
            body.additional_minutes,
            body.refresh_tools,
            identity.name,
        )
        return await task_detail(request, task_id)

    @app.post("/api/tasks/{task_id}/budget", response_model=TaskView, tags=["tasks"])
    async def adjust_task_budget(
        request: Request, task_id: UUID, body: BudgetAdjustBody
    ) -> TaskView:
        identity = require_user_or_service(request)
        if identity.kind != "user":
            raise HTTPException(403, "User token required")
        task = await _task(request, task_id)
        if body.additional_cost and body.currency != "USD":
            raise HTTPException(422, "追加金额必须明确使用 USD")
        if task.get("mode") == "ctf":
            raise HTTPException(409, "mode_mismatch")
        await require_task_model(request, task_id, task)
        await _service(request).adjust_budget(
            task_id,
            body.additional_cost,
            body.max_minutes,
            identity.name,
            request_id=body.request_id,
        )
        return await task_detail(request, task_id)

    @app.post("/api/tasks/{task_id}/start", response_model=StatusResult, tags=["tasks"])
    async def start_task(request: Request, task_id: UUID) -> StatusResult:
        require_user_or_service(request)
        task = await _task(request, task_id)
        if request.app.state.native_ctf_publication.exclusive:
            if business_contract(task) == BOARD_LEGACY:
                raise HTTPException(409, "专用原生 CTF 部署不能启动旧版任务")
            if task["status"] == "provisioning":
                return StatusResult(status="provisioning")
        if task.get("mode") == "ctf":
            if business_contract(task) == BOARD_LEGACY:
                raise HTTPException(409, "历史 CTF 任务没有原生执行引用")
            if task["status"] == "running":
                return StatusResult(status="running")
            raise HTTPException(409, f"CTF task cannot start from {task['status']}")
        await require_task_model(request, task_id, task)
        if task["status"] != "created":
            raise HTTPException(409, "Task is not created")
        events = await _service(request).transition(task_id, "provisioning", actor="user")
        return StatusResult(status="provisioning", events=events)

    @app.post("/api/tasks/{task_id}/stop", response_model=StatusResult, tags=["tasks"])
    async def stop_task(request: Request, task_id: UUID) -> StatusResult:
        require_user_or_service(request)
        service = _service(request)
        task = await _task(request, task_id)
        if task.get("mode") == "ctf":
            from uuid import uuid4

            if business_contract(task) != BOARD_LEGACY and task["status"] in {
                "finished",
                "failed",
                "stopped",
            }:
                return StatusResult(status=task["status"])
            stopped = await request.app.state.ctf_service.request_finish(
                task_id,
                actor="user",
                request_id=uuid4(),
                conclusion={"end_reason": "user_stop", "summary": "Stopped by user"},
            )
            return StatusResult(status=(stopped or {}).get("status", "closing"))
        status = task["status"]
        events: list[dict[str, Any]] = []
        if status in {"created", "provisioning"}:
            events = await service.transition(task_id, "stopped", actor="user", reason="manual")
            status = "stopped"
        elif status == "running":
            try:
                events = await service.transition(task_id, "closing", actor="user", reason="manual")
            except RuleViolation as exc:
                if exc.code != "invalid_transition":
                    raise
                status = (await _task(request, task_id))["status"]
                if status not in {"closing", "finished", "failed", "stopped"}:
                    raise
            else:
                status = "closing"
        if status == "closing":
            board = await service.state(task_id)
            for agent in board["agents"].values():
                if agent["status"] == "running":
                    try:
                        events.extend(await service.conclude(task_id, agent["id"], "closing"))
                    except RuleViolation as exc:
                        if exc.code != "agent_inactive":
                            raise
        return StatusResult(status=status, events=events)

    @app.delete("/api/tasks/{task_id}", status_code=202, tags=["tasks"])
    async def delete_task(request: Request, task_id: UUID) -> dict[str, Any]:
        identity = require_user_or_service(request)
        if identity.kind != "user":
            raise HTTPException(403, "User token required")
        return await _conversations(request).request_delete(task_id)

    @app.post("/api/tasks/{task_id}/purge", tags=["system"])
    async def purge_task(request: Request, task_id: UUID) -> dict[str, bool]:
        require_service(request)
        if request.app.state.engine is not None:
            async with request.app.state.engine.connect() as conn:
                if await pi_pending(conn, task_id):
                    raise HTTPException(409, "原 Pi 调用尚未确认，不能删除任务")
        result = await _conversations(request).purge(task_id, request.app.state.objects)
        await request.app.state.workspace_cache.invalidate_task(task_id)
        await request.app.state.native_cache.invalidate_task(task_id)
        return result

    @app.get("/api/tasks/{task_id}/archive-data", tags=["system"])
    async def archive_data(request: Request, task_id: UUID) -> dict[str, Any]:
        require_service(request)
        if (await _task(request, task_id)).get("mode") == "ctf":
            return await request.app.state.ctf_service.export_archive(task_id)
        return await _conversations(request).export_archive(task_id)

    @app.get("/api/conversations/pending", tags=["system"])
    async def pending_conversations(request: Request) -> list[dict[str, Any]]:
        require_service(request)
        return await _conversations(request).pending()

    @app.post("/api/conversations/recover", tags=["system"])
    async def recover_conversations(request: Request) -> dict[str, int]:
        require_service(request)
        return await _conversations(request).recover()

    @app.get("/api/tasks/{task_id}/agents/{agent_id}/messages", tags=["conversations"])
    async def agent_messages(
        request: Request,
        task_id: UUID,
        agent_id: str,
        status: Literal["queued"] | None = None,
    ) -> dict[str, Any]:
        if getattr(request.state, "task_mode", "blackboard") == "ctf":
            identity = require_task_reader(request, task_id)
            if identity.kind == "agent" and identity.agent_id != agent_id:
                raise HTTPException(403, "Private messages access denied")
            data = await request.app.state.ctf_service.state(task_id)
            return {
                "messages": [
                    {
                        key: value
                        for key, value in item.items()
                        if not key.startswith("claim_") and key != "lease_until"
                    }
                    for item in data["messages"]
                    if (item["recipient_id"] == agent_id or item["sender_id"] == agent_id)
                    and (status is None or item["status"] == status)
                ]
            }
        require_user_or_service(request)
        return await _conversations(request).list_messages(task_id, agent_id, status)

    @app.get("/api/ctf/reviews/pending-tasks", tags=["conversations"])
    async def pending_ctf_reviews(request: Request) -> dict[str, list[UUID]]:
        require_service(request)
        terminal = s.tasks.c.status.in_(TERMINAL_TASKS)
        async with request.app.state.engine.connect() as conn:
            tasks = (
                (
                    await conn.execute(
                        select(s.tasks.c.id)
                        .where(
                            s.tasks.c.mode == "ctf",
                            s.tasks.c.deleting.is_(False),
                            s.ctf_members.c.task_id == s.tasks.c.id,
                            s.ctf_members.c.id == s.ctf_messages.c.recipient_id,
                            (
                                terminal
                                & (
                                    s.tasks.c.cleanup_ready.is_(True)
                                    | native_ctf_review_closed(s.tasks.c.id)
                                )
                            )
                            | (~terminal & s.ctf_members.c.lifecycle.in_(("stopped", "removed"))),
                            s.ctf_messages.c.task_id == s.tasks.c.id,
                            s.ctf_messages.c.sender_kind == "user",
                            s.ctf_messages.c.purpose == "review",
                            s.ctf_messages.c.review_model["pi_model"].astext.is_not(None),
                            s.ctf_messages.c.request_run_number == s.tasks.c.run_number,
                            s.ctf_messages.c.status.in_(("queued", "leased")),
                        )
                        .distinct()
                        .order_by(s.tasks.c.id)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
        return {"task_ids": list(tasks)}

    @app.post("/api/tasks/{task_id}/agents/{agent_id}/messages", tags=["conversations"])
    async def post_agent_message(
        request: Request, task_id: UUID, agent_id: str, body: UserMessageBody
    ) -> dict[str, Any]:
        identity = require_user_or_service(request)
        if identity.kind != "user":
            raise HTTPException(403, "User token required")
        task = await _task(request, task_id)
        review_model = None
        origin = await review_source(request.app.state.engine, task, agent_id)
        if origin is not None:
            registry = request.app.state.pi_registry
            if registry is None or not registry.enabled:
                raise HTTPException(503, "终态复盘暂没有可用的 Pi 模型")
            async with request.app.state.engine.connect() as conn:
                frozen = await task_pi_model(conn, task_id)
            candidates = [
                model
                for model in await registry.list()
                if model.enabled
                and model.has_secret
                and model.tool_call
                and model.has_text_limits()
            ]
            selected = next(
                (model for model in candidates if frozen and model.id == frozen[1].id), None
            ) or next(iter(candidates), None)
            if selected is None:
                raise HTTPException(503, "终态复盘暂没有可用的 Pi 模型")
            review_model = PiReviewSelection(
                authority=settings.pi_model_authority, pi_model=selected
            )
        if getattr(request.state, "task_mode", "blackboard") == "ctf":
            return await request.app.state.ctf_service.post_message(
                task_id,
                actor="user",
                recipient_id=agent_id,
                body=body.content,
                message_id=body.id,
                review_model=review_model,
                review_origin=origin,
                request_user=identity.name,
            )
        return await _conversations(request).post_message(
            task_id,
            agent_id,
            body.id,
            body.content,
            review_model=review_model,
            review_origin=origin,
            request_user=identity.name,
        )

    @app.get("/api/tasks/{task_id}/agents/{agent_id}/session", tags=["conversations"])
    async def get_agent_session(request: Request, task_id: UUID, agent_id: str) -> dict[str, Any]:
        if getattr(request.state, "task_mode", "blackboard") == "ctf":
            identity = require_task_reader(request, task_id)
            if identity.kind == "agent" and identity.agent_id != agent_id:
                raise HTTPException(403, "Private session access denied")
        else:
            require_service(request)
        return await _conversations(request).get_session(task_id, agent_id)

    @app.put("/api/tasks/{task_id}/agents/{agent_id}/session", tags=["conversations"])
    async def put_agent_session(
        request: Request, task_id: UUID, agent_id: str, body: SessionBody
    ) -> dict[str, Any]:
        require_service(request)
        return await _conversations(request).put_session(
            task_id,
            agent_id,
            body.session,
            body.opening_instructions,
            body.origin,
            body.expected_revision,
            [delivery.model_dump() for delivery in body.deliveries],
            body.review_claim.model_dump() if body.review_claim else None,
            expected_started_at=body.expected_started_at,
            **(
                {"expected_derive_round": body.expected_derive_round}
                if body.expected_derive_round is not None
                else {}
            ),
        )

    @app.post(
        "/api/tasks/{task_id}/agents/{agent_id}/messages/{message_id}/claim", tags=["conversations"]
    )
    async def claim_agent_message(
        request: Request, task_id: UUID, agent_id: str, message_id: UUID, body: ClaimBody
    ) -> dict[str, Any]:
        require_service(request)
        return await _conversations(request).claim(task_id, agent_id, message_id, body.mode)

    @app.post(
        "/api/tasks/{task_id}/agents/{agent_id}/messages/{message_id}/complete",
        tags=["conversations"],
    )
    async def complete_agent_message(
        request: Request, task_id: UUID, agent_id: str, message_id: UUID, body: CompleteBody
    ) -> dict[str, Any]:
        require_service(request)
        return await _conversations(request).complete(
            task_id,
            agent_id,
            message_id,
            body.claim_token,
            body.session,
            body.opening_instructions,
            body.origin,
            body.expected_revision,
            body.content,
            body.usage,
            expected_started_at=body.expected_started_at,
        )

    @app.post(
        "/api/tasks/{task_id}/agents/{agent_id}/messages/{message_id}/fail", tags=["conversations"]
    )
    async def fail_agent_message(
        request: Request, task_id: UUID, agent_id: str, message_id: UUID, body: FailBody
    ) -> dict[str, Any]:
        require_service(request)
        return await _conversations(request).fail(
            task_id, agent_id, message_id, body.claim_token, body.error
        )

    @app.get("/api/tasks/{task_id}/report", tags=["tasks"], response_class=PlainTextResponse)
    async def report(
        request: Request, task_id: UUID, run: int | None = Query(default=None, ge=1)
    ) -> PlainTextResponse:
        require_task_reader(request, task_id)
        task = await _task(request, task_id)
        number = run or task.get("run_number", 1)
        uri = (
            (await _run_record(request, task_id, number)).get("report_uri")
            if run
            else task.get("report_uri")
        )
        if not uri or uri != _run_uri("reports", task_id, number):
            raise HTTPException(404, "Report not found")
        if not await request.app.state.objects.exists(uri):
            raise HTTPException(404, "Report not found")
        data = await request.app.state.objects.get(uri)
        return PlainTextResponse(data.decode("utf-8"), media_type="text/markdown; charset=utf-8")

    @app.get("/api/tasks/{task_id}/workspace", tags=["tasks"])
    async def workspace(
        request: Request, task_id: UUID, run: int | None = Query(default=None, ge=1)
    ) -> StreamingResponse:
        require_task_reader(request, task_id)
        uri = await _workspace_uri(request, task_id, run)
        return StreamingResponse(
            request.app.state.objects.stream(uri),
            media_type="application/zstd",
            headers={"Content-Disposition": f'attachment; filename="{task_id}.tar.zst"'},
        )

    @app.get(
        "/api/tasks/{task_id}/workspace/tree", response_model=WorkspaceTreeView, tags=["tasks"]
    )
    async def workspace_tree(
        request: Request, task_id: UUID, run: int | None = Query(default=None, ge=1)
    ) -> WorkspaceTreeView:
        require_task_reader(request, task_id)
        uri = await _workspace_uri(request, task_id, run)
        try:
            entries = await request.app.state.workspace_cache.tree(request.app.state.objects, uri)
        except ArchiveTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except (InvalidArchive, InvalidPath) as exc:
            raise HTTPException(400, str(exc)) from exc
        return WorkspaceTreeView(
            entries=[WorkspaceEntryView(**entry.__dict__) for entry in entries]
        )

    @app.get(
        "/api/tasks/{task_id}/workspace/file", response_model=WorkspaceFileView, tags=["tasks"]
    )
    async def workspace_file(
        request: Request,
        task_id: UUID,
        path: str = Query(min_length=1),
        run: int | None = Query(default=None, ge=1),
    ) -> WorkspaceFileView:
        require_task_reader(request, task_id)
        uri = await _workspace_uri(request, task_id, run)
        try:
            preview = await request.app.state.workspace_cache.file(
                request.app.state.objects, uri, path
            )
        except ArchiveTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except (InvalidArchive, InvalidPath, NotPreviewable) as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, "Workspace file not found") from exc
        return WorkspaceFileView(**preview)

    @app.get("/api/tasks/{task_id}/state", tags=["board"])
    async def state(request: Request, task_id: UUID) -> dict[str, Any]:
        identity = require_task_reader(request, task_id)
        task = await _task(request, task_id)
        if task.get("mode") == "ctf":
            from bbx_blackboard.ctf_api import state as ctf_state

            return await ctf_state(request, task_id)
        board = await _service(request).state(task_id)
        engine = request.app.state.engine
        if engine is None:
            return board
        async with engine.connect() as conn:
            policy = await legacy_task_snapshot(conn, task_id)
            if direct := await task_pi_model(conn, task_id):
                _, unknown = await pi_totals(conn, task_id, settings.pi_site_id)
                board["task"]["pi_fee_hold"] = bool(unknown)
                board["task"]["pi_model"] = PiModelRef(
                    model_id=direct[1].id,
                    model_revision=direct[1].model_revision,
                ).model_dump(mode="json")
            if frozen := await task_gateway_model(conn, task_id):
                board["task"]["gateway_model"] = (
                    frozen[0] if identity.kind == "service" else frozen[0].public()
                ).model_dump(mode="json")
            if policy is not None:
                receipt = await legacy_run_receipt(conn, task_id, board["task"]["run_number"])
                if identity.kind == "service":
                    require_runtime_version(request)
                    board["task"]["legacy_network"] = policy.model_dump(mode="json")
                    board["task"]["legacy_network_receipt"] = receipt
                else:
                    board["task"]["legacy_network"] = public_network(policy, receipt).model_dump(
                        mode="json"
                    )
        return board

    @app.get("/api/tasks/{task_id}/snapshot", tags=["board"], response_class=PlainTextResponse)
    async def snapshot(
        request: Request,
        task_id: UUID,
        format: Literal["yaml"] = "yaml",
        max_lines: int | None = Query(default=None, ge=1),
    ) -> PlainTextResponse:
        require_task_reader(request, task_id)
        await _task(request, task_id)
        from bbx_blackboard.domain.snapshot import snapshot as make_snapshot

        board = await _service(request).state(task_id)
        limit = max_lines or int(board["task"]["params"].get("snapshot_max_lines", 150))
        return PlainTextResponse(make_snapshot(board, limit), media_type="application/yaml")

    @app.get("/api/tasks/{task_id}/events", response_model=list[Event | CtfEvent], tags=["board"])
    async def events(
        request: Request,
        task_id: UUID,
        since: int = Query(default=0, ge=0),
        for_agent: str | None = Query(default=None, alias="for"),
    ) -> list[dict[str, Any]]:
        identity = require_task_reader(request, task_id)
        await _task(request, task_id)
        if identity.kind == "agent":
            if for_agent is not None and for_agent != identity.agent_id:
                raise HTTPException(403, "Agent filter denied")
            for_agent = identity.agent_id
        return await _service(request).events(task_id, since, for_agent)

    @app.get("/api/tasks/{task_id}/stream", tags=["board"])
    async def stream(
        request: Request, task_id: UUID, since: int = Query(default=0, ge=0)
    ) -> Response:
        identity = require_task_reader(request, task_id)
        await _task(request, task_id)
        last = request.headers.get("last-event-id")
        if last is not None:
            if not last.isdecimal():
                raise HTTPException(422, "Last-Event-ID must be a nonnegative integer")
            since = int(last)
        from bbx_blackboard.sse import stream_response

        event_service: Any = _service(request)
        if getattr(request.state, "task_mode", "blackboard") == "ctf" and identity.kind == "agent":
            from bbx_blackboard.ctf_api import ReaderEvents

            event_service = ReaderEvents(event_service, request.app.state.ctf_service, identity)
        return stream_response(
            event_service,
            request.app.state.dispatcher,
            task_id,
            since=since,
            for_agent=identity.agent_id if identity.kind == "agent" else None,
        )

    @app.get("/api/tasks/{task_id}/objects/{object_id}", tags=["board"])
    async def get_object(
        request: Request, task_id: UUID, object_id: str, depth: int = Query(default=1, ge=0, le=1)
    ) -> dict[str, Any]:
        require_task_reader(request, task_id)
        await _task(request, task_id)
        return await _service(request).get_object(task_id, object_id, depth)

    @app.get("/api/tasks/{task_id}/search", response_model=list[SearchHit], tags=["board"])
    async def search(
        request: Request,
        task_id: UUID,
        q: str | None = None,
        k: int = Query(default=3, ge=1, le=100),
        type: Literal["fact", "intent", "all"] = "all",
    ) -> list[SearchHit]:
        require_task_reader(request, task_id)
        await _task(request, task_id)
        needle = q.casefold() if q else ""
        hits: list[tuple[int, SearchHit]] = []
        async with request.app.state.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            for kind, table in (("fact", s.facts), ("intent", s.intents)):
                if type not in {"all", kind}:
                    continue
                fields = ("statement",) if kind == "fact" else ("statement", "expected", "method")
                rows = (
                    await conn.execute(
                        select(table.c.id, table.c.version, *(table.c[field] for field in fields))
                        .where(table.c.task_id == task_id, table.c.challenge_id.is_(None))
                        .order_by(table.c.version)
                    )
                ).mappings()
                for item in rows:
                    haystack = " ".join(str(item.get(field) or "") for field in fields).casefold()
                    if needle in haystack:
                        hits.append(
                            (
                                int(item["version"]),
                                SearchHit(
                                    id=item["id"],
                                    type=cast(Literal["fact", "intent"], kind),
                                    statement=item["statement"],
                                ),
                            )
                        )
        return [hit for _, hit in sorted(hits, key=lambda row: row[0], reverse=True)[:k]]

    @app.get("/api/evidence", tags=["board"])
    async def evidence(request: Request, uri: str = Query(min_length=1)) -> StreamingResponse:
        task_id = _key_task(uri)
        if task_id is None:
            raise HTTPException(422, "Invalid evidence URI")
        require_task_reader(request, task_id)
        task = await _task(request, task_id)
        if uri.startswith("inputs/"):
            item = next(
                (item for item in task.get("initial_attachments", []) if item["uri"] == uri), None
            )
            if item is None or task["deleting"]:
                raise HTTPException(404, "Input not found")
            if not await request.app.state.objects.exists(uri):
                raise HTTPException(404, "Input not found")
            return StreamingResponse(
                request.app.state.objects.stream(uri),
                media_type="application/octet-stream",
                headers=download_headers(item["filename"]),
            )
        if not await request.app.state.objects.exists(uri):
            raise HTTPException(404, "Evidence not found")
        return StreamingResponse(request.app.state.objects.stream(uri))

    @app.post("/api/tasks/{task_id}/facts", tags=["agent"])
    async def post_fact(
        request: Request, task_id: UUID, body: PostFactRequest, dry_run: bool = False
    ) -> dict[str, Any]:
        identity = require_agent_writer(request, task_id)
        await _task(request, task_id)
        for item in body.evidence:
            if not item.uri or _key_task(item.uri) != task_id:
                raise HTTPException(403, "Evidence URI belongs to another task")
        return await _service(request).post_fact(
            task_id,
            identity.agent_id or "",
            body,
            dry_run=dry_run,
            expected_derive_round=identity.derive_round or 1,
        )

    @app.post("/api/tasks/{task_id}/intents", tags=["agent"])
    async def post_intent(
        request: Request,
        task_id: UUID,
        body: PostIntentRequest,
        dry_run: bool = False,
        claim: bool | None = None,
    ) -> dict[str, Any]:
        identity = require_agent_writer(request, task_id)
        await _task(request, task_id)
        data = body.model_dump()
        if claim is not None:
            data["claim"] = claim
        return await _service(request).post_intent(
            task_id,
            identity.agent_id or "",
            data,
            dry_run=dry_run,
            expected_derive_round=identity.derive_round or 1,
        )

    @app.post("/api/tasks/{task_id}/intents/{intent_id}/claim", tags=["agent"])
    async def claim(request: Request, task_id: UUID, intent_id: str) -> list[dict[str, Any]]:
        identity = require_agent_writer(request, task_id)
        await _task(request, task_id)
        return await _service(request).claim(
            task_id,
            identity.agent_id or "",
            intent_id,
            expected_derive_round=identity.derive_round or 1,
        )

    @app.post("/api/tasks/{task_id}/intents/{intent_id}/release", tags=["agent"])
    async def release(
        request: Request, task_id: UUID, intent_id: str, body: ReleaseBody
    ) -> list[dict[str, Any]]:
        identity = require_agent_writer(request, task_id)
        await _task(request, task_id)
        return await _service(request).release(
            task_id,
            identity.agent_id or "",
            intent_id,
            body.note,
            expected_derive_round=identity.derive_round or 1,
        )

    @app.post("/api/tasks/{task_id}/close", tags=["agent"])
    async def close(request: Request, task_id: UUID, body: CloseBody) -> list[dict[str, Any]]:
        identity = require_agent_writer(request, task_id)
        await _task(request, task_id)
        task = await _task(request, task_id)
        if body.report_uri and body.report_uri != _run_uri("reports", task_id, task["run_number"]):
            raise HTTPException(403, "Report URI belongs to another task")
        return await _service(request).submit_close(
            task_id,
            identity.agent_id or "",
            body.model_dump(exclude={"report_uri"}),
            report_uri=body.report_uri,
            expected_derive_round=identity.derive_round or 1,
        )

    @app.post("/api/tasks/{task_id}/status", tags=["system"])
    async def transition(request: Request, task_id: UUID, body: StatusBody) -> list[dict[str, Any]]:
        require_service(request)
        await _task(request, task_id)
        return await _service(request).transition(task_id, body.status, reason=body.reason)

    @app.post("/api/tasks/{task_id}/archive", response_model=list[Event], tags=["system"])
    async def record_archive(
        request: Request, task_id: UUID, body: ArchiveBody
    ) -> list[dict[str, Any]]:
        require_service(request)
        await _task(request, task_id)
        backend = (
            request.app.state.ctf_service
            if (await _task(request, task_id)).get("mode") == "ctf"
            else _service(request)
        )
        return await backend.record_archive(task_id, body.uri, body.size, body.fallback)

    @app.post("/api/tasks/{task_id}/cleanup-ready", response_model=list[Event], tags=["system"])
    async def record_cleanup(request: Request, task_id: UUID) -> list[dict[str, Any]]:
        require_service(request)
        await _task(request, task_id)
        backend = (
            request.app.state.ctf_service
            if (await _task(request, task_id)).get("mode") == "ctf"
            else _service(request)
        )
        return await backend.record_cleanup(task_id)

    @app.post("/api/tasks/{task_id}/agents", response_model=AgentRegistered, tags=["system"])
    async def register_agent(
        request: Request, task_id: UUID, body: AgentRegisterBody
    ) -> AgentRegistered:
        require_service(request)
        await _task(request, task_id)
        agent_id = await _service(request).register_agent(
            task_id,
            body.task_type,
            is_seed=body.is_seed,
            intent_id=body.intent_id,
            close_mode=body.close_mode,
            derive_parallel=body.derive_parallel,
            derive_review=body.derive_review,
        )
        agent = await _agent_record(request, task_id, agent_id)
        derive_round = int(agent.get("derive_round") or 1) if body.task_type == "derive" else None
        return AgentRegistered(
            agent_id=agent_id,
            token=issue_agent_token(settings, task_id, agent_id, derive_round),
            derive_round=derive_round,
            started_at=agent.get("started_at"),
        )

    @app.patch("/api/tasks/{task_id}/agents/{agent_id}", tags=["system"])
    async def heartbeat(
        request: Request,
        task_id: UUID,
        agent_id: str,
        body: HeartbeatBody | ReceiptHeartbeatRequest,
    ) -> dict[str, Any]:
        require_service(request)
        if isinstance(body, ReceiptHeartbeatRequest):
            body = HeartbeatBody.model_validate(body.receipt.model_dump())
        if body.request_id is not None:
            return await _service(request).heartbeat(task_id, agent_id, **body.model_dump())
        task = await _task(request, task_id)
        agent = await _agent_record(request, task_id, agent_id)
        profile = await _profiles(request).get(task["agent_profile"], task["agent_profile_version"])
        model = ModelConfig.model_validate(profile["models"][agent["task_type"]])
        return await _service(request).heartbeat(
            task_id,
            agent_id,
            steps=body.steps,
            context_tokens=body.context_tokens,
            usage=body.usage,
            last_seen_version=body.last_seen_version,
            price=model.price,
            model=model,
            requested_at=body.requested_at,
            billing_mode=task.get("billing_mode"),
            expected_started_at=body.expected_started_at,
            **(
                cast(dict[str, Any], {"expected_derive_round": body.expected_derive_round})
                if body.expected_derive_round is not None
                else {}
            ),
        )

    @app.post("/api/tasks/{task_id}/agents/{agent_id}/conclude", tags=["system"])
    async def conclude(
        request: Request, task_id: UUID, agent_id: str, body: ConcludeBody
    ) -> list[dict[str, Any]]:
        require_service(request)
        await _task(request, task_id)
        return await _service(request).conclude(
            task_id,
            agent_id,
            body.reason,
            **(
                {"expected_derive_round": body.expected_derive_round}
                if body.expected_derive_round is not None
                else {}
            ),
        )

    @app.post("/api/tasks/{task_id}/agents/{agent_id}/grace", tags=["system"])
    async def grace(
        request: Request, task_id: UUID, agent_id: str, body: GraceBody | None = None
    ) -> dict[str, int]:
        require_service(request)
        await _task(request, task_id)
        try:
            remaining = await _service(request).take_grace(
                task_id,
                agent_id,
                **(
                    {"expected_derive_round": body.expected_derive_round}
                    if body and body.expected_derive_round is not None
                    else {}
                ),
            )
        except RuleViolation as exc:
            if exc.code == "grace_exhausted":
                raise HTTPException(409, str(exc)) from exc
            raise
        return {"remaining": remaining}

    @app.post("/api/tasks/{task_id}/agents/{agent_id}/finish", tags=["system"])
    async def finish(
        request: Request, task_id: UUID, agent_id: str, body: FinishBody
    ) -> list[dict[str, Any]]:
        require_service(request)
        await _task(request, task_id)
        return await _service(request).finish_agent(
            task_id,
            agent_id,
            body.receipt,
            body.end_reason,
            **body.model_dump(exclude={"receipt", "end_reason"}, exclude_none=True),
        )

    @app.post("/api/tasks/{task_id}/intents/{intent_id}/system_close", tags=["system"])
    async def system_close(request: Request, task_id: UUID, intent_id: str) -> list[dict[str, Any]]:
        require_service(request)
        await _task(request, task_id)
        return await _service(request).system_close(task_id, intent_id)

    @app.post("/api/tasks/{task_id}/claim_for", tags=["system"])
    async def claim_for(
        request: Request, task_id: UUID, body: ClaimForBody
    ) -> list[dict[str, Any]]:
        require_service(request)
        await _task(request, task_id)
        return await _service(request).claim_for(task_id, body.intent_id, body.agent_id)

    @app.post("/api/tasks/{task_id}/tool_calls", tags=["system"])
    async def tool_call(
        request: Request, task_id: UUID, body: ToolCallBody
    ) -> list[dict[str, Any]]:
        require_service(request)
        await _task(request, task_id)
        data = body.model_dump(exclude={"agent_id", "expected_derive_round"})
        return await _service(request).record_tool_call(
            task_id,
            body.agent_id,
            data,
            **(
                {"expected_derive_round": body.expected_derive_round}
                if body.expected_derive_round is not None
                else {}
            ),
        )

    @app.post("/api/tasks/{task_id}/agents/{agent_id}/traces", tags=["system"])
    async def agent_trace(
        request: Request, task_id: UUID, agent_id: str, body: AgentTraceBody
    ) -> list[dict[str, Any]]:
        require_service(request)
        await _task(request, task_id)
        return await _service(request).record_agent_trace(
            task_id,
            agent_id,
            body.model_dump(exclude={"expected_derive_round"}),
            **(
                {"expected_derive_round": body.expected_derive_round}
                if body.expected_derive_round is not None
                else {}
            ),
        )

    @app.post("/api/tasks/{task_id}/uploads", response_model=UploadResult, tags=["system"])
    async def upload(
        request: Request, task_id: UUID, key: str = Query(min_length=1)
    ) -> UploadResult:
        require_service(request)
        await _task(request, task_id)
        limit = _upload_limit(task_id, key)
        size = 0
        with TemporaryFile() as content:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "Object exceeds upload limit")
                await anyio.to_thread.run_sync(content.write, chunk)
            await anyio.to_thread.run_sync(content.seek, 0)
            async with request.app.state.engine.begin() as conn:
                row = (
                    await conn.execute(
                        select(s.tasks.c.deleting).where(s.tasks.c.id == task_id).with_for_update()
                    )
                ).first()
                if row is None:
                    raise HTTPException(404, "Task not found")
                if row[0]:
                    raise HTTPException(409, "Task is being deleted")
                await request.app.state.objects.put(key, content, length=size)
        return UploadResult(key=key, size=size)

    @app.get("/api/profiles", response_model=list[ProfileName], tags=["profiles"])
    async def profiles(request: Request) -> list[dict[str, Any]]:
        require_user_or_service(request)
        return await _profiles(request).list()

    @app.get(
        "/api/profiles/{name}/versions", response_model=list[ProfileVersion], tags=["profiles"]
    )
    async def profile_versions(request: Request, name: str) -> list[dict[str, Any]]:
        require_user_or_service(request)
        return await _profiles(request).versions(name)

    @app.get(
        "/api/profiles/{name}/versions/{version}", response_model=ProfileDocument, tags=["profiles"]
    )
    async def profile_version(request: Request, name: str, version: int) -> ProfileDocument:
        require_user_or_service(request)
        return _profile_document(await _profiles(request).get(name, version))

    @app.post("/api/profiles/{name}/versions", response_model=ProfileDocument, tags=["profiles"])
    async def create_profile_version(
        request: Request, name: str, body: AgentProfile
    ) -> ProfileDocument:
        identity = require_user_or_service(request)
        if not name or "/" in name:
            raise HTTPException(422, "Invalid profile name")
        try:
            default = await _profiles(request).get("default")
            body = AgentProfile.model_validate(
                {
                    **body.model_dump(mode="json"),
                    "models": default["models"],
                }
            )
            from bbx_blackboard.mcp_catalog import validate_profile_tools

            await validate_profile_tools(request, body)
        except ValueError:
            raise HTTPException(422, "工具配置不一致") from None
        row = await _profiles(request).create(name, body, identity.name)
        return _profile_document(row)

    app.include_router(native_runner_bootstrap_router)
    app.include_router(ctf_router)
    app.include_router(challenge_board_router)
    app.include_router(runner_control_router)
    app.include_router(runner_management_router)
    app.include_router(runner_tool_router)
    app.include_router(runner_gateway_private_router)
    app.include_router(sandbox_tools_router)
    app.include_router(native_inputs_router)
    app.include_router(native_vpn_router)
    app.mount("/api/tasks/{task_id}/business-mcp-v2", app.state.direct_business_mcp)
    app.include_router(platform_router)
    app.include_router(mcp_catalog_router)
    app.include_router(settings_router)
    app.include_router(progress_router)
    web_dist = _web_dist()
    if web_dist.is_dir():
        app.mount("/", SPAStaticFiles(directory=web_dist, html=True), name="web")
    return app
