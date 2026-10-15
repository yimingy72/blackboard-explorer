"""Create and recover task-isolated envd containers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import socket
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
import httpx
from bbx_contracts.ctf import CtfAgentProfile
from bbx_contracts.models import AgentProfile
from bbx_contracts.traffic import TrafficManifest
from bbx_objects import ObjectStore
from docker.errors import NotFound

from bbx_runtime.clients import EnvdClient, RemoteError, object_store
from bbx_runtime.execenv.archive import (
    ARCHIVE_MAX_BYTES,
    ArchiveCapacityError,
    build_archive,
    initial_attachments,
)
from bbx_runtime.settings import Settings
from bbx_runtime.traffic.collector import TrafficCollector

MANAGED = "bbx.managed"
TASK_ID = "bbx.task-id"
ROLE = "bbx.role"
TRAFFIC_REQUIRED = "bbx.traffic-required"
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
    traffic_required: bool = False


@dataclass(frozen=True)
class ArchiveResult:
    uri: str
    size: int
    fallback: str


def runtime_container_id() -> str | None:
    """Return the local Docker container ID when running inside a container."""
    if not Path("/.dockerenv").exists():
        return None
    return socket.gethostname()


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
        self.traffic_collectors: dict[str, TrafficCollector] = {}

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
        if self.settings.exec_access_mode == "network":
            runtime_id = runtime_container_id()
            containers = network.attrs.get("Containers")
            if (
                runtime_id
                and containers is not None
                and not any(
                    runtime_id == container_id
                    or runtime_id.startswith(container_id)
                    or container_id.startswith(runtime_id)
                    for container_id in containers
                )
            ):
                raise RuntimeError(
                    f"Execution network {self.settings.exec_network} is not attached to "
                    "agent-runtime; set EXEC_NETWORK to the shared Compose exec network"
                )
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

    async def _create_envd(self, task_id: UUID, profile: AgentProfile | CtfAgentProfile) -> Any:
        name = self._name(task_id)
        await self._check_name(name, task_id, "envd")
        network = await self._execution_network()
        token = task_token(self.settings.envd_token_secret.get_secret_value(), task_id)
        environment = {
            "ENVD_TOKEN": token,
            "ENVD_TASK_ID": str(task_id),
            "PRIVILEGED_PREFIXES": ",".join(profile.privileged_allowlist),
            "ENVD_TRAFFIC_ENABLED": str(self.settings.traffic_enabled).lower(),
            "ENVD_TRAFFIC_MAX_BYTES": str(self.settings.traffic_max_bytes),
        }
        if isinstance(profile, CtfAgentProfile):
            environment.update(ENVD_MODE="ctf")
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
            network=getattr(network, "name", self.settings.exec_network),
            labels={
                **self._labels(task_id, "envd"),
                TRAFFIC_REQUIRED: str(self.settings.traffic_enabled).lower(),
            },
            environment=environment,
            cap_add=["NET_ADMIN", *(["SYS_ADMIN"] if self.settings.traffic_enabled else [])],
            sysctls=(
                {"net.ipv4.ip_forward": "1", "net.ipv6.conf.all.forwarding": "1"}
                if self.settings.traffic_enabled
                else {}
            ),
            devices=["/dev/net/tun:/dev/net/tun:rwm"],
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
            traffic_required=envd.labels.get(TRAFFIC_REQUIRED) == "true",
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
        self,
        task_id: UUID | str,
        profile: AgentProfile | CtfAgentProfile,
        restore_uri: str | None = None,
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
                    if result.get("restored") is not True:
                        raise RuntimeError("Task archive restore did not complete")
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

    async def start_traffic(self, handle: ExecEnvHandle, task: dict[str, Any]) -> None:
        """Gate first dispatch on capture readiness and its durable initial manifest."""
        tid, run = str(handle.task_id), int(task.get("run_number") or 1)
        previous = self.traffic_collectors.get(tid)
        if previous is not None:
            if previous.run == run and not previous.stopped:
                return
            await previous.close()
            self.traffic_collectors.pop(tid, None)
        client = EnvdClient(handle.base_url, handle.token)
        try:
            # Existing images without this capability have no recoverable capture history.
            if (await client.health()).get("traffic") is not True:
                if handle.traffic_required:
                    raise RuntimeError(
                        "New execution image lacks required traffic capture capability"
                    )
                await client.close()
                return
            if not self.settings.traffic_enabled:
                status = await client.traffic("status")
                if (
                    not status.get("source_id")
                    or status.get("task_id") != tid
                    or status.get("run", status.get("run_number")) != run
                ):
                    await client.close()
                    return
                # Disabling new capture must not discard an existing source's pending files.
            collector = TrafficCollector(
                client,
                self.objects,
                tid,
                run,
                poll_seconds=self.settings.traffic_poll_seconds,
                max_bytes=self.settings.traffic_max_bytes,
            )
            await collector.start()
        except BaseException:
            await client.close()
            raise
        self.traffic_collectors[tid] = collector

    async def finish_traffic(
        self, task_id: UUID | str, *, discard: bool = False, run_number: int | None = None
    ) -> None:
        tid = str(task_id)
        collector = self.traffic_collectors.get(tid)
        if collector is not None:
            await collector.close(discard=discard)
            self.traffic_collectors.pop(tid, None)
        elif not discard and run_number is not None:
            uri = f"traffic/{tid}/run-{run_number}/manifest.json"
            if await self.objects.exists(uri):
                manifest = TrafficManifest.model_validate_json(await self.objects.get(uri))
                if str(manifest.task_id) != tid or manifest.run != run_number:
                    raise ValueError("Traffic manifest ownership mismatch")
                if manifest.finished_at is None:
                    timestamp = datetime.now(UTC).isoformat()
                    missing = (
                        "Execution capture unavailable; final fragments could not be confirmed"
                    )
                    updated = manifest.model_dump(mode="json")
                    updated.update(
                        status="partial",
                        error=manifest.error or missing,
                        finished_at=timestamp,
                        updated_at=timestamp,
                    )
                    await self.objects.put(
                        uri, json.dumps(updated).encode(), content_type="application/json"
                    )

    async def suspend_traffic(self) -> None:
        for collector in list(self.traffic_collectors.values()):
            try:
                await collector.suspend()
            except Exception as error:
                LOGGER.warning("Traffic shutdown flush deferred (%s)", type(error).__name__)
        self.traffic_collectors.clear()

    async def ensure_initial_inputs(
        self, handle: ExecEnvHandle, attachments: list[dict[str, Any]]
    ) -> None:
        """Materialize registered originals once through the existing safe restore."""
        if not attachments:
            return
        originals = initial_attachments(handle.task_id, attachments)
        async with EnvdClient(handle.base_url, handle.token) as envd:
            if (await envd.restore_status())["restored"]:
                return
            data = {
                "format": "bbx.task-archive.v1",
                "task_id": str(handle.task_id),
                "run_number": 1,
                "state": {"task": {"initial_attachments": originals}, "facts": {}},
            }
            with tempfile.TemporaryDirectory(prefix="bbx-task-inputs-") as temporary:
                output = Path(temporary) / "inputs.tar.zst"
                await build_archive(self.objects, handle.task_id, data, output)
                if output.stat().st_size > ARCHIVE_MAX_BYTES:
                    raise ArchiveCapacityError("Compressed input archive exceeds restore limit")

                async def chunks():
                    with output.open("rb") as file:
                        while chunk := await asyncio.to_thread(file.read, 65536):
                            yield chunk

                result = await envd.restore(chunks())
                if result.get("restored") is not True:
                    raise RuntimeError("Initial attachment restore did not complete")

    async def archive_to_store(
        self, handle: ExecEnvHandle, run_number: int = 1, *, uri: str | None = None
    ) -> ArchiveResult:
        await self.finish_traffic(handle.task_id)
        uri = uri or (
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

    async def archive_task(self, task_id: UUID | str, data: dict[str, Any]) -> ArchiveResult:
        task = self._task(task_id)
        await self.finish_traffic(task)
        run_number = data.get("run_number")
        if not isinstance(run_number, int) or run_number < 1:
            raise ValueError("Invalid archive run number")
        uri = (
            f"workspace/{task}.tar.zst"
            if run_number == 1
            else f"workspace/{task}/run-{run_number}.tar.zst"
        )
        data = {**data, "runtime_audit": await self._archive_audit(task, run_number)}
        with tempfile.TemporaryDirectory(prefix="bbx-task-archive-") as temporary:
            output = Path(temporary) / "workspace.tar.zst"
            await build_archive(self.objects, task, data, output)
            size = output.stat().st_size
            if size > ARCHIVE_MAX_BYTES:
                raise ArchiveCapacityError("Compressed archive exceeds restore limit")
            with output.open("rb") as file:
                await self.objects.put(uri, file, length=size, content_type="application/zstd")
        return ArchiveResult(uri, size, "none")

    async def _archive_audit(self, task: UUID, run_number: int) -> dict[str, Any]:
        uri = f"toolcalls/{task}/runtime-audit-run-{run_number}.txt"
        # A lost container must not prevent archiving evidence already in MinIO.
        if await self._role(task, "envd") is None:
            if await self.objects.exists(uri):
                return {"status": "saved", "result_uri": uri}
            return {"status": "unavailable", "reason": "runtime_missing"}
        handle = await self.find(task)
        if handle is None:
            raise RuntimeError("Execution environment disappeared during audit collection")
        async with EnvdClient(handle.base_url, handle.token) as envd:
            health = await envd.health()
            if health.get("runtime_audit") is not True:
                return {"status": "unavailable", "reason": "legacy_runtime"}
            audit = await envd.audit()
        if audit.get("format") != "bbx.runtime-audit.v1" or not all(
            isinstance(audit.get(key), str) for key in ("commands", "sudo")
        ):
            raise ValueError("Invalid execution environment audit snapshot")
        encoded = json.dumps(
            {**audit, "task_id": str(task), "run_number": run_number}, ensure_ascii=False
        ).encode("utf-8")
        await self.objects.put(uri, encoded, content_type="application/json")
        return {
            "status": "saved",
            "result_uri": uri,
            "size": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    async def destroy(self, task_id: UUID | str) -> None:
        task = self._task(task_id)
        await self.finish_traffic(task)
        for role in ("relay", "envd"):
            container = await self._role(task, role)
            if container is not None:
                await asyncio.to_thread(container.remove, force=True)

    async def destroy_confirmed(self, task_id: str, container_id: str) -> None:
        """Remove exactly the recorded execution container, then verify its absence."""
        task = self._task(task_id)
        await self.finish_traffic(task)
        try:
            container = await asyncio.to_thread(self.docker.containers.get, container_id)
        except NotFound:
            return
        if any(container.labels.get(k) != v for k, v in self._labels(task, "envd").items()):
            raise RuntimeError("CTF replacement container ownership mismatch")
        await asyncio.to_thread(container.remove, force=True)
        try:
            await asyncio.to_thread(self.docker.containers.get, container_id)
        except NotFound:
            return
        raise RuntimeError("CTF execution container destruction is unconfirmed")
