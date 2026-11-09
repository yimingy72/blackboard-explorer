"""Original Sandbox's fixed WireGuard link and one trusted low-UID canary."""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

from bbx_contracts.runner_vpn import (
    VPN_DYNAMIC_PROFILE,
    NativeTaskVpnNetwork,
    NativeVpnLink,
    NativeVpnOperation,
    NativeVpnTunnel,
)
from opensandbox import Sandbox
from opensandbox.models.execd import ExecutionHandlers, ExecutionInit, RunCommandOpts

CONTROL = "/opt/bbx/control"


async def root_vpn(sandbox: Sandbox, action, body):
    if action not in {"key", "status", "peer", "probe", "probe-status", "fence"}:
        raise ValueError("Unknown original Task link action")
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()
    if len(raw) > 8192:
        raise ValueError("Original Task link proof exceeds its scope bound")
    observed = None

    async def initialized(event: ExecutionInit):
        nonlocal observed
        if not event.id or observed not in {None, event.id}:
            raise ValueError("Original Task link execution identity changed")
        observed = event.id

    async with asyncio.timeout(20):
        result = await sandbox.commands.run(
            [
                "/usr/bin/env",
                "-i",
                "/usr/bin/python3",
                "-I",
                "-S",
                CONTROL + "/task-vpn.py",
                "sandbox",
                action,
                raw,
            ],
            opts=RunCommandOpts(working_directory=CONTROL, timeout=timedelta(seconds=18)),
            handlers=ExecutionHandlers(on_init=initialized),
        )
    text = "".join(item.text for item in result.logs.stdout)
    if (
        not observed
        or result.id != observed
        or result.complete is None
        or result.exit_code != 0
        or result.error is not None
        or len(text) > 4096
    ):
        raise ValueError("Original Task link result is unconfirmed")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Invalid original Task link result")
    return value


async def supervise_sandbox_vpn(host, sandbox):
    deadline = datetime.fromisoformat(host.config["hard_deadline"])
    dynamic = host.config.get("vpn_profile") == VPN_DYNAMIC_PROFILE
    key_attempted = probe_attempted = False
    operation_id = None
    while datetime.now(UTC) < deadline:
        try:
            view = await host.control("GET", "vpn")
            state = view["state"]
            if dynamic and view.get("native_network") == "default":
                default = view.get("default_network")
                if default:
                    if operation_id is not None:
                        raise ValueError("Original Task default network identity changed")
                    host.observe_default_network(default)
                await asyncio.sleep(0.8)
                continue
            if not view.get("vpn_operation_id") or (
                dynamic
                and state in {"idle", "source_pending", "acquiring", "awaiting_policy", "requested"}
            ):
                await asyncio.sleep(1)
                continue
            network = view.get("network")
            if dynamic and not network:
                await asyncio.sleep(1)
                continue
            if dynamic:
                network = NativeTaskVpnNetwork.model_validate(network).model_dump(
                    mode="json", exclude_none=True
                )
            if dynamic and host.default_network is not None:
                raise ValueError("Original Task default network cannot become a VPN")
            preflight = network["preflight"] if dynamic else host.config["traffic_targets"][0]
            operation = NativeVpnOperation.model_validate(
                {
                    "scope": view["scope"],
                    "vpn_operation_id": view["vpn_operation_id"],
                    **({"network_digest": network["network_digest"]} if dynamic else {}),
                }
            )
            scope = operation.scope.model_dump(mode="json")
            for field in ("task_id", "operation_id", "sandbox_pod_uid"):
                if scope[field] != host.config[field]:
                    raise ValueError("Original Task link Sandbox scope changed")
            context = scope.get("sandbox_execution_context_id")
            if context and context != host.config["execution_context_id"]:
                raise ValueError("Original Task link execution context changed")
            if operation_id not in {None, operation.vpn_operation_id}:
                raise ValueError("A new VPN login cannot replace the original Task link")
            operation_id = operation.vpn_operation_id
            body = operation.model_dump(mode="json", exclude_none=True)
            if dynamic:
                if host.vpn_network not in (None, network):
                    raise ValueError("Original Task VPN network configuration changed")
                host.vpn_network = network
            if state in {"disconnecting", "disconnected", "failed", "unknown", "closed", "stopped"}:
                host.vpn_link = None
                await root_vpn(sandbox, "fence", body)
            elif state != "held":
                value = await root_vpn(sandbox, "status", body)
                if value.get("state") == "absent":
                    if key_attempted:
                        raise ValueError("Original Task link key creation is unconfirmed")
                    key_attempted = True
                    value = await root_vpn(sandbox, "key", body)
                link = NativeVpnLink.model_validate(value).model_dump(mode="json")
                await host.control("POST", "vpn/link", {**body, "link": link})
                remote = view.get("remote_public_key")
                if remote:
                    with_peer = {
                        **body,
                        "remote_public_key": remote,
                        **(
                            {"preflight": preflight, "route_policy": network["route_policy"]}
                            if dynamic
                            else {"targets": host.config["traffic_targets"]}
                        ),
                    }
                    installed = NativeVpnLink.model_validate(
                        await root_vpn(sandbox, "peer", with_peer)
                    ).model_dump(mode="json")
                    if installed != link:
                        raise ValueError("Original Task link installation changed")
                    host.vpn_link = {
                        "vpn_operation_id": str(operation_id),
                        "link_ifindex": link["ifindex"],
                        **({"network_digest": network["network_digest"]} if dynamic else {}),
                    }
                    if view.get("can_probe"):
                        tunnel = NativeVpnTunnel.model_validate(view["tunnel"]).model_dump(
                            mode="json"
                        )
                        proof = await root_vpn(sandbox, "probe-status", body)
                        if proof.get("state") == "absent":
                            if probe_attempted:
                                raise ValueError("Original Task link canary is unconfirmed")
                            probe_attempted = True
                            proof = await root_vpn(sandbox, "probe", with_peer)
                        if proof.get("link") != link or type(proof.get("passed")) is not bool:
                            raise ValueError("Original Task link canary receipt changed")
                        await host.control(
                            "POST",
                            "vpn/preflight",
                            {
                                **body,
                                **preflight,
                                "link": link,
                                "tunnel": tunnel,
                                "passed": proof["passed"],
                            },
                        )
        except Exception:
            # BB and the two kernel gates deny a stale or unresolved original link.
            pass
        await asyncio.sleep(0.8)
