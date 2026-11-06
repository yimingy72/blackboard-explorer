"""Two generated test keys only; this is not the platform's TenantProvider service."""

import http.server
import json
from pathlib import Path


class Tenant(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        mapping = json.loads(Path("/var/run/bbx/tenants.json").read_text())
        namespace = mapping.get(self.headers.get("OPEN-SANDBOX-API-KEY", ""))
        body = json.dumps(
            {"namespace": namespace, "ttl": 1} if namespace else {"code": "UNAUTHORIZED"}
        ).encode()
        self.send_response(200 if namespace else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Tenant).serve_forever()
