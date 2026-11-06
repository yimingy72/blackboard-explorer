"""Two lightweight tenants exercise the patched Server, not the business platform."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.transport import RetryPolicy

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
STATE = ROOT / ".data/bbx-archv2-g7"
CONFIG = STATE / "kubeconfig"
SYSTEM = "bbx-archv2-g7-system"
NAMESPACES = {letter: "bbx-archv2-g7-task-" + letter for letter in "ab"}
PYTHON_IMAGE = (
    "python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30"
)


def kubectl(*args, data=None):
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(CONFIG), *args],
        input=json.dumps(data).encode() if data is not None else None,
        capture_output=True,
    )
    if result.returncode:
        # Even FailedCreate diagnostics can contain route tokens. Keep them private.
        raise RuntimeError("Owned kubectl operation failed")
    return result.stdout.decode()


def apply(document):
    kubectl("apply", "-f", "-", data=document)


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, indent=2)
        output.flush()
        os.fsync(output.fileno())


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def server_runtime_values(gateway_host):
    runtime = {"server": {"gateway": {"host": gateway_host}}}
    content = {
        "patch_identity": json.loads((STATE / "identity.json").read_text()),
        "runtime": runtime,
        "profile_files": {
            name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
            for name in ("Dockerfile", "values/server.yaml", "batchsandbox-template.yaml")
        },
    }
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    # Content changes roll the official Deployment even when the local image tag is reused.
    runtime["server"]["podAnnotations"] = {"bbx.archv2/content-sha256": digest}
    return runtime


def verified_patch_identity():
    expected = json.loads((STATE / "identity.json").read_text())
    actual = json.loads(
        kubectl(
            "-n",
            SYSTEM,
            "exec",
            "deployment/bbx-archv2-g7-server",
            "--",
            "cat",
            "/etc/bbx-g7-patch.json",
        )
    )
    if actual != expected:
        raise RuntimeError("Server patch identity differs from this build")
    return actual


def setup():
    assert kubectl("config", "current-context").strip() == "kind-bbx-archv2-g7"
    path = STATE / "credentials.json"
    if path.exists():
        credentials = json.loads(path.read_text())
    else:
        credentials: dict[str, Any] = {
            letter: {"key": secrets.token_urlsafe(24), "execd": secrets.token_urlsafe(24)}
            for letter in "ab"
        }
        credentials["signing"] = "a=" + base64.b64encode(secrets.token_bytes(32)).decode()
        credentials["ports"] = {name: free_port() for name in ("server", "ingress", "tenant")}
        private_json(path, credentials)
    for namespace in (SYSTEM, *NAMESPACES.values()):
        apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})
    for letter, namespace in NAMESPACES.items():
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "bbx-archv2-g7-execd", "namespace": namespace},
                "stringData": {"token": credentials[letter]["execd"]},
            }
        )
        apply(
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {"name": "bbx-archv2-g7-quota", "namespace": namespace},
                "spec": {
                    "hard": {
                        "pods": "1",
                        "requests.cpu": "300m",
                        "requests.memory": "256Mi",
                        "limits.cpu": "500m",
                        "limits.memory": "384Mi",
                    }
                },
            }
        )
    for name, values in {
        "bbx-archv2-g7-tenants": {
            "tenants.json": json.dumps(
                {credentials[letter]["key"]: namespace for letter, namespace in NAMESPACES.items()}
            )
        },
        "bbx-archv2-g7-signing": {"active-key": "a", "keys": credentials["signing"]},
    }.items():
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": SYSTEM},
                "stringData": values,
            }
        )
    for name, values in {
        "bbx-archv2-g7-template": {
            "template.yaml": (HERE / "batchsandbox-template.yaml").read_text()
        },
        "bbx-archv2-g7-tenant-source": {
            "tenant_fixture.py": (HERE / "tests/tenant_fixture.py").read_text()
        },
    }.items():
        apply(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": SYSTEM},
                "data": values,
            }
        )
    labels = {"app": "bbx-archv2-g7-tenant"}
    apply(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "bbx-archv2-g7-tenant", "namespace": SYSTEM},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "tenant",
                                "image": PYTHON_IMAGE,
                                "imagePullPolicy": "Never",
                                "command": ["python", "-I", "/app/tenant_fixture.py"],
                                "resources": {
                                    "requests": {"cpu": "10m", "memory": "32Mi"},
                                    "limits": {"cpu": "100m", "memory": "96Mi"},
                                },
                                "volumeMounts": [
                                    {"name": "source", "mountPath": "/app", "readOnly": True},
                                    {"name": "keys", "mountPath": "/var/run/bbx", "readOnly": True},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "source",
                                "configMap": {"name": "bbx-archv2-g7-tenant-source"},
                            },
                            {"name": "keys", "secret": {"secretName": "bbx-archv2-g7-tenants"}},
                        ],
                    },
                },
            },
        }
    )
    apply(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "bbx-archv2-g7-tenant", "namespace": SYSTEM},
            "spec": {"selector": labels, "ports": [{"port": 8080, "targetPort": 8080}]},
        }
    )
    runtime = STATE / "server-runtime.json"
    runtime.write_text(
        json.dumps(server_runtime_values("127.0.0.1:" + str(credentials["ports"]["ingress"])))
    )
    runtime.chmod(0o600)
    print("Prepared exactly two private test tenants.")


@contextmanager
def forward(name, port):
    with (STATE / (name + ".log")).open("wb") as log:
        process = subprocess.Popen(
            [
                "kubectl",
                "--kubeconfig",
                str(CONFIG),
                "-n",
                SYSTEM,
                "port-forward",
                "service/bbx-archv2-g7-" + name,
                str(port) + (":8080" if name == "tenant" else ":80"),
                "--address",
                "127.0.0.1",
            ],
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Owned port-forward exited")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("Owned port-forward timed out")
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def api_counts():
    totals = {verb: 0 for verb in ("GET", "LIST", "WATCH", "POST", "DELETE")}
    for line in kubectl("get", "--raw", "/metrics").splitlines():
        if (
            not line.startswith("apiserver_request_total{")
            or 'resource="batchsandboxes"' not in line
            or 'subresource=""' not in line
        ):
            continue
        for verb in totals:
            if 'verb="' + verb + '"' in line:
                totals[verb] += int(float(line.rsplit(" ", 1)[1]))
    return totals


async def verify():
    identity = verified_patch_identity()
    credentials = json.loads((STATE / "credentials.json").read_text())
    evidence = {"cluster": "bbx-archv2-g7", "task_count": 2, "checks": {}, "create_seconds": {}}
    evidence["patch_identity"] = identity
    evidence["checks"]["current_patch_identity_matches"] = True
    sandboxes = {}
    with ExitStack() as stack:
        for name, port in credentials["ports"].items():
            stack.enter_context(forward(name, port))
        base = "http://127.0.0.1:" + str(credentials["ports"]["server"])
        with httpx.Client(trust_env=False, timeout=10) as client:

            def counts():
                response = client.get(
                    "http://127.0.0.1:" + str(credentials["ports"]["tenant"]) + "/counts"
                )
                response.raise_for_status()
                return response.json()

            try:
                for letter, namespace in NAMESPACES.items():
                    config = ConnectionConfig(
                        domain="127.0.0.1:" + str(credentials["ports"]["server"]),
                        api_key=credentials[letter]["key"],
                        headers={"X-EXECD-ACCESS-TOKEN": credentials[letter]["execd"]},
                        disable_metrics=True,
                        request_timeout=timedelta(seconds=60),
                        retry_policy=RetryPolicy(max_retries=0, wake_retry_enabled=False),
                    )
                    matches = json.loads(
                        kubectl(
                            "-n",
                            namespace,
                            "get",
                            "batchsandboxes",
                            "-l",
                            "bbx-archv2-g7-task=" + letter,
                            "-o",
                            "json",
                        )
                    )["items"]
                    assert len(matches) <= 1, "Ambiguous prior create; no replay"
                    started = time.perf_counter()
                    if matches:
                        sandbox = await Sandbox.connect(
                            matches[0]["metadata"]["name"],
                            connection_config=config,
                            connect_timeout=timedelta(seconds=120),
                        )
                    else:
                        operation_id = "bbx-archv2-g7-" + uuid4().hex
                        private_json(
                            STATE / ("create-" + letter + ".attempted"),
                            {"create_op": operation_id, "namespace": namespace},
                        )
                        sandbox = await Sandbox.create(
                            PYTHON_IMAGE,
                            connection_config=config,
                            timeout=timedelta(minutes=15),
                            ready_timeout=timedelta(seconds=120),
                            secure_access=True,
                            metadata={"bbx-archv2-g7-task": letter, "bbx-create-op": operation_id},
                            entrypoint=["/usr/bin/tail", "-f", "/dev/null"],
                            resource={"cpu": "100m", "memory": "128Mi"},
                        )
                    evidence["create_seconds"][letter] = round(time.perf_counter() - started, 3)
                    sandboxes[letter] = sandbox
                    pods = json.loads(kubectl("-n", namespace, "get", "pods", "-o", "json"))[
                        "items"
                    ]
                    assert len(pods) == 1 and pods[0]["status"]["phase"] == "Running"
                    evidence["checks"][letter + "_namespace_pod_ready"] = True
                before = counts()
                assert (await sandboxes["a"].get_info()).id == sandboxes["a"].id
                after = counts()
                assert after[NAMESPACES["a"]] == before[NAMESPACES["a"]] + 1
                evidence["checks"]["evicted_active_tenant_resolved_again"] = True
                assert (
                    client.get(
                        base + "/sandboxes/" + sandboxes["a"].id,
                        headers={"OPEN-SANDBOX-API-KEY": credentials["b"]["key"]},
                    ).status_code
                    == 404
                )
                evidence["checks"]["cross_tenant_id_not_found"] = True
                start_counts = api_counts()
                latencies = []
                for index in range(20):
                    sandbox = sandboxes["ab"[index % 2]]
                    started = time.perf_counter()
                    assert (await sandbox.get_info()).id == sandbox.id
                    latencies.append(round((time.perf_counter() - started) * 1000, 3))
                end_counts = api_counts()
                evidence["steady_20_reads"] = {
                    "api_delta": {
                        key: end_counts[key] - value for key, value in start_counts.items()
                    },
                    "client_count": 20,
                    "latency_ms_min": min(latencies),
                    "latency_ms_max": max(latencies),
                    "latency_ms_p50": sorted(latencies)[9],
                    "latency_ms_p95": sorted(latencies)[18],
                }
                assert evidence["steady_20_reads"]["api_delta"]["GET"] >= 20
                before = counts()
                for letter in "ab":
                    response = client.delete(
                        base + "/sandboxes/" + sandboxes[letter].id,
                        headers={"OPEN-SANDBOX-API-KEY": credentials[letter]["key"]},
                    )
                    assert response.status_code in (200, 204)
                    kubectl(
                        "-n",
                        NAMESPACES[letter],
                        "wait",
                        "--for=delete",
                        "batchsandbox/" + sandboxes[letter].id,
                        "--timeout=90s",
                    )
                after = counts()
                assert after[NAMESPACES["a"]] > before[NAMESPACES["a"]]
                evidence["checks"]["evicted_active_tenant_cleanup_resolved_again"] = True
                evidence["checks"]["both_workloads_deleted"] = all(
                    not json.loads(kubectl("-n", namespace, "get", "batchsandboxes", "-o", "json"))[
                        "items"
                    ]
                    for namespace in NAMESPACES.values()
                )
                evidence["tenant_resolve_counts"] = after
                (STATE / "evidence.json").write_text(json.dumps(evidence, indent=2))
                print(json.dumps(evidence, indent=2))
            finally:
                for sandbox in sandboxes.values():
                    await sandbox.close()


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["setup"]:
            setup()
        elif sys.argv[1:] == ["verify"]:
            asyncio.run(verify())
        else:
            raise ValueError("Expected setup or verify")
    except Exception as error:
        # Do not accidentally print SDK route credentials in exception URLs.
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print(
            "G7 fixture failed: "
            f"{type(error).__name__} at {Path(frame.filename).name}:{frame.lineno}",
            file=sys.stderr,
        )
        sys.exit(1)
