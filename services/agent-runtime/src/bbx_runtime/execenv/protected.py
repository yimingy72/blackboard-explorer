"""Provision and fence the original protected legacy Docker execution run."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import stat
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from bbx_contracts.legacy_network import (
    LEGACY_DOCKER_OWNER,
    LEGACY_NETWORK_VERSION,
    LegacyNetworkAbsentReceipt,
    LegacyNetworkReceipt,
    LegacyNetworkSnapshot,
    load_legacy_network_policy,
    snapshot,
)
from docker.errors import NotFound

from bbx_runtime.clients import EnvdClient

if TYPE_CHECKING:
    from bbx_contracts.models import AgentProfile

    from bbx_runtime.execenv.manager import ExecEnvManager

OWNER, TASK, RUN, POLICY, ROLE = "bbx.managed", "bbx.task-id", "bbx.run", "bbx.policy", "bbx.role"
ROLES = ("envd", "relay", "gateway")
RESERVED = ("0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "198.18.0.0/15", "224.0.0.0/3")
GATEWAY_ROOT = "/run/bbx-gateway"
GATEWAY_CMD = ["/usr/bin/python3", "-I", "-S", "/opt/bbx/gateway.py"]
LIMIT = 256 * 1024


@dataclass
class ProtectedRun:
    task: dict[str, Any]
    network: Any
    envd: Any
    relay: Any = None
    gateway: Any = None


def frozen(task: dict[str, Any]) -> LegacyNetworkSnapshot:
    return LegacyNetworkSnapshot.model_validate(task["legacy_network"])


def original(task: dict[str, Any]) -> LegacyNetworkReceipt | None:
    value = task.get("legacy_network_receipt")
    if value and value.get("phase") != "absent":
        return LegacyNetworkReceipt.model_validate(value)
    return None


def labels(task: dict[str, Any], role: str) -> dict[str, str]:
    return {
        OWNER: LEGACY_DOCKER_OWNER,
        TASK: str(UUID(str(task["id"]))),
        RUN: str(task["run_number"]),
        POLICY: frozen(task).policy_digest,
        ROLE: role,
    }


def name(task: dict[str, Any], role: str) -> str:
    return f"bbx-protected-{UUID(str(task['id'])).hex}-{task['run_number']}-{role}"


def started(container):
    return container.attrs["State"]["StartedAt"]


def attached(container):
    return {
        value["NetworkID"]: value
        for value in container.attrs["NetworkSettings"]["Networks"].values()
    }


def other_network(container, execution):
    if container is None:
        return None
    (identifier,) = set(attached(container)) - {execution.id}
    return identifier


def ipv4(container, network):
    return str(ipaddress.IPv4Address(attached(container)[network.id]["IPAddress"]))


def platform_subnets(candidates, execution, control, *, legacy_names, forced):
    """Do not treat unrelated Docker network names as a default LAN deny policy."""
    result: set[str] = set()
    current = {execution.id, control.id}
    for candidate in (execution, control, *candidates):
        if not forced and (
            candidate.id not in current
            and candidate.name not in legacy_names
            and (candidate.attrs.get("Labels") or {}).get(OWNER) != LEGACY_DOCKER_OWNER
        ):
            continue
        for item in candidate.attrs.get("IPAM", {}).get("Config") or []:
            if item.get("Subnet") and ":" not in item["Subnet"]:
                result.add(str(ipaddress.ip_network(item["Subnet"])))
    return result


def read_credentials(policy):
    if policy.credential_file is None:
        return {}
    fd = os.open(policy.credential_file, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Protected gateway credentials require a private regular file")
        raw = file.read(LIMIT + 1)
    if len(raw) > LIMIT or hashlib.sha256(raw).hexdigest() != policy.credential_sha256:
        raise ValueError("Original protected gateway credentials changed")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Protected gateway credentials are invalid") from None
    return value


class ProtectedExecution:
    def __init__(self, manager: ExecEnvManager) -> None:
        self.manager = manager
        self.docker = manager.docker
        self.settings = manager.settings
        try:
            self.policy = load_legacy_network_policy(self.settings.legacy_network_policy_file)
        except (OSError, ValueError):
            self.policy = None

    def token(self, task):
        secret = self.settings.envd_token_secret.get_secret_value().encode()
        value = f"{LEGACY_NETWORK_VERSION}:{UUID(str(task['id']))}:{task['run_number']}"
        return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()

    async def roles(self, task):
        def collect():
            result = {}
            for role in ROLES:
                expected = labels(task, role)
                items = self.docker.containers.list(
                    all=True,
                    filters={"label": [f"{k}={v}" for k, v in expected.items()]},
                )
                if len(items) > 1:
                    raise RuntimeError("Protected Docker execution identity is ambiguous")
                if items:
                    container = items[0]
                    container.reload()
                    if container.name != name(task, role) or any(
                        container.labels.get(k) != v for k, v in expected.items()
                    ):
                        raise RuntimeError("Protected Docker execution owner changed")
                    result[role] = container
            return result

        return await asyncio.to_thread(collect)

    async def network(self, task, *, role="network", create=False):
        def lookup():
            if role == "control" and self.settings.exec_access_mode == "network":
                configured = self.settings.legacy_control_network
                if not configured or configured == self.settings.exec_network:
                    raise RuntimeError(
                        "Protected execution needs a separate trusted control bridge"
                    )
                network = self.docker.networks.get(configured)
                network.reload()
                if network.attrs["Driver"] != "bridge":
                    raise RuntimeError("Protected control network must be a trusted bridge")
                from bbx_runtime.execenv.manager import runtime_in_container, runtime_network_macs

                if runtime_in_container() and not any(
                    (entry.get("MacAddress") or "").lower() in runtime_network_macs()
                    for entry in network.attrs.get("Containers", {}).values()
                ):
                    raise RuntimeError("Runtime is not attached to the trusted control bridge")
                return network
            isolated = role == "network" and frozen(task).mode != "default"
            try:
                network = self.docker.networks.get(name(task, role))
            except NotFound:
                if not create:
                    return None
                network = self.docker.networks.create(
                    name(task, role),
                    driver="bridge",
                    internal=isolated,
                    enable_ipv6=False,
                    labels=labels(task, role),
                    check_duplicate=True,
                )
            network.reload()
            if (
                network.attrs.get("Internal") is not isolated
                or network.attrs.get("EnableIPv6")
                or network.attrs["Driver"] != "bridge"
                or network.attrs["Labels"] != labels(task, role)
            ):
                raise RuntimeError("Original protected execution bridge changed")
            return network

        return await asyncio.to_thread(lookup)

    async def create(self, task, role, image, **options):
        container = await self.manager._create_container(
            image,
            name=name(task, role),
            labels=labels(task, role),
            detach=True,
            use_config_proxy=False,
            **options,
        )
        await asyncio.to_thread(container.start)
        await asyncio.to_thread(container.reload)
        return container

    def configured(self, task):
        if self.policy is None or frozen(task) != snapshot(self.policy):
            raise RuntimeError("Original protected network publication changed")
        if self.policy.mode == "default" and self.settings.exec_egress_mode != "direct":
            raise RuntimeError("Protected default execution requires an ordinary direct network")
        return self.policy.gateway

    def receipt(
        self, resources, phase, *, capture_ready=False, probe_ready=False, capture_closed=False
    ):
        task = resources.task
        return LegacyNetworkReceipt(
            run_number=task["run_number"],
            policy_digest=frozen(task).policy_digest,
            phase=phase,
            envd_id=resources.envd.id,
            envd_started_at=started(resources.envd),
            network_id=resources.network.id,
            relay_id=resources.relay.id if resources.relay else None,
            relay_started_at=started(resources.relay) if resources.relay else None,
            relay_network_id=other_network(resources.relay, resources.network),
            gateway_id=resources.gateway.id if resources.gateway else None,
            gateway_started_at=started(resources.gateway) if resources.gateway else None,
            gateway_network_id=other_network(resources.gateway, resources.network),
            capture_ready=capture_ready,
            probe_ready=probe_ready,
            capture_closed=capture_closed,
        )

    def bind(self, resources, receipt):
        fields = (
            "envd_id",
            "envd_started_at",
            "network_id",
            "relay_id",
            "relay_started_at",
            "relay_network_id",
            "gateway_id",
            "gateway_started_at",
            "gateway_network_id",
        )
        actual = self.receipt(resources, "prepared")
        if any(getattr(actual, key) != getattr(receipt, key) for key in fields):
            raise RuntimeError("Original protected Docker process or bridge changed")

    def topology(self, resources):
        network, envd, relay, gateway = (
            resources.network,
            resources.envd,
            resources.relay,
            resources.gateway,
        )
        if set(attached(envd)) != {network.id} or envd.attrs["HostConfig"].get("Privileged"):
            raise RuntimeError("Original protected envd has an unapproved network or privilege")
        if gateway and (len(attached(gateway)) != 2 or network.id not in attached(gateway)):
            raise RuntimeError("Original protected gateway topology changed")
        if not relay or len(attached(relay)) != 2 or network.id not in attached(relay):
            raise RuntimeError("Original protected control relay topology changed")
        known = {c.id for c in (envd, relay, gateway) if c}
        if set(network.attrs["Containers"]) != known:
            raise RuntimeError("An unowned endpoint uses the protected execution bridge")

    async def setup(self, task: dict[str, Any], profile: AgentProfile):
        from bbx_runtime.execenv.manager import RELAY

        gate = self.configured(task)
        configured_image = self.settings.legacy_envd_image
        if profile.exec_image != configured_image or (
            configured_image != "bbx-exec-env:latest"
            and not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", configured_image)
        ):
            raise RuntimeError("The selected image is not the trusted protected envd release")
        trusted_image = await asyncio.to_thread(self.docker.images.get, configured_image)
        network = await self.network(task, create=True)
        assert network is not None
        control = await self.network(task, role="control", create=True)
        assert control is not None
        containers = await self.roles(task)
        deny = set(RESERVED) | platform_subnets(
            await asyncio.to_thread(self.docker.networks.list),
            network,
            control,
            legacy_names={self.settings.exec_network, self.settings.legacy_control_network},
            forced=gate is not None,
        )
        if len(deny) > 90:
            raise RuntimeError("Protected execution network inventory exceeds its bound")
        if gate:
            deny.update(gate.deny_cidrs)
            deny.add(gate.upstream.ipv4 + "/32")
            probe = ipaddress.IPv4Address(gate.probe.ipv4)
            if any(probe in ipaddress.ip_network(cidr) for cidr in deny):
                raise RuntimeError("Protected probe overlaps a Docker or control network")
            uplink = await asyncio.to_thread(self.docker.networks.get, gate.uplink_network)
            await asyncio.to_thread(uplink.reload)
            if (
                uplink.id in {network.id, control.id}
                or uplink.attrs.get("Internal")
                or uplink.attrs["Driver"] != "bridge"
            ):
                raise RuntimeError("Original protected gateway uplink is unavailable")
            if "gateway" not in containers:
                endpoints = {
                    uplink.name: self.docker.api.create_endpoint_config(),
                    network.name: self.docker.api.create_endpoint_config(),
                }
                containers["gateway"] = await self.create(
                    task,
                    "gateway",
                    gate.image,
                    network=uplink.name,
                    networking_config=endpoints,
                    cap_drop=["ALL"],
                    cap_add=["NET_ADMIN"],
                    read_only=True,
                    security_opt=["no-new-privileges"],
                    devices=["/dev/net/tun:/dev/net/tun:rwm"] if frozen(task).mode == "vpn" else [],
                    tmpfs={GATEWAY_ROOT: "rw,noexec,nosuid,mode=0700,size=2m"},
                    sysctls={"net.ipv4.ip_forward": "1"},
                    mem_limit="128m",
                    pids_limit=64,
                )
            await asyncio.to_thread(containers["gateway"].reload)
            if set(attached(containers["gateway"])) != {network.id, uplink.id}:
                raise RuntimeError("Original protected gateway networks changed")
        gateway = containers.get("gateway")
        task_id = UUID(str(task["id"]))
        if "envd" not in containers:
            env = {
                "ENVD_TASK_ID": str(task_id),
                "ENVD_TOKEN": self.token(task),
                "ENVD_TRAFFIC_ENABLED": "true",
                "ENVD_TRAFFIC_MAX_BYTES": str(self.settings.traffic_max_bytes),
                "ENVD_PROTECTED_NETWORK": frozen(task).mode,
                "ENVD_PROTECTED_DENY": json.dumps(sorted(deny)),
            }
            if gate:
                env.update(
                    ENVD_PROTECTED_NEXT_HOP=ipv4(gateway, network),
                    ENVD_PROTECTED_ALLOW=json.dumps(gate.allow_cidrs),
                )
            security = ["no-new-privileges"]
            if self.settings.exec_apparmor_profile:
                security.append(f"apparmor={self.settings.exec_apparmor_profile}")
            containers["envd"] = await self.create(
                task,
                "envd",
                trusted_image.id,
                network=network.name,
                environment=env,
                cap_add=["NET_ADMIN", "SYS_ADMIN"],
                security_opt=security,
                sysctls={"net.ipv4.ip_forward": "1", "net.ipv6.conf.all.forwarding": "1"},
                nano_cpus=int(profile.exec_resources.cpus * 1_000_000_000),
                mem_limit=profile.exec_resources.mem,
                pids_limit=max(profile.exec_resources.pids, self.settings.exec_min_pids),
            )
        envd = containers["envd"]
        if envd.status != "running":
            raise RuntimeError("Protected envd will not restart")
        if "relay" not in containers:
            # The existing relay forwards a fixed numeric envd destination only.
            endpoint = {
                net.name: self.docker.api.create_endpoint_config() for net in (control, network)
            }
            if len(endpoint) == 1:
                # An unforced host relay still uses distinct bridge and execution networks.
                raise RuntimeError("Protected relay needs a separate control bridge")
            containers["relay"] = await self.create(
                task,
                "relay",
                trusted_image.id,
                command=["python3", "-I", "-c", RELAY, ipv4(envd, network), "protected"],
                entrypoint=[],
                network=control.name,
                networking_config=endpoint,
                ports={"8080/tcp": ("127.0.0.1", 0)}
                if self.settings.exec_access_mode == "relay"
                else {},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                read_only=True,
                mem_limit="64m",
                pids_limit=64,
            )
        await asyncio.to_thread(network.reload)
        resources = ProtectedRun(task, network, envd, containers.get("relay"), gateway)
        self.topology(resources)
        if other_network(resources.relay, network) != control.id:
            raise RuntimeError("Original protected relay control bridge changed")
        return resources, sorted(deny)

    async def find(self, task, *, terminal=False):
        receipt = original(task)
        if receipt is None:
            return None
        network, containers = await asyncio.gather(self.network(task), self.roles(task))
        envd = containers.get("envd")
        if not network or not envd:
            if terminal and receipt.phase == "exited":
                return None
            raise RuntimeError("Original protected execution environment is missing")
        resources = ProtectedRun(
            task, network, envd, containers.get("relay"), containers.get("gateway")
        )
        self.bind(resources, receipt)
        if not terminal:
            self.topology(resources)
            if receipt.phase != "ready" or any(c.status != "running" for c in containers.values()):
                raise RuntimeError("Original protected execution is not running")
        return resources

    async def handle(self, resources):
        from bbx_runtime.execenv.manager import ExecEnvHandle

        task_id = UUID(str(resources.task["id"]))
        if resources.envd.status != "running" or (
            resources.relay and resources.relay.status != "running"
        ):
            return None
        if self.settings.exec_access_mode == "relay":
            base_url = f"http://127.0.0.1:{await self.manager._relay_port(resources.relay)}"
        else:
            base_url = f"http://{(resources.relay or resources.envd).name}:8080"
        return ExecEnvHandle(
            task_id=task_id,
            container_id=resources.envd.id,
            name=resources.envd.name,
            base_url=base_url,
            token=self.token(resources.task),
            relay_id=resources.relay.id if resources.relay else None,
            traffic_required=True,
            protected=True,
        )

    async def gateway_file(self, gateway, filename):
        until = time.monotonic() + 35
        while time.monotonic() < until:
            await asyncio.to_thread(gateway.reload)
            if gateway.status != "running":
                break
            result = await asyncio.to_thread(
                gateway.exec_run, ["/usr/bin/cat", f"{GATEWAY_ROOT}/{filename}"]
            )
            if result.exit_code == 0 and len(result.output) <= 128:
                return result.output.decode("ascii").strip()
            await asyncio.sleep(0.25)
        raise RuntimeError("Original protected gateway did not become ready")

    def deliver(self, gateway, body):
        # Docker archive writes to tmpfs are refused on a read-only root filesystem.
        # A fixed local Docker exec receives bounded bytes on stdin (never argv/env).
        job = self.docker.api.exec_create(
            gateway.id,
            [*GATEWAY_CMD, "configure"],
            stdin=True,
            stdout=False,
            stderr=False,
        )["Id"]
        stream = self.docker.api.exec_start(job, socket=True)
        try:
            wire = getattr(stream, "_sock", None)
            if not isinstance(wire, socket.socket):
                raise RuntimeError("Original Docker gateway transport is unavailable")
            wire.settimeout(5)
            wire.sendall(json.dumps(body, separators=(",", ":")).encode("ascii"))
            wire.shutdown(socket.SHUT_WR)
            until = time.monotonic() + 8
            while time.monotonic() < until:
                status = self.docker.api.exec_inspect(job)
                if not status["Running"] and status["ExitCode"] is not None:
                    if status["ExitCode"] == 0:
                        return
                    break
                time.sleep(0.1)
        finally:
            stream.close()
        raise RuntimeError("Original protected gateway private start is unconfirmed")

    async def provision(self, task, profile, service):
        gate = self.configured(task)
        tid = str(task["id"])
        observed = original(task)
        if observed is not None:
            if observed.phase == "ready":
                resources = await self.find(task)
                assert resources
                handle = await self.handle(resources)
                if handle is None:
                    raise RuntimeError("Original protected execution is unavailable")
                return handle
            if observed.phase != "prepared":
                raise RuntimeError("Original protected gateway start cannot be replayed")
        if observed:
            resources = await self.find(task, terminal=True)
            assert resources
            self.topology(resources)
            deny = self.execution_deny(resources)
        else:
            resources, deny = await self.setup(task, profile)
        handle = await self.handle(resources)
        if handle is None:
            raise RuntimeError("Original protected control path is unavailable")
        if observed:
            self.bind(resources, observed)
        else:
            await service.legacy_execution_network(tid, self.receipt(resources, "prepared"))
        await self.manager.wait_healthy(handle)
        async with EnvdClient(handle.base_url, handle.token) as envd:
            health = await envd.health()
            if (
                health.get("protected_network") != LEGACY_NETWORK_VERSION
                or health.get("task_id") != tid
                or health.get("network_mode") != frozen(task).mode
                or health.get("traffic") is not True
            ):
                raise RuntimeError("Original protected envd capture capability is unavailable")
            if task.get("resume_workspace_uri"):
                await self.manager.restore(handle, task["resume_workspace_uri"])
            if attachments := task.get("initial_attachments"):
                await self.manager.ensure_initial_inputs(handle, attachments)
            link = None
            if gate:
                gateway = resources.gateway
                assert gateway
                public_key = await self.gateway_file(gateway, "public-key")
                if not re.fullmatch(r"[A-Za-z0-9+/]{43}=", public_key):
                    raise RuntimeError("Original protected gateway public identity is invalid")
                link = await envd.protected_network("link", public_key=public_key)
            # Capture/storage preparation occurs with no forwarding grant.
            await self.manager.start_traffic(handle, task)
            if gate:
                gateway = resources.gateway
                assert gateway and link
                credentials = await asyncio.to_thread(read_credentials, gate)
                claim = await service.legacy_execution_network(
                    tid, self.receipt(resources, "starting")
                )
                if claim.get("launch_allowed") is not True:
                    raise RuntimeError("Original protected gateway start is already claimed")
                await asyncio.to_thread(
                    self.deliver,
                    gateway,
                    {
                        "mode": frozen(task).mode,
                        "envd_ipv4": ipv4(resources.envd, resources.network),
                        "gateway_ipv4": ipv4(gateway, resources.network),
                        "envd_public_key": link["public_key"],
                        "allow": gate.allow_cidrs,
                        "deny": [cidr for cidr in deny if ":" not in cidr],
                        "upstream": gate.upstream.model_dump(mode="json"),
                        "credentials": credentials,
                    },
                )
                await self.gateway_file(gateway, "ready")
            current = (await service.state(tid))["task"]
            if (
                current["status"] != "provisioning"
                or current["run_number"] != task["run_number"]
                or frozen(current) != frozen(task)
            ):
                raise RuntimeError("Original protected startup is no longer authorized")
            await envd.protected_network("renew")
            probe = await envd.protected_network(
                "probe", **(gate.probe.model_dump() if gate else {})
            )
            if probe != {"capture_ready": True, "probe_ready": gate is not None}:
                raise RuntimeError("Original protected Agent path is unconfirmed")
        await service.legacy_execution_network(
            tid,
            self.receipt(
                resources,
                "ready",
                capture_ready=True,
                probe_ready=gate is not None,
            ),
        )
        return handle

    async def renew(self, task, handle):
        resources = await self.find(task)
        if not resources or resources.envd.id != handle.container_id:
            raise RuntimeError("Original protected execution cannot be renewed")
        if resources.gateway:
            result = await asyncio.to_thread(resources.gateway.exec_run, [*GATEWAY_CMD, "renew"])
            if result.exit_code != 0:
                raise RuntimeError("Original protected gateway lease was revoked")
        async with EnvdClient(handle.base_url, handle.token) as envd:
            if await envd.protected_network("renew") != {"renewed": True}:
                raise RuntimeError("Original protected envd lease was revoked")

    @staticmethod
    def execution_deny(resources):
        env = resources.envd.attrs["Config"]["Env"]
        (value,) = [
            entry.split("=", 1)[1] for entry in env if entry.startswith("ENVD_PROTECTED_DENY=")
        ]
        return json.loads(value)

    async def stop_container(self, container):
        await asyncio.to_thread(container.reload)
        if container.status == "running":
            await asyncio.to_thread(container.stop, timeout=2)
            await asyncio.to_thread(container.reload)
        if container.status != "exited" or not container.attrs["State"].get(
            "FinishedAt", ""
        ).startswith("20"):
            raise RuntimeError("Original protected container exit is unconfirmed")

    async def fence(self, task, service):
        previous = original(task)
        if previous is None or previous.phase == "exited":
            return None
        resources = await self.find(task, terminal=True)
        assert resources
        if previous.phase == "fenced":
            return await self.handle(resources)
        if resources.gateway:
            await self.stop_container(resources.gateway)
        handle = await self.handle(resources)
        if handle:
            try:
                async with EnvdClient(handle.base_url, handle.token) as envd:
                    async with asyncio.timeout(5):
                        if await envd.protected_network("fence") != {"fenced": True}:
                            raise RuntimeError("Protected envd did not fence")
            except Exception:
                await self.stop_container(resources.envd)
                handle = None
        else:
            await self.stop_container(resources.envd)
        await service.legacy_execution_network(
            str(task["id"]), previous.model_copy(update={"phase": "fenced"})
        )
        return handle

    async def destroy(self, task, service, *, capture_closed):
        previous = original(task)
        tid = str(task["id"])
        network, containers = await asyncio.gather(self.network(task), self.roles(task))
        control = (
            await self.network(task, role="control")
            if self.settings.exec_access_mode == "relay"
            else None
        )
        if previous:
            if network and network.id != previous.network_id:
                raise RuntimeError("Original protected execution bridge was replaced")
            if control and control.id != previous.relay_network_id:
                raise RuntimeError("Original protected control bridge was replaced")
            for role, container in containers.items():
                if container.id != getattr(previous, role + "_id") or started(container) != getattr(
                    previous, role + "_started_at"
                ):
                    raise RuntimeError("Original protected container was replaced")
        if previous and previous.phase != "exited":
            if not network or "envd" not in containers:
                raise RuntimeError("Original protected process exited without positive proof")
            resources = ProtectedRun(
                task,
                network,
                containers["envd"],
                containers.get("relay"),
                containers.get("gateway"),
            )
            self.bind(resources, previous)
        for role in ("gateway", "envd", "relay"):
            if container := containers.get(role):
                await self.stop_container(container)
        if previous:
            await service.legacy_execution_network(
                tid,
                previous.model_copy(
                    update={
                        "phase": "exited",
                        "capture_closed": capture_closed or previous.capture_closed,
                    }
                ),
            )
        for container in containers.values():
            await asyncio.to_thread(container.remove, v=True)
        if network:
            await asyncio.to_thread(network.remove)
        if control:
            await asyncio.to_thread(control.remove)
        if not previous:
            await service.legacy_execution_network(
                tid,
                LegacyNetworkAbsentReceipt(
                    run_number=task["run_number"],
                    policy_digest=frozen(task).policy_digest,
                    phase="absent",
                ),
            )
