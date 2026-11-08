"""Only the P4i private cluster; no writes can use a previous slice's state."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = ROOT / "deploy/kubernetes/p3f"
FINAL_ATTEMPT = True
STATE = ROOT / ".data/p4i-native-seats-traffic-20261108" / "k8s"
KUBE_STATE = STATE
CLUSTER = "bbx-p4i-k8s"
NODE = CLUSTER + "-control-plane"
CONTEXT = "kind-" + CLUSTER
NETWORK = CLUSTER + "-network"
CONFIG = STATE / "kubeconfig"
SYSTEM = "bbx-p4i-system"
KUBECTL = STATE / "bin/kubectl"


def kubectl(*args, data=None):
    result = subprocess.run(
        [str(KUBECTL), "--kubeconfig", str(CONFIG), "--context", CONTEXT, *args],
        input=None if data is None else json.dumps(data).encode(),
        capture_output=True,
        timeout=190,
    )
    if result.returncode:
        raise RuntimeError("The owned Kubernetes operation was refused")
    return result.stdout.decode()


def owned_cluster(*, initial=False):
    value = json.loads(subprocess.check_output(["docker", "inspect", NODE]))[0]
    if value["Config"]["Labels"].get("io.x-k8s.kind.cluster") != CLUSTER:
        raise RuntimeError("Foreign kind node refused")
    endpoint = kubectl("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    endpoints = {
        "https://" + p["HostIp"] + ":" + p["HostPort"]
        for p in value["NetworkSettings"]["Ports"]["6443/tcp"]
    }
    if kubectl("config", "current-context").strip() != CONTEXT or endpoint not in endpoints:
        raise RuntimeError("The kubeconfig does not point to the owned API endpoint")
    node = json.loads(kubectl("get", "node", NODE, "-o", "json"))
    cluster = json.loads(kubectl("get", "namespace", "kube-system", "-o", "json"))
    identity = {
        "container_id": value["Id"],
        "node_uid": node["metadata"]["uid"],
        "cluster_uid": cluster["metadata"]["uid"],
        "node_name": NODE,
        "endpoint": endpoint,
        "version": node["status"]["nodeInfo"]["kubeletVersion"],
    }
    if not initial:
        original = json.loads((STATE / "cluster-identity.json").read_text())
        if identity != original:
            raise RuntimeError("The original private cluster identity changed")
    return identity
