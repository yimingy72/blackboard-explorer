"""Protected legacy execution network policy and original runtime receipts."""

import hashlib
import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .models import ContractModel
from .runner import Digest
from .runner_vpn import NativeTaskVpnPeer, NativeVpnTarget

LEGACY_NETWORK_VERSION = "legacy-agent-network-v1"
LEGACY_NETWORK_HEADER = "x-bbx-legacy-execution"
LEGACY_DOCKER_OWNER = "agent-runtime-legacy-network"
LegacyNetworkMode = Literal["default", "vpn", "socks5"]
LegacyNetworkPhase = Literal["prepared", "starting", "ready", "fenced", "exited", "unknown"]
DOCKER_ID = r"^[0-9a-f]{64}$"
DOCKER_STARTED_AT = r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$"
POLICY_LIMIT = 32768
GATEWAY_CREDENTIAL_LIMIT = 256 * 1024


class LegacyGatewayProbe(NativeVpnTarget):
    scheme: Literal["http", "https"] = "http"
    path: str = Field(default="/", pattern=r"^/[A-Za-z0-9._~/-]{0,255}$")
    status: int = Field(default=200, ge=200, le=599, strict=True)
    server_name: str | None = Field(default=None, min_length=1, max_length=253)

    @field_validator("path")
    @classmethod
    def absolute_origin_path(cls, value):
        if value.startswith("//") or any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("Expected a bounded HTTP health path on the original origin")
        return value

    @field_validator("server_name")
    @classmethod
    def exact_host_name(cls, value):
        if value is not None and not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
            for part in value.split(".")
        ):
            raise ValueError("Expected an ASCII HTTP/TLS hostname")
        return value


class LegacyGatewayPolicy(ContractModel):
    image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    uplink_network: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    upstream: NativeTaskVpnPeer
    allow_cidrs: list[str] = Field(min_length=1, max_length=32)
    deny_cidrs: list[str] = Field(min_length=1, max_length=32)
    probe: LegacyGatewayProbe
    credential_file: str | None = Field(default=None, min_length=1, max_length=512)
    credential_sha256: Digest | None = None

    @field_validator("allow_cidrs", "deny_cidrs")
    @classmethod
    def canonical_cidrs(cls, values):
        networks = [ipaddress.IPv4Network(value) for value in values]
        if values != [str(value) for value in sorted(set(networks))]:
            raise ValueError("Expected distinct canonical sorted IPv4 networks")
        return values

    @model_validator(mode="after")
    def fixed_authority(self):
        if (self.credential_file is None) != (self.credential_sha256 is None):
            raise ValueError("A private network credential needs an exact digest")
        if self.credential_file:
            path = Path(self.credential_file)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or any(ord(char) < 32 for char in self.credential_file)
            ):
                raise ValueError("Private network credentials require a fixed absolute path")
        probe = ipaddress.IPv4Address(self.probe.ipv4)
        if (
            self.probe.ipv4 == self.upstream.ipv4
            or not any(probe in ipaddress.IPv4Network(block) for block in self.allow_cidrs)
            or any(probe in ipaddress.IPv4Network(block) for block in self.deny_cidrs)
        ):
            raise ValueError("The legacy network probe is outside the allowed solver path")
        return self


class LegacyNetworkPolicy(ContractModel):
    mode: LegacyNetworkMode = "default"
    gateway: LegacyGatewayPolicy | None = None

    @model_validator(mode="after")
    def selected_gateway(self):
        if self.mode == "default":
            if self.gateway is not None:
                raise ValueError("Default execution must not configure a private gateway")
        elif self.gateway is None:
            raise ValueError("Configured legacy execution requires a fixed gateway")
        elif self.mode == "vpn":
            if not self.gateway.upstream.server_name or not self.gateway.credential_file:
                raise ValueError("VPN requires a verified server name and private configuration")
        elif self.gateway.upstream.server_name is not None:
            raise ValueError("SOCKS5 does not accept an OpenVPN server name")
        return self


class LegacyNetworkSnapshot(ContractModel):
    version: Literal["legacy-agent-network-v1"] = LEGACY_NETWORK_VERSION
    mode: LegacyNetworkMode
    policy_digest: Digest


class LegacyNetworkReceipt(ContractModel):
    run_number: int = Field(ge=1, strict=True)
    policy_digest: Digest
    phase: LegacyNetworkPhase
    envd_id: str = Field(pattern=DOCKER_ID)
    envd_started_at: str = Field(pattern=DOCKER_STARTED_AT)
    network_id: str = Field(pattern=DOCKER_ID)
    relay_id: str | None = Field(default=None, pattern=DOCKER_ID)
    relay_started_at: str | None = Field(default=None, pattern=DOCKER_STARTED_AT)
    relay_network_id: str | None = Field(default=None, pattern=DOCKER_ID)
    gateway_id: str | None = Field(default=None, pattern=DOCKER_ID)
    gateway_started_at: str | None = Field(default=None, pattern=DOCKER_STARTED_AT)
    gateway_network_id: str | None = Field(default=None, pattern=DOCKER_ID)
    capture_ready: bool = False
    probe_ready: bool = False
    capture_closed: bool = False

    @model_validator(mode="after")
    def exact_boot_pairs(self):
        for identifier, started, second_network in (
            (self.relay_id, self.relay_started_at, self.relay_network_id),
            (self.gateway_id, self.gateway_started_at, self.gateway_network_id),
        ):
            if len({value is None for value in (identifier, started, second_network)}) != 1:
                raise ValueError(
                    "Protected containers require original start time and second bridge"
                )
        ids = [value for value in (self.envd_id, self.relay_id, self.gateway_id) if value]
        networks = [
            value
            for value in (self.network_id, self.relay_network_id, self.gateway_network_id)
            if value
        ]
        if len(set(ids)) != len(ids) or len(set(networks)) != len(networks):
            raise ValueError("Protected execution roles require distinct original identities")
        return self


class LegacyNetworkAbsentReceipt(ContractModel):
    run_number: int = Field(ge=1, strict=True)
    policy_digest: Digest
    phase: Literal["absent"]


class LegacyNetworkView(ContractModel):
    mode: LegacyNetworkMode
    state: Literal[
        "pending", "prepared", "starting", "ready", "fenced", "exited", "unknown", "absent"
    ]
    capture_required: Literal[True] = True


def load_legacy_network_policy(path: Path | None) -> LegacyNetworkPolicy:
    if path is None:
        return LegacyNetworkPolicy()
    with path.open("rb") as source:
        raw = source.read(POLICY_LIMIT + 1)
    if len(raw) > POLICY_LIMIT:
        raise ValueError("Legacy network publication exceeds its byte limit")
    return LegacyNetworkPolicy.model_validate_json(raw)


def read_gateway_credentials(policy: LegacyGatewayPolicy) -> dict[str, str]:
    if policy.credential_file is None:
        return {}
    fd = os.open(policy.credential_file, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Protected gateway credentials require a private regular file")
        raw = file.read(GATEWAY_CREDENTIAL_LIMIT + 1)
    if (
        len(raw) > GATEWAY_CREDENTIAL_LIMIT
        or hashlib.sha256(raw).hexdigest() != policy.credential_sha256
    ):
        raise ValueError("Original protected gateway credentials changed")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Protected gateway credentials are invalid") from None
    return value


def snapshot(policy: LegacyNetworkPolicy) -> LegacyNetworkSnapshot:
    canonical = json.dumps(
        policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return LegacyNetworkSnapshot(
        mode=policy.mode, policy_digest=hashlib.sha256(canonical.encode()).hexdigest()
    )
