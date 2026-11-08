"""Frozen deployment and original identity contracts for one Task VPN session."""

import ipaddress
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .models import ContractModel
from .runner import Digest

VPN_PROFILE = "openvpn-ipv4-tcp-v1"
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


class NativeVpnCapability(ContractModel):
    profile: Literal["openvpn-ipv4-tcp-v1"]
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


class NativeVpnObservation(NativeVpnOperation):
    phase: Literal["link", "tunnel", "held", "closed", "failed", "unknown"]
    link: NativeVpnLink | None = None
    tunnel: NativeVpnTunnel | None = None
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
    tunnel: NativeVpnTunnel
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
