"""Workspace tree and preview read only bounded tar.zst members."""

import io
import tarfile
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import bbx_blackboard.api as api
import bbx_blackboard.workspace as workspace
import httpx
import pytest
import zstandard
from bbx_blackboard.auth import issue_agent_token
from bbx_blackboard.settings import Settings
from pydantic import SecretStr


def archive(*members: tuple[str, bytes | None, str | None]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                tar.addfile(info)
            elif data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return zstandard.ZstdCompressor().compress(buffer.getvalue())


class Objects:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values
        self.streams: list[str] = []

    async def exists(self, uri: str) -> bool:
        return uri in self.values

    async def stream(self, uri: str, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        self.streams.append(uri)
        data = self.values[uri]
        for offset in range(0, len(data), chunk_size):
            yield data[offset : offset + chunk_size]


def settings() -> Settings:
    return Settings.model_construct(
        postgres_password=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        service_token=SecretStr("service-test"),
        agent_token_secret=SecretStr("workspace-test-agent-secret-32bytes"),
        admin_users=SecretStr("admin:test"),
    )


def app_for(monkeypatch: pytest.MonkeyPatch, task_id: UUID, objects: Objects, *, archived=True):
    app = api.create_app(settings())
    app.state.objects = objects

    async def task(_request, requested):
        return {
            "id": requested,
            "workspace_uri": f"workspace/{task_id}.tar.zst" if archived else None,
        }

    monkeypatch.setattr(api, "_task", task)
    return app


@pytest.mark.asyncio
async def test_workspace_tree_and_file_preview_from_envd_style_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = uuid4()
    uri = f"workspace/{task_id}.tar.zst"
    long_text = b"A" * (workspace.PREVIEW_BYTES + 33) + b"END"
    objects = Objects(
        {
            uri: archive(
                (".", None, None),
                ("./shared", None, None),
                ("./shared/readme.txt", b"hello workspace", None),
                ("./agents/agent-1/large.txt", long_text, None),
                ("./agents/agent-1/binary.dat", b"a\x00b", None),
                ("./shared/absolute-link", None, "/etc/passwd"),
            )
        }
    )
    app = app_for(monkeypatch, task_id, objects)
    headers = {"Authorization": "Bearer service-test"}
    base = f"/api/tasks/{task_id}/workspace"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        tree = await client.get(f"{base}/tree", headers=headers)
        assert tree.status_code == 200, tree.text
        entries = {item["path"]: item for item in tree.json()["entries"]}
        assert entries["shared"] == {"path": "shared", "kind": "directory", "size": 0}
        assert entries["shared/readme.txt"] == {
            "path": "shared/readme.txt",
            "kind": "file",
            "size": 15,
        }
        assert entries["agents/agent-1"]["kind"] == "directory"
        assert entries["shared/absolute-link"]["kind"] == "link"
        small = await client.get(
            f"{base}/file", params={"path": "shared/readme.txt"}, headers=headers
        )
        assert small.json() == {
            "path": "shared/readme.txt",
            "size": 15,
            "text": "hello workspace",
            "truncated": False,
            "binary": False,
        }
        large = await client.get(
            f"{base}/file", params={"path": "agents/agent-1/large.txt"}, headers=headers
        )
        result = large.json()
        assert result["size"] == len(long_text) and result["truncated"] is True
        assert result["text"].startswith("A" * 100) and result["text"].endswith("END")
        assert len(result["text"].encode()) <= workspace.PREVIEW_BYTES
        binary = await client.get(
            f"{base}/file", params={"path": "agents/agent-1/binary.dat"}, headers=headers
        )
        assert binary.json()["binary"] is True and binary.json()["text"] == ""
        for path in ("shared/absolute-link", "shared"):
            assert (
                await client.get(f"{base}/file", params={"path": path}, headers=headers)
            ).status_code == 400
        for path in ("../etc/passwd", "/etc/passwd", "./shared/readme.txt", "shared//readme.txt"):
            assert (
                await client.get(f"{base}/file", params={"path": path}, headers=headers)
            ).status_code == 400
        assert (
            await client.get(f"{base}/file", params={"path": "missing"}, headers=headers)
        ).status_code == 404
    assert objects.streams == [uri]
    await app.state.workspace_cache.close()


@pytest.mark.asyncio
async def test_workspace_requires_registered_existing_archive_and_task_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, other = uuid4(), uuid4()
    uri = f"workspace/{task_id}.tar.zst"
    objects = Objects({})
    app = app_for(monkeypatch, task_id, objects)
    base = f"/api/tasks/{task_id}/workspace/tree"
    agent = issue_agent_token(settings(), other, "agent-1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(base)).status_code == 401
        assert (
            await client.get(base, headers={"Authorization": f"Bearer {agent}"})
        ).status_code == 403
        assert (
            await client.get(base, headers={"Authorization": "Bearer service-test"})
        ).status_code == 404
        objects.values[uri] = archive(("./proof.txt", b"proof", None))
        assert (
            await client.get(base, headers={"Authorization": "Bearer service-test"})
        ).status_code == 200
    await app.state.workspace_cache.close()


@pytest.mark.asyncio
async def test_hardlinks_are_listed_without_following_and_special_members_are_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = uuid4()
    uri = f"workspace/{task_id}.tar.zst"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        regular = tarfile.TarInfo("./regular.txt")
        regular.size = 4
        tar.addfile(regular, io.BytesIO(b"safe"))
        hard = tarfile.TarInfo("./hardlink")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "/etc/passwd"
        tar.addfile(hard)
        special = tarfile.TarInfo("./device")
        special.type = tarfile.CHRTYPE
        tar.addfile(special)
    objects = Objects({uri: zstandard.ZstdCompressor().compress(buffer.getvalue())})
    app = app_for(monkeypatch, task_id, objects)
    base = f"/api/tasks/{task_id}/workspace"
    headers = {"Authorization": "Bearer service-test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        tree = (await client.get(f"{base}/tree", headers=headers)).json()["entries"]
        assert {item["path"]: item["kind"] for item in tree} == {
            "hardlink": "link",
            "regular.txt": "file",
        }
        link = await client.get(f"{base}/file", params={"path": "hardlink"}, headers=headers)
        assert link.status_code == 400
        device = await client.get(f"{base}/file", params={"path": "device"}, headers=headers)
        assert device.status_code == 404
    await app.state.workspace_cache.close()


@pytest.mark.asyncio
async def test_workspace_rejects_unsafe_members_and_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = uuid4()
    uri = f"workspace/{task_id}.tar.zst"
    objects = Objects({uri: archive(("../outside.txt", b"secret", None))})
    app = app_for(monkeypatch, task_id, objects)
    base = f"/api/tasks/{task_id}/workspace/tree"
    headers = {"Authorization": "Bearer service-test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(base, headers=headers)).status_code == 400
        objects.values[uri] = archive(("./a", b"a", None), ("./b", b"b", None))
        monkeypatch.setattr(workspace, "MAX_MEMBERS", 1)
        assert (await client.get(base, headers=headers)).status_code == 413
        monkeypatch.setattr(workspace, "MAX_MEMBERS", 50_000)
        monkeypatch.setattr(workspace, "MAX_DECOMPRESSED", 100)
        assert (await client.get(base, headers=headers)).status_code == 413
        monkeypatch.setattr(workspace, "MAX_DECOMPRESSED", 512 * 1024 * 1024)
        monkeypatch.setattr(workspace, "MAX_COMPRESSED", 1)
        assert (await client.get(base, headers=headers)).status_code == 413
    await app.state.workspace_cache.close()


@pytest.mark.asyncio
async def test_workspace_cache_evicts_old_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    objects = Objects(
        {
            f"workspace/{number}.tar.zst": archive((f"./file-{number}.txt", b"ok", None))
            for number in range(3)
        }
    )
    cache = workspace.WorkspaceArchiveCache()
    first = None
    remaining = []
    original_index = workspace._index_tar

    def bounded_index(compressed):
        assert len(cache._entries) <= 1  # Never hold two old indexes while opening another.
        return original_index(compressed)

    monkeypatch.setattr(workspace, "_index_tar", bounded_index)
    try:
        for number in (0, 1, 2, 0):
            entries = await cache.tree(objects, f"workspace/{number}.tar.zst")
            assert entries[0].path == f"file-{number}.txt"
            if number == 0 and first is None:
                first = cache._entries["workspace/0.tar.zst"]
            if number == 2:
                assert first is not None and first.file.closed
        assert objects.streams == [
            "workspace/0.tar.zst",
            "workspace/1.tar.zst",
            "workspace/2.tar.zst",
            "workspace/0.tar.zst",
        ]
        remaining = list(cache._entries.values())
    finally:
        await cache.close()
    assert all(index.file.closed for index in remaining)
