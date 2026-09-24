"""Workspace file and archive boundary checks without containers."""

import importlib
import io
import os
import tarfile
from pathlib import Path

import httpx
import pytest
from bbx_envd.core import workspace_path
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.routing import Route


@pytest.fixture
def service(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ENVD_TOKEN", "replace-me")
    module = importlib.import_module("bbx_envd.app")
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(module, "WORKSPACE", root)
    monkeypatch.setattr(
        module,
        "workspace_path",
        lambda value, strict=True: workspace_path(value, root=root, strict=strict),
    )
    monkeypatch.setattr(module.settings, "evidence_max_bytes", 4)
    return module, root


def request(path: Path, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/files",
            "query_string": f"path={path}".encode(),
            "headers": [],
        }
    )


@pytest.mark.asyncio
async def test_files_rejects_directory_and_fifo(service) -> None:
    module, root = service
    pipe = root / "pipe"
    os.mkfifo(pipe)
    app = Starlette(routes=[Route("/files", module.files)])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in (root, pipe):
            response = await client.get("/files", params={"path": str(path)})
            assert response.status_code == 400
            assert response.json() == {"error": "not a file"}


@pytest.mark.asyncio
async def test_files_snapshot_limit_and_head(service, monkeypatch) -> None:
    module, root = service
    evidence = root / "evidence"
    evidence.write_bytes(b"1234")
    app = Starlette(routes=[Route("/files", module.files)])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.head("/files", params={"path": str(evidence)})
        assert response.status_code == 200
        assert response.headers["content-length"] == "4"
        assert response.content == b""

        original_temporary_file = module.tempfile.TemporaryFile

        def grow_before_copy():
            with evidence.open("ab") as target:
                target.write(b"5")
            return original_temporary_file()

        monkeypatch.setattr(module.tempfile, "TemporaryFile", grow_before_copy)
        response = await client.get("/files", params={"path": str(evidence)})
        assert response.status_code == 413
        monkeypatch.setattr(module.tempfile, "TemporaryFile", original_temporary_file)

    evidence.write_bytes(b"1234")
    snapshot = await module.files(request(evidence))
    evidence.write_bytes(b"changed")
    body = bytearray()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    await snapshot(
        {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET"}, receive, send
    )
    assert bytes(body) == b"1234"
    assert snapshot.snapshot.closed


@pytest.mark.asyncio
async def test_snapshot_closes_on_disconnect(service) -> None:
    module, _ = service
    snapshot = io.BytesIO(b"safe")
    response = module.SnapshotResponse(snapshot, 4)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        raise OSError("disconnected")

    with pytest.raises(ClientDisconnect):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET"}, receive, send
        )
    assert snapshot.closed


@pytest.mark.asyncio
async def test_files_and_stat_reject_path_replacement(service, monkeypatch) -> None:
    module, root = service
    evidence = root / "evidence"
    evidence.write_bytes(b"safe")
    outside = root.parent / "outside"
    outside.write_bytes(b"secret")
    real_workspace_path = module.workspace_path

    def swap_after_validation(value, strict=True):
        result = real_workspace_path(value, strict=strict)
        evidence.unlink()
        evidence.symlink_to(outside)
        return result

    monkeypatch.setattr(module, "workspace_path", swap_after_validation)
    assert (await module.files(request(evidence))).status_code == 400
    evidence.unlink()
    evidence.write_bytes(b"safe")
    assert (await module.stat(request(evidence))).status_code == 400


@pytest.mark.asyncio
async def test_archive_fallback_keeps_exclusions(service, monkeypatch) -> None:
    module, root = service
    agent = root / "agents" / "agent-1"
    agent.mkdir(parents=True)
    (agent / "keep.txt").write_text("keep")
    (agent / "skip.o").write_text("skip")
    for directory in ("node_modules", "__pycache__", "target", ".git/objects"):
        omitted = agent / directory
        omitted.mkdir(parents=True)
        (omitted / "hidden.txt").write_text("skip")
    shared = root / "shared"
    shared.mkdir()
    (shared / "outside-fallback.txt").write_text("shared")
    monkeypatch.setattr(module.settings, "archive_max_bytes", 1)

    class FakeCompressor:
        def __init__(self, args, stdin, stdout):
            class Pipe(io.BytesIO):
                def close(self):
                    if not self.closed:
                        stdout.write(self.getvalue())
                    super().close()

            self.stdin = Pipe()

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", FakeCompressor)
    app = Starlette(routes=[Route("/archive", module.archive, methods=["POST"])])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/archive")
    assert response.status_code == 200
    assert response.headers["x-archive-fallback"] == "agents-only"
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:") as archive:
        names = archive.getnames()
    assert "agents/agent-1/keep.txt" in names
    assert all("outside-fallback" not in name for name in names)
    assert all(
        not any(
            term in name
            for term in ("skip.o", "node_modules", "__pycache__", "target", ".git/objects")
        )
        for name in names
    )
