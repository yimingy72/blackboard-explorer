"""Owner-scoped staging over final task object keys, without object copies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from tempfile import TemporaryFile
from typing import Any, BinaryIO
from urllib.parse import quote
from uuid import UUID, uuid4

import anyio
from bbx_contracts.models import InitialAttachment
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from bbx_blackboard.auth import Principal, principal, require_user_or_service
from bbx_blackboard.store import schema as s

FILE_LIMIT = 50 * 1024 * 1024
TOTAL_LIMIT = 200 * 1024 * 1024
FILE_COUNT = 20
UPLOAD_SECONDS = 300
CLEANUP_SECONDS = 300
CLEANUP_BATCH = 100
logger = logging.getLogger(__name__)


def owner_key(identity: Principal) -> str:
    return f"{identity.kind}:{identity.name}"


def request_hash(body: dict[str, Any]) -> str:
    def scalar(value: Any) -> str:
        if isinstance(value, Decimal):
            text = format(value, "f")
            return text.rstrip("0").rstrip(".") if "." in text else text
        return str(value)

    encoded = json.dumps(
        body,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=scalar,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def check_filename(filename: str) -> None:
    try:
        size = len(filename.encode("utf-8"))
    except UnicodeError:
        raise HTTPException(422, "文件名须为有效 UTF-8 文本") from None
    if (
        filename in {"", ".", ".."}
        or "/" in filename
        or "\\" in filename
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in filename)
        or size > 255
    ):
        raise HTTPException(422, "文件名须为单个有效文件名，不能包含目录或控制字符")


def check_owner(row: dict[str, Any], owner: str) -> None:
    if row["owner"] != owner:
        raise HTTPException(403, "附件组访问被拒绝")


def check_mutable(row: dict[str, Any], owner: str) -> None:
    check_owner(row, owner)
    if row["bound_task_id"] is not None:
        raise HTTPException(409, "附件组已绑定任务，不能修改")
    if row["deleting"] or row["expires_at"] <= datetime.now(UTC):
        raise HTTPException(410, "附件组已取消或过期，请重新上传")


class InputGroupView(BaseModel):
    id: UUID
    expires_at: datetime
    files: list[InitialAttachment]


class InputGroups:
    def __init__(self, engine: AsyncEngine, objects: Any) -> None:
        self.engine = engine
        self.objects = objects

    async def create(self, owner: str) -> dict[str, Any]:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                insert(s.task_input_groups)
                .values(id=uuid4(), owner=owner, expires_at=datetime.now(UTC) + timedelta(hours=24))
                .returning(s.task_input_groups)
            )
            return dict(result.mappings().one())

    async def get(self, group_id: UUID) -> dict[str, Any]:
        async with self.engine.connect() as conn:
            return await self.row(conn, group_id)

    @staticmethod
    async def row(conn: AsyncConnection, group_id: UUID, *, lock: bool = False) -> dict[str, Any]:
        query = select(s.task_input_groups).where(s.task_input_groups.c.id == group_id)
        if lock:
            query = query.with_for_update()
        row = (await conn.execute(query)).mappings().first()
        if row is None:
            raise HTTPException(404, "附件组不存在")
        return dict(row)

    async def prior_task(self, group_id: UUID, owner: str, digest: str) -> dict[str, Any] | None:
        row = await self.get(group_id)
        check_owner(row, owner)
        if row["bound_task_id"] is None:
            check_mutable(row, owner)
            return None
        if row["create_request_hash"] != digest:
            raise HTTPException(409, "该附件组已用于不同的任务创建请求")
        async with self.engine.connect() as conn:
            task = (
                (await conn.execute(select(s.tasks).where(s.tasks.c.id == row["bound_task_id"])))
                .mappings()
                .first()
            )
        if task is None or task["deleting"]:
            raise HTTPException(409, "任务正在删除")
        return dict(task)

    async def upload(
        self, group_id: UUID, owner: str, filename: str, content: BinaryIO, size: int, sha: str
    ) -> InitialAttachment:
        check_filename(filename)
        if size > FILE_LIMIT:
            raise HTTPException(413, "单个附件不能超过 50 MiB")
        fid = uuid4()
        attachment = InitialAttachment(
            id=fid,
            filename=filename,
            path=f"/workspace/shared/inputs/{fid}/{filename}",
            uri=f"inputs/{group_id}/{fid}/{filename}",
            size=size,
            sha256=sha,
        )
        async with self.engine.begin() as conn:
            row = await self.row(conn, group_id, lock=True)
            check_mutable(row, owner)
            files = row["files"]
            if len(files) >= FILE_COUNT or sum(item["size"] for item in files) + size > TOTAL_LIMIT:
                raise HTTPException(413, "最多 20 个附件，合计不能超过 200 MiB")
            pending = asyncio.create_task(
                self.objects.put(attachment.uri, content, length=size, part_size=FILE_LIMIT)
            )
            cancelled: asyncio.CancelledError | None = None
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError as error:
                    cancelled = error
            pending.result()
            if cancelled is not None:
                # Keep the file and group lock alive until a MinIO worker thread
                # finishes, so expiry cleanup cannot delete the group first.
                raise cancelled
            await conn.execute(
                update(s.task_input_groups)
                .where(s.task_input_groups.c.id == group_id)
                .values(files=[*files, attachment.model_dump(mode="json")])
            )
        # A failed or interrupted transaction can leave a blob under this group's
        # prefix. The collector removes only keys absent from committed files.
        return attachment

    async def delete_file(self, group_id: UUID, owner: str, file_id: UUID) -> None:
        async with self.engine.begin() as conn:
            row = await self.row(conn, group_id, lock=True)
            check_mutable(row, owner)
            files = [item for item in row["files"] if item["id"] != str(file_id)]
            await conn.execute(
                update(s.task_input_groups)
                .where(s.task_input_groups.c.id == group_id)
                .values(files=files)
            )
        # Logical deletion commits first. Failed S3 removal stays discoverable as
        # an orphan, including when the response is lost and deletion is retried.
        try:
            await self.cleanup_group(group_id)
        except Exception:
            logger.warning("Input orphan cleanup will be retried")

    async def delete_group(self, group_id: UUID, owner: str) -> None:
        async with self.engine.begin() as conn:
            result = (
                (
                    await conn.execute(
                        select(s.task_input_groups)
                        .where(s.task_input_groups.c.id == group_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if result is None:
                return
            row = dict(result)
            check_owner(row, owner)
            if row["bound_task_id"] is not None:
                raise HTTPException(409, "附件组已绑定任务，不能修改")
            await conn.execute(
                update(s.task_input_groups)
                .where(s.task_input_groups.c.id == group_id)
                .values(deleting=True)
            )
        try:
            await self.cleanup_group(group_id)
        except Exception:
            raise HTTPException(503, "附件清理暂时失败，稍后重试") from None

    async def cleanup_group(self, group_id: UUID) -> None:
        async with self.engine.begin() as conn:
            result = (
                (
                    await conn.execute(
                        select(s.task_input_groups)
                        .where(s.task_input_groups.c.id == group_id)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .first()
            )
            if result is None:
                return
            remove_group = result["bound_task_id"] is None and (
                result["deleting"] or result["expires_at"] <= datetime.now(UTC)
            )
            keep = set() if remove_group else {item["uri"] for item in result["files"]}
            for key in await self.objects.list(f"inputs/{group_id}/"):
                if key not in keep:
                    await self.objects.remove(key)
            if remove_group:
                await conn.execute(
                    delete(s.task_input_groups).where(s.task_input_groups.c.id == group_id)
                )

    async def cleanup_once(self, after: UUID | None = None) -> UUID | None:
        query = (
            select(s.task_input_groups.c.id).order_by(s.task_input_groups.c.id).limit(CLEANUP_BATCH)
        )
        if after is not None:
            query = query.where(s.task_input_groups.c.id > after)
        async with self.engine.connect() as conn:
            ids = list((await conn.execute(query)).scalars())
        for gid in ids:
            try:
                async with asyncio.timeout(60):
                    await self.cleanup_group(gid)
            except Exception:
                logger.warning("Input group cleanup will be retried")
        return ids[-1] if len(ids) == CLEANUP_BATCH else None

    async def run_cleanup(self) -> None:
        cursor = None
        while True:
            try:
                cursor = await self.cleanup_once(cursor)
            except Exception:
                logger.warning("Input cleanup scan will be retried")
            await asyncio.sleep(CLEANUP_SECONDS)


router = APIRouter(prefix="/api/task-input-groups", tags=["inputs"])


def groups(request: Request) -> InputGroups:
    store = request.app.state.input_groups
    if store is None:
        raise HTTPException(503, "Blackboard is starting")
    return store


async def readable(request: Request, group_id: UUID) -> dict[str, Any]:
    identity = principal(request)
    row = await groups(request).get(group_id)
    if row["bound_task_id"] is None:
        check_mutable(row, owner_key(identity))
    else:
        if identity.kind == "agent" and identity.task_id != row["bound_task_id"]:
            raise HTTPException(403, "Task access denied")
        async with groups(request).engine.connect() as conn:
            task = (
                await conn.execute(
                    select(s.tasks.c.deleting).where(s.tasks.c.id == row["bound_task_id"])
                )
            ).first()
        if task is None or task[0]:
            raise HTTPException(404, "Task not found")
    return row


def download_headers(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
        "X-Content-Type-Options": "nosniff",
    }


@router.post("", response_model=InputGroupView)
async def create_group(request: Request) -> dict[str, Any]:
    identity = require_user_or_service(request)
    return await groups(request).create(owner_key(identity))


@router.get("/{group_id}", response_model=InputGroupView)
async def get_group(request: Request, group_id: UUID) -> dict[str, Any]:
    return await readable(request, group_id)


@router.post(
    "/{group_id}/files",
    response_model=InitialAttachment,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def upload_file(
    request: Request, group_id: UUID, filename: str = Query(min_length=1)
) -> InitialAttachment:
    identity = require_user_or_service(request)
    store = groups(request)
    check_filename(filename)
    check_mutable(await store.get(group_id), owner_key(identity))
    size, digest = 0, hashlib.sha256()
    with TemporaryFile() as content:
        try:
            async with asyncio.timeout(UPLOAD_SECONDS):
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > FILE_LIMIT:
                        raise HTTPException(413, "单个附件不能超过 50 MiB")
                    digest.update(chunk)
                    await anyio.to_thread.run_sync(content.write, chunk)
                await anyio.to_thread.run_sync(content.seek, 0)
        except TimeoutError:
            raise HTTPException(408, "附件上传超时，请重新上传") from None
        return await store.upload(
            group_id, owner_key(identity), filename, content, size, digest.hexdigest()
        )


@router.get("/{group_id}/files/{file_id}", response_class=StreamingResponse)
async def download_file(request: Request, group_id: UUID, file_id: UUID) -> StreamingResponse:
    row = await readable(request, group_id)
    item = next((item for item in row["files"] if item["id"] == str(file_id)), None)
    if item is None or not await groups(request).objects.exists(item["uri"]):
        raise HTTPException(404, "附件不存在")
    return StreamingResponse(
        groups(request).objects.stream(item["uri"]),
        media_type="application/octet-stream",
        headers=download_headers(item["filename"]),
    )


@router.delete("/{group_id}/files/{file_id}")
async def remove_file(request: Request, group_id: UUID, file_id: UUID) -> dict[str, bool]:
    identity = require_user_or_service(request)
    await groups(request).delete_file(group_id, owner_key(identity), file_id)
    return {"deleted": True}


@router.delete("/{group_id}")
async def remove_group(request: Request, group_id: UUID) -> dict[str, bool]:
    identity = require_user_or_service(request)
    await groups(request).delete_group(group_id, owner_key(identity))
    return {"deleted": True}
