"""Build a task archive from declared, already persisted Fact attachments."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast
from uuid import UUID

import zstandard
from bbx_objects import ObjectStore

ARCHIVE_MAX_BYTES = 2 * 1024 * 1024 * 1024
EXPANDED_MAX_BYTES = ARCHIVE_MAX_BYTES * 4
REFERENCE_FIELDS = {
    "uri",
    "result_uri",
    "report_uri",
    "workspace_uri",
    "resume_workspace_uri",
    "trace_uri",
    "image_uri",
}


class ArchiveCapacityError(ValueError):
    """The archive cannot be restored within envd's fixed default limits."""


class _ExpandedLimitWriter:
    def __init__(self, writer: Any) -> None:
        self.writer = writer
        self.size = 0

    def write(self, data: bytes) -> int:
        self.size += len(data)
        if self.size > EXPANDED_MAX_BYTES:
            raise ArchiveCapacityError("Expanded tar exceeds restore limit")
        return self.writer.write(data)


def _member(path: str) -> str:
    if not path.startswith("/workspace/") or "\\" in path or "\x00" in path:
        raise ValueError("Evidence path must be inside /workspace")
    parts = path.removeprefix("/workspace/").split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Unsafe evidence path")
    if parts[0] == ".bbx":
        raise ValueError("Evidence conflicts with archive metadata")
    if parts[0] == "agents" and (
        len(parts) < 3 or re.fullmatch(r"agent-[0-9]{1,6}", parts[1]) is None
    ):
        raise ValueError("Invalid agent evidence path")
    return str(PurePosixPath(*parts))


def _owned(uri: str, task_id: UUID) -> bool:
    task = str(task_id)
    return uri.startswith((f"evidence/{task}/", f"toolcalls/{task}/", f"traces/{task}/")) and all(
        part not in {"", ".", ".."} for part in uri.split("/")
    )


def _references(value: Any, task_id: UUID) -> list[str]:
    """Collect object pointers from typed URI fields, never from prose."""
    found: set[str] = set()
    task = str(task_id)
    prefixes = (
        f"evidence/{task}/",
        f"toolcalls/{task}/",
        f"traces/{task}/",
        f"reports/{task}/",
        f"workspace/{task}/",
    )
    exact = {f"reports/{task}.md", f"workspace/{task}.tar.zst"}

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    key in REFERENCE_FIELDS
                    and isinstance(child, str)
                    and (child.startswith(prefixes) or child in exact)
                ):
                    found.add(child)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return sorted(found)


def _write_tar(
    output: Path, staged: dict[str, Path], task_json: bytes, manifest_json: bytes
) -> None:
    """Keep compression and tar file reads off the event loop."""
    paths = sorted(staged)
    directories = {".bbx", "shared"}
    for member in paths:
        parent = PurePosixPath(member).parent
        while str(parent) != ".":
            directories.add(str(parent))
            parent = parent.parent
    if len(directories) + len(paths) + 2 > 100_000:
        raise ArchiveCapacityError("Too many archive entries")
    with output.open("wb") as compressed:
        with zstandard.ZstdCompressor().stream_writer(compressed, closefd=False) as writer:
            bounded = cast(BinaryIO, _ExpandedLimitWriter(writer))
            with tarfile.open(fileobj=bounded, mode="w|") as tar:
                for directory in sorted(directories, key=lambda p: (p.count("/"), p)):
                    info = tarfile.TarInfo(directory + "/")
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o1777 if directory == "shared" else 0o755
                    tar.addfile(info)
                for name, content in (
                    (".bbx/task.json", task_json),
                    (".bbx/manifest.json", manifest_json),
                ):
                    info = tarfile.TarInfo(name)
                    info.mode = 0o644
                    info.size = len(content)
                    tar.addfile(info, fileobj=io.BytesIO(content))
                for member, source in sorted(staged.items()):
                    info = tarfile.TarInfo(member)
                    info.mode = 0o644
                    info.size = source.stat().st_size
                    with source.open("rb") as file:
                        tar.addfile(info, file)


async def build_archive(
    objects: ObjectStore, task_id: UUID, data: dict[str, Any], output: Path
) -> None:
    """Validate declarations, stream selected objects to disk, then write tar.zst."""
    if data.get("format") != "bbx.task-archive.v1" or str(data.get("task_id")) != str(task_id):
        raise ValueError("Invalid task archive snapshot")
    state = data.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("facts"), dict):
        raise ValueError("Task archive snapshot lacks facts")

    attachments: list[dict[str, Any]] = []
    selected: dict[str, dict[str, Any]] = {}
    for fact_id, fact in state["facts"].items():
        if not isinstance(fact, dict):
            raise ValueError("Invalid Fact in archive snapshot")
        version = fact.get("version")
        if not isinstance(version, int) or version < 0:
            raise ValueError("Fact version is required for archive selection")
        for evidence in fact.get("evidence", []):
            if not isinstance(evidence, dict) or not isinstance(evidence.get("uri"), str):
                raise ValueError("Evidence URI is required")
            uri = evidence["uri"]
            if not _owned(uri, task_id):
                raise ValueError("Evidence URI belongs to another task")
            path = evidence.get("path")
            member = _member(path) if path is not None else None
            row = {
                "fact_id": str(fact_id),
                "version": version,
                "uri": uri,
                "path": path,
                "size": evidence.get("size"),
                "auto": bool(evidence.get("auto", False)),
            }
            attachments.append(row)
            if member is not None:
                previous = selected.get(member)
                if previous is None or (version, str(fact_id), uri) > (
                    previous["version"],
                    previous["fact_id"],
                    previous["uri"],
                ):
                    selected[member] = row

    paths = sorted(selected)
    for path in paths:
        if path == "shared":
            raise ValueError("Evidence conflicts with shared directory")
        parent = PurePosixPath(path).parent
        while str(parent) != ".":
            if str(parent) in selected:
                raise ValueError("Evidence file and directory paths conflict")
            parent = parent.parent
    if len(paths) + 3 > 100_000:
        raise ArchiveCapacityError("Too many archive entries")

    # Check every declared object, including old versions and pathless tool records.
    for uri in sorted({row["uri"] for row in attachments}):
        if not await objects.exists(uri):
            raise FileNotFoundError(f"Declared evidence object is missing: {uri}")

    with tempfile.TemporaryDirectory(prefix="bbx-task-archive-") as temporary:
        staged: dict[str, Path] = {}
        sizes: dict[str, int] = {}
        hashes: dict[str, str] = {}
        expanded_size = 0
        for index, (member, row) in enumerate(sorted(selected.items())):
            source = Path(temporary) / str(index)
            size = 0
            digest = hashlib.sha256()
            with source.open("wb") as file:
                async for chunk in objects.stream(row["uri"]):
                    size += len(chunk)
                    expanded_size += len(chunk)
                    if expanded_size > EXPANDED_MAX_BYTES:
                        raise ArchiveCapacityError("Expanded archive exceeds restore limit")
                    digest.update(chunk)
                    file.write(chunk)
            if isinstance(row["size"], int) and size != row["size"]:
                raise ValueError("Evidence object size differs from declaration")
            sizes[row["uri"]] = size
            hashes[row["uri"]] = digest.hexdigest()
            staged[member] = source
        for uri in sorted({row["uri"] for row in attachments} - sizes.keys()):
            size = 0
            async for chunk in objects.stream(uri):
                size += len(chunk)
            sizes[uri] = size
        for row in attachments:
            if isinstance(row["size"], int) and sizes[row["uri"]] != row["size"]:
                raise ValueError("Evidence object size differs from declaration")

        manifest = {
            "format": "bbx.task-archive.v1",
            "task_id": str(task_id),
            "run_number": data.get("run_number"),
            "policy": "declared-evidence",
            "attachments": attachments,
            "restored_files": [
                {
                    "path": path,
                    "uri": row["uri"],
                    "fact_id": row["fact_id"],
                    "version": row["version"],
                    "size": sizes[row["uri"]],
                    "sha256": hashes[row["uri"]],
                }
                for path, row in sorted(selected.items())
            ],
            "references": _references(data, task_id),
        }
        task_json = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()
        manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
        if expanded_size + len(task_json) + len(manifest_json) > EXPANDED_MAX_BYTES:
            raise ArchiveCapacityError("Expanded archive exceeds restore limit")
        work = asyncio.create_task(
            asyncio.to_thread(_write_tar, output, staged, task_json, manifest_json)
        )
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            # A cancelled cleanup must not remove staged files while the writer uses them.
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
            work.exception()
            raise
