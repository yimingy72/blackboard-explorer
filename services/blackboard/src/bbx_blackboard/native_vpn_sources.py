"""Bounded, private intake for one frozen Task OpenVPN connection."""

import asyncio
import ipaddress
import re
import socket
import ssl
from urllib.parse import urlsplit

import httpx
from anyio import to_process
from bbx_contracts.runner_vpn import (
    VPN_SOURCE_LIMIT,
    NativeTaskVpnProfile,
    approved_ipv4,
    https_vpn_source,
)
from cryptography.hazmat.primitives import serialization
from cryptography.x509 import load_pem_x509_certificate

BUILTIN_DENY = (
    "0.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/3",
)
PEM_SECTIONS = {"ca": "ca", "cert": "cert", "key": "key", "tls-crypt": "tls_crypt"}
SAFE_FLAGS = {
    ("client",),
    ("dev", "tun"),
    ("proto", "udp"),
    ("proto", "udp4"),
    ("nobind",),
    ("persist-key",),
    ("persist-tun",),
    ("auth-nocache",),
    ("remote-cert-tls", "server"),
    ("resolv-retry", "infinite"),
}
HOST = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?")


def checked_address(value: str, allowed: list[str], denied: list[str]) -> str:
    address = ipaddress.IPv4Address(approved_ipv4(value))
    if any(address in ipaddress.IPv4Network(block) for block in (*BUILTIN_DENY, *denied)):
        raise ValueError("VPN address overlaps the control or reserved network")
    if not any(address in ipaddress.IPv4Network(block) for block in allowed):
        raise ValueError("VPN address is outside the published network boundary")
    return str(address)


async def resolve_vpn_ipv4(host: str, port: int) -> set[str]:
    return {
        str(item[4][0])
        for item in await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    }


async def pin_address(host, port, allowed, denied, resolver):
    try:
        direct = str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError:
        resolved = await resolver(host, port)
    else:
        resolved = {direct}
    if not resolved or len(resolved) > 16:
        raise ValueError("VPN address resolution is not bounded")
    validated = [checked_address(value, allowed, denied) for value in resolved]
    return min(validated, key=ipaddress.IPv4Address)


def require_source_origin(url: str, profile: NativeTaskVpnProfile) -> tuple[str, int]:
    authority = https_vpn_source(url)
    if authority not in {https_vpn_source(origin) for origin in profile.source_origins}:
        raise ValueError("HTTPS configuration origin is not published")
    return authority


async def fetch_vpn_configuration(
    url: str,
    profile: NativeTaskVpnProfile,
    denied: list[str],
    *,
    resolver=resolve_vpn_ipv4,
    tls: ssl.SSLContext | None = None,
) -> bytes:
    hostname, port = require_source_origin(url, profile)
    async with asyncio.timeout(8):
        ip = await pin_address(hostname, port, profile.source_cidrs, denied, resolver)
        authority = hostname if port == 443 else f"{hostname}:{port}"
        target = httpx.URL(scheme="https", host=ip, port=port, path=urlsplit(url).path or "/")
        async with httpx.AsyncClient(
            verify=tls if tls is not None else True,
            trust_env=False,
            follow_redirects=False,
            timeout=5,
        ) as client:
            async with client.stream(
                "GET",
                target,
                headers={"Host": authority, "Accept-Encoding": "identity"},
                extensions={"sni_hostname": hostname},
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise ValueError("HTTPS source did not return a plain configuration")
                size = response.headers.get("content-length")
                if size is not None and not (size.isdecimal() and int(size) <= VPN_SOURCE_LIMIT):
                    raise ValueError("HTTPS configuration exceeds its byte limit")
                payload = bytearray()
                async for chunk in response.aiter_raw():
                    if len(payload) + len(chunk) > VPN_SOURCE_LIMIT:
                        raise ValueError("HTTPS configuration exceeds its byte limit")
                    payload.extend(chunk)
                if not payload:
                    raise ValueError("HTTPS configuration is empty")
                return bytes(payload)


def parse_vpn_configuration(raw: bytes):
    if not 0 < len(raw) <= VPN_SOURCE_LIMIT or b"\0" in raw:
        raise ValueError("OpenVPN configuration is outside its byte limit")
    remote = None
    server_name = None
    material = {}
    opened = None
    payload = []
    seen = set()
    for original in raw.decode("ascii").splitlines():
        line = original.strip()
        if opened:
            if line == f"</{opened}>":
                if not payload:
                    raise ValueError("Empty OpenVPN inline asset")
                material[PEM_SECTIONS[opened]] = "\n".join(payload) + "\n"
                opened, payload = None, []
            elif (
                opened == "tls-crypt" and not payload and (not line or line.startswith(("#", ";")))
            ):
                continue
            elif line.startswith("<") or not line or len(line) > 256:
                raise ValueError("Invalid OpenVPN inline asset")
            else:
                payload.append(line)
            continue
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("<") and line.endswith(">"):
            tag = line[1:-1]
            if tag not in PEM_SECTIONS or PEM_SECTIONS[tag] in material:
                raise ValueError("Unsupported OpenVPN inline asset")
            opened = tag
            continue
        fields = tuple(line.split())
        if fields[0] in seen:
            raise ValueError("Repeated OpenVPN directive")
        seen.add(fields[0])
        if fields in SAFE_FLAGS:
            continue
        if fields[0] == "remote" and len(fields) in {3, 4}:
            host, port = fields[1:3]
            if (
                not HOST.fullmatch(host)
                or not port.isdecimal()
                or not 1 <= int(port) <= 65535
                or (len(fields) == 4 and fields[3] not in {"udp", "udp4"})
            ):
                raise ValueError("Expected one UDP4 OpenVPN remote")
            remote = (host.lower(), int(port))
        elif (
            fields[0] == "verify-x509-name"
            and len(fields) == 3
            and fields[2] == "name"
            and HOST.fullmatch(fields[1])
            and len(fields[1]) <= 128
        ):
            server_name = fields[1]
        elif fields[0] == "verb" and len(fields) == 2 and fields[1] in set("0123456"):
            continue
        elif fields == ("data-ciphers", "AES-256-GCM:AES-128-GCM"):
            continue
        else:
            raise ValueError("Unsupported OpenVPN directive")
    if opened or remote is None or not {"ca", "cert", "key"} <= material.keys():
        raise ValueError("OpenVPN remote or certificate material is missing")
    for name in ("ca", "cert"):
        load_pem_x509_certificate(material[name].encode("ascii"))
    cert = load_pem_x509_certificate(material["cert"].encode("ascii"))
    key = serialization.load_pem_private_key(material["key"].encode("ascii"), password=None)
    if cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) != key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ):
        raise ValueError("OpenVPN client certificate and key do not match")
    if "tls_crypt" in material and not re.fullmatch(
        r"-----BEGIN OpenVPN Static key V1-----\n(?:[0-9a-fA-F]{32}\n){16}"
        r"-----END OpenVPN Static key V1-----\n",
        material["tls_crypt"],
    ):
        raise ValueError("OpenVPN tls-crypt key is not valid")
    return remote, server_name, material


async def parse_private_configuration(raw, limiter, *, timeout=3):
    # Private-key validation can hold the GIL; cancellation must stop the worker.
    async with asyncio.timeout(timeout):
        return await to_process.run_sync(
            parse_vpn_configuration, raw, cancellable=True, limiter=limiter
        )


def vpn_asset_cipher(settings):
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    key = hashlib.sha256(
        b"bbx-native-vpn-assets-v1\0" + settings.agent_token_secret.get_secret_value().encode()
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key))
