"""Task-authorized native display filters and immutable analysis revisions."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from bbx_contracts.traffic import (
    TrafficId,
    TrafficNativeFrameDetail,
    TrafficNativePacketPage,
    TrafficNativeQueryRequest,
    TrafficNativeQueryView,
)
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from bbx_blackboard.auth import require_user_or_service
from bbx_blackboard.native import NativeAnalysisCache, NativeFilterError, NativeUnavailable
from bbx_blackboard.traffic import _manifest

router = APIRouter(prefix="/api/tasks/{task_id}/traffic/native", tags=["traffic"])


class DisplayFilterRequest(BaseModel):
    display_filter: str = Field(default="", max_length=4096)


def cache(request: Request) -> NativeAnalysisCache:
    return request.app.state.native_cache


def unavailable(error: Exception) -> HTTPException:
    if isinstance(error, NativeFilterError):
        return HTTPException(422, str(error))
    return HTTPException(503, "Native analysis is unavailable or a resource limit was reached")


@router.post("/check")
async def check_filter(
    request: Request, response: Response, task_id: UUID, body: DisplayFilterRequest
) -> dict[str, str | bool]:
    response.headers["Cache-Control"] = "no-store"
    await _manifest(request, task_id, None)
    try:
        version = await cache(request).check(body.display_filter)
        return {"valid": True, "engine_version": version}
    except (NativeUnavailable, NativeFilterError) as error:
        raise unavailable(error) from error


@router.post("/queries", response_model=TrafficNativeQueryView, status_code=202)
async def create_query(
    request: Request, response: Response, task_id: UUID, body: TrafficNativeQueryRequest
) -> TrafficNativeQueryView:
    response.headers["Cache-Control"] = "no-store"
    objects, _, run, manifest = await _manifest(request, task_id, body.run)
    try:
        await cache(request).check(body.display_filter)
        query = cache(request).create(task_id, run, body.display_filter, body.agent_id, objects)
        await cache(request).refresh(query, manifest.native if manifest else [])
        return query.view()
    except (NativeUnavailable, NativeFilterError) as error:
        raise unavailable(error) from error


@router.get("/queries/{query_id}", response_model=TrafficNativeQueryView)
async def query_status(
    request: Request,
    response: Response,
    task_id: UUID,
    query_id: UUID,
    seen_revision: Annotated[str | None, Query(max_length=64)] = None,
) -> TrafficNativeQueryView:
    response.headers["Cache-Control"] = "no-store"
    require_user_or_service(request)
    query = cache(request).query(query_id, task_id)
    _, _, _, manifest = await _manifest(request, task_id, query.run)
    try:
        await cache(request).refresh(query, manifest.native if manifest else [])
        return query.view(seen_revision)
    except NativeUnavailable as error:
        raise unavailable(error) from error


@router.get("/queries/{query_id}/packets", response_model=TrafficNativePacketPage)
async def query_packets(
    request: Request,
    response: Response,
    task_id: UUID,
    query_id: UUID,
    revision: Annotated[str, Query(min_length=64, max_length=64)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
) -> TrafficNativePacketPage:
    response.headers["Cache-Control"] = "no-store"
    require_user_or_service(request)
    query = cache(request).query(query_id, task_id)
    await _manifest(request, task_id, query.run)
    if query.revision is None or revision != query.revision:
        raise HTTPException(409, "Native analysis revision changed; refresh the query")
    records = query.records[offset : offset + limit]
    return TrafficNativePacketPage(
        query_id=query_id,
        revision=revision,
        records=records,
        total=len(query.records),
        next_offset=offset + len(records) if offset + len(records) < len(query.records) else None,
    )


@router.get(
    "/queries/{query_id}/sources/{source_id}/frames/{number}",
    response_model=TrafficNativeFrameDetail,
)
async def query_frame(
    request: Request,
    response: Response,
    task_id: UUID,
    query_id: UUID,
    source_id: TrafficId,
    number: int,
    revision: Annotated[str, Query(min_length=64, max_length=64)],
) -> TrafficNativeFrameDetail:
    response.headers["Cache-Control"] = "no-store"
    require_user_or_service(request)
    query = cache(request).query(query_id, task_id)
    await _manifest(request, task_id, query.run)
    if query.revision is None or revision != query.revision:
        raise HTTPException(409, "Native analysis revision changed; refresh the query")
    try:
        return await cache(request).frame(query, source_id, number, revision)
    except NativeUnavailable as error:
        raise unavailable(error) from error
