"""Create and recover task-isolated envd containers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import docker
import httpx
from bbx_contracts.models import AgentProfile
from bbx_objects import ObjectStore
from docker.errors import NotFound

from bbx_runtime.clients import EnvdClient, RemoteError, object_store
from bbx_runtime.settings import Settings

MANAGED = "bbx.managed"
TASK_ID = "bbx.task-id"
ROLE = "bbx.role"
CAPABILITIES = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL"]
LOGGER = logging.getLogger(__name__)

# One fixed destination per relay, supplied when the container starts.
RELAY = """
import select
import socket
import socketserver
import sys

target = sys.argv[1]

class Forward(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection((target, 8080), timeout=10) as upstream:
            upstream.settimeout(None)
            peers = {self.request: upstream, upstream: self.request}
            while True:
                ready, _, _ = select.select(tuple(peers), [], [], 30)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)

socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer.daemon_threads = True
with socketserver.ThreadingTCPServer(('0.0.0.0', 8080), Forward) as server:
    server.serve_forever()
"""


def task_token(secret: str, task_id: UUID | str) -> str:
    task = str(UUID(str(task_id)))
    return hmac.new(secret.encode("utf-8"), task.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ExecEnvHandle:
    task_id: UUID
    container_id: str
    name: str
    base_url: str
    token: str = field(repr=False)
    relay_id: str | None = None


@dataclass(frozen=True)
class ArchiveResult:
    uri: str
    size: int
    fallback: str


class ExecEnvManager:
    def __init__(
        self,
        settings: Settings,
        *,
        docker_client: docker.DockerClient | None = None,
        objects: ObjectStore | None = None,
    ) -> None:
        self.settings = settings
        self.docker = docker_client or docker.from_env()
        self.objects = objects or object_store(settings)

    @staticmethod
    def _task(task_id: UUID | str) -> UUID:
        return UUID(str(task_id))

    @staticmethod
    def _name(task_id: UUID) -> str:
        return f"bbx-exec-{task_id.hex[:8]}"

    @staticmethod
    def _relay_name(task_id: UUID) -> str:
        return f"bbx-exec-relay-{task_id.hex[:8]}"

    @staticmethod
    def _labels(task_id: UUID, role: str) -> dict[str, str]:
        return {MANAGED: "agent-runtime", TASK_ID: str(task_id), ROLE: role}

    async def _execution_network(self) -> Any:
        network = await asyncio.to_thread(self.docker.networks.get, self.settings.exec_network)
        await asyncio.to_thread(network.reload)
        required_internal = self.settings.exec_egress_mode == "proxy"
        if network.attrs.get("Internal") is not required_internal:
            expected = "internal" if required_internal else "non-internal"
            raise RuntimeError(f"Execution network {self.settings.exec_network} must be {expected}")
        return network

    async def _role(self, task_id: UUID, role: str) -> Any | None:
        found = await asyncio.to_thread(
            self.docker.containers.list,
            all=True,
            filters={
                "label": [f"{MANAGED}=agent-runtime", f"{TASK_ID}={task_id}", f"{ROLE}={role}"]
            },
        )
        if len(found) > 1:
            raise RuntimeError(f"Multiple {role} containers for task {task_id}")
        return found[0] if found else None

    async def _check_name(self, name: str, task_id: UUID, role: str) -> None:
        try:
            existing = await asyncio.to_thread(self.docker.containers.get, name)
        except NotFound:
            return
        if any(
            existing.labels.get(key) != value for key, value in self._labels(task_id, role).items()
        ):
            raise RuntimeError(f"Container name {name} is owned by another task")
        raise RuntimeError(f"Container {name} exists but could not be found by its task labels")

    async def _running(self, container: Any) -> None:
        await asyncio.to_thread(container.reload)
        if container.status != "running":
            await asyncio.to_thread(container.start)

    async def _create_envd(self, task_id: UUID, profile: AgentProfile) -> Any:
        name = self._name(task_id)
        await self._check_name(name, task_id, "envd")
        token = task_token(self.settings.envd_token_secret.get_secret_value(), task_id)
        environment = {
            "ENVD_TOKEN": token,
            "PRIVILEGED_PREFIXES": ",".join(profile.privileged_allowlist),
        }
        if self.settings.exec_egress_mode == "proxy":
            proxy = self.settings.egress_proxy_url
            if not proxy:
                raise RuntimeError("Proxy egress requires EGRESS_PROXY_URL")
            environment.update(
                {key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
            )
        container = await asyncio.to_thread(
            self.docker.containers.create,
            profile.exec_image,
            name=name,
            detach=True,
            network=self.settings.exec_network,
            labels=self._labels(task_id, "envd"),
            environment=environment,
            cap_drop=["ALL"],
            cap_add=CAPABILITIES,
            security_opt=["no-new-privileges"],
            read_only=False,
            nano_cpus=int(profile.exec_resources.cpus * 1_000_000_000),
            mem_limit=profile.exec_resources.mem,
            pids_limit=profile.exec_resources.pids,
        )
        try:
            await asyncio.to_thread(container.start)
        except BaseException:
            await asyncio.to_thread(container.remove, force=True)
            raise
        return container

    async def _create_relay(self, task_id: UUID, envd_name: str) -> Any:
        name = self._relay_name(task_id)
        await self._check_name(name, task_id, "relay")
        relay = await asyncio.to_thread(
            self.docker.containers.create,
            "bbx-exec-env:latest",
            command=["python3", "-c", RELAY, envd_name],
            name=name,
            detach=True,
            network="bridge",
            ports={"8080/tcp": ("127.0.0.1", 0)},
            labels=self._labels(task_id, "relay"),
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            read_only=True,
        )
        try:
            await asyncio.to_thread(relay.start)
            network = await self._execution_network()
            assert relay.id is not None
            await asyncio.to_thread(network.connect, relay.id)
        except BaseException:
            await asyncio.to_thread(relay.remove, force=True)
            raise
        return relay

    async def _relay_port(self, relay: Any) -> int:
        await asyncio.to_thread(relay.reload)
        ports = relay.attrs["NetworkSettings"]["Ports"].get("8080/tcp") or []
        if len(ports) != 1 or ports[0].get("HostIp") != "127.0.0.1":
            raise RuntimeError("Relay must publish exactly one localhost port")
        return int(ports[0]["HostPort"])

    async def _handle(self, task_id: UUID, envd: Any, relay: Any | None) -> ExecEnvHandle:
        base_url = f"http://{envd.name}:8080"
        if self.settings.exec_access_mode == "relay":
            if relay is None:
                raise RuntimeError("Host access requires a task relay")
            base_url = f"http://127.0.0.1:{await self._relay_port(relay)}"
        return ExecEnvHandle(
            task_id=task_id,
            container_id=envd.id,
            name=envd.name,
            base_url=base_url,
            token=task_token(self.settings.envd_token_secret.get_secret_value(), task_id),
            relay_id=relay.id if relay is not None else None,
        )

    async def wait_healthy(self, handle: ExecEnvHandle, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        async with EnvdClient(handle.base_url, handle.token) as envd:
            while True:
                try:
                    if (await envd.health()).get("status") == "ok":
                        return
                except (httpx.HTTPError, RemoteError):
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"envd did not become healthy for task {handle.task_id}")
                await asyncio.sleep(0.25)

    async def provision(
        self, task_id: UUID | str, profile: AgentProfile, restore_uri: str | None = None
    ) -> ExecEnvHandle:
        task = self._task(task_id)
        await self._execution_network()
        created: list[Any] = []
        try:
            envd = await self._role(task, "envd")
            if envd is None:
                envd = await self._create_envd(task, profile)
                created.append(envd)
            else:
                await self._running(envd)
            relay = None
            if self.settings.exec_access_mode == "relay":
                relay = await self._role(task, "relay")
                if relay is None:
                    relay = await self._create_relay(task, envd.name)
                    created.append(relay)
                else:
                    await self._running(relay)
            handle = await self._handle(task, envd, relay)
            await self.wait_healthy(handle)
            if restore_uri:
                async with EnvdClient(handle.base_url, handle.token) as client:
                    status = await client.restore_status()
                    if not status["restored"]:
                        result = await client.restore(self.objects.stream(restore_uri))
                    else:
                        result = status
                    if result.get("skipped_links"):
                        LOGGER.warning(
                            "Task %s restored without %s archive links",
                            task,
                            result["skipped_links"],
                        )
            return handle
        except BaseException:
            for container in reversed(created):
                await asyncio.to_thread(container.remove, force=True)
            raise

    async def find(self, task_id: UUID | str) -> ExecEnvHandle | None:
        task = self._task(task_id)
        await self._execution_network()
        envd = await self._role(task, "envd")
        if envd is None:
            return None
        await self._running(envd)
        relay = None
        if self.settings.exec_access_mode == "relay":
            relay = await self._role(task, "relay")
            if relay is None:
                relay = await self._create_relay(task, envd.name)
            else:
                await self._running(relay)
        return await self._handle(task, envd, relay)

    async def create_user(self, handle: ExecEnvHandle, agent_id: str) -> dict[str, str]:
        async with EnvdClient(handle.base_url, handle.token) as envd:
            return await envd.create_user(agent_id)

    async def archive_to_store(self, handle: ExecEnvHandle, run_number: int = 1) -> ArchiveResult:
        uri = (
            f"workspace/{handle.task_id}.tar.zst"
            if run_number == 1
            else f"workspace/{handle.task_id}/run-{run_number}.tar.zst"
        )
        async with EnvdClient(handle.base_url, handle.token) as envd:
            async with envd.archive_stream() as response:
                with tempfile.TemporaryFile() as output:
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        await asyncio.to_thread(output.write, chunk)
                    await asyncio.to_thread(output.seek, 0)
                    await self.objects.put(
                        uri, output, length=size, content_type="application/zstd"
                    )
                    return ArchiveResult(
                        uri, size, response.headers.get("X-Archive-Fallback", "none")
                    )

    async def destroy(self, task_id: UUID | str) -> None:
        task = self._task(task_id)
        for role in ("relay", "envd"):
            container = await self._role(task, role)
            if container is not None:
                await asyncio.to_thread(container.remove, force=True)
