"""Task-local paths and ownership checks, never the default cluster context."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
STATE = ROOT / ".data/bbx-archv2-g1-k8s"
CONFIG = STATE / "kubeconfig"
CLUSTER = "bbx-archv2-g1-k8s"
NODE = CLUSTER + "-control-plane"
SYSTEM = CLUSTER + "-system"
TASK = CLUSTER + "-task"
REVISION = "bbx-g1-k8s-environment-1"
IMAGE = "bbx-archv2-sandbox-g1-k8s:20261106"
CLAIM = CLUSTER + "-workspace"


class UnsupportedEnvironment(RuntimeError):
    """The fixed profile cannot run; no permission fallback is allowed."""


def owned_node():
    labels = json.loads(
        subprocess.check_output(
            [
                "docker",
                "inspect",
                NODE,
                "--format",
                "{{json .Config.Labels}}",
            ],
            text=True,
        )
    )
    if labels.get("io.x-k8s.kind.cluster") != CLUSTER:
        raise RuntimeError("Foreign kind node refused")


def kubectl(*args, data=None):
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(CONFIG), *args],
        input=json.dumps(data).encode() if data is not None else None,
        capture_output=True,
    )
    if result.returncode:
        # Pod events and CRI/SDK failures may carry route credentials.
        raise RuntimeError("Owned kubectl operation failed")
    return result.stdout.decode()


def owned_cluster():
    owned_node()
    if kubectl("config", "current-context").strip() != "kind-" + CLUSTER:
        raise RuntimeError("Foreign kubeconfig refused")
    ports = json.loads(
        subprocess.check_output(
            [
                "docker",
                "inspect",
                NODE,
                "--format",
                "{{json .NetworkSettings.Ports}}",
            ],
            text=True,
        )
    )["6443/tcp"]
    expected = {"https://" + item["HostIp"] + ":" + item["HostPort"] for item in ports}
    endpoint = kubectl("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    if endpoint not in expected:
        raise RuntimeError("Kubeconfig does not point at the owned node API endpoint")


def apply(document):
    kubectl("apply", "-f", "-", data=document)
