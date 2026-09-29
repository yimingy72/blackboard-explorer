"""Declared attachments alone determine the restorable task archive."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
import threading
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import zstandard
from bbx_objects import ObjectStore
from bbx_runtime.execenv import archive as archive_module
from bbx_runtime.execenv.archive import ArchiveCapacityError, build_archive


class Objects:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values

    async def exists(self, uri: str) -> bool:
        return uri in self.values

    async def stream(self, uri: str):
        yield self.values[uri]


def snapshot(task: UUID, facts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "format": "bbx.task-archive.v1",
        "task_id": str(task),
        "run_number": 1,
        "state": {"task": {"id": str(task)}, "facts": facts or {}, "intents": {}, "agents": {}},
        "events": [],
        "sessions": [],
        "messages": [],
        "task_runs": [],
    }


def evidence(task: UUID, name: str, path: str | None, *, size: int | None = None) -> dict:
    return {"uri": f"evidence/{task}/agent-1/{name}", "path": path, "size": size}


def members(output: Path) -> tuple[list[str], dict[str, bytes]]:
    raw = zstandard.ZstdDecompressor().decompress(output.read_bytes(), max_output_size=10_000_000)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        return tar.getnames(), {
            member.name: cast(io.BufferedReader, tar.extractfile(member)).read()
            for member in tar
            if member.isfile()
        }


@pytest.mark.asyncio
async def test_selected_script_and_dependency_restore_with_all_version_references(
    tmp_path: Path,
) -> None:
    task = uuid4()
    path = "/workspace/agents/agent-1/replay.sh"
    old = evidence(task, "old", path, size=3)
    new = evidence(task, "new", path, size=3)
    dep = evidence(task, "dep", "/workspace/shared/input.json", size=2)
    tool = {"uri": f"toolcalls/{task}/call.txt", "path": None, "auto": True}
    data = snapshot(
        task,
        {
            "F1": {"version": 10, "evidence": [old, tool]},
            "F2": {"version": 11, "evidence": [new, dep]},
        },
    )
    data["events"] = [{"payload": {"trace_uri": f"traces/{task}/agent-1/step.json"}}]
    data["events"].append({"payload": {"statement": f"traces/{task}/not-an-object.json"}})
    data["state"]["task"]["report_uri"] = f"reports/{task}.md"
    image_uri = f"evidence/{task}/agent-1/view.png"
    data["sessions"] = [{"session": {"image_records": [{"uri": image_uri}]}}]
    prior_archive = f"workspace/{task}.tar.zst"
    data["task_runs"] = [{"workspace_uri": prior_archive}]
    values = {old["uri"]: b"old", new["uri"]: b"new", dep["uri"]: b"{}", tool["uri"]: b"tool"}
    values[f"evidence/{task}/agent-1/unused-large"] = b"x" * 100_000
    output = tmp_path / "archive.tar.zst"
    await build_archive(cast(ObjectStore, Objects(values)), task, data, output)
    names, contents = members(output)
    assert "shared" in names
    assert contents["agents/agent-1/replay.sh"] == b"new"
    assert contents["shared/input.json"] == b"{}"
    assert not any("unused-large" in name or "call.txt" in name for name in names)
    assert "old" not in contents.values()
    manifest = json.loads(contents[".bbx/manifest.json"])
    assert manifest["policy"] == "declared-evidence"
    assert {row["uri"] for row in manifest["attachments"]} == {
        old["uri"],
        new["uri"],
        dep["uri"],
        tool["uri"],
    }
    assert manifest["restored_files"][0]["uri"] == new["uri"]
    assert manifest["restored_files"][0]["size"] == 3
    assert manifest["restored_files"][0]["sha256"] == hashlib.sha256(b"new").hexdigest()
    assert f"traces/{task}/agent-1/step.json" in manifest["references"]
    assert f"traces/{task}/not-an-object.json" not in manifest["references"]
    assert f"reports/{task}.md" in manifest["references"]
    assert image_uri in manifest["references"]
    assert prior_archive in manifest["references"]
    assert json.loads(contents[".bbx/task.json"]) == data


@pytest.mark.asyncio
async def test_empty_task_has_metadata_and_shared_directory(tmp_path: Path) -> None:
    task = uuid4()
    output = tmp_path / "empty.tar.zst"
    await build_archive(cast(ObjectStore, Objects({})), task, snapshot(task), output)
    names, contents = members(output)
    assert "shared" in names
    assert json.loads(contents[".bbx/manifest.json"])["attachments"] == []


@pytest.mark.asyncio
async def test_tar_headers_and_padding_count_toward_restore_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = uuid4()
    data = snapshot(task)
    assert len(json.dumps(data).encode()) < 1024
    monkeypatch.setattr(archive_module, "EXPANDED_MAX_BYTES", 1024)
    with pytest.raises(ArchiveCapacityError, match="Expanded tar exceeds restore limit"):
        await build_archive(
            cast(ObjectStore, Objects({})), task, data, tmp_path / "too-large.tar.zst"
        )


@pytest.mark.asyncio
async def test_missing_declared_object_fails_before_archive(tmp_path: Path) -> None:
    task = uuid4()
    ev = evidence(task, "missing", "/workspace/shared/replay.sh")
    data = snapshot(task, {"F1": {"version": 1, "evidence": [ev]}})
    output = tmp_path / "missing.tar.zst"
    with pytest.raises(FileNotFoundError):
        await build_archive(cast(ObjectStore, Objects({})), task, data, output)
    assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/workspace/../escape",
        "/workspace/shared/../escape",
        "/workspace/.bbx/task.json",
        "/workspace/shared",
        "/workspace//empty",
        "/other/task/file",
    ],
)
async def test_unsafe_or_reserved_path_is_rejected(tmp_path: Path, path: str) -> None:
    task = uuid4()
    ev = evidence(task, "bad", path)
    data = snapshot(task, {"F1": {"version": 1, "evidence": [ev]}})
    with pytest.raises(ValueError):
        await build_archive(
            cast(ObjectStore, Objects({ev["uri"]: b"x"})), task, data, tmp_path / "bad.tar.zst"
        )


@pytest.mark.asyncio
async def test_foreign_object_and_file_directory_collision_are_rejected(tmp_path: Path) -> None:
    task = uuid4()
    foreign = evidence(uuid4(), "foreign", "/workspace/shared/file")
    with pytest.raises(ValueError, match="another task"):
        await build_archive(
            cast(ObjectStore, Objects({})),
            task,
            snapshot(task, {"F1": {"version": 1, "evidence": [foreign]}}),
            tmp_path / "foreign.tar.zst",
        )
    first = evidence(task, "first", "/workspace/shared/name")
    second = evidence(task, "second", "/workspace/shared/name/child")
    with pytest.raises(ValueError, match="conflict"):
        await build_archive(
            cast(ObjectStore, Objects({})),
            task,
            snapshot(task, {"F1": {"version": 1, "evidence": [first, second]}}),
            tmp_path / "collision.tar.zst",
        )


@pytest.mark.asyncio
async def test_cancel_waits_for_background_writer_before_staging_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = uuid4()
    ev = evidence(task, "script", "/workspace/shared/replay.sh", size=1)
    data = snapshot(task, {"F1": {"version": 1, "evidence": [ev]}})
    started = threading.Event()
    release = threading.Event()
    staged_directory: list[Path] = []

    def held_writer(
        output: Path, staged: dict[str, Path], task_json: bytes, manifest_json: bytes
    ) -> None:
        staged_directory.append(next(iter(staged.values())).parent)
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(archive_module, "_write_tar", held_writer)
    work = asyncio.create_task(
        build_archive(
            cast(ObjectStore, Objects({ev["uri"]: b"x"})), task, data, tmp_path / "a.tar.zst"
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), 1)
        await asyncio.wait_for(asyncio.sleep(0), 0.2)
        assert not work.done()
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done()
        assert staged_directory[0].exists()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(work, 1)
    assert not staged_directory[0].exists()
