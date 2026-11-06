"""One owned Kubernetes Sandbox, one replacement, and no model/target traffic."""

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
import traceback
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from bbx_sandbox_execution.connect import read_execution_context
from bbx_sandbox_execution.execution import (
    AssignmentBinding,
    CommandJournal,
    CommandRequest,
    SandboxExecution,
    UnknownAssignmentCleanup,
)
from common import (
    CLAIM,
    CLUSTER,
    HERE,
    NODE,
    REVISION,
    STATE,
    SYSTEM,
    TASK,
    UnsupportedEnvironment,
    apply,
    kubectl,
    owned_cluster,
)
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import PVC, Volume
from opensandbox.transport import RetryPolicy


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


def setup():
    owned_cluster()
    credentials_path = STATE / "credentials.json"
    if credentials_path.exists():
        credentials = json.loads(credentials_path.read_text())
    else:
        credentials = {
            "api_key": secrets.token_urlsafe(24),
            "execd_token": secrets.token_urlsafe(24),
            "signing": "a=" + base64.b64encode(secrets.token_bytes(32)).decode(),
            "ports": {name: free_port() for name in ("server", "ingress")},
        }
        private_json(credentials_path, credentials)
    for namespace in (SYSTEM, TASK):
        apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})
    for namespace, name, data in (
        (TASK, CLUSTER + "-execd", {"token": credentials["execd_token"]}),
        (SYSTEM, CLUSTER + "-signing", {"active-key": "a", "keys": credentials["signing"]}),
        (
            SYSTEM,
            CLUSTER + "-tenants",
            {
                "tenants.toml": '[[tenants]]\nname = "bbx-g1"\nnamespace = "'
                + TASK
                + '"\napi_keys = ['
                + json.dumps(credentials["api_key"])
                + "]\n"
            },
        ),
    ):
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace},
                "stringData": data,
            }
        )
    apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": CLUSTER + "-template", "namespace": SYSTEM},
            "data": {"template.json": (HERE / "batchsandbox-template.json").read_text()},
        }
    )
    apply(
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": CLUSTER + "-quota", "namespace": TASK},
            "spec": {
                "hard": {
                    "pods": "1",
                    "requests.cpu": "500m",
                    "requests.memory": "512Mi",
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
            "metadata": {"name": CLUSTER + "-isolation", "namespace": TASK},
            "spec": {
                "endpointSelector": {},
                "ingress": [
                    {
                        "fromEndpoints": [
                            {"matchLabels": {"k8s:io.kubernetes.pod.namespace": namespace}}
                            for namespace in (SYSTEM, TASK)
                        ]
                    }
                ],
                "egress": [
                    {"toEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": TASK}}]},
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
    runtime = STATE / "server-runtime.json"
    runtime.write_text(
        json.dumps(
            {"server": {"gateway": {"host": "127.0.0.1:" + str(credentials["ports"]["ingress"])}}}
        )
    )
    runtime.chmod(0o600)
    print("Prepared one private file tenant, fixed template and test Secret references.")


def pod_ready(pod):
    return any(
        item["type"] == "Ready" and item["status"] == "True"
        for item in pod.get("status", {}).get("conditions", [])
    )


def task_pod():
    pods = json.loads(kubectl("-n", TASK, "get", "pods", "-o", "json"))["items"]
    assert len(pods) <= 1, "Unexpected extra Task Pod"
    return pods[0] if pods else None


def cri_identity(pod, container_name):
    owned_cluster()
    assert pod["metadata"]["namespace"] in (SYSTEM, TASK)
    assert pod["spec"]["nodeName"] == NODE
    current = json.loads(
        kubectl(
            "-n", pod["metadata"]["namespace"], "get", "pod", pod["metadata"]["name"], "-o", "json"
        )
    )
    assert current["metadata"]["uid"] == pod["metadata"]["uid"], (
        "Pod identity changed before CRI inspection"
    )
    status = next(
        item
        for item in current["status"].get("containerStatuses", [])
        if item["name"] == container_name
    )
    container_id = status["containerID"].removeprefix("containerd://")
    assert len(container_id) == 64 and set(container_id) <= set("0123456789abcdef")
    command = [
        "docker",
        "exec",
        NODE,
        "crictl",
        "--runtime-endpoint",
        "unix:///run/containerd/containerd.sock",
    ]
    result = subprocess.run([*command, "inspect", container_id], capture_output=True, check=True)
    # The raw document includes environment values; never persist, log or return it.
    inspected = json.loads(result.stdout)
    assert inspected["status"]["labels"]["io.kubernetes.pod.uid"] == current["metadata"]["uid"]
    assert inspected["status"]["metadata"]["name"] == container_name
    spec = inspected["info"]["runtimeSpec"]
    if isinstance(spec, str):
        spec = json.loads(spec)
    namespace = next(
        (item for item in spec["linux"]["namespaces"] if item["type"] == "cgroup"), None
    )
    private = namespace is not None and not namespace.get("path")
    safe = {
        "pod_uid": current["metadata"]["uid"],
        "container_id": container_id,
        "cgroup_namespace_declared_private": private,
        "host_pid": current["spec"].get("hostPID", False),
        "host_path": any("hostPath" in volume for volume in current["spec"].get("volumes", [])),
    }
    observation = STATE / ("runtime-observation-" + container_id[:16] + ".json")
    observation.write_text(json.dumps(safe, indent=2))
    if not private:
        raise UnsupportedEnvironment("CRI does not declare a private cgroup namespace")
    namespace_result = subprocess.run(
        [*command, "exec", container_id, "/usr/bin/readlink", "/proc/self/ns/cgroup"],
        capture_output=True,
        check=True,
    )
    node_namespace = subprocess.check_output(
        ["docker", "exec", NODE, "readlink", "/proc/self/ns/cgroup"], text=True
    ).strip()
    actual_namespace = namespace_result.stdout.decode().strip()
    if actual_namespace == node_namespace:
        raise UnsupportedEnvironment("Container shares the node cgroup namespace")
    safe["cgroup_namespace_inode"] = int(actual_namespace.split("[", 1)[1].rstrip("]"))
    membership = subprocess.check_output(
        [
            *command,
            "exec",
            container_id,
            "/usr/bin/cat",
            "/proc/self/cgroup",
        ],
        text=True,
    ).strip()
    safe["cgroup_root_is_current_container"] = membership == "0::/"
    observation.write_text(json.dumps(safe, indent=2))
    if membership != "0::/":
        raise UnsupportedEnvironment(
            "Private cgroup namespace is not rooted at the current container"
        )
    return safe


def preflight():
    owned_cluster()
    pods = json.loads(
        kubectl(
            "-n",
            SYSTEM,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/instance=bbx-archv2-g1-k8s-server",
            "-o",
            "json",
        )
    )["items"]
    assert len(pods) == 1 and pod_ready(pods[0])
    observed = cri_identity(pods[0], "main")
    (STATE / "preflight.json").write_text(json.dumps(observed, indent=2))
    print("Owned control Pod CRI declares and actually uses a private cgroup namespace.")


@contextmanager
def forward(name, port):
    with (STATE / (name + ".log")).open("wb") as log:
        process = subprocess.Popen(
            [
                "kubectl",
                "--kubeconfig",
                str(STATE / "kubeconfig"),
                "-n",
                SYSTEM,
                "port-forward",
                "service/" + CLUSTER + "-" + name,
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


def capture_claim(pod, *, first):
    claim = json.loads(kubectl("-n", TASK, "get", "pvc", CLAIM, "-o", "json"))
    assert pod_ready(pod) and claim["status"]["phase"] == "Bound"
    path = STATE / "first-ready-pvc.json"
    actual = {"pod_uid": pod["metadata"]["uid"], "pvc_uid": claim["metadata"]["uid"]}
    if first and not path.exists():
        private_json(path, actual)
    baseline = json.loads(path.read_text())
    assert baseline["pvc_uid"] == actual["pvc_uid"]
    return actual["pvc_uid"]


def inspect_template(pod):
    assert not pod["spec"].get("hostPID", False) and not pod["spec"].get("hostNetwork", False)
    assert not any("hostPath" in volume for volume in pod["spec"]["volumes"])
    main = next(item for item in pod["spec"]["containers"] if item["name"] == "sandbox")
    assert main["command"] == ["/opt/bbx/control/launcher", "--kubernetes-bootstrap-cgroup-v2"]
    assert not main["securityContext"].get("privileged", False)
    expected = json.loads((HERE / "batchsandbox-template.json").read_text())["spec"][
        "shardPatches"
    ][0]["spec"]["containers"][0]
    assert main["securityContext"] == expected["securityContext"]
    env = {item["name"]: item for item in main["env"]}
    assert env["BBX_POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
    assert env["BBX_ENVIRONMENT_REVISION"]["value"] == REVISION
    assert env["EXECD_ACCESS_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": CLUSTER + "-execd",
        "key": "token",
    }
    assert env["EXECD_INIT"]["value"] == "1" and env["OPENSANDBOX_ID"]["value"]
    return {
        "env_names": sorted(env),
        "execd_token_secret": CLUSTER + "-execd",
        "pod_uid_field": "metadata.uid",
        "execd_init": True,
        "security_profile_unchanged": True,
    }


async def wait_ready_pod(old_uid=None):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        pod = task_pod()
        if pod and pod_ready(pod) and pod["metadata"]["uid"] != old_uid:
            return pod
        await asyncio.sleep(0.5)
    raise RuntimeError("Owned replacement Pod did not become Ready")


def request(command, *, background=False):
    key = uuid4().hex
    return CommandRequest(
        command_id=key, invocation_id="inv-" + key, command=command, background=background
    )


async def create_or_resume(config, image):
    matches = json.loads(
        kubectl("-n", TASK, "get", "batchsandboxes", "-l", "bbx-g1-task=single", "-o", "json")
    )["items"]
    assert len(matches) <= 1, "Ambiguous original create; no replay"
    if matches:
        return await Sandbox.connect(
            matches[0]["metadata"]["name"],
            connection_config=config,
            connect_timeout=timedelta(seconds=120),
        )
    operation = "bbx-archv2-g1-" + uuid4().hex
    private_json(STATE / "create.attempted", {"operation": operation, "namespace": TASK})
    creating = asyncio.create_task(
        Sandbox.create(
            image,
            connection_config=config,
            timeout=timedelta(minutes=30),
            ready_timeout=timedelta(seconds=120),
            secure_access=True,
            metadata={"bbx-g1-task": "single", "bbx-create-op": operation},
            entrypoint=["/usr/bin/tail", "-f", "/dev/null"],
            resource={"cpu": "300m", "memory": "512Mi"},
            resource_requests={"cpu": "50m", "memory": "128Mi"},
            volumes=[
                Volume(
                    name="bbx-workspace",
                    mountPath="/workspace",
                    pvc=PVC(
                        claimName=CLAIM,
                        createIfNotExists=True,
                        deleteOnSandboxTermination=False,
                        storage="64Mi",
                    ),
                )
            ],
        )
    )
    try:
        while not creating.done():
            pod = task_pod()
            if pod:
                for status in pod.get("status", {}).get("containerStatuses", []):
                    terminated = status.get("state", {}).get("terminated", {}) or status.get(
                        "lastState", {}
                    ).get("terminated", {})
                    if status["name"] == "sandbox" and terminated.get("exitCode") == 126:
                        logs = kubectl(
                            "-n",
                            TASK,
                            "logs",
                            pod["metadata"]["name"],
                            "-c",
                            "sandbox",
                            "--tail=20",
                        )
                        if "prepare private assignment cgroup mount" in logs:
                            raise UnsupportedEnvironment(
                                "Fixed native cgroup initialization was refused in the final Pod"
                            )
                        raise RuntimeError(
                            "Final bootstrap identity/configuration refused; no capability claim"
                        )
                    if (
                        status["name"] == "sandbox"
                        and status.get("containerID")
                        and "running" in status.get("state", {})
                    ):
                        observed = cri_identity(pod, "sandbox")
                        (STATE / "sandbox-cri.json").write_text(json.dumps(observed, indent=2))
            await asyncio.sleep(0.5)
        return await creating
    finally:
        if not creating.done():
            creating.cancel()
            try:
                await creating
            except asyncio.CancelledError:
                pass


async def verify():
    owned_cluster()
    credentials = json.loads((STATE / "credentials.json").read_text())
    image = json.loads((STATE / "sandbox-image.json").read_text())
    evidence = {"cluster": CLUSTER, "task_count": 1, "checks": {}}
    journal = CommandJournal(STATE / "audit/commands.sqlite")
    sandboxes = []
    try:
        with ExitStack() as stack:
            for name, port in credentials["ports"].items():
                stack.enter_context(forward(name, port))
            config = ConnectionConfig(
                domain="127.0.0.1:" + str(credentials["ports"]["server"]),
                api_key=credentials["api_key"],
                headers={"X-EXECD-ACCESS-TOKEN": credentials["execd_token"]},
                disable_metrics=True,
                request_timeout=timedelta(seconds=150),
                retry_policy=RetryPolicy(max_retries=0, wake_retry_enabled=False),
            )
            sandbox = await create_or_resume(config, image["image"] + "@" + image["digest"])
            sandboxes.append(sandbox)
            pod = await wait_ready_pod()
            pvc_uid = capture_claim(pod, first=True)
            evidence["template"] = inspect_template(pod)
            evidence["initial_cri"] = cri_identity(pod, "sandbox")
            context = await read_execution_context(sandbox)
            assert (
                context["pod_uid"] == pod["metadata"]["uid"]
                and context["environment_revision"] == REVISION
            )
            binding = AssignmentBinding(
                task_id="bbx-g1-task",
                agent_id="worker-a",
                role="worker",
                assignment_id="bbx-g1-assignment",
                generation=1,
                epoch=1,
                sandbox_id=sandbox.id,
                pod_uid=pod["metadata"]["uid"],
                environment_revision=REVISION,
                network_identity="bbx-g1-network-1",
                namespace="worker-a",
                ipv4="198.19.1.2",
                process_accounting="cgroup-v2",
                execution_context_id=context["execution_context_id"],
            )
            adapter = SandboxExecution(sandbox, binding, journal)
            code = """import json,os,time,subprocess
from pathlib import Path
if os.fork(): raise SystemExit(0)
os.setsid()
if os.fork(): os._exit(0)
fd=os.open('/dev/null',os.O_RDWR)
for target in (0,1,2): os.dup2(fd,target)
if fd>2: os.close(fd)
data={'uid':os.getuid(),'gid':os.getgid(),'groups':os.getgroups(),
 'status':Path('/proc/self/status').read_text(),'fds':os.listdir('/proc/self/fd'),
 'cgroup':Path('/proc/self/cgroup').read_text(),
 'addresses':subprocess.check_output(['ip','-4','-o','addr'],text=True),
 'env_clean':not any(key in os.environ for key in (
  'EXECD_ACCESS_TOKEN','BBX_POD_UID','BBX_ENVIRONMENT_REVISION','EXECD_INIT'))}
Path('/workspace/g1-identity.json').write_text(json.dumps(data))
while True:
 Path('/workspace/g1-heartbeat').write_text(str(time.monotonic_ns()))
 time.sleep(.02)
"""
            command = request("python3 -I -c " + shlex.quote(code), background=True)
            private_json(
                STATE / "verify.attempted",
                {
                    "pod_uid": pod["metadata"]["uid"],
                    "command_id": command.command_id,
                    "invocation_id": command.invocation_id,
                },
            )
            await adapter.run(command)
            for _ in range(100):
                try:
                    observed = json.loads(
                        await adapter.files.read_file("/workspace/g1-identity.json")
                    )
                    break
                except Exception:
                    await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Owned setsid descendant did not become observable")
            assert (observed["uid"], observed["gid"], observed["groups"]) == (10000, 10000, [])
            assert (
                "CapEff:\t0000000000000000" in observed["status"]
                and "CapBnd:\t0000000000000000" in observed["status"]
            )
            assert (
                "NoNewPrivs:\t1" in observed["status"]
                and len(observed["fds"]) <= 4
                and observed["env_clean"]
            )
            assert observed["cgroup"].strip() == "0::/" + binding.scope_request()[0]
            assert "198.19.1.2/30" in observed["addresses"]
            await adapter.files.write_file(
                "/workspace/g1-retained.txt", "same Task low-UID workspace"
            )
            environ = await adapter.files.read_file("/proc/self/environ", offset=1, limit=1)
            if credentials["execd_token"] in environ:
                raise RuntimeError("Low-identity filesystem worker inherited the test token")
            evidence["checks"]["low_uid_files_env_caps_fds_and_assignment_netns"] = True
            receipt = await adapter.stop_assignment(command.command_id)
            assert (
                receipt["execution_context_id"] == context["execution_context_id"]
                and receipt["cgroup_namespace"] == evidence["initial_cri"]["cgroup_namespace_inode"]
            )
            evidence["initial_cleanup"] = receipt
            before = await adapter.files.read_file("/workspace/g1-heartbeat")
            await asyncio.sleep(0.1)
            assert await adapter.files.read_file("/workspace/g1-heartbeat") == before
            evidence["checks"]["real_sets_id_tree_sealed_killed_and_observed_empty"] = True
            old_uid = pod["metadata"]["uid"]
            kubectl(
                "-n", TASK, "delete", "pod", pod["metadata"]["name"], "--wait=true", "--timeout=60s"
            )
            replacement = await wait_ready_pod(old_uid)
            assert capture_claim(replacement, first=False) == pvc_uid
            evidence["replacement_cri"] = cri_identity(replacement, "sandbox")
            fresh = await Sandbox.connect(
                sandbox.id, connection_config=config, connect_timeout=timedelta(seconds=120)
            )
            sandboxes.append(fresh)
            fresh_context = await read_execution_context(fresh)
            assert fresh_context["pod_uid"] == replacement["metadata"]["uid"] != old_uid
            assert (
                fresh_context["environment_revision"] == REVISION
                and fresh_context["execution_context_id"] != context["execution_context_id"]
            )
            stale = SandboxExecution(fresh, binding, journal)
            denied = await stale.run(request("echo bad > /workspace/g1-stale-must-not-exist"))
            assert denied.exit_code == 126
            try:
                await stale.assignment_cleanup_status(command.command_id)
            except UnknownAssignmentCleanup:
                pass
            else:
                raise RuntimeError("Old scope was falsely confirmed after Pod replacement")
            renewed = SandboxExecution(
                fresh,
                replace(
                    binding,
                    generation=2,
                    pod_uid=replacement["metadata"]["uid"],
                    execution_context_id=fresh_context["execution_context_id"],
                ),
                journal,
            )
            assert (
                await renewed.files.read_file("/workspace/g1-retained.txt")
                == "same Task low-UID workspace"
            )
            short = request("test ! -e /workspace/g1-stale-must-not-exist; id -u")
            assert (await renewed.run(short)).exit_code == 0
            evidence["replacement_cleanup"] = await renewed.stop_assignment(short.command_id)
            evidence["checks"][
                "one_controller_replacement_old_binding_rejected_new_binding_valid"
            ] = True
            evidence["checks"]["pvc_first_ready_uid_and_shared_file_retained"] = True
            await fresh.kill()
            kubectl(
                "-n", TASK, "wait", "--for=delete", "batchsandbox/" + sandbox.id, "--timeout=90s"
            )
            claim = json.loads(kubectl("-n", TASK, "get", "pvc", CLAIM, "-o", "json"))
            assert claim["metadata"]["uid"] == pvc_uid
            evidence["pvc_uid"] = pvc_uid
            evidence["checks"]["sandbox_stop_retains_original_pvc"] = True
            (STATE / "evidence.json").write_text(json.dumps(evidence, indent=2))
            print(json.dumps({"result": "passed", "checks": evidence["checks"]}, indent=2))
    finally:
        for sandbox in sandboxes:
            await sandbox.close()
        journal.close()


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["setup"]:
            setup()
        elif sys.argv[1:] == ["preflight"]:
            preflight()
        elif sys.argv[1:] == ["verify"]:
            asyncio.run(verify())
        else:
            raise ValueError("Expected setup, preflight or verify")
    except UnsupportedEnvironment as error:
        (STATE / "unsupported.json").write_text(
            json.dumps({"result": "unsupported", "reason": str(error)}, indent=2)
        )
        print("Fixed profile unsupported; no permissions were widened.")
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print(
            "G1 fixture failed: "
            f"{type(error).__name__} at {Path(frame.filename).name}:{frame.lineno}",
            file=sys.stderr,
        )
        sys.exit(1)
