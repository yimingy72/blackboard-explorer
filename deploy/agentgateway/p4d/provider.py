"""Scripted fake OpenAI provider and a read-only check of flushed budget usage."""

import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

CALLS = []
LOCK = Lock()
TOKEN = os.environ["BBX_TEST_PROVIDER_KEY"]


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        match self.path:
            case "/control/durable-counter":
                with sqlite3.connect("file:/probe/data.db?mode=ro", uri=True) as db:
                    used = db.execute("SELECT used_amount, unit FROM budget_usage").fetchall()
                payload = [{"used_nanodollars": amount, "unit": unit} for amount, unit in used]
            case "/control/calls":
                with LOCK:
                    payload = list(CALLS)
            case "/control/ready":
                payload = {"ready": True}
            case _:
                self.send_error(404)
                return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        if self.headers.get("authorization") != "Bearer " + TOKEN:
            self.send_error(401)
            return
        request = json.loads(self.rfile.read(int(self.headers["content-length"])))
        case = request["messages"][0]["content"]
        with LOCK:
            CALLS.append(case)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(choices, usage=None):
            chunk = {
                "id": "fake-" + case,
                "object": "chat.completion.chunk",
                "created": 0,
                "model": request["model"],
                "choices": choices,
            }
            if usage is not None:
                chunk["usage"] = usage
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")

        send([{"index": 0, "delta": {"content": "synthetic"}, "finish_reason": None}])
        send([{"index": 0, "delta": {}, "finish_reason": "stop"}])
        send([], {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200})
        self.wfile.write(b"data: [DONE]\n\n")


ThreadingHTTPServer(("0.0.0.0", 8000), Provider).serve_forever()
