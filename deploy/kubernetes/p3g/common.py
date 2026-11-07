"""Only this slice's private kind endpoint and ownership, before any cluster write."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = ROOT / "deploy/kubernetes/p3f"
STATE = ROOT / ".data/p3g-retained-20261107"
CLUSTER = "bbx-p3f-k8s"
NODE = CLUSTER + "-control-plane"
CONFIG = STATE / "kubeconfig"
SYSTEM = "bbx-p3f-system"
KUBECTL = STATE / "bin/kubectl"


def kubectl(*args, data=None):
    result = subprocess.run(
        [str(KUBECTL), "--kubeconfig", str(CONFIG), *args],
        input=None if data is None else json.dumps(data).encode(),
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("Owned Kubernetes operation refused")
    return result.stdout.decode()


def owned_cluster():
    value = json.loads(subprocess.check_output(["docker", "inspect", NODE]))[0]
    if value["Config"]["Labels"].get("io.x-k8s.kind.cluster") != CLUSTER:
        raise RuntimeError("Foreign kind node refused")
    if kubectl("config", "current-context").strip() != "kind-" + CLUSTER:
        raise RuntimeError("Foreign context refused")
    endpoints = {
        "https://" + p["HostIp"] + ":" + p["HostPort"]
        for p in value["NetworkSettings"]["Ports"]["6443/tcp"]
    }
    actual = kubectl("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    if actual not in endpoints:
        raise RuntimeError("Kubeconfig does not point to the owned node endpoint")
    return value["Id"]
