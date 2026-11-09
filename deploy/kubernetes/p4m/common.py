"""Bind inherited acceptance helpers to one isolated P4m environment."""

import runpy
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3]
ROOT = SOURCE.parents[2] if SOURCE.name == "candidate" else SOURCE
HERE = SOURCE / "deploy/kubernetes/p3f"
STATE = ROOT / ".data/p4m-task-network-20261109/k8s"
KUBE_STATE = STATE
FINAL_ATTEMPT = True
CLUSTER = "bbx-p4m-k8s"
NODE = CLUSTER + "-control-plane"
CONTEXT = "kind-" + CLUSTER
NETWORK = CLUSTER + "-network"
CONFIG = STATE / "kubeconfig"
SYSTEM = "bbx-p4m-system"
KUBECTL = STATE / "bin/kubectl"

_shared = runpy.run_path(str(SOURCE / "deploy/kubernetes/p4l/common.py"))
kubectl = _shared["kubectl"]
owned_cluster = _shared["owned_cluster"]
kubectl.__globals__.update(
    STATE=STATE, NODE=NODE, CONTEXT=CONTEXT, CONFIG=CONFIG, KUBECTL=KUBECTL, CLUSTER=CLUSTER
)
