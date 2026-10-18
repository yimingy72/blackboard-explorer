"""Read-only, task-authorized access to bounded traffic archive objects."""

from __future__ import annotations

import asyncio
import gzip
import json
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterable
from io import BytesIO
from typing import Annotated, Any, Literal
from uuid import UUID

from bbx_contracts.traffic import (
    TRAFFIC_PACKET_BATCH_LIMIT,
    TrafficCapture,
    TrafficChunk,
    TrafficListResponse,
    TrafficManifest,
    TrafficRecordDetail,
    TrafficRecordSummary,
)
from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter

from bbx_blackboard.auth import require_user_or_service
from bbx_blackboard.input_groups import download_headers

router = APIRouter(prefix="/api/tasks/{task_id}/traffic", tags=["traffic"])
_MAX_JSON = 8 * 1024 * 1024
_RECORDS = TypeAdapter(list[TrafficRecordSummary])
_DETAILS = TypeAdapter(list[TrafficRecordDetail])
Run = Annotated[int | None, Query(ge=1)]
RecordId = Annotated[str, Path(pattern=r"^r-[1-9][0-9]{0,18}$")]
FileId = Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]


def _unavailable() -> HTTPException:
    return HTTPException(503, "Traffic archive is unavailable or incomplete; retry later")


async def _json(
    objects: Any, key: str, *, compressed: bool = False, max_bytes: int = _MAX_JSON
) -> Any:
    """Bound compressed input and decompressed output independently."""
    try:
        data = bytearray()
        source = objects.stream(key)
        try:
            async for chunk in source:
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("Traffic object exceeds its read limit")
        finally:
            await source.aclose()
        if compressed:
            with gzip.GzipFile(fileobj=BytesIO(data)) as reader:
                decoded = reader.read(max_bytes + 1)
            if len(decoded) > max_bytes:
                raise ValueError("Traffic object exceeds its decoded read limit")
        else:
            decoded = bytes(data)
        return json.loads(decoded)
    except Exception as exc:
        raise _unavailable() from exc


async def _manifest(
    request: Request, task_id: UUID, run: int | None
) -> tuple[Any, str, int, TrafficManifest | None]:
    require_user_or_service(request)
    # Local import keeps the shared task lookup and its test seam without an API import cycle.
    from bbx_blackboard.api import _task

    task = await _task(request, task_id)
    current = task.get("run_number", 1)
    number = run if run is not None else current
    if number > current:
        raise HTTPException(404, "Task run not found")
    root = f"traffic/{task_id}/run-{number}/"
    objects = request.app.state.objects
    if objects is None:
        raise _unavailable()
    try:
        if not await objects.exists(root + "manifest.json"):
            return objects, root, number, None
        manifest = TrafficManifest.model_validate(await _json(objects, root + "manifest.json"))
        if manifest.task_id != task_id or manifest.run != number:
            raise ValueError("Traffic manifest does not belong to the requested task run")
    except HTTPException:
        raise
    except Exception as exc:
        raise _unavailable() from exc
    return objects, root, number, manifest


async def _chunk(objects: Any, root: str, descriptor: TrafficChunk) -> list[TrafficRecordSummary]:
    try:
        records = _RECORDS.validate_python(
            await _json(
                objects,
                root + f"chunks/{descriptor.id}.json.gz",
                compressed=True,
                max_bytes=TRAFFIC_PACKET_BATCH_LIMIT
                if descriptor.detail_storage == "chunk"
                else _MAX_JSON,
            )
        )
        sequences = [record.seq for record in records]
        if (
            len(records) != descriptor.count
            or not sequences
            or sequences != sorted(set(sequences))
            or sequences[0] != descriptor.first_seq
            or sequences[-1] != descriptor.last_seq
        ):
            raise ValueError("Traffic chunk does not match its published index")
        if descriptor.detail_storage != "chunk" and any(
            record.record_kind == "packet" for record in records
        ):
            raise ValueError("Packet details must use bounded chunk storage")
        return records
    except HTTPException:
        raise
    except Exception as exc:
        raise _unavailable() from exc


async def _filtered_chunks(
    request: Request,
    objects: Any,
    root: str,
    descriptors: Iterable[TrafficChunk],
    agent_id: str | None,
) -> AsyncIterator[list[TrafficRecordSummary]]:
    # Immutable chunk identities allow a small owner-only index for older archives.
    if not hasattr(request.app.state, "traffic_chunk_owners"):
        request.app.state.traffic_chunk_owners = OrderedDict()
    cache: OrderedDict = request.app.state.traffic_chunk_owners
    pending = []

    def key(item: TrafficChunk) -> tuple:
        return root, item.id, item.first_seq, item.last_seq, item.count

    async def read(item: TrafficChunk) -> list[TrafficRecordSummary]:
        records = await _chunk(objects, root, item)
        owners = frozenset(record.agent_id for record in records)
        if len(owners) <= 16:
            cache[key(item)] = owners
            cache.move_to_end(key(item))
            while len(cache) > 8192:
                cache.popitem(last=False)
        return records

    async def batch() -> list:
        return await asyncio.gather(*(read(item) for item in pending), return_exceptions=True)

    for descriptor in descriptors:
        owners = (
            descriptor.agent_ids if descriptor.agent_ids is not None else cache.get(key(descriptor))
        )
        if agent_id is not None and owners is not None and agent_id not in owners:
            continue
        pending.append(descriptor)
        if len(pending) == (8 if agent_id is not None else 1):
            for result in await batch():
                if isinstance(result, BaseException):
                    raise result
                yield result
            pending.clear()
    for result in await batch():
        if isinstance(result, BaseException):
            raise result
        yield result


@router.get("", response_model=TrafficListResponse)
async def list_traffic(
    request: Request,
    response: Response,
    task_id: UUID,
    run: Run = None,
    agent_id: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
    protocol: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    q: Annotated[str | None, Query(max_length=300)] = None,
    kind: Annotated[Literal["packet", "http", "logical"] | None, Query()] = None,
    direction: Annotated[
        Literal["inbound", "outbound", "loopback", "unknown"] | None, Query()
    ] = None,
    before: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    after: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> TrafficListResponse:
    response.headers["Cache-Control"] = "no-store"
    if before is not None and after is not None:
        raise HTTPException(422, "Traffic before and after cursors are mutually exclusive")
    objects, root, number, manifest = await _manifest(request, task_id, run)
    if manifest is None:
        return TrafficListResponse(
            task_id=task_id, run=number, availability="not_recorded", next_after=after
        )
    snapshot_seq = manifest.chunks[-1].last_seq if manifest.chunks else 0
    matched = []
    search = (q or "").casefold()
    descriptors = (
        descriptor
        for descriptor in (manifest.chunks if after is not None else reversed(manifest.chunks))
        if (before is None or descriptor.first_seq < before)
        and (after is None or descriptor.last_seq > after)
    )
    async for chunk in _filtered_chunks(request, objects, root, descriptors, agent_id):
        for record in chunk if after is not None else reversed(chunk):
            if before is not None and record.seq >= before:
                continue
            if after is not None and record.seq <= after:
                continue
            if kind is not None and record.record_kind != kind:
                continue
            if direction is not None and (
                record.packet is None or record.packet.direction != direction
            ):
                continue
            if agent_id is not None and record.agent_id != agent_id:
                continue
            if protocol is not None and not any(
                value is not None and value.casefold() == protocol.casefold()
                for value in (
                    record.protocol,
                    record.transport_protocol,
                    record.application_protocol,
                )
            ):
                continue
            if (
                search
                and search
                not in " ".join(
                    str(value or "")
                    for value in (
                        record.summary,
                        record.url,
                        record.method,
                        record.source_ip,
                        record.source_port,
                        record.destination_ip,
                        record.destination_port,
                        record.status_code,
                        record.protocol,
                        record.transport_protocol,
                        record.application_protocol,
                        record.packet.info if record.packet else None,
                        record.packet.interface if record.packet else None,
                        record.packet.source_id if record.packet else None,
                    )
                ).casefold()
            ):
                continue
            matched.append(record)
            if len(matched) > limit:
                break
        if len(matched) > limit:
            break
    has_more = len(matched) > limit
    records = matched[:limit]
    capture = TrafficCapture.model_validate(
        manifest.model_dump(include=set(TrafficCapture.model_fields))
    )
    return TrafficListResponse(
        task_id=task_id,
        run=number,
        availability="recorded",
        capture=capture,
        records=records,
        next_before=records[-1].seq if has_more and after is None else None,
        snapshot_seq=snapshot_seq,
        next_after=(records[-1].seq if has_more else max(after, snapshot_seq))
        if after is not None
        else None,
        has_more=has_more,
        files=[item for item in manifest.files if agent_id is None or item.agent_id == agent_id],
    )


@router.get("/records/{record_id}", response_model=TrafficRecordDetail)
async def traffic_record(
    request: Request, response: Response, task_id: UUID, record_id: RecordId, run: Run = None
) -> TrafficRecordDetail:
    response.headers["Cache-Control"] = "no-store"
    objects, root, _, manifest = await _manifest(request, task_id, run)
    sequence = int(record_id[2:])
    if manifest is None:
        raise HTTPException(404, "Traffic record not found")
    descriptor = next(
        (chunk for chunk in manifest.chunks if chunk.first_seq <= sequence <= chunk.last_seq), None
    )
    if descriptor is None:
        raise HTTPException(404, "Traffic record not found")
    summaries = await _chunk(objects, root, descriptor)
    summary = next((record for record in summaries if record.id == record_id), None)
    if summary is None:
        raise HTTPException(404, "Traffic record not found")
    try:
        if descriptor.detail_storage == "chunk":
            details = _DETAILS.validate_python(
                await _json(
                    objects,
                    root + f"detail-chunks/{descriptor.id}.json.gz",
                    compressed=True,
                    max_bytes=TRAFFIC_PACKET_BATCH_LIMIT,
                )
            )
            if len(details) != len(summaries) or any(
                detail.model_dump(include=set(TrafficRecordSummary.model_fields))
                != published.model_dump()
                for detail, published in zip(details, summaries, strict=True)
            ):
                raise ValueError("Traffic detail bundle does not match its published summaries")
            detail = next(detail for detail in details if detail.id == record_id)
        else:
            detail = TrafficRecordDetail.model_validate(
                await _json(objects, root + f"records/{record_id}.json.gz", compressed=True)
            )
        if (
            detail.model_dump(include=set(TrafficRecordSummary.model_fields))
            != summary.model_dump()
        ):
            raise ValueError("Traffic detail does not match its published summary")
    except HTTPException:
        raise
    except Exception as exc:
        raise _unavailable() from exc
    return detail


@router.get("/files/{file_id}", response_class=StreamingResponse)
async def traffic_file(
    request: Request, task_id: UUID, file_id: FileId, run: Run = None
) -> StreamingResponse:
    objects, root, _, manifest = await _manifest(request, task_id, run)
    item = next((item for item in manifest.files if item.id == file_id), None) if manifest else None
    if item is None:
        raise HTTPException(404, "Traffic file not found")
    key = root + f"pcap/{item.id}"
    source = None
    try:
        if not await objects.exists(key):
            raise _unavailable()
        source = objects.stream(key)
        first = await anext(source, b"")
        if not first and item.size_bytes:
            raise _unavailable()
    except Exception as exc:
        if source is not None:
            await source.aclose()
        raise _unavailable() from exc

    async def content():
        size = 0
        try:
            size += len(first)
            if size > item.size_bytes:
                raise RuntimeError("Traffic file does not match its published size")
            yield first
            async for chunk in source:
                size += len(chunk)
                if size > item.size_bytes:
                    raise RuntimeError("Traffic file does not match its published size")
                yield chunk
            if size != item.size_bytes:
                raise RuntimeError("Traffic file is incomplete")
        finally:
            await source.aclose()

    filename = re.sub(r"[\x00-\x1f/\\]", "_", item.name)
    headers = download_headers(filename)
    headers["Cache-Control"] = "no-store"
    headers["Content-Length"] = str(item.size_bytes)
    return StreamingResponse(content(), media_type=item.content_type, headers=headers)
