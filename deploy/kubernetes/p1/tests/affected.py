"""Only retained-volume startup, cancellation and directly captured claim identity."""

import asyncio
import importlib.util
import json
import os
import shlex
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from bbx_sandbox_execution.execution import (
    AssignmentBinding,
    CommandJournal,
    CommandRequest,
    SandboxExecution,
)
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxException
from opensandbox.models.sandboxes import PVC, Volume
from opensandbox.transport import RetryPolicy

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("p1_probe", ROOT / "deploy/kubernetes/p1/probe.py")
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


async def wait_phase(namespace: str, name: str, expected: str) -> None:
    for _ in range(150):
        pod = json.loads(probe.kubectl("-n", namespace, "get", "pod", name, "-o", "json"))
        if pod["status"]["phase"] == expected:
            return
        assert pod["status"]["phase"] != "Failed", "Fixture Pod failed; preserve its evidence"
        await asyncio.sleep(0.2)
    raise AssertionError("Fixture did not finish; preserve its objects")


async def run() -> None:
    credentials = json.loads((probe.STATE / "credentials.json").read_text())
    journal = CommandJournal(probe.STATE / "review-audit" / "commands.sqlite")
    evidence = {"checks": {}, "tasks": {}}
    sandboxes = {}
    adapters = {}
    with ExitStack() as stack:
        for service in ("server", "ingress"):
            stack.enter_context(
                probe.forward("bbx-archv2-" + service, credentials["ports"][service])
            )
        try:
            for letter, namespace in probe.NAMESPACES.items():
                config = ConnectionConfig(
                    domain="127.0.0.1:" + str(credentials["ports"]["server"]),
                    api_key=credentials[letter]["key"],
                    headers={"X-EXECD-ACCESS-TOKEN": credentials[letter]["execd"]},
                    disable_metrics=True,
                    request_timeout=timedelta(seconds=120),
                    retry_policy=RetryPolicy(max_retries=0, wake_retry_enabled=False),
                )
                existing = json.loads(
                    probe.kubectl(
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
                assert len(existing) <= 1
                if existing:
                    sandbox = await Sandbox.connect(
                        existing[0]["metadata"]["name"], connection_config=config
                    )
                else:
                    fd = os.open(
                        probe.STATE / ("affected-create-" + letter + ".attempted"),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                    )
                    with os.fdopen(fd, "w") as output:
                        json.dump({"namespace": namespace, "operation_id": uuid4().hex}, output)
                        output.flush()
                        os.fsync(output.fileno())
                    sandbox = await Sandbox.create(
                        "bbx-archv2-sandbox-k8s:20261106",
                        connection_config=config,
                        secure_access=True,
                        timeout=timedelta(minutes=30),
                        ready_timeout=timedelta(seconds=120),
                        entrypoint=["/usr/bin/tail", "-f", "/dev/null"],
                        metadata={"bbx-archv2-task": letter},
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
                pod = json.loads(probe.kubectl("-n", namespace, "get", "pods", "-o", "json"))[
                    "items"
                ][0]
                assert any(
                    condition["type"] == "Ready" and condition["status"] == "True"
                    for condition in pod["status"]["conditions"]
                )
                uid = probe.capture_ready_claim(sandbox.id, namespace, pod["metadata"]["uid"])
                evidence["tasks"][letter] = {
                    "sandbox_id": sandbox.id,
                    "pod_uid": pod["metadata"]["uid"],
                    "pvc_uid_at_ready": uid,
                }
                binding = AssignmentBinding(
                    task_id=namespace,
                    agent_id="worker-a",
                    role="worker",
                    assignment_id="assignment-a",
                    generation=1,
                    epoch=1,
                    sandbox_id=sandbox.id,
                    pod_uid=pod["metadata"]["uid"],
                    environment_revision="tmp-review-1",
                    network_identity="network-a",
                    namespace="worker-a",
                    ipv4="198.19.1.2",
                )
                adapters[letter] = SandboxExecution(sandbox, binding, journal)
                await adapters[letter].files.write_file(
                    "/workspace/review-marker", "task-" + letter
                )
            assert (
                evidence["tasks"]["a"]["pvc_uid_at_ready"]
                != evidence["tasks"]["b"]["pvc_uid_at_ready"]
            )
            adapter = adapters["a"]
            attack_code = (
                "import os;assert not os.path.lexists('/workspace/.tmp-review-saved');"
                "os.rename('/workspace/.tmp','/workspace/.tmp-review-saved');"
                "os.symlink('/var/lib/bbx/control','/workspace/.tmp')"
            )
            attack = CommandRequest(
                command_id=uuid4().hex,
                invocation_id=uuid4().hex,
                command="python3 -I -c " + shlex.quote(attack_code),
            )
            assert (await adapter.run(attack)).exit_code == 0
            negative = "bbx-archv2-tmp-negative"
            script = (
                "before=$(stat -c '%u:%g:%a' /var/lib/bbx/control); "
                "if /opt/bbx/control/kubernetes-setup.sh >/tmp/startup-error 2>&1; "
                "then exit 80; fi; after=$(stat -c '%u:%g:%a' /var/lib/bbx/control); "
                'test "$before" = \'0:0:700\' && test "$after" = "$before" && '
                "grep -q 'Unsafe workspace .tmp' /tmp/startup-error && "
                "printf 'startup_refused_control_unchanged'"
            )
            probe.apply(
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": negative, "namespace": probe.NAMESPACES["a"]},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "negative",
                                "image": "bbx-archv2-sandbox-k8s:20261106",
                                "imagePullPolicy": "Never",
                                "command": ["/bin/sh", "-c", script],
                                "env": [
                                    {
                                        "name": "EXECD_ACCESS_TOKEN",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "bbx-archv2-execd",
                                                "key": "token",
                                            }
                                        },
                                    }
                                ],
                                "securityContext": {
                                    "runAsUser": 0,
                                    "capabilities": {
                                        "drop": ["ALL"],
                                        "add": [
                                            "CHOWN",
                                            "DAC_OVERRIDE",
                                            "FOWNER",
                                            "SETUID",
                                            "SETGID",
                                            "SETPCAP",
                                        ],
                                    },
                                },
                                "resources": {
                                    "requests": {"cpu": "10m", "memory": "32Mi"},
                                    "limits": {"cpu": "100m", "memory": "96Mi"},
                                },
                                "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "workspace",
                                "persistentVolumeClaim": {"claimName": "bbx-archv2-workspace"},
                            }
                        ],
                    },
                }
            )
            await wait_phase(probe.NAMESPACES["a"], negative, "Succeeded")
            assert (
                probe.kubectl("-n", probe.NAMESPACES["a"], "logs", negative)
                == "startup_refused_control_unchanged"
            )
            probe.kubectl(
                "-n",
                probe.NAMESPACES["a"],
                "delete",
                "pod",
                negative,
                "--wait=true",
                "--timeout=15s",
            )
            restore_code = (
                "import os;assert os.path.islink('/workspace/.tmp');"
                "assert os.readlink('/workspace/.tmp')=='/var/lib/bbx/control';"
                "os.unlink('/workspace/.tmp');"
                "os.rename('/workspace/.tmp-review-saved','/workspace/.tmp')"
            )
            restore = CommandRequest(
                command_id=uuid4().hex,
                invocation_id=uuid4().hex,
                command="python3 -I -c " + shlex.quote(restore_code),
            )
            assert (await adapter.run(restore)).exit_code == 0
            evidence["checks"]["retained_tmp_symlink_refused_without_control_ownership_change"] = (
                True
            )
            cancel_id = uuid4().hex
            pid_file = "/workspace/.review-cancel-" + cancel_id
            running = CommandRequest(
                command_id=cancel_id,
                invocation_id=uuid4().hex,
                command='sleep 60 & child=$!; printf \'%s %s\' "$$" "$child" > '
                + pid_file
                + "; wait",
                background=True,
            )
            result = await adapter.run(running)
            pids = []
            for _ in range(50):
                try:
                    pids = [
                        int(value) for value in (await adapter.files.read_file(pid_file)).split()
                    ]
                    if len(pids) == 2:
                        break
                except SandboxException:
                    pass
                await asyncio.sleep(0.05)
            assert len(pids) == 2
            await adapter.interrupt(running.command_id)
            status = {"running": None}
            for _ in range(50):
                status = await adapter.status(running.command_id)
                if status["running"] is False:
                    break
                await asyncio.sleep(0.1)
            assert status["running"] is False
            gone = CommandRequest(
                command_id=uuid4().hex,
                invocation_id=uuid4().hex,
                command="python3 -I -c "
                + shlex.quote(
                    "from pathlib import Path;"
                    + f"assert not any(Path('/proc/'+str(p)).exists() for p in {pids!r})"
                ),
            )
            assert (await adapter.run(gone)).exit_code == 0
            evidence["checks"]["cancellation_after_safe_startup"] = True
            evidence["cancel"] = {"command_id": running.command_id, "execution_id": result.id}
            for letter, sandbox in sandboxes.items():
                await sandbox.kill()
                namespace = probe.NAMESPACES[letter]
                for _ in range(100):
                    if not json.loads(probe.kubectl("-n", namespace, "get", "pods", "-o", "json"))[
                        "items"
                    ]:
                        break
                    await asyncio.sleep(0.2)
                assert not json.loads(probe.kubectl("-n", namespace, "get", "pods", "-o", "json"))[
                    "items"
                ]
                claim = json.loads(
                    probe.kubectl(
                        "-n", namespace, "get", "pvc", "bbx-archv2-workspace", "-o", "json"
                    )
                )
                assert claim["metadata"]["uid"] == evidence["tasks"][letter]["pvc_uid_at_ready"]
                evidence["tasks"][letter]["pvc_uid_after_stop"] = claim["metadata"]["uid"]
            evidence["checks"]["both_claims_match_direct_ready_snapshot_after_stop"] = True
            (probe.STATE / "affected-evidence.json").write_text(json.dumps(evidence, indent=2))
            print(json.dumps(evidence["checks"]))
        finally:
            for sandbox in sandboxes.values():
                await sandbox.close()
            journal.close()


asyncio.run(run())
