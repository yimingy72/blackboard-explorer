"""Build a task archive from declared, already persisted task attachments."""

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
from bbx_contracts.models import InitialAttachment
from bbx_objects import ObjectStore

ARCHIVE_MAX_BYTES = 2 * 1024 * 1024 * 1024
EXPANDED_MAX_BYTES = ARCHIVE_MAX_BYTES * 4
REFERENCE_FIELDS = {
    "uri",
    "result_uri",
    "response_uri",
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


def initial_attachments(task_id: UUID, value: Any) -> list[dict[str, Any]]:
    """Validate registered originals against their exact task and workspace paths."""
    if not isinstance(value, list):
        raise ValueError("Initial attachments must be a list")
    result = []
    seen = set()
    for item in value:
        attachment = InitialAttachment.model_validate(item)
        filename = attachment.filename
        if (
            filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)
            or len(filename.encode("utf-8")) > 255
        ):
            raise ValueError("Unsafe initial attachment filename")
        key = str(attachment.id)
        if key in seen:
            raise ValueError("Duplicate initial attachment")
        seen.add(key)
        if (
            attachment.uri != f"inputs/{task_id}/{key}/{attachment.filename}"
            or attachment.path != f"/workspace/shared/inputs/{key}/{attachment.filename}"
        ):
            raise ValueError("Initial attachment belongs to another task or path")
        _member(attachment.path)
        result.append(attachment.model_dump(mode="json"))
    return result


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
        f"inputs/{task}/",
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
    if not isinstance(state, dict):
        raise ValueError("Task archive snapshot lacks state")
    ctf = state.get("task", {}).get("mode") == "ctf"
    facts = state.get("facts", {}) if ctf else state.get("facts")
    if not isinstance(facts, dict):
        raise ValueError("Task archive snapshot lacks facts")

    attachments: list[dict[str, Any]] = []
    selected: dict[str, dict[str, Any]] = {}
    originals = initial_attachments(task_id, state.get("task", {}).get("initial_attachments", []))
    original_uris = {item["uri"] for item in originals}
    original_paths = {}
    for attachment in originals:
        member = _member(attachment["path"])
        row = {**attachment, "fact_id": None, "version": 0, "auto": True, "initial": True}
        attachments.append(row)
        selected[member] = row
        original_paths[member] = attachment["uri"]
    for fact_id, fact in facts.items():
        if not isinstance(fact, dict):
            raise ValueError("Invalid Fact in archive snapshot")
        version = fact.get("version")
        if not isinstance(version, int) or version < 0:
            raise ValueError("Fact version is required for archive selection")
        for evidence in fact.get("evidence", []):
            if not isinstance(evidence, dict) or not isinstance(evidence.get("uri"), str):
                raise ValueError("Evidence URI is required")
            uri = evidence["uri"]
            if not _owned(uri, task_id) and uri not in original_uris:
                raise ValueError("Evidence URI belongs to another task")
            path = evidence.get("path")
            member = _member(path) if path is not None else None
            if member in original_paths and uri != original_paths[member]:
                raise ValueError("Evidence conflicts with an initial attachment path")
            if uri in original_uris and member is not None and original_paths.get(member) != uri:
                raise ValueError("Initial attachment URI has a different evidence path")
            row = {
                "fact_id": str(fact_id),
                "version": version,
                "uri": uri,
                "path": path,
                "size": evidence.get("size"),
                "auto": bool(evidence.get("auto", False)),
            }
            attachments.append(row)
            if member is not None and member not in original_paths:
                previous = selected.get(member)
                if previous is None or (version, str(fact_id), uri) > (
                    previous["version"],
                    previous["fact_id"],
                    previous["uri"],
                ):
                    selected[member] = row

    if ctf:
        declared = state.get("artifacts", [])
        if not isinstance(declared, list):
            raise ValueError("CTF artifact declarations must be a list")
        artifact_ids: set[str] = set()
        for artifact in declared:
            if not isinstance(artifact, dict) or str(artifact.get("task_id")) != str(task_id):
                raise ValueError("CTF artifact belongs to another task")
            artifact_id = str(UUID(str(artifact.get("id"))))
            if artifact_id in artifact_ids:
                raise ValueError("Duplicate CTF artifact declaration")
            artifact_ids.add(artifact_id)
            version = artifact.get("created_version")
            if type(version) is not int or version < 1:
                raise ValueError("CTF artifact registration version is required")
            path, uri = artifact.get("path"), artifact.get("uri")
            if not isinstance(path, str) or not isinstance(uri, str):
                raise ValueError("CTF artifact path and URI are required")
            member = _member(path)
            filename = artifact.get("filename")
            parts = uri.split("/")
            if (
                len(parts) != 4
                or parts[:2] != ["evidence", str(task_id)]
                or parts[3] != filename
                or PurePosixPath(member).name != filename
            ):
                raise ValueError("CTF artifact URI belongs to another task or path")
            UUID(parts[2])
            if type(artifact.get("size")) is not int or artifact["size"] < 0:
                raise ValueError("CTF artifact size is required")
            if (
                not isinstance(artifact.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is None
            ):
                raise ValueError("CTF artifact SHA-256 is required")
            if member in original_paths:
                raise ValueError("CTF artifact conflicts with an initial attachment path")
            row = {**artifact, "artifact_id": artifact_id, "fact_id": None, "version": version}
            attachments.append(row)
            previous = selected.get(member)
            if previous is None or (version, artifact_id, uri) > (
                previous["version"],
                previous.get("artifact_id", ""),
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
            if row.get("sha256") is not None and digest.hexdigest() != row["sha256"]:
                raise ValueError("Attachment SHA-256 differs from declaration")
            sizes[row["uri"]] = size
            hashes[row["uri"]] = digest.hexdigest()
            staged[member] = source
        for uri in sorted({row["uri"] for row in attachments} - sizes.keys()):
            size = 0
            digest = hashlib.sha256()
            async for chunk in objects.stream(uri):
                size += len(chunk)
                digest.update(chunk)
            sizes[uri] = size
            hashes[uri] = digest.hexdigest()
        for row in attachments:
            if isinstance(row["size"], int) and sizes[row["uri"]] != row["size"]:
                raise ValueError("Evidence object size differs from declaration")
            if row.get("sha256") is not None and hashes[row["uri"]] != row["sha256"]:
                raise ValueError("Attachment SHA-256 differs from declaration")

        manifest = {
            "format": "bbx.task-archive.v1",
            "task_id": str(task_id),
            "run_number": data.get("run_number"),
            "policy": "registered-ctf-artifacts" if ctf else "declared-evidence",
            "attachments": attachments,
            "initial_attachments": originals,
            "restored_files": [
                {
                    "path": path,
                    "uri": row["uri"],
                    "fact_id": row["fact_id"],
                    **({"artifact_id": row["artifact_id"]} if "artifact_id" in row else {}),
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
