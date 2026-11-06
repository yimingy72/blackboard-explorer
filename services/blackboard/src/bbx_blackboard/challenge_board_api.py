"""HTTP adapter for the shared, identity-bound challenge knowledge handlers."""

from uuid import UUID

from bbx_contracts.challenge_board import (
    Announcement,
    AnnouncementReadRequest,
    AnnouncementRequest,
    BoardChallengeRequest,
    BoardFinishRequest,
    BoardPageRequest,
    ChallengeFactRequest,
    ChallengeIntentRequest,
    ChallengeKnowledge,
    ChallengeKnowledgeObject,
    ChallengeKnowledgeWrite,
    FlagBoard,
    FlagCandidate,
    FlagCandidateRequest,
    FlagCaptureRequest,
    FlagDispatchRequest,
    FlagReceiptRequest,
    FlagSubmission,
    FlagSubmissionRequest,
    FlagUnknownRequest,
    FlagValueDetail,
)
from bbx_contracts.models import SearchHit
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from bbx_blackboard.auth import require_agent_writer, require_service, require_task_reader
from bbx_blackboard.challenge_board import ChallengeBoardService
from bbx_blackboard.domain.snapshot import snapshot as make_snapshot
from bbx_blackboard.flag_board import FlagBoardService
from bbx_blackboard.task_board import TaskBoardService

knowledge_router = APIRouter(prefix="/{challenge_id}", tags=["challenge-knowledge"])

router = APIRouter(prefix="/api/tasks/{task_id}/ctf/knowledge", tags=["challenge-board"])


def service(request: Request) -> ChallengeBoardService:
    backend = request.app.state.ctf_service
    if backend is None:
        raise HTTPException(503, "Challenge knowledge service is unavailable")
    return ChallengeBoardService(backend)


def tasks(request):
    return TaskBoardService(service(request).ctf)


def flags(request):
    return FlagBoardService(
        service(request).ctf, request.app.state.settings.agent_token_secret.get_secret_value()
    )


@router.get("/tools")
async def tools(request: Request, task_id: UUID):
    return await tasks(request).manifest(task_id, require_task_reader(request, task_id))


@router.get("/announcements", response_model=list[Announcement])
async def announcements(
    request: Request,
    task_id: UUID,
    after_revision: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
):
    return await tasks(request).announcements(
        task_id, require_task_reader(request, task_id), after_revision, limit
    )


@router.post("/announcements", response_model=Announcement)
async def publish_announcement(request: Request, task_id: UUID, body: AnnouncementRequest):
    return await tasks(request).publish_announcement(
        task_id, require_agent_writer(request, task_id), body
    )


@router.post("/challenges")
async def create_challenge(request: Request, task_id: UUID, body: BoardChallengeRequest):
    return await tasks(request).create_challenge(
        task_id, require_agent_writer(request, task_id), body
    )


@router.get("/challenges")
async def challenges(
    request: Request,
    task_id: UUID,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return await tasks(request).challenges(
        task_id, require_task_reader(request, task_id), limit, offset
    )


@router.post("/submissions/{submission_id}/dispatch")
async def dispatch(
    request: Request,
    response: Response,
    task_id: UUID,
    submission_id: UUID,
    body: FlagDispatchRequest,
):
    identity = require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await flags(request).dispatch(task_id, submission_id, identity, body)


@router.post("/platform-results")
async def capture_platform_result(request: Request, task_id: UUID, body: FlagCaptureRequest):
    return await flags(request).capture(task_id, require_service(request), body)


@router.post("/submissions/{submission_id}/unknown", response_model=FlagSubmission)
async def unknown(request: Request, task_id: UUID, submission_id: UUID, body: FlagUnknownRequest):
    return await flags(request).unknown(task_id, submission_id, require_service(request), body)


@router.post("/submissions/{submission_id}/receipt", response_model=FlagSubmission)
async def receipt(request: Request, task_id: UUID, submission_id: UUID, body: FlagReceiptRequest):
    return await flags(request).receipt(task_id, submission_id, require_service(request), body)


@knowledge_router.get("/flags", response_model=FlagBoard)
async def flag_board(request: Request, task_id: UUID, challenge_id: UUID):
    return await flags(request).board(task_id, challenge_id, require_task_reader(request, task_id))


@knowledge_router.post("/flags/candidates", response_model=FlagCandidate)
async def record_flag(
    request: Request, task_id: UUID, challenge_id: UUID, body: FlagCandidateRequest
):
    return await flags(request).candidate(
        task_id, challenge_id, require_agent_writer(request, task_id), body
    )


@knowledge_router.get("/flags/candidates/{candidate_id}/value", response_model=FlagValueDetail)
async def flag_value(
    request: Request, response: Response, task_id: UUID, challenge_id: UUID, candidate_id: UUID
):
    response.headers["Cache-Control"] = "no-store"
    return await flags(request).value(
        task_id, challenge_id, candidate_id, require_task_reader(request, task_id)
    )


@knowledge_router.post("/flags/submissions", response_model=FlagSubmission)
async def submit_flag(
    request: Request, task_id: UUID, challenge_id: UUID, body: FlagSubmissionRequest
):
    return await flags(request).prepare(
        task_id, challenge_id, require_agent_writer(request, task_id), body
    )


@router.post("/tools/{operation}")
async def tool(request: Request, response: Response, task_id: UUID, operation: str, body: dict):
    identity = require_agent_writer(request, task_id)
    manifest = await tasks(request).manifest(task_id, identity)
    if not any(operation in names for names in manifest["roles"].values()):
        raise HTTPException(403, "Tool is not published for this role")
    response.headers["Cache-Control"] = "no-store"
    arguments = dict(body)
    try:
        if operation == "board_announcements":
            parsed = AnnouncementReadRequest.model_validate(arguments)
            return await tasks(request).announcements(
                task_id, identity, parsed.after_revision, parsed.limit
            )
        if operation == "board_challenges":
            parsed = BoardPageRequest.model_validate(arguments)
            return await tasks(request).challenges(task_id, identity, parsed.limit, parsed.offset)
        if operation == "board_publish_announcement":
            return await tasks(request).publish_announcement(task_id, identity, arguments)
        if operation == "board_create_challenge":
            return await tasks(request).create_challenge(task_id, identity, arguments)
        if operation == "board_finish_task":
            parsed = BoardFinishRequest.model_validate(arguments)
            return await tasks(request).finish(
                task_id, identity, parsed.request_id, parsed.conclusion
            )
        cid = UUID(arguments.pop("challenge_id"))
        if operation in {"board_read", "board_search", "board_flags", "board_flag_value"}:
            if operation == "board_read" and not arguments:
                return await service(request).board(task_id, cid, identity)
            if operation == "board_flags" and not arguments:
                return await flags(request).board(task_id, cid, identity)
            if operation == "board_flag_value" and set(arguments) == {"candidate_id"}:
                return await flags(request).value(
                    task_id, cid, UUID(arguments["candidate_id"]), identity
                )
            if operation == "board_search" and set(arguments) <= {"q", "k"}:
                limit = arguments.get("k", 20)
                if (
                    type(limit) is not int
                    or not 1 <= limit <= 100
                    or not isinstance(arguments.get("q", ""), str)
                ):
                    raise ValueError("Invalid search")
                return await service(request).search(
                    task_id, cid, identity, arguments.get("q", ""), limit
                )
            raise ValueError("Unexpected arguments")
        if operation in {"board_post_fact", "board_post_intent"}:
            return await service(request).write(
                task_id,
                cid,
                identity,
                arguments,
                kind="fact" if operation == "board_post_fact" else "intent",
            )
        if operation == "board_record_flag":
            return await flags(request).candidate(task_id, cid, identity, arguments)
        if operation == "board_submit_flag":
            return await flags(request).prepare(task_id, cid, identity, arguments)
    except ValidationError as error:
        raise HTTPException(
            422, {"issues": error.errors(include_input=False, include_context=False)}
        ) from error
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "Invalid business tool arguments") from error
    raise HTTPException(404, "Unknown business tool")


@knowledge_router.get("", response_model=ChallengeKnowledge)
async def board(request: Request, task_id: UUID, challenge_id: UUID):
    identity = require_task_reader(request, task_id)
    return await service(request).board(task_id, challenge_id, identity)


@knowledge_router.post("/facts", response_model=ChallengeKnowledgeWrite)
async def post_fact(
    request: Request, task_id: UUID, challenge_id: UUID, body: ChallengeFactRequest
):
    identity = require_agent_writer(request, task_id)
    return await service(request).write(task_id, challenge_id, identity, body, kind="fact")


@knowledge_router.post("/intents", response_model=ChallengeKnowledgeWrite)
async def post_intent(
    request: Request, task_id: UUID, challenge_id: UUID, body: ChallengeIntentRequest
):
    identity = require_agent_writer(request, task_id)
    return await service(request).write(task_id, challenge_id, identity, body, kind="intent")


@knowledge_router.get("/objects/{object_id}", response_model=ChallengeKnowledgeObject)
async def get_object(
    request: Request,
    task_id: UUID,
    challenge_id: UUID,
    object_id: str,
    depth: int = Query(default=0, ge=0, le=1),
):
    identity = require_task_reader(request, task_id)
    return await service(request).get_object(task_id, challenge_id, identity, object_id, depth)


@knowledge_router.get("/search", response_model=list[SearchHit])
async def search(
    request: Request,
    task_id: UUID,
    challenge_id: UUID,
    q: str = "",
    k: int = Query(default=20, ge=1, le=100),
):
    identity = require_task_reader(request, task_id)
    return await service(request).search(task_id, challenge_id, identity, q, k)


@knowledge_router.get("/snapshot", response_class=PlainTextResponse)
async def snapshot(
    request: Request,
    task_id: UUID,
    challenge_id: UUID,
    max_lines: int = Query(default=150, ge=1, le=1000),
):
    identity = require_task_reader(request, task_id)
    data = await service(request).board(task_id, challenge_id, identity)
    return PlainTextResponse(make_snapshot(data, max_lines), media_type="application/yaml")


router.include_router(knowledge_router)
