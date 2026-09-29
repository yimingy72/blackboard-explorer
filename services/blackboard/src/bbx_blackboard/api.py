"""FastAPI boundary for the blackboard service."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

import anyio
from bbx_contracts.models import (
    AgentProfile,
    Event,
    ModelConfig,
    PostFactRequest,
    PostIntentRequest,
    SubmitCloseRequest,
    TaskSpec,
    Usage,
)
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from bbx_blackboard.auth import (
    COOKIE_NAME,
    check_user_password,
    issue_agent_token,
    issue_user_token,
    require_agent_writer,
    require_service,
    require_task_reader,
    require_user_or_service,
)
from bbx_blackboard.billing import router as billing_router
from bbx_blackboard.conversations import Conversations
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.platform import PlatformStore
from bbx_blackboard.platform import router as platform_router
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.service import BoardService, ObjectStore
from bbx_blackboard.settings import Settings
from bbx_blackboard.store import schema as s
from bbx_blackboard.worker_settings import model_context_threshold, task_profile
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
    model_id: str | None = Field(default=None, min_length=1)
    model_version: int | None = Field(default=None, ge=1)
    profile_version: int | None = Field(default=None, ge=1)


class TaskCreated(BaseModel):
    id: UUID
    agent_profile: str
    agent_profile_version: int


class TaskView(BaseModel):
    cost_currency: str | None = None
    id: UUID
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


class ResumeBody(BaseModel):
    request_id: UUID
    additional_cost: Decimal = Field(ge=0)
    additional_minutes: int = Field(ge=0)
    refresh_tools: bool = False


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
    close_mode: Literal["judge", "final"] | None = None
    derive_parallel: bool | None = None
    derive_review: bool = False

    @model_validator(mode="after")
    def check_derive_mode(self) -> AgentRegisterBody:
        if self.derive_review and (self.task_type != "derive" or self.derive_parallel is True):
            raise ValueError("derive_review requires a nonparallel derive agent")
        return self


class AgentRegistered(BaseModel):
    agent_id: str
    token: str
    derive_round: int | None = None


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


class HeartbeatBody(BaseModel):
    requested_at: AwareDatetime | None = None
    expected_derive_round: int | None = Field(default=None, ge=1)
    steps: int = Field(ge=0)
    context_tokens: int = Field(ge=0)
    usage: Usage
    last_seen_version: int = Field(ge=0)


class ConcludeBody(BaseModel):
    reason: str = Field(min_length=1)
    expected_derive_round: int | None = Field(default=None, ge=1)


class GraceBody(BaseModel):
    expected_derive_round: int | None = Field(default=None, ge=1)


class FinishBody(BaseModel):
    receipt: dict[str, Any]
    end_reason: str = Field(min_length=1)
    expected_derive_round: int | None = Field(default=None, ge=1)


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


class SearchHit(BaseModel):
    id: str
    type: Literal["fact", "intent"]
    statement: str


class ProfileName(BaseModel):
    name: str
    latest_version: int


class ProfileVersion(BaseModel):
    name: str
    version: int
    created_by: str | None
    created_at: datetime


class ProfileDocument(ProfileVersion):
    profile: AgentProfile


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
        or (parts[0] == "toolcalls" and (len(parts) != 3 or not parts[2].endswith(".txt")))
        or (
            parts[0] == "traces" and (len(parts) != 4 or not re.fullmatch(r"[^/]+\.json", parts[3]))
        )
        or parts[0] not in {"evidence", "toolcalls", "traces"}
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
        profile=AgentProfile.model_validate({key: row[key] for key in AgentProfile.model_fields}),
    )


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
        app.state.board_service = BoardService(app.state.engine, app.state.objects)
        app.state.conversations = Conversations(app.state.engine)
        app.state.profile_store = ProfileStore(app.state.engine)
        app.state.platform_store = PlatformStore(
            app.state.engine, settings.agent_token_secret.get_secret_value()
        )
        await app.state.profile_store.ensure_bundled(
            settings.profiles_dir, app.state.platform_store
        )
        if own_dispatcher:
            from bbx_blackboard.sse import SSEDispatcher

            app.state.dispatcher = SSEDispatcher(settings)
        await app.state.dispatcher.start()
        try:
            yield
        finally:
            await app.state.workspace_cache.close()
            await app.state.dispatcher.stop()
            if own_engine:
                await app.state.engine.dispose()

    app = FastAPI(title="Blackboard API", version="1.0.0", lifespan=lifespan)
    app.include_router(billing_router)
    app.state.settings = settings
    app.state.engine = engine
    app.state.objects = objects
    app.state.dispatcher = dispatcher
    app.state.board_service = (
        BoardService(engine, objects) if engine is not None and objects is not None else None
    )
    app.state.conversations = Conversations(engine) if engine is not None else None
    app.state.profile_store = ProfileStore(engine) if engine is not None else None
    app.state.platform_store = (
        PlatformStore(engine, settings.agent_token_secret.get_secret_value())
        if engine is not None
        else None
    )
    app.state.workspace_cache = WorkspaceArchiveCache()

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
        from fastapi.responses import JSONResponse

        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing_error(_request: Request, _exc: KeyError) -> Response:
        from fastapi.responses import JSONResponse

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

    @app.post("/api/tasks", response_model=TaskCreated, tags=["tasks"])
    async def create_task(request: Request, body: TaskCreateBody) -> TaskCreated:
        require_user_or_service(request)
        profiles = _profiles(request)
        if body.model_version is not None and body.model_id is None:
            raise HTTPException(422, "模型版本需要同时指定模型")
        if body.model_id is not None or (
            body.agent_profile == "default" and body.profile_version is None
        ):
            row = await task_profile(
                profiles, request.app.state.platform_store, body.model_id, body.model_version
            )
        else:
            row = await profiles.get(body.agent_profile, body.profile_version)
        threshold = model_context_threshold(row["models"])
        if threshold is not None and row["params"].get("context_threshold") != threshold:
            content = {key: row[key] for key in AgentProfile.model_fields}
            content["params"] = {**content["params"], "context_threshold": threshold}
            row = await profiles.create(
                "task-settings", AgentProfile.model_validate(content), "task"
            )
        spec = body.model_dump(exclude={"profile_version", "model_id", "model_version"})
        spec["agent_profile"] = row["name"]
        spec["params"] = {**row["params"], **body.params}
        if threshold is not None:
            spec["params"]["context_threshold"] = threshold
        task_id = await _service(request).create_task(spec, profile_version=row["version"])
        return TaskCreated(
            id=task_id,
            agent_profile=row["name"],
            agent_profile_version=row["version"],
        )

    @app.get("/api/tasks/deletions", tags=["tasks"])
    async def pending_deletions(request: Request) -> list[UUID]:
        require_service(request)
        return await _conversations(request).deletions()

    @app.get("/api/tasks", response_model=list[TaskView], tags=["tasks"])
    async def list_tasks(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[TaskView]:
        require_user_or_service(request)
        async with request.app.state.engine.connect() as conn:
            ids = (
                (
                    await conn.execute(
                        select(s.tasks.c.id)
                        .order_by(s.tasks.c.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
        return [await task_detail(request, task_id) for task_id in ids]

    @app.get("/api/tasks/{task_id}", response_model=TaskView, tags=["tasks"])
    async def task_detail(request: Request, task_id: UUID) -> TaskView:
        require_task_reader(request, task_id)
        await _task(request, task_id)
        board = await _service(request).state(task_id)
        task = board["task"]
        profile = await request.app.state.profile_store.get(
            task["agent_profile"], task["agent_profile_version"]
        )
        currencies = {
            model.get("price", {}).get("currency") for model in profile["models"].values()
        }
        currency = currencies.pop() if len(currencies) == 1 else None
        return TaskView(
            cost_currency=currency,
            id=task_id,
            goal=task["goal"],
            status=task["status"],
            deleting=task["deleting"],
            acceptance_state=task["acceptance_state"],
            usage=task["usage"],
            agents=[
                agent
                for agent in board["agents"].values()
                if agent["status"] in {"running", "concluding"}
            ],
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
            runs=await _task_runs(request, task_id),
        )

    @app.post("/api/tasks/{task_id}/resume", response_model=TaskView, tags=["tasks"])
    async def resume_task(request: Request, task_id: UUID, body: ResumeBody) -> TaskView:
        identity = require_user_or_service(request)
        if identity.kind != "user":
            raise HTTPException(403, "User token required")
        await _task(request, task_id)
        await _service(request).resume(
            task_id,
            body.request_id,
            body.additional_cost,
            body.additional_minutes,
            body.refresh_tools,
            identity.name,
        )
        return await task_detail(request, task_id)

    @app.post("/api/tasks/{task_id}/start", response_model=StatusResult, tags=["tasks"])
    async def start_task(request: Request, task_id: UUID) -> StatusResult:
        require_user_or_service(request)
        task = await _task(request, task_id)
        if task["status"] != "created":
            raise HTTPException(409, "Task is not created")
        events = await _service(request).transition(task_id, "provisioning", actor="user")
        return StatusResult(status="provisioning", events=events)

    @app.post("/api/tasks/{task_id}/stop", response_model=StatusResult, tags=["tasks"])
    async def stop_task(request: Request, task_id: UUID) -> StatusResult:
        require_user_or_service(request)
        service = _service(request)
        task = await _task(request, task_id)
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
        result = await _conversations(request).purge(task_id, request.app.state.objects)
        await request.app.state.workspace_cache.invalidate(f"workspace/{task_id}.tar.zst")
        return result

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
        require_user_or_service(request)
        return await _conversations(request).list_messages(task_id, agent_id, status)

    @app.post("/api/tasks/{task_id}/agents/{agent_id}/messages", tags=["conversations"])
    async def post_agent_message(
        request: Request, task_id: UUID, agent_id: str, body: UserMessageBody
    ) -> dict[str, Any]:
        identity = require_user_or_service(request)
        if identity.kind != "user":
            raise HTTPException(403, "User token required")
        return await _conversations(request).post_message(task_id, agent_id, body.id, body.content)

    @app.get("/api/tasks/{task_id}/agents/{agent_id}/session", tags=["conversations"])
    async def get_agent_session(request: Request, task_id: UUID, agent_id: str) -> dict[str, Any]:
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
        require_task_reader(request, task_id)
        await _task(request, task_id)
        return await _service(request).state(task_id)

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

    @app.get("/api/tasks/{task_id}/events", response_model=list[Event], tags=["board"])
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

        return stream_response(
            _service(request),
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
        board = await _service(request).state(task_id)
        needle = q.casefold() if q else ""
        hits: list[tuple[int, SearchHit]] = []
        for kind, rows in (("fact", board["facts"]), ("intent", board["intents"])):
            if type not in {"all", kind}:
                continue
            for item in rows.values():
                haystack = " ".join(
                    str(item.get(field) or "")
                    for field in (
                        ("statement",) if kind == "fact" else ("statement", "expected", "method")
                    )
                ).casefold()
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
        await _task(request, task_id)
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
        return await _service(request).record_archive(task_id, body.uri, body.size, body.fallback)

    @app.post("/api/tasks/{task_id}/cleanup-ready", response_model=list[Event], tags=["system"])
    async def record_cleanup(request: Request, task_id: UUID) -> list[dict[str, Any]]:
        require_service(request)
        await _task(request, task_id)
        return await _service(request).record_cleanup(task_id)

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
            close_mode=body.close_mode,
            derive_parallel=body.derive_parallel,
            derive_review=body.derive_review,
        )
        agent = (await _service(request).state(task_id))["agents"][agent_id]
        derive_round = int(agent.get("derive_round") or 1) if body.task_type == "derive" else None
        return AgentRegistered(
            agent_id=agent_id,
            token=issue_agent_token(settings, task_id, agent_id, derive_round),
            derive_round=derive_round,
        )

    @app.patch("/api/tasks/{task_id}/agents/{agent_id}", tags=["system"])
    async def heartbeat(
        request: Request, task_id: UUID, agent_id: str, body: HeartbeatBody
    ) -> dict[str, Any]:
        require_service(request)
        board = await _service(request).state(task_id)
        agent = board["agents"].get(agent_id)
        if agent is None:
            raise HTTPException(404, "Agent not found")
        task = board["task"]
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
            **(
                {"expected_derive_round": body.expected_derive_round}
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
            **(
                {"expected_derive_round": body.expected_derive_round}
                if body.expected_derive_round is not None
                else {}
            ),
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
            body = await request.app.state.platform_store.normalize_profile(body)
        except ValueError:
            raise HTTPException(422, "平台模型引用规范化后币种或工具配置不一致") from None
        row = await _profiles(request).create(name, body, identity.name)
        return _profile_document(row)

    app.include_router(platform_router)
    app.include_router(settings_router)
    web_dist = _web_dist()
    if web_dist.is_dir():
        app.mount("/", SPAStaticFiles(directory=web_dist, html=True), name="web")
    return app
