"""Destroy only the private, UID-verified slice cluster after joint evidence is saved."""

import argparse
import json
import subprocess

from common import CLUSTER, NODE, STATE, kubectl, owned_cluster

parser = argparse.ArgumentParser()
parser.add_argument("--destroy-owned-cluster", action="store_true")
args = parser.parse_args()
if not args.destroy_owned_cluster:
    raise SystemExit("Joint verification must finish before explicit owned-cluster cleanup")
expected = json.loads((STATE / "cluster-identity.json").read_text())
if owned_cluster() != expected["container_id"]:
    raise SystemExit("Original kind node identity changed; cleanup refused")
node = json.loads(kubectl("get", "node", NODE, "-o", "json"))
if node["metadata"]["uid"] != expected["node_uid"]:
    raise SystemExit("Original Kubernetes Node UID changed; cleanup refused")
allowed = {
    "cilium-secrets",
    "default",
    "kube-node-lease",
    "kube-public",
    "kube-system",
    "local-path-storage",
    "bbx-p3f-system",
}
namespaces = json.loads(kubectl("get", "namespaces", "-o", "json"))["items"]
for ns in namespaces:
    meta = ns["metadata"]
    if (
        meta["name"] not in allowed
        and meta.get("annotations", {}).get("bbx.dev/source-id") != "bbx-p3f-local-k8s-20261106"
    ):
        raise SystemExit("An unknown namespace exists; cleanup refused")
claims = json.loads(kubectl("get", "pvc", "-A", "-o", "json"))["items"]
if any(
    c["metadata"].get("annotations", {}).get("bbx.dev/source-id") != "bbx-p3f-local-k8s-20261106"
    for c in claims
):
    raise SystemExit("An unknown PVC exists; cleanup refused")
projection = {
    "node": expected,
    "namespaces": [
        {"name": ns["metadata"]["name"], "uid": ns["metadata"]["uid"]} for ns in namespaces
    ],
    "retained_claims_before_test_cluster_cleanup": [
        {
            "namespace": c["metadata"]["namespace"],
            "name": c["metadata"]["name"],
            "uid": c["metadata"]["uid"],
        }
        for c in claims
    ],
}
(STATE / "cleanup-uids.json").write_text(json.dumps(projection, indent=2))
subprocess.run(
    [
        str(STATE / "bin/kind"),
        "delete",
        "cluster",
        "--name",
        CLUSTER,
        "--kubeconfig",
        str(STATE / "kubeconfig"),
    ],
    check=True,
)
network = "bbx-p3f-k8s-network"
raw = subprocess.run(["docker", "network", "inspect", network], capture_output=True, text=True)
if raw.returncode == 0 and not json.loads(raw.stdout)[0]["Containers"]:
    subprocess.run(["docker", "network", "rm", network], check=True, capture_output=True)
print("Only the original private test cluster and its empty owned network were removed.")
