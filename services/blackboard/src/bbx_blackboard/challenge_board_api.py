"""HTTP adapter for the shared, identity-bound challenge knowledge handlers."""

from uuid import UUID

from bbx_contracts.challenge_board import (
    ChallengeFactRequest,
    ChallengeIntentRequest,
    ChallengeKnowledge,
    ChallengeKnowledgeObject,
    ChallengeKnowledgeWrite,
)
from bbx_contracts.models import SearchHit
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from bbx_blackboard.auth import require_agent_writer, require_task_reader
from bbx_blackboard.challenge_board import ChallengeBoardService
from bbx_blackboard.domain.snapshot import snapshot as make_snapshot

router = APIRouter(
    prefix="/api/tasks/{task_id}/ctf/knowledge/{challenge_id}", tags=["challenge-knowledge"]
)


def service(request: Request) -> ChallengeBoardService:
    backend = request.app.state.ctf_service
    if backend is None:
        raise HTTPException(503, "Challenge knowledge service is unavailable")
    return ChallengeBoardService(backend)


@router.get("", response_model=ChallengeKnowledge)
async def board(request: Request, task_id: UUID, challenge_id: UUID):
    identity = require_task_reader(request, task_id)
    return await service(request).board(task_id, challenge_id, identity)


@router.post("/facts", response_model=ChallengeKnowledgeWrite)
async def post_fact(
    request: Request, task_id: UUID, challenge_id: UUID, body: ChallengeFactRequest
):
    identity = require_agent_writer(request, task_id)
    return await service(request).write(task_id, challenge_id, identity, body, kind="fact")


@router.post("/intents", response_model=ChallengeKnowledgeWrite)
async def post_intent(
    request: Request, task_id: UUID, challenge_id: UUID, body: ChallengeIntentRequest
):
    identity = require_agent_writer(request, task_id)
    return await service(request).write(task_id, challenge_id, identity, body, kind="intent")


@router.get("/objects/{object_id}", response_model=ChallengeKnowledgeObject)
async def get_object(
    request: Request,
    task_id: UUID,
    challenge_id: UUID,
    object_id: str,
    depth: int = Query(default=0, ge=0, le=1),
):
    identity = require_task_reader(request, task_id)
    return await service(request).get_object(task_id, challenge_id, identity, object_id, depth)


@router.get("/search", response_model=list[SearchHit])
async def search(
    request: Request,
    task_id: UUID,
    challenge_id: UUID,
    q: str = "",
    k: int = Query(default=20, ge=1, le=100),
):
    identity = require_task_reader(request, task_id)
    return await service(request).search(task_id, challenge_id, identity, q, k)


@router.get("/snapshot", response_class=PlainTextResponse)
async def snapshot(
    request: Request,
    task_id: UUID,
    challenge_id: UUID,
    max_lines: int = Query(default=150, ge=1, le=1000),
):
    identity = require_task_reader(request, task_id)
    data = await service(request).board(task_id, challenge_id, identity)
    return PlainTextResponse(make_snapshot(data, max_lines), media_type="application/yaml")
