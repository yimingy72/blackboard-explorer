"""Build the isolated VPN variant over the already verified Kali tool image."""

import argparse
import hashlib
import json
import runpy
import subprocess
from pathlib import Path

from common import ROOT, STATE, owned_cluster

TOOL = "bbx-p4j-ctf-tools:eae04778af44-sixth"
VPN = "bbx-p4l-vpn-sandbox:20261108-aead"
PLUGIN = "squat/generic-device-plugin:p4l-probe-0.2.0"
PLUGIN_DIGEST = "sha256:bf312a67334be792d3a3d3042c99225aec469957dc1737911970aebbf82d1545"


def build(candidate):
    owned_cluster()
    capture = runpy.run_path(str(ROOT / "deploy/kubernetes/p4i/build_images.py"))
    capture["main"].__globals__.update(
        {
            name: tag.replace("bbx-p4i-", "bbx-p4l-")
            for name, tag in capture.items()
            if name in {"TOOLS", "LEGACY", "CAPTURE"}
        },
        TAGS={name: tag.replace("bbx-p4i-", "bbx-p4l-") for name, tag in capture["TAGS"].items()},
    )
    capture["main"](candidate)
    build_vpn(candidate, capture["build"])


def build_vpn(candidate, docker_build):
    owned_cluster()
    overlay = candidate / "services/sandbox-execution"
    fixed_tool = json.loads(
        (
            ROOT / ".data/p4j-native-ctf-product-20261108/kali-overlay-final/image-release.json"
        ).read_text()
    )["base"]["reference"]
    local_ref = TOOL + "@" + fixed_tool.rsplit("@", 1)[1]
    original = subprocess.check_output(
        ["docker", "image", "inspect", local_ref, "--format", "{{.Id}}"]
    )
    assert original == subprocess.check_output(
        ["docker", "image", "inspect", TOOL, "--format", "{{.Id}}"]
    )
    digest = docker_build(
        VPN,
        "--pull=false",
        "--build-arg",
        "TOOL_IMAGE=" + TOOL,
        "--build-arg",
        "ASSIGNMENT_NETWORK_V1=1",
        "--build-arg",
        "TASK_VPN_V1=1",
        str(overlay),
    )
    assert original == subprocess.check_output(
        ["docker", "image", "inspect", TOOL, "--format", "{{.Id}}"]
    )
    loader = runpy.run_path(str(ROOT / "deploy/kubernetes/p3f/load_images.py"))
    loader["load"](VPN, digest)
    loader["load"](PLUGIN, PLUGIN_DIGEST)
    path = STATE / "build-source.json"
    inventory = json.loads(path.read_text())
    inventory["images"][VPN] = {"digest": digest, "reference": VPN.rsplit(":", 1)[0] + "@" + digest}
    inventory["vpn_source"] = {
        "tools": fixed_tool,
        "helper_sha256": hashlib.sha256(
            (overlay / "launcher/task-vpn.py").read_bytes()
        ).hexdigest(),
        "plugin": PLUGIN.rsplit(":", 1)[0] + "@" + PLUGIN_DIGEST,
    }
    path.write_text(json.dumps(inventory, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    build(parser.parse_args().candidate.resolve())
