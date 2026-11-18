"""A two-Task official Kubernetes integration probe, never a platform controller."""

import asyncio
import base64
import json
import os
import secrets
import shlex
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from bbx_sandbox_execution.execution import (
    AssignmentBinding,
    CommandJournal,
    CommandRequest,
    SandboxExecution,
)
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import PVC, Volume
from opensandbox.transport import RetryPolicy

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / ".data/bbx-archv2-k8s"
CONFIG = STATE / "kubeconfig"
SYSTEM = "bbx-archv2-system"
NAMESPACES = {letter: "bbx-archv2-task-" + letter for letter in "ab"}


def kubectl(*args: str, data: dict | None = None) -> str:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(CONFIG), *args],
        input=json.dumps(data).encode() if data is not None else None,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode()


def apply(document: dict) -> None:
    kubectl("apply", "-f", "-", data=document)


def free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def capture_ready_claim(sandbox_id: str, namespace: str, pod_uid: str) -> str:
    claim = json.loads(kubectl("-n", namespace, "get", "pvc", "bbx-archv2-workspace", "-o", "json"))
    assert claim["status"]["phase"] == "Bound"
    path = STATE / ("ready-claim-" + sandbox_id + ".json")
    observed = {
        "sandbox_id": sandbox_id,
        "namespace": namespace,
        "pod_uid": pod_uid,
        "pvc_uid": claim["metadata"]["uid"],
    }
    if path.exists():
        baseline = json.loads(path.read_text())
        assert baseline["sandbox_id"] == sandbox_id and baseline["namespace"] == namespace
        assert baseline["pvc_uid"] == observed["pvc_uid"], (
            "PVC identity changed after first observed Ready"
        )
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(observed, output)
            output.flush()
            os.fsync(output.fileno())
    return observed["pvc_uid"]


def setup() -> None:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert kubectl("config", "current-context").strip() == "kind-bbx-archv2-k8s"
    credentials: dict[str, Any] = {
        letter: {"key": secrets.token_urlsafe(24), "execd": secrets.token_urlsafe(24)}
        for letter in "ab"
    }
    credentials["ports"] = {"server": free_port(), "ingress": free_port()}
    path = STATE / "credentials.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(credentials, output)
    for namespace in (SYSTEM, *NAMESPACES.values()):
        apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})
    for letter, namespace in NAMESPACES.items():
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "bbx-archv2-execd", "namespace": namespace},
                "stringData": {"token": credentials[letter]["execd"]},
            }
        )
        apply(
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {"name": "bbx-archv2-quota", "namespace": namespace},
                "spec": {
                    "hard": {
                        "pods": "2",
                        "requests.cpu": "500m",
                        "requests.memory": "768Mi",
                        "limits.cpu": "1",
                        "limits.memory": "1Gi",
                        "persistentvolumeclaims": "1",
                        "requests.storage": "64Mi",
                    }
                },
            }
        )
        apply(
            {
                "apiVersion": "cilium.io/v2",
                "kind": "CiliumNetworkPolicy",
                "metadata": {"name": "bbx-archv2-isolation", "namespace": namespace},
                "spec": {
                    "endpointSelector": {},
                    "ingress": [
                        {
                            "fromEndpoints": [
                                {"matchLabels": {"k8s:io.kubernetes.pod.namespace": namespace}},
                                {"matchLabels": {"k8s:io.kubernetes.pod.namespace": SYSTEM}},
                            ]
                        }
                    ],
                    "egress": [
                        {
                            "toEndpoints": [
                                {"matchLabels": {"k8s:io.kubernetes.pod.namespace": namespace}}
                            ]
                        },
                        {
                            "toEndpoints": [
                                {
                                    "matchLabels": {
                                        "k8s:io.kubernetes.pod.namespace": "kube-system",
                                        "k8s:k8s-app": "kube-dns",
                                    }
                                }
                            ],
                            "toPorts": [
                                {
                                    "ports": [
                                        {"port": "53", "protocol": "UDP"},
                                        {"port": "53", "protocol": "TCP"},
                                    ]
                                }
                            ],
                        },
                    ],
                    "egressDeny": [{"toEntities": ["host", "remote-node", "kube-apiserver"]}],
                },
            }
        )
    apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "bbx-archv2-tenants", "namespace": SYSTEM},
            "stringData": {
                "tenants.json": json.dumps(
                    {
                        credentials[letter]["key"]: namespace
                        for letter, namespace in NAMESPACES.items()
                    }
                )
            },
        }
    )
    apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "bbx-archv2-signing", "namespace": SYSTEM},
            "stringData": {
                "active-key": "a",
                "keys": "a=" + base64.b64encode(secrets.token_bytes(32)).decode(),
            },
        }
    )
    apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "bbx-archv2-template", "namespace": SYSTEM},
            "data": {
                "template.yaml": (Path(__file__).parent / "batchsandbox-template.yaml").read_text()
            },
        }
    )
    apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "bbx-archv2-tenant-source", "namespace": SYSTEM},
            "data": {
                "tenant_provider.py": (
                    Path(__file__).parent / "fixtures/tenant_provider.py"
                ).read_text()
            },
        }
    )
    labels = {"app": "bbx-archv2-tenant"}
    apply(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "bbx-archv2-tenant", "namespace": SYSTEM},
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
                                "image": "bbx-archv2-sandbox-k8s:20261106",
                                "imagePullPolicy": "Never",
                                "command": ["/usr/bin/python3", "-I", "/app/tenant_provider.py"],
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
                            {"name": "source", "configMap": {"name": "bbx-archv2-tenant-source"}},
                            {"name": "keys", "secret": {"secretName": "bbx-archv2-tenants"}},
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
            "metadata": {"name": "bbx-archv2-tenant", "namespace": SYSTEM},
            "spec": {"selector": labels, "ports": [{"port": 8080, "targetPort": 8080}]},
        }
    )
    (STATE / "server-runtime.json").write_text(
        json.dumps(
            {"server": {"gateway": {"host": "127.0.0.1:" + str(credentials["ports"]["ingress"])}}}
        )
    )
    print("Prepared exactly two test tenants; generated credentials are private and not displayed.")


@contextmanager
def forward(service: str, port: int):
    with (STATE / (service + ".log")).open("wb") as log:
        process = subprocess.Popen(
            [
                "kubectl",
                "--kubeconfig",
                str(CONFIG),
                "-n",
                SYSTEM,
                "port-forward",
                "service/" + service,
                str(port) + ":80",
                "--address",
                "127.0.0.1",
            ],
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Own port-forward exited before readiness")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("Own port-forward did not become ready")
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


async def verify() -> None:
    credentials = json.loads((STATE / "credentials.json").read_text())
    evidence = {"cluster": "bbx-archv2-k8s", "task_count": 2, "checks": {}}
    journal = CommandJournal(STATE / "audit" / "commands.sqlite")
    sandboxes = {}
    with ExitStack() as stack:
        for service in ("server", "ingress"):
            stack.enter_context(forward("bbx-archv2-" + service, credentials["ports"][service]))
        base = "http://127.0.0.1:" + str(credentials["ports"]["server"])
        with httpx.Client(trust_env=False) as client:
            assert (
                client.get(
                    base + "/sandboxes", headers={"OPEN-SANDBOX-API-KEY": "invalid-test-key"}
                ).status_code
                == 401
            )
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
                        "bbx-archv2-task=" + letter,
                        "-o",
                        "json",
                    )
                )["items"]
                assert len(matches) <= 1, "Ambiguous original create; do not create again"
                if matches:
                    sandbox = await Sandbox.connect(
                        matches[0]["metadata"]["name"],
                        connection_config=config,
                        connect_timeout=timedelta(seconds=120),
                    )
                else:
                    attempt = STATE / ("create-" + letter + ".attempted")
                    fd = os.open(attempt, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    operation_id = "bbx-archv2-" + uuid4().hex
                    with os.fdopen(fd, "w") as record:
                        json.dump({"create_op": operation_id, "namespace": namespace}, record)
                        record.flush()
                        os.fsync(record.fileno())
                    sandbox = await Sandbox.create(
                        "bbx-archv2-sandbox-k8s:20261106",
                        connection_config=config,
                        timeout=timedelta(minutes=30),
                        ready_timeout=timedelta(seconds=120),
                        secure_access=True,
                        metadata={
                            "bbx-archv2-task": letter,
                            "bbx-create-op": operation_id,
                        },
                        env={
                            "BBX_CONTROL_MARKER": "bbx-archv2-not-for-solver",
                            "LD_PRELOAD": "/must-not-load.so",
                            "BASH_ENV": "/must-not-source",
                        },
                        entrypoint=["/usr/bin/tail", "-f", "/dev/null"],
                        resource={"cpu": "300m", "memory": "512Mi"},
                        resource_requests={"cpu": "50m", "memory": "128Mi"},
                        volumes=[
                            Volume(
                                name="bbx-workspace",
                                mountPath="/workspace",
                                pvc=PVC(
                                    claimName="bbx-archv2-workspace",
                                    createIfNotExists=True,
                                    deleteOnSandboxTermination=False,
                                    storage="64Mi",
                                ),
                            )
                        ],
                    )
                sandboxes[letter] = sandbox
                pods = json.loads(kubectl("-n", namespace, "get", "pods", "-o", "json"))["items"]
                assert len(pods) == 1
                pod = pods[0]
                assert any(
                    condition["type"] == "Ready" and condition["status"] == "True"
                    for condition in pod["status"]["conditions"]
                )
                pvc_uid = capture_ready_claim(sandbox.id, namespace, pod["metadata"]["uid"])
                main = next(
                    container
                    for container in pod["spec"]["containers"]
                    if container["name"] == "sandbox"
                )
                assert main["command"] == ["/opt/bbx/control/launcher", "--kubernetes-bootstrap"]
                assert main["securityContext"]["privileged"] is False
                secret_env = next(
                    item for item in main["env"] if item["name"] == "EXECD_ACCESS_TOKEN"
                )
                assert (
                    secret_env["valueFrom"]["secretKeyRef"]["name"] == "bbx-archv2-execd"
                    and "value" not in secret_env
                )
                binding = AssignmentBinding(
                    task_id=namespace,
                    agent_id="worker-a",
                    role="worker",
                    assignment_id="assignment-a",
                    generation=1,
                    epoch=1,
                    sandbox_id=sandbox.id,
                    pod_uid=pod["metadata"]["uid"],
                    environment_revision="k8s-1",
                    network_identity="network-a",
                    namespace="worker-a",
                    ipv4="198.19.1.2",
                    execution_profile="net-raw",
                )
                adapter = SandboxExecution(sandbox, binding, journal)
                await adapter.files.write_file("/workspace/marker.txt", "task-" + letter)
                command = CommandRequest(
                    command_id=uuid4().hex,
                    invocation_id=uuid4().hex,
                    command="id -u; grep -E 'CapEff|NoNewPrivs' /proc/self/status; "
                    "tr '\\0' ' ' </proc/1/cmdline; printf '\\n'",
                    timeout_seconds=10,
                )
                result = await adapter.run(command)
                output = journal.output_preview(command.command_id)["text"]
                assert (
                    result.exit_code == 0
                    and "10000" in output
                    and "0000000000002000" in output
                    and "--init" in output
                )
                assert (
                    credentials[letter]["execd"] not in output
                    and "bbx-archv2-not-for-solver" not in output
                )
                if letter == "a":
                    worker_env = await adapter.files.read_file(
                        "/proc/self/environ", offset=1, limit=1
                    )
                    assert (
                        credentials[letter]["execd"] not in worker_env
                        and "EXECD_ACCESS_TOKEN=" not in worker_env
                    )
                    assert "BBX_CONTROL_MARKER=" not in worker_env
                    evidence["checks"]["file_worker_environment_clean"] = True
                evidence[letter] = {
                    "sandbox_id": sandbox.id,
                    "pod_uid": pod["metadata"]["uid"],
                    "pod_ip": pod["status"]["podIP"],
                    "namespace": namespace,
                    "pvc_uid": pvc_uid,
                    "environment_names": [entry["name"] for entry in main["env"]],
                    "execd_secret_reference": secret_env["valueFrom"],
                    "command_id": command.command_id,
                    "execution_id": result.id,
                }
            with httpx.Client(trust_env=False) as client:
                for owner, other in (("a", "b"), ("b", "a")):
                    headers = {"OPEN-SANDBOX-API-KEY": credentials[other]["key"]}
                    assert client.get(
                        base + "/sandboxes/" + sandboxes[owner].id, headers=headers
                    ).status_code in (403, 404)
                    assert client.delete(
                        base + "/sandboxes/" + sandboxes[owner].id, headers=headers
                    ).status_code in (403, 404)
                    endpoint = await sandboxes[owner].get_endpoint(44772)
                    request_headers = endpoint.build_request_headers(
                        sandboxes[owner].connection_config
                    )
                    url = (
                        "http://"
                        + endpoint.endpoint
                        + "/command/status/"
                        + evidence[owner]["execution_id"]
                    )
                    assert client.get(url, headers=request_headers).status_code == 200
                    missing_route_status = client.get(url).status_code
                    assert missing_route_status in (400, 401, 403, 404)
                    route_only = {
                        key: value
                        for key, value in request_headers.items()
                        if key.lower() != "x-execd-access-token"
                    }
                    assert client.get(url, headers=route_only).status_code == 401
                    evidence["checks"]["auth_statuses_" + owner] = {
                        "authorized": 200,
                        "missing_route": missing_route_status,
                        "missing_execd": 401,
                    }
            a = sandboxes["a"]
            binding = AssignmentBinding(
                task_id=NAMESPACES["a"],
                agent_id="worker-a",
                role="worker",
                assignment_id="assignment-a",
                generation=1,
                epoch=1,
                sandbox_id=a.id,
                pod_uid=evidence["a"]["pod_uid"],
                environment_revision="k8s-1",
                network_identity="network-a",
                namespace="worker-a",
                ipv4="198.19.1.2",
            )
            adapter = SandboxExecution(a, binding, journal)
            own = CommandRequest(
                command_id=uuid4().hex,
                invocation_id=uuid4().hex,
                command=f"curl --noproxy '*' -fsS --max-time 3 http://{evidence['a']['pod_ip']}:18081/marker.txt",
            )
            assert (await adapter.run(own)).exit_code == 0 and journal.output_preview(
                own.command_id
            )["text"].strip() == "task-a"
            for port in (18081, 44772):
                crossed = CommandRequest(
                    command_id=uuid4().hex,
                    invocation_id=uuid4().hex,
                    command=f"curl --noproxy '*' -fsS --max-time 2 http://{evidence['b']['pod_ip']}:{port}/marker.txt",
                )
                assert (await adapter.run(crossed)).exit_code != 0
            assert (
                await sandboxes["b"]
                .files_with_identity(10000, 10000)
                .read_file("/workspace/marker.txt")
                == "task-b"
            )
            await a.kill()
            for _ in range(100):
                if not json.loads(kubectl("-n", NAMESPACES["a"], "get", "pods", "-o", "json"))[
                    "items"
                ]:
                    break
                await asyncio.sleep(0.2)
            else:
                raise AssertionError("Task A Pod teardown was not observed")
            claim = json.loads(
                kubectl("-n", NAMESPACES["a"], "get", "pvc", "bbx-archv2-workspace", "-o", "json")
            )
            assert claim["status"]["phase"] == "Bound"
            assert (await sandboxes["b"].get_info()).id == sandboxes["b"].id
            evidence["checks"].update(
                {
                    "two_namespace_tenants": True,
                    "cross_tenant_ids_denied": True,
                    "ingress_and_execd_auth_separate": True,
                    "cross_task_pod_ips_denied": True,
                    "task_markers_private": True,
                    "stop_pod_observed_pvc_retained": True,
                    "other_task_healthy": True,
                }
            )
            await finish_checks(evidence, sandboxes["b"], journal)
            (STATE / "evidence.json").write_text(json.dumps(evidence, indent=2))
            print(json.dumps({"checks": evidence["checks"], "tasks": 2}))
        finally:
            for sandbox in sandboxes.values():
                await sandbox.close()
            journal.close()


async def finish_checks(evidence: dict, sandbox: Sandbox, journal: CommandJournal) -> None:
    namespace = NAMESPACES["a"]
    claim = json.loads(kubectl("-n", namespace, "get", "pvc", "bbx-archv2-workspace", "-o", "json"))
    assert claim["metadata"]["uid"] == evidence["a"]["pvc_uid"]
    evidence["retained_claim"] = {
        "uid": claim["metadata"]["uid"],
        "pre_stop_uid": evidence["a"]["pvc_uid"],
        "owner_references": claim["metadata"].get("ownerReferences", []),
    }
    containers = json.loads(
        subprocess.check_output(
            ["docker", "exec", "bbx-archv2-k8s-control-plane", "crictl", "ps", "-a", "-o", "json"]
        )
    )["containers"]
    assert not any(
        container["state"] == "CONTAINER_RUNNING"
        and container.get("labels", {}).get("io.kubernetes.pod.uid") == evidence["a"]["pod_uid"]
        for container in containers
    )
    reader = "bbx-archv2-retained-read"
    apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": reader, "namespace": namespace},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "securityContext": {"runAsUser": 10000, "runAsGroup": 10000},
                "containers": [
                    {
                        "name": "read",
                        "image": "bbx-archv2-sandbox-k8s:20261106",
                        "imagePullPolicy": "Never",
                        "command": ["/usr/bin/cat", "/workspace/marker.txt"],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "16Mi"},
                            "limits": {"cpu": "100m", "memory": "64Mi"},
                        },
                        "volumeMounts": [
                            {"name": "workspace", "mountPath": "/workspace", "readOnly": True}
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "workspace",
                        "persistentVolumeClaim": {
                            "claimName": "bbx-archv2-workspace",
                            "readOnly": True,
                        },
                    }
                ],
            },
        }
    )
    for _ in range(100):
        phase = json.loads(kubectl("-n", namespace, "get", "pod", reader, "-o", "json"))["status"][
            "phase"
        ]
        if phase == "Succeeded":
            break
        assert phase != "Failed", "Read-only retained data probe failed"
        await asyncio.sleep(0.2)
    else:
        raise AssertionError("Retained data probe outcome is unknown; preserve its objects")
    assert kubectl("-n", namespace, "logs", reader).strip() == "task-a"
    kubectl("-n", namespace, "delete", "pod", reader, "--wait=true", "--timeout=15s")
    binding = AssignmentBinding(
        task_id=NAMESPACES["b"],
        agent_id="worker-a",
        role="worker",
        assignment_id="assignment-a",
        generation=1,
        epoch=1,
        sandbox_id=sandbox.id,
        pod_uid=evidence["b"]["pod_uid"],
        environment_revision=evidence["b"].get("environment_revision", "k8s-1"),
        network_identity="network-b",
        namespace="worker-a",
        ipv4="198.19.1.2",
        execution_profile="net-raw",
    )
    adapter = SandboxExecution(sandbox, binding, journal)
    raw_source = (
        (ROOT / "services/sandbox-execution/tests/raw_probe.py")
        .read_text()
        .replace("198.19.4.2", "127.0.0.1")
    )
    await adapter.files.write_file("/workspace/.p1-raw-probe.py", raw_source)
    raw = CommandRequest(
        command_id=uuid4().hex,
        invocation_id=uuid4().hex,
        command="python3 -I /workspace/.p1-raw-probe.py",
    )
    assert (await adapter.run(raw)).exit_code == 0
    observed = json.loads(journal.output_preview(raw.command_id)["text"])
    assert observed["uid"] == 10000 and observed["raw_socket"] and observed["icmp_reply"]
    assert not observed["can_setuid"] and observed["net_admin_exit"] != 0
    for name in ("CapEff", "CapPrm", "CapBnd"):
        assert name + ":\t0000000000002000" in observed["status"]
    assert "NoNewPrivs:\t1" in observed["status"]
    own = CommandRequest(
        command_id=uuid4().hex,
        invocation_id=uuid4().hex,
        command=f"curl --noproxy '*' -fsS --max-time 3 http://{evidence['b']['pod_ip']}:18081/marker.txt",
    )
    assert (await adapter.run(own)).exit_code == 0 and journal.output_preview(own.command_id)[
        "text"
    ].strip() == "task-b"
    cancel_id = uuid4().hex
    running = CommandRequest(
        command_id=cancel_id,
        invocation_id=uuid4().hex,
        command='sleep 60 & child=$!; printf \'%s %s\' "$$" "$child" '
        f"> /workspace/.cancel-{cancel_id}; wait",
        background=True,
    )
    result = await adapter.run(running)
    pids = [
        int(value)
        for value in (await adapter.files.read_file("/workspace/.cancel-" + cancel_id)).split()
    ]
    assert len(pids) == 2
    await adapter.interrupt(cancel_id)
    status: dict[str, Any] = {"running": None}
    for _ in range(50):
        status = await adapter.status(cancel_id)
        if status["running"] is False:
            break
        await asyncio.sleep(0.1)
    assert status["running"] is False
    check = CommandRequest(
        command_id=uuid4().hex,
        invocation_id=uuid4().hex,
        command="python3 -c "
        + shlex.quote(
            "from pathlib import Path;import hashlib,json;"
            f"assert not any(Path('/proc/'+str(p)).exists() for p in {pids!r});"
            "assert Path('/proc/1/comm').read_text().strip()=='execd';"
            "assert b'--init' in Path('/proc/1/cmdline').read_bytes();"
            "assert hashlib.sha256(Path('/opt/opensandbox/execd').read_bytes()).digest()=="
            "hashlib.sha256(Path('/opt/bbx/control/execd').read_bytes()).digest()"
        ),
    )
    assert (await adapter.run(check)).exit_code == 0
    endpoint = await sandbox.get_endpoint(44772)
    headers = endpoint.build_request_headers(sandbox.connection_config)
    headers["OpenSandbox-Secure-Access"] = "invalid-test-route-token"
    with httpx.Client(trust_env=False) as client:
        bad_route = client.get(
            "http://"
            + endpoint.endpoint
            + "/command/status/"
            + journal.execution_id(cancel_id, binding),
            headers=headers,
        ).status_code
        credentials = json.loads((STATE / "credentials.json").read_text())
        server_url = "http://127.0.0.1:" + str(credentials["ports"]["server"])
        for letter in "ab":
            response = client.get(
                server_url + "/sandboxes",
                headers={"OPEN-SANDBOX-API-KEY": credentials[letter]["key"]},
            )
            assert response.status_code == 200
            assert (sandbox.id in response.text) is (letter == "b")
    assert bad_route in (401, 403)
    pod = json.loads(kubectl("-n", NAMESPACES["b"], "get", "pods", "-o", "json"))["items"][0]
    assert pod["metadata"]["uid"] == binding.pod_uid
    main = next(
        container for container in pod["spec"]["containers"] if container["name"] == "sandbox"
    )
    assert next(mount for mount in main["volumeMounts"] if mount["name"] == "opensandbox-bin")[
        "readOnly"
    ]
    claims = {
        letter: json.loads(kubectl("-n", ns, "get", "pvc", "bbx-archv2-workspace", "-o", "json"))[
            "metadata"
        ]["uid"]
        for letter, ns in NAMESPACES.items()
    }
    assert claims["a"] != claims["b"]
    evidence["cancel"] = {
        "command_id": cancel_id,
        "execution_id": result.id,
        "parent_child_absent": True,
    }
    evidence["checks"].update(
        {
            "ready_pvc_uid_retained": True,
            "retained_marker_read_after_stop": True,
            "stopped_task_runtime_not_running": True,
            "raw_icmp_in_final_pod": True,
            "no_setuid_or_net_admin": True,
            "other_task_target_healthy": True,
            "final_pod_command_cancelled": True,
            "tampered_route_denied": bad_route,
            "pid1_official_and_bin_sha_matches": True,
            "official_bin_mount_read_only": True,
            "namespace_pvc_uids_distinct": True,
            "tenant_lists_scoped_after_stop": True,
        }
    )


async def postverify() -> None:
    credentials = json.loads((STATE / "credentials.json").read_text())
    evidence = json.loads((STATE / "evidence.json").read_text())
    with ExitStack() as stack:
        for service in ("server", "ingress"):
            stack.enter_context(forward("bbx-archv2-" + service, credentials["ports"][service]))
        config = ConnectionConfig(
            domain="127.0.0.1:" + str(credentials["ports"]["server"]),
            api_key=credentials["b"]["key"],
            headers={"X-EXECD-ACCESS-TOKEN": credentials["b"]["execd"]},
            disable_metrics=True,
            retry_policy=RetryPolicy(max_retries=0, wake_retry_enabled=False),
        )
        sandbox = await Sandbox.connect(evidence["b"]["sandbox_id"], connection_config=config)
        journal = CommandJournal(STATE / "audit" / "commands.sqlite")
        try:
            await finish_checks(evidence, sandbox, journal)
            (STATE / "evidence.json").write_text(json.dumps(evidence, indent=2))
            print(json.dumps({"checks": evidence["checks"], "no_new_task_created": True}))
        finally:
            await sandbox.close()
            journal.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        setup()
    elif sys.argv[1:] == ["verify"]:
        asyncio.run(verify())
    elif sys.argv[1:] == ["postverify"]:
        asyncio.run(postverify())
    else:
        raise SystemExit("Use setup or verify against the dedicated bbx-archv2 kind only")
