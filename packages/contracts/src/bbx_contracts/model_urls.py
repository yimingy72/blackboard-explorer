"""Resolve host-local model endpoints without changing saved connection identity."""

from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def in_container() -> bool:
    return Path("/.dockerenv").exists()


def resolve_model_url(url: str, *, containerized: bool) -> str:
    """Route loopback model endpoints to the Docker host only inside containers."""
    if not containerized:
        return url
    parts = urlsplit(url)
    hostname = parts.hostname
    if hostname is None:
        return url
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ip_address(hostname).is_loopback
        except ValueError:
            return url
    if not loopback:
        return url
    userinfo, separator, address = parts.netloc.rpartition("@")
    suffix = (
        address[address.index("]") + 1 :] if address.startswith("[") else address[len(hostname) :]
    )
    netloc = f"{userinfo}{separator}host.docker.internal{suffix}"
    return urlunsplit(parts._replace(netloc=netloc))
