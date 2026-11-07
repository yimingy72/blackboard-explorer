"""Use only the private P3g cluster; all P3h evidence has a separate state root."""

import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = ROOT / "deploy/kubernetes/p3f"
STATE = ROOT / ".data/p3h-sandbox-tools-20261107"
BASE = runpy.run_path(str(ROOT / "deploy/kubernetes/p3g/common.py"))
KUBE_STATE = BASE["STATE"]
CONFIG = BASE["CONFIG"]
KUBECTL = BASE["KUBECTL"]
NODE = BASE["NODE"]
SYSTEM = "bbx-p3h-system"
kubectl = BASE["kubectl"]


def owned_cluster():
    expected = json.loads((KUBE_STATE / "cluster-identity.json").read_text())
    if BASE["owned_cluster"]() != expected["container_id"]:
        raise RuntimeError("Foreign kind node UID refused")
    node = json.loads(kubectl("get", "node", NODE, "-o", "json"))
    cluster = json.loads(kubectl("get", "namespace", "kube-system", "-o", "json"))
    if (
        node["metadata"]["uid"] != expected["node_uid"]
        or cluster["metadata"]["uid"] != expected["cluster_uid"]
    ):
        raise RuntimeError("The private cluster identity changed")
    return expected["container_id"]
