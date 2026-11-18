"""Verify one approved HTTP response from the protected Solver path."""

import ipaddress
import re
import signal
import socket
import ssl
import sys


def check_probe(address, port, scheme, path, status, server_name):
    ip = ipaddress.IPv4Address(address)
    if (
        str(ip) != address
        or not 1 <= port <= 65535
        or scheme not in {"http", "https"}
        or not re.fullmatch(r"/(?!/)[A-Za-z0-9._~/-]{0,255}", path)
        or any(segment in {".", ".."} for segment in path.split("/"))
        or not 200 <= status <= 599
        or (
            server_name
            and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", server_name)
        )
    ):
        raise ValueError("Original HTTP probe is invalid")
    host = server_name or address
    with socket.create_connection((address, port), timeout=2) as plain:
        if scheme == "https":
            with ssl.create_default_context().wrap_socket(plain, server_hostname=host) as tls:
                return (
                    response_status(tls, host if port == 443 else f"{host}:{port}", path) == status
                )
        return response_status(plain, host if port == 80 else f"{host}:{port}", path) == status


def response_status(conn, host, path):
    conn.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii")
    )
    with conn.makefile("rb") as response:
        line = response.readline(256)
    match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?\r\n", line)
    if not match:
        raise ValueError("The protected probe did not receive an HTTP status")
    return int(match[1])


if __name__ == "__main__":
    signal.alarm(4)
    ipv4, raw_port, protocol, target_path, raw_status, name = sys.argv[1:]
    if not check_probe(ipv4, int(raw_port), protocol, target_path, int(raw_status), name):
        raise SystemExit(1)
