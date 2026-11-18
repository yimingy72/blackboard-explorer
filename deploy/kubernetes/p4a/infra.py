"""Prepare only the P4a isolated kind, fixed tools, Cilium and upstream CRDs."""

import json
import os
import runpy
import subprocess
import time

from common import CLUSTER, CONFIG, CONTEXT, HERE, NETWORK, NODE, STATE, kubectl, owned_cluster


def main():
    exists = subprocess.run(["docker", "inspect", NODE], capture_output=True).returncode == 0
    if exists:
        owned_cluster()
    # The fixed preparation imports P4a's common module and cannot address older state roots.
    runpy.run_path(str(HERE / "prepare.py"), run_name="p4a_pinned_preparation")
    env = {
        **os.environ,
        "KUBECONFIG": str(CONFIG),
        "KIND_EXPERIMENTAL_DOCKER_NETWORK": NETWORK,
        **{f"HELM_{k.upper()}_HOME": str(STATE / f"helm-{k}") for k in ("cache", "config", "data")},
    }
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
        until = time.monotonic() + 60
        while True:
            try:
                identity = owned_cluster(initial=True)
                break
            except RuntimeError:
                if time.monotonic() >= until:
                    raise
                time.sleep(0.5)
        (STATE / "cluster-identity.json").write_text(json.dumps(identity, indent=2))
    owned_cluster()
    subprocess.run(
        ["docker", "update", "--cpus", "2", "--memory", "6g", "--memory-swap", "6g", NODE],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "docker",
            "exec",
            NODE,
            "/bin/sh",
            "-c",
            'if ! grep -q "net.ipv4.ip_forward" /var/lib/kubelet/config.yaml; then '
            'printf "\\nallowedUnsafeSysctls:\\n- net.ipv4.ip_forward\\n" '
            ">> /var/lib/kubelet/config.yaml; systemctl restart kubelet; fi",
        ],
        check=True,
        capture_output=True,
    )
    loader = runpy.run_path(str(HERE / "load_images.py"), run_name="p4a_fixed_images")
    for tag, digest in loader["OFFICIAL"].items():
        loader["load"](tag, digest, allow_pull=True)
    for image in json.loads((STATE / "cilium-images.json").read_text()):
        tag, digest = image.split("@")
        loader["load"](tag, digest, allow_pull=True)
    subprocess.run(
        [
            str(STATE / "bin/helm"),
            "--kubeconfig",
            str(CONFIG),
            "--kube-context",
            CONTEXT,
            "upgrade",
            "--install",
            "bbx-p4a-cilium",
            str(STATE / "cilium.tgz"),
            "-n",
            "kube-system",
            "-f",
            str(HERE / "values/cilium.yaml"),
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    kubectl("-n", "kube-system", "rollout", "status", "daemonset/cilium", "--timeout=180s")
    kubectl("wait", "--for=condition=Ready", "node/" + NODE, "--timeout=90s")
    kubectl("apply", "-f", str(STATE / "upstream/kubernetes/config/crd/bases"))
    owned_cluster()
    network = json.loads(subprocess.check_output(["docker", "network", "inspect", NETWORK]))[0]
    if set(network["Containers"]) != {owned_cluster()["container_id"]}:
        raise RuntimeError("The private kind network contains an unexpected node")
    (STATE / "ownership.json").write_text(
        json.dumps(
            {
                "network_id": network["Id"],
                "default_context": subprocess.check_output(
                    ["kubectl", "config", "current-context"], text=True
                ).strip(),
            },
            indent=2,
        )
    )
    print("P4a private Kubernetes 1.34/Cilium/official CRDs ready; no Task or model deployment.")


if __name__ == "__main__":
    main()
