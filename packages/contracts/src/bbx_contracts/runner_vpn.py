"""Frozen deployment and original identity contracts for one Task VPN session."""

import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .models import ContractModel
from .runner import Digest

VPN_PROFILE = "openvpn-ipv4-tcp-v1"
VPN_DYNAMIC_PROFILE = "openvpn-task-ipv4-tcp-v1"
VPN_PROFILES = {VPN_PROFILE, VPN_DYNAMIC_PROFILE}
VPN_SOURCE_LIMIT = 128 * 1024
NETWORK_USE_DEFAULT = "network_use_default"
VPN_CONFIGURE = "vpn_configure"
VPN_STATUS = "vpn_status"
VPN_DISCONNECT = "vpn_disconnect"
VPN_TOOLS = (VPN_CONFIGURE, VPN_STATUS, VPN_DISCONNECT)
NAME = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
CID = r"^containerd://[0-9a-f]{64}$"
WG_PUBLIC_KEY = r"^[A-Za-z0-9+/]{43}=$"


def approved_ipv4(value: str) -> str:
    address = ipaddress.IPv4Address(value)
    if (
        str(address) != value
        or address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        raise ValueError("An approved numeric IPv4 address is required")
    return value


class NativeVpnCredential(ContractModel):
    namespace: str = Field(pattern=NAME)
    name: str = Field(pattern=NAME)
    uid: UUID
    cert_sha256: Digest


class NativeVpnPeer(ContractModel):
    ipv4: str
    port: int = Field(ge=1, le=65535, strict=True)
    server_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")

    _ipv4 = field_validator("ipv4")(approved_ipv4)


class NativeVpnProfile(ContractModel):
    profile: Literal["openvpn-ipv4-tcp-v1"]
    id: str = Field(pattern=NAME)
    label: str = Field(min_length=1, max_length=100)
    egress_image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    sandbox_image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    credential: NativeVpnCredential
    peer: NativeVpnPeer
    peer_namespace: str | None = Field(default=None, pattern=NAME)
    tun_ipv4: str
    tun_resource: Literal["devic.es/tun"]

    _ipv4 = field_validator("tun_ipv4")(approved_ipv4)


def https_vpn_source(value: str) -> tuple[str, int]:
    if not value.startswith("https://") or any(
        char.isspace() or ord(char) < 33 or ord(char) == 127 or char in "\\?#%" for char in value
    ):
        raise ValueError("Expected a public HTTPS configuration URL without credentials")
    url = urlsplit(value)
    host = url.hostname or ""
    if (
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host)
        or "." not in host
        or any(not re.fullmatch(NAME, label) for label in host.split("."))
        or re.fullmatch(r"[0-9.]+", host)
        or url.port == 0
        or url.username is not None
        or url.password is not None
        or url.netloc not in {host, f"{host}:{url.port}"}
    ):
        raise ValueError("Expected an exact HTTPS hostname without credentials")
    return host, 443 if url.port is None else url.port


class NativeTaskVpnProfile(ContractModel):
    profile: Literal["openvpn-task-ipv4-tcp-v1"]
    id: str = Field(pattern=NAME)
    label: str = Field(min_length=1, max_length=100)
    egress_image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    sandbox_image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    tun_resource: Literal["devic.es/tun"]
    source_origins: list[str] = Field(default_factory=list, max_length=16)
    source_cidrs: list[str] = Field(default_factory=list, max_length=32)
    peer_cidrs: list[str] = Field(min_length=1, max_length=32)
    default_cidrs: list[str] = Field(min_length=1, max_length=32)
    target_cidrs: list[str] = Field(min_length=1, max_length=32)
    deny_cidrs: list[str] = Field(min_length=1, max_length=32)

    @field_validator("source_origins")
    @classmethod
    def exact_origins(cls, values):
        origins = [https_vpn_source(value) for value in values]
        if len(set(origins)) != len(origins) or any(urlsplit(value).path for value in values):
            raise ValueError("Expected distinct VPN source origins")
        return values

    @field_validator("source_cidrs", "peer_cidrs", "default_cidrs", "target_cidrs", "deny_cidrs")
    @classmethod
    def exact_ipv4_cidrs(cls, values):
        if len(set(values)) != len(values) or any(
            str(ipaddress.IPv4Network(value)) != value for value in values
        ):
            raise ValueError("Expected distinct canonical IPv4 CIDRs")
        return values

    @model_validator(mode="after")
    def paired_sources(self):
        if bool(self.source_origins) != bool(self.source_cidrs):
            raise ValueError("Remote VPN sources require both origins and address boundaries")
        return self


def vpn_deployment(value: object) -> NativeVpnProfile | NativeTaskVpnProfile:
    if isinstance(value, dict) and value.get("profile") == VPN_DYNAMIC_PROFILE:
        return NativeTaskVpnProfile.model_validate(value)
    return NativeVpnProfile.model_validate(value)


class NativeVpnTarget(ContractModel):
    ipv4: str
    port: int = Field(ge=1, le=65535, strict=True)

    _ipv4 = field_validator("ipv4")(approved_ipv4)


class NativeTaskRoutePolicy(ContractModel):
    allow_cidrs: list[str] = Field(min_length=1, max_length=32)
    deny_cidrs: list[str] = Field(min_length=1, max_length=96)

    @field_validator("allow_cidrs", "deny_cidrs")
    @classmethod
    def canonical_cidrs(cls, values):
        networks = [ipaddress.IPv4Network(value) for value in values]
        if values != [str(network) for network in sorted(set(networks))]:
            raise ValueError("Expected canonical sorted Task VPN ranges")
        return values


class NativeTaskVpnConfigure(ContractModel):
    source_id: UUID | None = None
    https_url: str | None = Field(default=None, max_length=2048)
    preflight: NativeVpnTarget = Field(description="One TCP probe; does not define solver scope")

    @model_validator(mode="after")
    def one_source(self):
        if (self.source_id is None) == (self.https_url is None):
            raise ValueError("Choose exactly one VPN configuration source")
        if self.https_url is not None:
            https_vpn_source(self.https_url)
        return self


class NativeTaskVpnPeer(NativeVpnTarget):
    server_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")


class NativeTaskVpnNetwork(ContractModel):
    network_digest: Digest
    peer: NativeTaskVpnPeer
    route_policy: NativeTaskRoutePolicy
    preflight: NativeVpnTarget


class NativeTaskNetworkStatus(ContractModel):
    mode: Literal["unselected", "default", "vpn"]
    state: Literal["unselected", "preparing", "ready", "failed", "disconnected", "stopped"]


class NativeVpnCapability(ContractModel):
    profile: Literal["openvpn-ipv4-tcp-v1", "openvpn-task-ipv4-tcp-v1"]
    id: str
    label: str


class NativeVpnConfigure(ContractModel):
    profile_id: str = Field(pattern=NAME)


class NativeVpnStatus(ContractModel):
    pass


class NativeVpnDisconnect(ContractModel):
    operation_id: UUID


class NativeVpnScope(ContractModel):
    task_id: UUID
    operation_id: UUID
    resource_generation: int = Field(ge=1, strict=True)
    runner_pod_uid: UUID
    sandbox_pod_uid: UUID
    sandbox_container_id: str = Field(pattern=CID)
    runner_pod_ipv4: str
    sandbox_pod_ipv4: str
    egress_container_id: str = Field(pattern=CID)
    profile_digest: Digest

    _ipv4 = field_validator("runner_pod_ipv4", "sandbox_pod_ipv4")(approved_ipv4)


class NativeVpnOperation(ContractModel):
    scope: NativeVpnScope
    vpn_operation_id: UUID
    network_digest: Digest | None = Field(default=None, exclude_if=lambda value: value is None)


class NativeVpnLink(ContractModel):
    public_key: str = Field(pattern=WG_PUBLIC_KEY)
    ifindex: int = Field(ge=1, strict=True)


class NativeVpnLinkReport(NativeVpnOperation):
    link: NativeVpnLink


class NativeVpnTunnel(ContractModel):
    pid: int = Field(ge=1, strict=True)
    start_ticks: int = Field(ge=1, strict=True)
    tun_ifindex: int = Field(ge=1, strict=True)
    tun_ipv4: str
    link: NativeVpnLink

    _ipv4 = field_validator("tun_ipv4")(approved_ipv4)


class NativeSocksEdge(ContractModel):
    kind: Literal["socks5"]
    pid: int = Field(ge=1, strict=True)
    start_ticks: int = Field(ge=1, strict=True)
    listener_ipv4: Literal["198.18.255.1"]
    listener_port: Literal[12345]
    link: NativeVpnLink


class NativeVpnObservation(NativeVpnOperation):
    phase: Literal["link", "tunnel", "held", "closed", "failed", "unknown"]
    link: NativeVpnLink | None = None
    tunnel: NativeVpnTunnel | NativeSocksEdge | None = None
    forwarding_closed: bool = False
    lease_revision: int | None = Field(default=None, ge=1, strict=True)
    applied_until_ms: int | None = Field(default=None, ge=1, strict=True)
    pause_id: UUID | None = None

    @model_validator(mode="after")
    def exact_fields(self):
        if self.phase == "link":
            valid = self.link is not None and self.tunnel is None and not self.forwarding_closed
        elif self.phase == "tunnel":
            valid = self.tunnel is not None and self.link is None and not self.forwarding_closed
        elif self.phase == "held":
            valid = (
                self.tunnel is not None
                and self.link is None
                and self.forwarding_closed
                and self.pause_id is not None
            )
        elif self.phase == "closed":
            valid = self.forwarding_closed and self.link is None and self.tunnel is None
        else:
            valid = self.link is None and self.tunnel is None
        if (
            not valid
            or (self.lease_revision is not None and self.phase != "tunnel")
            or (self.lease_revision is None) != (self.applied_until_ms is None)
            or (self.pause_id is not None and self.phase != "held")
        ):
            raise ValueError("Invalid original VPN observation")
        return self


class NativeVpnPreflight(NativeVpnOperation):
    link: NativeVpnLink
    tunnel: NativeVpnTunnel | NativeSocksEdge
    ipv4: str
    port: int = Field(ge=1, le=65535, strict=True)
    passed: bool

    _ipv4 = field_validator("ipv4")(approved_ipv4)


def vpn_targets_compatible(
    profile: NativeVpnProfile, targets: list[dict], control_egress: list[dict] | tuple = ()
) -> None:
    reserved = (ipaddress.IPv4Network("198.18.0.0/16"), ipaddress.IPv4Network("198.19.0.0/16"))
    transport = [ipaddress.IPv4Address(value) for value in (profile.peer.ipv4, profile.tun_ipv4)]
    if transport[0] == transport[1] or any(
        ip in network for ip in transport for network in reserved
    ):
        raise ValueError("VPN transport overlaps reserved native address space")
    control = [ipaddress.ip_network(cidr) for entry in control_egress for cidr in entry["cidrs"]]
    for target in targets:
        address = ipaddress.IPv4Address(target["ipv4"])
        if str(address) in {
            profile.peer.ipv4,
            profile.tun_ipv4,
        } or any(address in network for network in (*reserved, *control)):
            raise ValueError("VPN target overlaps its underlay or reserved task link")
