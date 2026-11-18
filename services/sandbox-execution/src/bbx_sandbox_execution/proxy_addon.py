"""Loaded by the existing CTF image's fixed mitmproxy installation."""

import importlib
import json
import os
from pathlib import Path
from typing import Any

from bbx_sandbox_execution.proxy import proxy_destination_allowed, proxy_identity


class AssignmentProxyAddon:
    def __init__(self) -> None:
        self.root = Path(os.environ["BBX_PROXY_CONTROL"])

    def _check(self, flow: Any) -> None:
        source = flow.client_conn.peername or ("", 0)
        allowed = False
        identity = None
        flow.metadata.pop("bbx_identity", None)
        try:
            records = json.loads((self.root / "grants.json").read_text())
            if not isinstance(records, dict):
                raise ValueError("Proxy grants must be an object")
            identity = proxy_identity(records, flow.metadata.get("proxyauth"), source[0])
            allowed = identity is not None and proxy_destination_allowed(
                identity, flow.request.host, flow.request.port
            )
        except (OSError, KeyError, TypeError, ValueError):
            pass
        if not allowed:
            http = importlib.import_module("mitmproxy.http")
            flow.response = http.Response.make(
                403, b"Task proxy denied source identity or destination"
            )
            return
        flow.metadata["bbx_identity"] = identity

    def http_connect(self, flow: Any) -> None:
        self._check(flow)

    def requestheaders(self, flow: Any) -> None:
        self._check(flow)

    def response(self, flow: Any) -> None:
        identity = flow.metadata.get("bbx_identity")
        if identity is None:
            return
        record = {
            **identity,
            "source_ip": flow.client_conn.peername[0],
            "destination_ip": flow.server_conn.peername[0] if flow.server_conn.peername else None,
            "status_code": flow.response.status_code,
            "attribution": "exact",
        }
        fd = os.open(
            self.root / "records.jsonl",
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
        try:
            data = (json.dumps(record) + "\n").encode()
            if os.write(fd, data) != len(data):
                raise OSError("Incomplete proxy audit record")
        finally:
            os.close(fd)


addons = [AssignmentProxyAddon()]
