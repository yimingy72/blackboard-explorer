"""Task labels, isolation settings, and idempotent lifecycle without Docker."""

from __future__ import annotations

import builtins
import hmac
import io
import json
import tarfile
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import docker
import pytest
import zstandard
from bbx_contracts.profile import load_profile
from bbx_objects import ObjectStore
from bbx_runtime.execenv import ExecEnvManager, task_token
from bbx_runtime.execenv import manager as manager_module
from bbx_runtime.settings import Settings
from docker.errors import NotFound
from pydantic import SecretStr


class Container:
    def __init__(self, owner: Containers, kwargs: dict[str, Any]) -> None:
        self.owner = owner
        self.kwargs = kwargs
        self.id = f"container-{len(owner.items) + 1}"
        self.name = kwargs["name"]
        self.labels = kwargs["labels"]
        self.status = "created"
        self.attrs = {
            "NetworkSettings": {
                "Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49152"}]}
            }
        }

    def start(self) -> None:
        self.status = "running"

    def reload(self) -> None:
        pass

    def remove(self, *, force: bool) -> None:
        assert force
        self.owner.items.remove(self)


class Containers:
    def __init__(self) -> None:
        self.items: list[Container] = []

    def list(self, *, all: bool, filters: dict[str, list[str]]) -> list[Container]:
        assert all
        required = [entry.split("=", 1) for entry in filters["label"]]
        return [
            item
            for item in self.items
            if builtins.all(item.labels.get(key) == value for key, value in required)
        ]

    def get(self, name: str) -> Container:
        for item in self.items:
            if item.name == name or item.id == name:
                return item
        raise NotFound("not found")

    def create(self, _image: str, **kwargs: Any) -> Container:
        item = Container(self, kwargs)
        self.items.append(item)
        return item


class Network:
    def __init__(self) -> None:
        self.connected: list[str] = []
        self.name = "bbx-test-exec"
        self.attrs = {"Internal": False, "Containers": {}}

    def reload(self) -> None:
        pass

    def connect(self, container_id: str) -> None:
        self.connected.append(container_id)


class Networks:
    def __init__(self) -> None:
        self.network = Network()

    def get(self, _name: str) -> Network:
        return self.network


class Docker:
    def __init__(self) -> None:
        self.containers = Containers()
        self.networks = Networks()


class Objects:
    async def put(self, *_args: Any, **_kwargs: Any) -> None:
        pass


def settings(mode: str = "relay", egress_mode: str = "direct") -> Settings:
    return Settings.model_construct(
        deepseek_api_key=SecretStr("fake-deepseek"),
        minio_root_password=SecretStr("fake-minio"),
        service_token=SecretStr("fake-service"),
        envd_token_secret=SecretStr("test-secret"),
        exec_access_mode=mode,
        exec_egress_mode=egress_mode,
        exec_network="bbx-test-exec",
    )


def test_task_token() -> None:
    task_id = uuid4()
    expected = hmac.new(b"test-secret", str(task_id).encode(), sha256).hexdigest()
    assert task_token("test-secret", task_id) == expected


@pytest.mark.asyncio
async def test_provision_idempotent_with_direct_egress_and_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    fake = Docker()
    manager = ExecEnvManager(
        settings(),
        docker_client=cast(docker.DockerClient, fake),
        objects=cast(ObjectStore, Objects()),
    )

    async def healthy(_handle):
        return None

    monkeypatch.setattr(manager, "wait_healthy", healthy)
    task_id = uuid4()
    handle = await manager.provision(task_id, profile)
    assert handle.base_url == "http://127.0.0.1:49152"
    assert "test-secret" not in repr(handle)
    assert handle.token not in repr(handle)
    assert len(fake.containers.items) == 2
    envd, relay = fake.containers.items
    assert envd.labels["bbx.task-id"] == str(task_id)
    assert envd.kwargs["network"] == "bbx-test-exec"
    assert "ports" not in envd.kwargs
    assert "cap_drop" not in envd.kwargs
    assert envd.kwargs["environment"]["PRIVILEGED_PREFIXES"] == ""
    assert not any("PROXY" in key.upper() for key in envd.kwargs["environment"])
    assert "security_opt" not in envd.kwargs
    assert not envd.kwargs.get("privileged", False)
    assert envd.kwargs["cap_add"] == ["NET_ADMIN"]
    assert envd.kwargs["devices"] == ["/dev/net/tun:/dev/net/tun:rwm"]
    assert envd.kwargs["nano_cpus"] == 2_000_000_000
    assert relay.kwargs["network"] == "bridge"
    assert relay.kwargs["ports"] == {"8080/tcp": ("127.0.0.1", 0)}
    assert relay.kwargs["cap_drop"] == ["ALL"]
    assert relay.kwargs["security_opt"] == ["no-new-privileges"]
    assert fake.networks.network.connected == [relay.id]
    again = await manager.provision(task_id, profile)
    assert again.container_id == handle.container_id
    assert len(fake.containers.items) == 2
    assert (await manager.find(task_id)) == handle
    await manager.destroy(task_id)
    assert fake.containers.items == []


@pytest.mark.asyncio
async def test_short_name_collision_never_reuses_another_task() -> None:
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    fake = Docker()
    manager = ExecEnvManager(
        settings("network"),
        docker_client=cast(docker.DockerClient, fake),
        objects=cast(ObjectStore, Objects()),
    )
    task_id = uuid4()
    other_task_id = uuid4()
    fake.containers.create(
        profile.exec_image,
        name=f"bbx-exec-{task_id.hex[:8]}",
        labels={
            "bbx.managed": "agent-runtime",
            "bbx.task-id": str(other_task_id),
            "bbx.role": "envd",
        },
    )
    with pytest.raises(RuntimeError, match="owned by another task"):
        await manager.provision(task_id, profile)
    assert len(fake.containers.items) == 1
    await manager.destroy(task_id)
    assert len(fake.containers.items) == 1


@pytest.mark.asyncio
async def test_failed_provision_keeps_preexisting_envd(monkeypatch: pytest.MonkeyPatch) -> None:
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    fake = Docker()
    task_id = uuid4()
    existing = fake.containers.create(
        profile.exec_image,
        name=f"bbx-exec-{task_id.hex[:8]}",
        labels={"bbx.managed": "agent-runtime", "bbx.task-id": str(task_id), "bbx.role": "envd"},
    )
    existing.start()
    manager = ExecEnvManager(
        settings(),
        docker_client=cast(docker.DockerClient, fake),
        objects=cast(ObjectStore, Objects()),
    )

    async def unhealthy(_handle):
        raise TimeoutError("health failed")

    monkeypatch.setattr(manager, "wait_healthy", unhealthy)
    with pytest.raises(TimeoutError, match="health failed"):
        await manager.provision(task_id, profile)
    assert fake.containers.items == [existing]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "egress_mode, internal, error",
    [("direct", True, "non-internal"), ("proxy", False, "must be internal")],
)
async def test_wrong_exec_network_is_rejected_before_creation(
    egress_mode: str, internal: bool, error: str
) -> None:
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    fake = Docker()
    fake.networks.network.attrs["Internal"] = internal
    manager = ExecEnvManager(
        settings(egress_mode=egress_mode),
        docker_client=cast(docker.DockerClient, fake),
        objects=cast(ObjectStore, Objects()),
    )
    task_id = uuid4()
    with pytest.raises(RuntimeError, match=error):
        await manager.provision(task_id, profile)
    assert fake.containers.items == []
    with pytest.raises(RuntimeError, match=error):
        await manager.find(task_id)


@pytest.mark.asyncio
async def test_network_mode_fails_fast_when_runtime_is_not_attached(monkeypatch):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    fake = Docker()
    manager = ExecEnvManager(
        settings("network"),
        docker_client=cast(docker.DockerClient, fake),
        objects=cast(ObjectStore, Objects()),
    )
    monkeypatch.setattr(manager_module, "runtime_container_id", lambda: "runtime-container")
    task_id = uuid4()
    with pytest.raises(RuntimeError, match="not attached to agent-runtime"):
        await manager.provision(task_id, profile)
    assert fake.containers.items == []


@pytest.mark.asyncio
async def test_explicit_proxy_mode_injects_proxy_on_internal_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    fake = Docker()
    fake.networks.network.attrs["Internal"] = True
    manager = ExecEnvManager(
        settings(egress_mode="proxy"),
        docker_client=cast(docker.DockerClient, fake),
        objects=cast(ObjectStore, Objects()),
    )

    async def healthy(_handle):
        return None

    monkeypatch.setattr(manager, "wait_healthy", healthy)
    await manager.provision(uuid4(), profile)
    env = fake.containers.items[0].kwargs["environment"]
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[key] == "http://egress-proxy:8888"


@pytest.mark.asyncio
async def test_runtime_audit_is_saved_with_task_and_run_identity(monkeypatch) -> None:
    task = uuid4()
    objects = AsyncMock()
    manager = ExecEnvManager(settings(), docker_client=cast(Any, Docker()), objects=objects)
    handle = manager_module.ExecEnvHandle(task, "container", "envd", "http://envd:8080", "token")
    monkeypatch.setattr(manager, "_role", AsyncMock(return_value=object()))
    monkeypatch.setattr(manager, "find", AsyncMock(return_value=handle))
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.health.return_value = {"status": "ok", "runtime_audit": True}
    client.audit.return_value = {
        "format": "bbx.runtime-audit.v1",
        "commands": '{"agent_id":"agent-1","command":"sudo -n id"}\n',
        "sudo": "agent-1 ; USER=root ; COMMAND=/usr/bin/id\n",
    }
    monkeypatch.setattr(manager_module, "EnvdClient", lambda *_: client)
    result = await manager._archive_audit(task, 3)
    assert result["result_uri"] == f"toolcalls/{task}/runtime-audit-run-3.txt"
    stored = json.loads(objects.put.call_args.args[1])
    assert stored["task_id"] == str(task) and stored["run_number"] == 3
    assert stored["commands"] == client.audit.return_value["commands"]
    client.audit.side_effect = TimeoutError("transport unavailable")
    objects.put.reset_mock()
    with pytest.raises(TimeoutError):
        await manager._archive_audit(task, 3)
    objects.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_and_legacy_runtimes_do_not_invent_audit_records(monkeypatch) -> None:
    task = uuid4()
    objects = AsyncMock()
    objects.exists.return_value = False
    manager = ExecEnvManager(settings(), docker_client=cast(Any, Docker()), objects=objects)
    role = AsyncMock(return_value=None)
    monkeypatch.setattr(manager, "_role", role)
    assert await manager._archive_audit(task, 1) == {
        "status": "unavailable",
        "reason": "runtime_missing",
    }
    objects.exists.return_value = True
    saved = await manager._archive_audit(task, 1)
    assert saved["status"] == "saved" and str(task) in saved["result_uri"]
    role.return_value = object()
    handle = manager_module.ExecEnvHandle(task, "container", "envd", "http://envd:8080", "token")
    monkeypatch.setattr(manager, "find", AsyncMock(return_value=handle))
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.health.return_value = {"status": "ok"}
    monkeypatch.setattr(manager_module, "EnvdClient", lambda *_: client)
    assert await manager._archive_audit(task, 1) == {
        "status": "unavailable",
        "reason": "legacy_runtime",
    }
    client.audit.assert_not_awaited()
    objects.put.assert_not_awaited()


class InputObjects:
    def __init__(self, values):
        self.values = values
        self.reads = []

    async def exists(self, uri):
        return uri in self.values

    async def stream(self, uri):
        self.reads.append(uri)
        yield self.values[uri]


class InputRestore:
    def __init__(self):
        self.ready = False
        self.calls = 0
        self.files = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def restore_status(self):
        return {"restored": self.ready}

    async def restore(self, content):
        self.calls += 1
        chunks = [chunk async for chunk in content]
        raw = zstandard.ZstdDecompressor().decompress(b"".join(chunks), max_output_size=1000000)
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive:
                if member.isfile():
                    file = archive.extractfile(member)
                    assert file is not None
                    self.files[member.name] = file.read()
                    assert member.mode == 0o644
        self.ready = True
        return {"restored": True}


def input_attachment(task_id, body):
    key = str(uuid4())
    return {
        "id": key,
        "filename": "initial.txt",
        "path": f"/workspace/shared/inputs/{key}/initial.txt",
        "uri": f"inputs/{task_id}/{key}/initial.txt",
        "size": len(body),
        "sha256": sha256(body).hexdigest(),
    }


async def test_initial_input_restore_is_idempotent_and_keeps_existing_work(monkeypatch):
    task_id, body = uuid4(), b"user bytes\x00\xff"
    attachment = input_attachment(task_id, body)
    objects = InputObjects({attachment["uri"]: body})
    manager = ExecEnvManager(
        settings(), docker_client=cast(Any, Docker()), objects=cast(ObjectStore, objects)
    )
    handle = manager_module.ExecEnvHandle(task_id, "container", "envd", "http://envd", "token")
    restore = InputRestore()
    monkeypatch.setattr(manager_module, "EnvdClient", lambda *_: restore)
    await manager.ensure_initial_inputs(handle, [attachment])
    member = attachment["path"].removeprefix("/workspace/")
    assert restore.files[member] == body and objects.reads == [attachment["uri"]]
    restore.files["agents/agent-1/work.txt"] = b"agent result"
    restore.files[member] = b"existing recovered work"
    await manager.ensure_initial_inputs(handle, [attachment])
    assert restore.calls == 1 and objects.reads == [attachment["uri"]]
    assert restore.files[member] == b"existing recovered work"
    assert restore.files["agents/agent-1/work.txt"] == b"agent result"


async def test_resumed_restore_takes_priority_over_original_materialization(monkeypatch, tmp_path):
    from bbx_runtime.execenv.archive import build_archive

    task_id, body = uuid4(), b"original"
    attachment = input_attachment(task_id, body)
    evidence_uri = f"evidence/{task_id}/agent-1/proof"
    objects = InputObjects({attachment["uri"]: body, evidence_uri: b"agent result"})
    output = tmp_path / "run.tar.zst"
    await build_archive(
        cast(ObjectStore, objects),
        task_id,
        {
            "format": "bbx.task-archive.v1",
            "task_id": str(task_id),
            "run_number": 1,
            "state": {
                "task": {"initial_attachments": [attachment]},
                "facts": {
                    "F1": {
                        "version": 1,
                        "evidence": [
                            {"uri": evidence_uri, "path": "/workspace/agents/agent-1/proof.txt"}
                        ],
                    }
                },
            },
        },
        output,
    )
    archive_uri = f"workspace/{task_id}.tar.zst"
    objects.values[archive_uri] = output.read_bytes()
    objects.reads.clear()
    restore = InputRestore()
    monkeypatch.setattr(manager_module, "EnvdClient", lambda *_: restore)
    manager = ExecEnvManager(
        settings(), docker_client=cast(Any, Docker()), objects=cast(ObjectStore, objects)
    )
    monkeypatch.setattr(manager, "wait_healthy", AsyncMock())
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    handle = await manager.provision(task_id, profile, archive_uri)
    await manager.ensure_initial_inputs(handle, [attachment])
    assert objects.reads == [archive_uri] and restore.calls == 1
    assert restore.files[attachment["path"].removeprefix("/workspace/")] == body
    assert restore.files["agents/agent-1/proof.txt"] == b"agent result"


@pytest.mark.parametrize("failure", ["missing", "sha256"])
async def test_failed_initial_input_does_not_restore_or_touch_old_files(monkeypatch, failure):
    task_id, body = uuid4(), b"original"
    attachment = input_attachment(task_id, body)
    values = {} if failure == "missing" else {attachment["uri"]: b"modified"}
    objects = InputObjects(values)
    restore = InputRestore()
    restore.files["agents/agent-1/proof.txt"] = b"existing work"
    manager = ExecEnvManager(
        settings(), docker_client=cast(Any, Docker()), objects=cast(ObjectStore, objects)
    )
    handle = manager_module.ExecEnvHandle(task_id, "container", "envd", "http://envd", "token")
    monkeypatch.setattr(manager_module, "EnvdClient", lambda *_: restore)
    with pytest.raises((ValueError, FileNotFoundError)):
        await manager.ensure_initial_inputs(handle, [attachment])
    assert restore.calls == 0 and not restore.ready
    assert restore.files == {"agents/agent-1/proof.txt": b"existing work"}


@pytest.mark.asyncio
async def test_ctf_container_has_trusted_fixed_mode_and_task_identity(monkeypatch):
    from bbx_contracts.ctf import load_ctf_profile

    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    fake = Docker()
    manager = ExecEnvManager(
        settings(),
        docker_client=cast(docker.DockerClient, fake),
        objects=cast(ObjectStore, Objects()),
    )

    async def healthy(_handle):
        return None

    monkeypatch.setattr(manager, "wait_healthy", healthy)
    task_id = uuid4()
    await manager.provision(task_id, profile)
    env = fake.containers.items[0].kwargs["environment"]
    assert env["ENVD_MODE"] == "ctf"
    assert env["ENVD_TASK_ID"] == str(task_id)


@pytest.mark.asyncio
async def test_confirmed_destruction_checks_identity_and_post_remove_absence(monkeypatch):
    from bbx_contracts.ctf import load_ctf_profile

    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    fake = Docker()
    manager = ExecEnvManager(
        settings(),
        docker_client=cast(docker.DockerClient, fake),
        objects=cast(ObjectStore, Objects()),
    )

    async def healthy(_handle):
        return None

    monkeypatch.setattr(manager, "wait_healthy", healthy)
    task_id = uuid4()
    handle = await manager.provision(task_id, profile)
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        await manager.destroy_confirmed(str(uuid4()), handle.container_id)
    container = fake.containers.get(handle.container_id)
    original_remove = container.remove
    monkeypatch.setattr(container, "remove", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await manager.destroy_confirmed(str(task_id), handle.container_id)
    monkeypatch.setattr(container, "remove", original_remove)
    await manager.destroy_confirmed(str(task_id), handle.container_id)
    await manager.destroy_confirmed(str(task_id), handle.container_id)
    assert all(item.id != handle.container_id for item in fake.containers.items)
