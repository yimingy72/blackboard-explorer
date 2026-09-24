"""Bounded, read-only indexes for archived workspace tar.zst objects."""

from __future__ import annotations

import asyncio
import tarfile
import tempfile
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol

import anyio
import zstandard

MAX_COMPRESSED = 2 * 1024 * 1024 * 1024
MAX_DECOMPRESSED = 512 * 1024 * 1024
MAX_MEMBERS = 50_000
PREVIEW_BYTES = 200 * 1024
HEAD_BYTES = PREVIEW_BYTES // 2
TAIL_BYTES = PREVIEW_BYTES // 2 - 64


class ArchiveTooLarge(ValueError):
    pass


class InvalidArchive(ValueError):
    pass


class InvalidPath(ValueError):
    pass


class NotPreviewable(ValueError):
    pass


class ArchiveSource(Protocol):
    def stream(self, uri: str, chunk_size: int = 65536) -> AsyncIterator[bytes]: ...


@dataclass(frozen=True)
class WorkspaceEntry:
    path: str
    kind: Literal["file", "directory", "link"]
    size: int


@dataclass
class ArchiveIndex:
    file: BinaryIO
    tar: tarfile.TarFile
    entries: list[WorkspaceEntry]
    files: dict[str, tarfile.TarInfo]

    def close(self) -> None:
        self.tar.close()
        self.file.close()


def normalized_path(value: str) -> str:
    """Normalize a tar member or requested relative path without hiding traversal."""
    while value.startswith("./"):
        value = value[2:]
    if value in {"", "."}:
        return ""
    if value.startswith("/") or "//" in value or "\\" in value or "\x00" in value:
        raise InvalidPath("Workspace path must be relative")
    parts = value.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidPath("Workspace path contains an unsafe component")
    return "/".join(parts)


def _index_tar(compressed: BinaryIO) -> ArchiveIndex:
    output = tempfile.TemporaryFile()
    tar: tarfile.TarFile | None = None
    index: ArchiveIndex | None = None
    try:
        with zstandard.ZstdDecompressor(max_window_size=64 * 1024 * 1024).stream_reader(
            compressed
        ) as reader:
            total = 0
            while chunk := reader.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DECOMPRESSED:
                    raise ArchiveTooLarge("Workspace archive exceeds preview limit")
                output.write(chunk)
        output.seek(0)
        tar = tarfile.open(fileobj=output, mode="r:")
        entries: dict[str, WorkspaceEntry] = {}
        files: dict[str, tarfile.TarInfo] = {}
        for count, member in enumerate(tar, start=1):
            if count > MAX_MEMBERS:
                raise ArchiveTooLarge("Workspace archive has too many members")
            path = normalized_path(member.name)
            if not path:
                continue  # envd's full archive starts with '.'
            if member.size < 0 or member.size > MAX_DECOMPRESSED:
                raise InvalidArchive("Invalid workspace member size")
            if member.isdir():
                kind: Literal["file", "directory", "link"] = "directory"
            elif member.isfile():
                kind = "file"
            elif member.issym() or member.islnk():
                kind = "link"
            else:
                continue  # devices, FIFOs and other special members are never exposed
            parts = path.split("/")
            for length in range(1, len(parts)):
                parent = "/".join(parts[:length])
                existing = entries.get(parent)
                if existing is not None and existing.kind != "directory":
                    raise InvalidArchive("Workspace member has a non-directory parent")
                entries.setdefault(parent, WorkspaceEntry(parent, "directory", 0))
            existing = entries.get(path)
            if existing is not None and not (existing.kind == "directory" and kind == "directory"):
                raise InvalidArchive("Duplicate workspace member")
            entries[path] = WorkspaceEntry(path, kind, member.size if kind == "file" else 0)
            if kind == "file":
                files[path] = member
        index = ArchiveIndex(
            output, tar, sorted(entries.values(), key=lambda item: item.path), files
        )
        return index
    except (zstandard.ZstdError, tarfile.TarError, EOFError) as exc:
        raise InvalidArchive("Unreadable workspace archive") from exc
    finally:
        if index is None:
            if tar is not None:
                tar.close()
            output.close()


def _decode(data: bytes, *, trim_start: bool = False, trim_end: bool = False) -> str | None:
    if any(byte < 32 and byte not in {9, 10, 13} for byte in data):
        return None
    for start in range(4 if trim_start else 1):
        for end in range(4 if trim_end else 1):
            try:
                return data[start : len(data) - end if end else None].decode("utf-8")
            except UnicodeDecodeError:
                pass
    return None


def _preview(index: ArchiveIndex, member: tarfile.TarInfo, path: str) -> dict[str, object]:
    source = index.tar.extractfile(member)
    if source is None:
        raise InvalidArchive("Unreadable workspace file")
    with source:
        size = member.size
        truncated = size > PREVIEW_BYTES
        if truncated:
            head = source.read(HEAD_BYTES)
            source.seek(size - TAIL_BYTES)
            tail = source.read(TAIL_BYTES)
            if len(head) != HEAD_BYTES or len(tail) != TAIL_BYTES:
                raise InvalidArchive("Incomplete workspace file")
            first = _decode(head, trim_end=True)
            last = _decode(tail, trim_start=True)
            if first is None or last is None:
                binary, text = True, ""
            else:
                binary, text = False, first + "\n…[中间省略]…\n" + last
        else:
            data = source.read(size)
            if len(data) != size:
                raise InvalidArchive("Incomplete workspace file")
            decoded = _decode(data)
            binary = decoded is None
            text = "" if binary else decoded
    return {"path": path, "size": size, "text": text, "truncated": truncated, "binary": binary}


class WorkspaceArchiveCache:
    """A single lock protects tar seeks and LRU eviction from concurrent requests."""

    def __init__(self) -> None:
        self._entries: OrderedDict[str, ArchiveIndex] = OrderedDict()
        self._lock = asyncio.Lock()

    async def _load(self, source: ArchiveSource, uri: str) -> ArchiveIndex:
        cached = self._entries.get(uri)
        if cached is not None:
            self._entries.move_to_end(uri)
            return cached
        with tempfile.TemporaryFile() as compressed:
            total = 0
            async for chunk in source.stream(uri, chunk_size=1024 * 1024):
                total += len(chunk)
                if total > MAX_COMPRESSED:
                    raise ArchiveTooLarge("Compressed workspace archive exceeds preview limit")
                await anyio.to_thread.run_sync(compressed.write, chunk)
            await anyio.to_thread.run_sync(compressed.seek, 0)
            if len(self._entries) >= 2:
                _, old = self._entries.popitem(last=False)
                await anyio.to_thread.run_sync(old.close)
            index = await anyio.to_thread.run_sync(_index_tar, compressed)
        self._entries[uri] = index
        return index

    async def tree(self, source: ArchiveSource, uri: str) -> list[WorkspaceEntry]:
        async with self._lock:
            return list((await self._load(source, uri)).entries)

    async def file(self, source: ArchiveSource, uri: str, path: str) -> dict[str, object]:
        normalized = normalized_path(path)
        if not normalized or normalized != path:
            raise InvalidPath("Workspace file path must be normalized")
        async with self._lock:
            index = await self._load(source, uri)
            member = index.files.get(path)
            if member is None:
                if any(entry.path == path and entry.kind != "file" for entry in index.entries):
                    raise NotPreviewable("Only regular workspace files can be previewed")
                raise FileNotFoundError(path)
            return await anyio.to_thread.run_sync(_preview, index, member, path)

    async def close(self) -> None:
        async with self._lock:
            while self._entries:
                _, index = self._entries.popitem(last=False)
                await anyio.to_thread.run_sync(index.close)
