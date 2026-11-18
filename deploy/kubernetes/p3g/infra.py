"""Reuse pinned P3f inputs with a separate state directory and private kubeconfig."""

import json
import os
import runpy
import subprocess

from common import CLUSTER, CONFIG, HERE, NODE, ROOT, STATE, kubectl, owned_cluster


def main():
    # The reused preparation imports this directory's common module, never P3f state.
    runpy.run_path(str(HERE / "prepare.py"), run_name="__main__")
    env = {
        **os.environ,
        "KUBECONFIG": str(CONFIG),
        "KIND_EXPERIMENTAL_DOCKER_NETWORK": "bbx-p3f-k8s-network",
    }
    exists = subprocess.run(["docker", "inspect", NODE], capture_output=True).returncode == 0
    if not exists:
        subprocess.run(
            [
                str(STATE / "bin/kind"),
                "create",
                "cluster",
                "--name",
                CLUSTER,
                "--config",
                str(HERE / "kind.yaml"),
                "--kubeconfig",
                str(CONFIG),
                "--wait",
                "0s",
            ],
            env=env,
            check=True,
        )
    container_id = owned_cluster()
    subprocess.run(
        ["docker", "update", "--cpus", "2", "--memory", "6g", "--memory-swap", "6g", NODE],
        capture_output=True,
        check=True,
    )
    loader = runpy.run_path(str(HERE / "load_images.py"), run_name="p3g_control_images")
    for image in json.loads((STATE / "cilium-images.json").read_text()):
        tag, digest = image.split("@")
        loader["load"](tag, digest, allow_pull=True)
    subprocess.run(
        [
            str(STATE / "bin/helm"),
            "--kubeconfig",
            str(CONFIG),
            "upgrade",
            "--install",
            "bbx-p3f-cilium",
            str(STATE / "cilium.tgz"),
            "-n",
            "kube-system",
            "-f",
            str(HERE / "values/cilium.yaml"),
        ],
        capture_output=True,
        check=True,
    )
    kubectl("-n", "kube-system", "rollout", "status", "daemonset/cilium", "--timeout=180s")
    kubectl("wait", "--for=condition=Ready", "node/" + NODE, "--timeout=90s")
    kubectl("apply", "-f", str(STATE / "upstream/kubernetes/config/crd/bases"))
    node = json.loads(kubectl("get", "node", NODE, "-o", "json"))
    cluster = json.loads(kubectl("get", "namespace", "kube-system", "-o", "json"))
    pods = json.loads(
        kubectl("get", "pods", "-n", "kube-system", "-l", "component=kube-apiserver", "-o", "json")
    )["items"]
    assert len(pods) == 1
    identity = {
        "source_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "container_id": container_id,
        "node_uid": node["metadata"]["uid"],
        "node_name": NODE,
        "cluster_uid": cluster["metadata"]["uid"],
        "version": node["status"]["nodeInfo"]["kubeletVersion"],
        "retirement_single_apiserver": {
            "pod_uid": pods[0]["metadata"]["uid"],
            "node_uid": node["metadata"]["uid"],
        },
        "endpoint": kubectl(
            "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"
        ),
    }
    (STATE / "cluster-identity.json").write_text(json.dumps(identity, indent=2))
    print("P3g private endpoint, source and single-server identities recorded.")


if __name__ == "__main__":
    main()
