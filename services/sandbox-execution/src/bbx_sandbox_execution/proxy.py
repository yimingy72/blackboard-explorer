"""Assignment/source-bound identity at the existing mitmproxy protocol boundary."""

from __future__ import annotations

import fcntl
import hmac
import ipaddress
import json
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bbx_sandbox_execution.execution import AssignmentBinding, CommandRequest


def issue_proxy_grant(
    path: Path,
    binding: AssignmentBinding,
    request: CommandRequest,
    destinations: list[str],
    control_addresses: list[str],
) -> str:
    """The trusted host publishes this private file; the solver receives only its token."""
    lock_fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return _write_proxy_grant(path, binding, request, destinations, control_addresses)
    finally:
        os.close(lock_fd)


def _write_proxy_grant(
    path: Path,
    binding: AssignmentBinding,
    request: CommandRequest,
    destinations: list[str],
    control_addresses: list[str],
) -> str:
    if not control_addresses:
        raise ValueError("The actual task control addresses are required")
    for destination in destinations:
        ipaddress.ip_network(destination)
    denied = ["127.0.0.0/8", "::1/128", "169.254.0.0/16", "fe80::/10", *control_addresses]
    for value in denied:
        ipaddress.ip_network(value)
    records = json.loads(path.read_text()) if path.exists() else {}
    if request.command_id in records:
        raise ValueError("A proxy grant already exists for this command")
    password = secrets.token_urlsafe(24)
    records[request.command_id] = {
        "password": password,
        "source_ip": binding.ipv4,
        "task_id": binding.task_id,
        "agent_id": binding.agent_id,
        "assignment_id": binding.assignment_id,
        "generation": binding.generation,
        "epoch": binding.epoch,
        "network_identity": binding.network_identity,
        "command_id": request.command_id,
        "destinations": destinations,
        "denied_destinations": denied,
        "expires_at": time.time() + request.timeout_seconds,
    }
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(records, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return password


def proxy_identity(records: dict[str, Any], auth: Any, source_ip: str) -> dict[str, Any] | None:
    if not isinstance(auth, (tuple, list)) or len(auth) != 2:
        return None
    identity = records.get(auth[0])
    if (
        not isinstance(identity, dict)
        or not isinstance(auth[1], str)
        or not hmac.compare_digest(identity["password"], auth[1])
        or identity["expires_at"] <= time.time()
    ):
        return None
    try:
        if _address(source_ip) != _address(identity["source_ip"]):
            return None
    except ValueError:
        return None
    return {key: value for key, value in identity.items() if key != "password"}


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def proxy_destination_allowed(identity: dict[str, Any], host: str, port: int) -> bool:
    # P1 proves numeric target forwarding only; DNS/TLS policy requires the G1 deployment seam.
    try:
        address = _address(host)
    except ValueError:
        return False
    if (
        address.is_unspecified
        or not 1 <= port <= 65535
        or any(address in ipaddress.ip_network(value) for value in identity["denied_destinations"])
    ):
        return False
    return any(address in ipaddress.ip_network(value) for value in identity["destinations"])
