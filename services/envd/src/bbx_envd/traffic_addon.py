"""mitmdump addon: preserve streaming and export only bounded display copies."""

import hmac
import importlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bbx_envd.traffic_format import PREVIEW_BYTES, message_preview, redact_text, redact_url


class TrafficAddon:
    def __init__(self) -> None:
        self.root = Path(os.environ["BBX_TRAFFIC_CONTROL"])
        self.active: dict[str, dict[str, Any]] = {}
        self.connections: dict[str, dict[str, str]] = {}

    def running(self) -> None:
        # Keep the official TLS key logger, bounded independently of proxy forwarding.
        # Contexts retain this callback; checking the marker on each line also covers
        # already-established sessions after the raw-capture limit.
        tls: Any = importlib.import_module("mitmproxy.net.tls")

        original = tls.log_master_secret
        if original is not None:

            def bounded_keylog(connection: Any, keymaterial: bytes) -> None:
                if (self.root / "disabled").exists():
                    return
                filename = self.root.parent / "tls.keys"
                if filename.stat().st_size + len(keymaterial) + 2 > 16 * 1024 * 1024:
                    (self.root / "keylog-full").touch(mode=0o600)
                    return
                original(connection, keymaterial)

            tls.log_master_secret = bounded_keylog
        (self.root / "ready").touch(mode=0o600)

    def identity(self, flow: Any) -> dict[str, str] | None:
        auth = flow.metadata.get("proxyauth")
        if not isinstance(auth, (tuple, list)) or len(auth) != 2:
            return None
        try:
            credentials = json.loads((self.root / "credentials.json").read_text())
            identity = credentials.get(auth[0])
            if identity and hmac.compare_digest(identity["password"], auth[1]):
                return {key: identity[key] for key in ("agent_id", "command_id")}
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None

    def http_connect(self, flow: Any) -> None:
        identity = self.identity(flow)
        if identity:
            self.connections[flow.client_conn.id] = identity

    def requestheaders(self, flow: Any) -> None:
        if (self.root / "disabled").exists():
            flow.request.stream = True
            return
        identity = self.identity(flow)
        if not identity:
            return
        if len(self.active) >= 1024:
            self.failure("Too many active HTTP streams")
            return
        self.connections[flow.client_conn.id] = identity
        self.active[flow.id] = {
            "flow": flow,
            "identity": identity,
            "request": bytearray(),
            "response": bytearray(),
            "request_bytes": 0,
            "response_bytes": 0,
        }
        flow.request.stream = self.tee(flow.id, "request")

    def tee(self, flow_id: str, side: str):
        def stream(chunk: bytes) -> bytes:
            record = self.active.get(flow_id)
            if record is not None:
                record[side + "_bytes"] += len(chunk)
                record[side].extend(chunk[: max(0, PREVIEW_BYTES - len(record[side]))])
            return chunk

        return stream

    def responseheaders(self, flow: Any) -> None:
        if (self.root / "disabled").exists():
            flow.response.stream = True
            return
        if flow.id in self.active:
            flow.response.stream = self.tee(flow.id, "response")

    def response(self, flow: Any) -> None:
        self.finish(flow)

    def error(self, flow: Any) -> None:
        self.finish(flow, "HTTP connection interrupted")

    def finish(self, flow: Any, error: str | None = None) -> None:
        record = self.active.pop(flow.id, None)
        if record is None:
            return
        client = flow.client_conn.peername or ("", None)
        server = flow.server_conn.peername or (flow.request.host, flow.request.port)
        request = flow.request
        response = flow.response
        identity = record["identity"]
        detail: dict[str, Any] = {
            "record_kind": "http",
            "timestamp": datetime.fromtimestamp(request.timestamp_start, UTC).isoformat(),
            "agent_id": identity["agent_id"],
            "attribution": "exact",
            "protocol": request.scheme.lower(),
            "transport_protocol": "tcp",
            "application_protocol": request.scheme.lower(),
            "source_ip": client[0],
            "source_port": client[1],
            "destination_ip": server[0],
            "destination_port": server[1],
            "method": request.method[:32],
            "url": redact_url(request.pretty_url),
            "status_code": response.status_code if response else None,
            "summary": f"{request.method[:32]} {redact_url(request.pretty_url)}"[:2048],
            "request_bytes": record["request_bytes"],
            "response_bytes": record["response_bytes"],
            "plaintext": "decoded",
            "request": None,
            "response": None,
            "observation": {"scope": "wire", "attributes": {"command_id": identity["command_id"]}},
        }
        for side, message in (("request", request), ("response", response)):
            if message is not None:
                detail[side] = message_preview(
                    list(message.headers.items(multi=True)),
                    bytes(record[side]),
                    record[side + "_bytes"],
                )
        if error:
            detail["observation"]["attributes"]["error"] = error
        self.emit(detail)

    def tls_failed_client(self, data: Any) -> None:
        self.tls_failure(data)

    def tls_failed_server(self, data: Any) -> None:
        self.tls_failure(data)

    def tls_failure(self, data: Any) -> None:
        context = data.context
        client, server = context.client, context.server
        identity = self.connections.get(client.id)
        source = client.peername or ("", None)
        target = server.peername or server.address or ("", None)
        self.emit(
            {
                "record_kind": "http",
                "timestamp": datetime.now(UTC).isoformat(),
                "agent_id": identity["agent_id"] if identity else None,
                "attribution": "exact" if identity else "unknown",
                "protocol": "tls",
                "transport_protocol": "tcp",
                "application_protocol": "tls",
                "source_ip": source[0],
                "source_port": source[1],
                "destination_ip": target[0],
                "destination_port": target[1],
                "method": None,
                "url": None,
                "status_code": None,
                "summary": "TLS handshake failed; plaintext unavailable",
                "request_bytes": 0,
                "response_bytes": 0,
                "plaintext": "encrypted",
                "request": None,
                "response": None,
            }
        )
        self.failure("TLS handshake failed; some HTTPS traffic could not be decoded")

    def client_disconnected(self, client: Any) -> None:
        self.connections.pop(client.id, None)

    def failure(self, reason: str) -> None:
        path = self.root / "failure"
        path.write_text(redact_text(reason)[:512])
        path.chmod(0o600)

    def emit(self, detail: dict[str, Any]) -> None:
        if (self.root / "disabled").exists():
            return
        identifier = uuid.uuid4().hex
        temporary = self.root / "inbox" / (identifier + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as target:
                os.chmod(temporary, 0o600)
                json.dump(detail, target, ensure_ascii=False, separators=(",", ":"))
            temporary.rename(temporary.with_suffix(".json"))
        except OSError:
            self.failure("Decoded traffic could not be persisted")

    def done(self) -> None:
        for record in list(self.active.values()):
            self.finish(record["flow"], "Capture stopped during HTTP stream")


addons = [TrafficAddon()]
