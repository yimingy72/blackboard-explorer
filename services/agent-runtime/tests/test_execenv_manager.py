"""Task labels, isolation settings, and idempotent lifecycle without Docker."""

from __future__ import annotations

import builtins
import hmac
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import docker
import pytest
from bbx_contracts.profile import load_profile
from bbx_objects import ObjectStore
from bbx_runtime.execenv import ExecEnvManager, task_token
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
            if item.name == name:
                return item
        raise NotFound("not found")

    def create(self, _image: str, **kwargs: Any) -> Container:
        item = Container(self, kwargs)
        self.items.append(item)
        return item


class Network:
    def __init__(self) -> None:
        self.connected: list[str] = []
        self.attrs = {"Internal": False}

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
    assert envd.kwargs["cap_drop"] == ["ALL"]
    assert "KILL" in envd.kwargs["cap_add"]
    assert envd.kwargs["environment"]["PRIVILEGED_PREFIXES"] == ""
    assert not any("PROXY" in key.upper() for key in envd.kwargs["environment"])
    assert envd.kwargs["security_opt"] == ["no-new-privileges"]
    assert envd.kwargs["nano_cpus"] == 2_000_000_000
    assert relay.kwargs["network"] == "bridge"
    assert relay.kwargs["ports"] == {"8080/tcp": ("127.0.0.1", 0)}
    assert relay.kwargs["cap_drop"] == ["ALL"]
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
