"""Only two generated keys and namespace counters; no production cache service."""

import http.server
import json
from pathlib import Path

COUNTS = {}


class Authority(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        mapping = json.loads(Path("/var/run/bbx/tenants.json").read_text())
        if self.path == "/counts":
            status, data = 200, COUNTS
        else:
            namespace = mapping.get(self.headers.get("OPEN-SANDBOX-API-KEY", ""))
            if namespace:
                COUNTS[namespace] = COUNTS.get(namespace, 0) + 1
            status, data = (
                (200, {"namespace": namespace, "ttl": 120})
                if namespace
                else (401, {"code": "UNAUTHORIZED"})
            )
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


http.server.HTTPServer(("0.0.0.0", 8080), Authority).serve_forever()
