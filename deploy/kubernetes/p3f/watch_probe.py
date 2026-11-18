"""One native Pod exit only; no Task, secrets, volumes, Pi, model or Sandbox API."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / ".data/p3f-k8s-20261106"
CANDIDATE = ROOT.parent / "p3f-bootstrap-20261106/candidate"
sys.path[:0] = [
    str(CANDIDATE / p) for p in ["services/agent-runtime/src", "packages/contracts/src"]
]
os.environ["PATH"] = str(ROOT / "bin") + os.pathsep + os.environ.get("PATH", "")


async def main():
    from bbx_runtime.runner_resources import RunnerResources

    cfg = json.loads((ROOT / "joint/resource-config.json").read_text())
    resources = RunnerResources(cfg)
    await resources._cluster()
    namespace = "bbx-p3f-live-watch"
    name = "exit-only"
    nsdoc = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {"bbx.dev/owner": "p3f", "bbx.dev/purpose": "live-exit-only"},
        },
    }
    assert await resources._get("namespace", namespace) is None
    await resources._kubectl("create", "-f", "-", body=nsdoc)
    ns = await resources._get("namespace", namespace)
    nsuid = ns["metadata"]["uid"]
    poddoc = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"bbx.dev/owner": "p3f", "bbx.dev/purpose": "live-exit-only"},
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 10,
            "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": True},
            "containers": [
                {
                    "name": "runner",
                    "image": cfg["runner_image"],
                    "command": [
                        "node",
                        "-e",
                        "process.on('SIGTERM',()=>process.exit(0));setInterval(()=>{},1000)",
                    ],
                    "securityContext": {
                        "readOnlyRootFilesystem": True,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"cpu": "100m", "memory": "128Mi"},
                    },
                }
            ],
        },
    }
    await resources._kubectl("create", "-f", "-", body=poddoc)
    end = asyncio.get_running_loop().time() + 60
    while True:
        pod = await resources._get("pod", name, namespace)
        ref = resources._pod_projection(pod)
        if (
            ref.get("container_id")
            and pod["status"]["containerStatuses"][0]["state"].get("running") is not None
        ):
            break
        if asyncio.get_running_loop().time() > end:
            raise RuntimeError("Tiny Pod did not run")
        await asyncio.sleep(0.2)
    watch = asyncio.create_task(resources._terminated_watch(ref, namespace))
    await asyncio.sleep(0)
    await resources._kubectl(
        "delete",
        "--raw",
        f"/api/v1/namespaces/{namespace}/pods/{name}",
        "-f",
        "-",
        body={
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": ref["pod_uid"]},
            "gracePeriodSeconds": 10,
        },
    )
    confirmed = await watch
    assert confirmed
    evidence = {
        "scope": "Single independent native Pod exit only; no Task/Pi/Sandbox/provider/Secret/PVC",
        "kubectl": "v1.34.0",
        "namespace_uid": nsuid,
        "pod_uid": ref["pod_uid"],
        "container_id": ref["container_id"],
        "container_name": ref["container_name"],
        "watch_start_resource_version": ref["resource_version"],
        "production_parser_sha256": hashlib.sha256(
            (CANDIDATE / "services/agent-runtime/src/bbx_runtime/runner_resources.py").read_bytes()
        ).hexdigest(),
        "original_exit_confirmed": confirmed,
        "delete_preconditions_uid": ref["pod_uid"],
    }
    await resources._kubectl(
        "delete",
        "--raw",
        f"/api/v1/namespaces/{namespace}",
        "-f",
        "-",
        body={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": nsuid}},
    )
    end = asyncio.get_running_loop().time() + 45
    while await resources._get("namespace", namespace):
        if asyncio.get_running_loop().time() > end:
            raise RuntimeError("Owned namespace cleanup unresolved")
        await asyncio.sleep(0.2)
    evidence["namespace_and_pod_removed"] = True
    (ROOT / "live-watch-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


asyncio.run(main())
