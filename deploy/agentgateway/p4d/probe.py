"""Test amount-only PUT on the pinned standalone gateway; write only redacted observations."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parent
IMAGE = (
    "cr.agentgateway.dev/agentgateway"
    "@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b"
)
COMMIT = "26f5aebe99a36a037fe54f8080a608aca5547e15"
KEYS = "/api/config/resources/llm.apiKey"
PROVIDER = ROOT / "provider.py"
EVIDENCE = ROOT.parents[2] / ".data" / "p4d-native-budget-probe"
MODEL = "bbx-p4d-fake-model"
KEY_NAME = "bbx-p4d-fake-task"


def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=40
    ).stdout.strip()


def wait_for(read, label):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if result := read():
                return result
        except (httpx.TransportError, httpx.HTTPStatusError):
            pass
        time.sleep(0.05)
    raise AssertionError(label)


def port(name, container_port):
    ports = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", name))
    return "http://127.0.0.1:" + ports[f"{container_port}/tcp"][0]["HostPort"]


def main(runtime, client, evidence, names):
    network, provider, gateway = names
    token = "bbx-p4d-test-" + secrets.token_hex(24)
    provider_token = "bbx-p4d-fake-" + secrets.token_hex(24)
    image_id = docker("image", "inspect", "python:3.12-slim", "--format", "{{.Id}}")
    raw_version = docker("run", "--rm", "--network", "none", IMAGE, "--version")
    assert COMMIT in raw_version and "1.7.0" in raw_version
    evidence["version"] = json.loads(raw_version)
    config = {
        "config": {
            "adminAddr": "0.0.0.0:15000",
            "storage": {"mode": "hybrid"},
            "database": {"url": "sqlite:///probe/data.db?mode=rwc"},
            "modelCatalog": [{"file": "/probe/catalog.json"}],
        },
        "frontendPolicies": {"accessLog": {"database": {"llm": "metadata"}}},
        "llm": {
            "port": 4000,
            "discovery": "disabled",
            "policies": {"apiKey": {"mode": "strict", "keys": []}},
            "models": [
                {
                    "name": MODEL,
                    "provider": "openAI",
                    "params": {"apiKey": provider_token, "baseUrl": f"http://{provider}:8000/v1"},
                }
            ],
        },
    }
    (runtime / "config.json").write_text(json.dumps(config))
    (runtime / "catalog.json").write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {"models": {MODEL: {"rates": {"input": "10", "output": "20"}}}}
                }
            }
        )
    )
    (runtime / "provider.vars").write_text(f"BBX_TEST_PROVIDER_KEY={provider_token}\n")
    docker("network", "create", network)
    docker(
        "run",
        "-d",
        "--name",
        provider,
        "--network",
        network,
        "-p",
        "127.0.0.1::8000",
        "--env-file",
        str(runtime / "provider.vars"),
        "-v",
        f"{runtime}:/probe:ro",
        "-v",
        f"{PROVIDER}:/provider.py:ro",
        image_id,
        "python",
        "/provider.py",
    )
    docker(
        "run",
        "-d",
        "--name",
        gateway,
        "--network",
        network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-p",
        "127.0.0.1::4000",
        "-p",
        "127.0.0.1::15000",
        "-v",
        f"{runtime}:/probe",
        IMAGE,
        "-f",
        "/probe/config.json",
    )
    admin, data, fake = port(gateway, 15000), port(gateway, 4000), port(provider, 8000)

    def get_json(url, **kw):
        return client.get(url, **kw).raise_for_status().json()

    def resource():
        resources = get_json(admin + KEYS)["resources"]
        assert len(resources) == 1
        return resources[0]

    def status(threshold, used, exceeded):
        rows = get_json(admin + "/api/budgets/status", params={"apiKeyName": KEY_NAME})["budgets"]
        if len(rows) != 1:
            return None
        item = rows[0]
        if Decimal(item["limit"]["amount"]) != Decimal(threshold):
            return None
        if Decimal(item["usage"]["used"]) != Decimal(used):
            return None
        assert item["apiKeyName"] == KEY_NAME and item["name"] == "task-usd"
        assert item["limit"]["unit"] == "USD" and item["onBudgetExceeded"] == "Block"
        assert item["window"] == {"start": 0, "end": None, "durationMs": None, "expired": False}
        assert item["usage"]["exceeded"] is exceeded
        return item

    def send(label):
        response = client.post(
            data + "/v1/chat/completions",
            headers={"Authorization": "Bearer " + token},
            json={"model": MODEL, "messages": [{"role": "user", "content": label}], "stream": True},
        )
        return {
            "http": response.status_code,
            "code": (
                response.json().get("error", {}).get("code")
                if response.status_code != 200
                else None
            ),
        }

    def provider_calls():
        return get_json(fake + "/control/calls")

    wait_for(lambda: get_json(fake + "/control/ready"), "fake provider ready")
    wait_for(lambda: isinstance(get_json(admin + KEYS)["resources"], list), "gateway ready")
    value = {
        "keyHash": "sha256:" + sha256(token.encode()).hexdigest(),
        "metadata": {"name": KEY_NAME},
        "allowedModels": [MODEL],
        "budgets": [
            {
                "name": "task-usd",
                "limit": {"unit": "USD", "amount": 0.005},
                "onBudgetExceeded": "Block",
            }
        ],
    }
    client.put(admin + KEYS, json={"resources": [{"value": value}]}).raise_for_status()
    original = resource()
    resource_path = KEYS + "/" + original["id"]
    assert (
        original["value"]["keyHash"] == value["keyHash"]
        and original["value"]["budgets"] == value["budgets"]
    )
    wait_for(lambda: status("0.005", "0", False), "original runtime budget applied")
    assert send("old-1")["http"] == 200
    assert send("old-2")["http"] == 200
    evidence["old_runtime"] = wait_for(lambda: status("0.005", "0.006", True), "old usage reached")
    evidence["old_block"] = send("old-blocked")
    assert evidence["old_block"] == {"http": 429, "code": "budget_exceeded"}
    assert provider_calls() == ["old-1", "old-2"]

    desired = deepcopy(original["value"])
    desired["budgets"][0]["limit"]["amount"] = 0.008
    missing = client.put(admin + KEYS + "/" + str(uuid4()), json={"value": desired})
    evidence["missing_resource_status"] = missing.status_code
    assert missing.status_code == 404 and resource() == original

    forwarded = []

    class DropAck(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_PUT(self):
            assert self.path == resource_path
            content = self.rfile.read(int(self.headers["content-length"]))
            with httpx.Client(trust_env=False, timeout=5) as transport:
                response = transport.put(
                    admin + self.path, content=content, headers={"Content-Type": "application/json"}
                )
            forwarded.append(response.status_code)
            # The caller cannot observe the successful gateway response.
            self.close_connection = True

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), DropAck)
    thread = Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            client.put(
                f"http://127.0.0.1:{proxy.server_port}" + resource_path, json={"value": desired}
            )
            raise AssertionError("Lost write ACK was delivered")
        except httpx.RemoteProtocolError:
            evidence["client_write_ack"] = "lost"
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join()
    assert forwarded == [200]
    evidence["write_forward_count"] = len(forwarded)
    saved = resource()
    assert saved["id"] == original["id"] and saved["value"] == desired
    assert (
        saved["createdAt"] == original["createdAt"]
        and saved["revision"] == original["revision"] + 1
    )
    evidence["desired_readback"] = {
        "same_uuid": saved["id"] == original["id"],
        "same_key_hash": saved["value"]["keyHash"] == original["value"]["keyHash"],
        "same_name": saved["value"]["metadata"]["name"] == original["value"]["metadata"]["name"],
        "same_other_fields": saved["value"] == desired,
        "old_revision": original["revision"],
        "new_revision": saved["revision"],
    }
    evidence["updated_runtime"] = wait_for(
        lambda: status("0.008", "0.006", False), "new runtime limit with preserved usage"
    )
    assert send("new-1")["http"] == 200
    evidence["new_runtime_after_call"] = wait_for(
        lambda: status("0.008", "0.009", True), "new call accrues on original usage"
    )
    evidence["new_block"] = send("new-blocked")
    assert evidence["new_block"] == {"http": 429, "code": "budget_exceeded"}
    assert provider_calls() == ["old-1", "old-2", "new-1"]

    def durable_counter():
        rows = get_json(fake + "/control/durable-counter")
        if rows == [{"used_nanodollars": 9_000_000, "unit": "USD"}]:
            return rows[0]

    evidence["durable_before_restart"] = wait_for(durable_counter, "native budget flush")
    docker("kill", gateway)
    docker("start", gateway)
    admin, data = port(gateway, 15000), port(gateway, 4000)
    evidence["runtime_after_sigkill_restart"] = wait_for(
        lambda: status("0.008", "0.009", True), "restart recovered usage and new limit"
    )
    assert resource() == saved
    evidence["restart_block"] = send("restart-blocked")
    assert evidence["restart_block"] == {"http": 429, "code": "budget_exceeded"}
    evidence["fake_provider_calls"] = provider_calls()
    assert evidence["fake_provider_calls"] == ["old-1", "old-2", "new-1"]


if __name__ == "__main__":
    prefix = "bbx-p4d-budget-" + secrets.token_hex(4)
    names = tuple(f"{prefix}-{kind}" for kind in ("net", "provider", "gateway"))
    evidence = {"image": IMAGE, "storage": "hybrid/sqlite", "started_at": time.time()}
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    result = EVIDENCE / f"{prefix}.json"
    try:
        with (
            TemporaryDirectory(dir=EVIDENCE, prefix="runtime-") as directory,
            httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as client,
        ):
            try:
                main(Path(directory), client, evidence, names)
                evidence["result"] = "passed"
            finally:
                docker("rm", "-f", *names[1:], check=False)
                docker("network", "rm", names[0], check=False)
    finally:
        evidence["finished_at"] = time.time()
        result.write_text(json.dumps(evidence, indent=2) + "\n")
        print(result)
