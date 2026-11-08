"""Build only the private P4i image variant and the reviewed Runner/Host candidate."""

import argparse
import hashlib
import json
import runpy
import shutil
import subprocess
from pathlib import Path

from common import ROOT, STATE, owned_cluster

TOOLS = "bbx-p4i-ubuntu-tools:20261107"
LEGACY = "bbx-p4i-sandbox:20261107"
CAPTURE = "bbx-p4i-capture-sandbox:20261107"
CAPTURE_FIXED = "bbx-p4i-capture-sandbox-fixed:20261108"
HOST_FIXED = "bbx-p4i-sandbox-host-fixed:20261108"
TAGS = {
    role: f"bbx-p4i-{name}:20261107"
    for role, name in {"host": "sandbox-host", "runner": "runner", "server": "server-g7"}.items()
}
UBUNTU = "ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852"


def build(tag, *args):
    with (STATE / (tag.split(":")[0] + ".log")).open("w") as log:
        subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "-t", tag, *args],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    return subprocess.check_output(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True
    ).strip()


def main(candidate):
    owned_cluster()
    tools = STATE / "tools-context/services/envd/Dockerfile"
    tools.write_text(tools.read_text().replace("FROM ubuntu:24.04\n", "FROM " + UBUNTU + "\n", 1))
    tool_id = build(
        TOOLS,
        "--build-arg",
        "APT_MIRROR=http://mirrors.aliyun.com/ubuntu",
        "--build-arg",
        "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple",
        "-f",
        str(tools),
        str(STATE / "tools-context"),
    )
    base = ["--network", "none", "--build-arg", "TOOL_IMAGE=" + TOOLS]
    source = str(candidate / "services/sandbox-execution")
    legacy_id = build(LEGACY, *base, source)
    capture_id = build(CAPTURE, *base, "--build-arg", "ASSIGNMENT_NETWORK_V1=1", source)
    helpers = runpy.run_path(str(ROOT / "deploy/kubernetes/p3f/load_images.py"))
    helpers["load"](LEGACY, legacy_id)
    helpers["load"](CAPTURE, capture_id)
    (STATE / "built-images.json").write_text(
        json.dumps(
            {
                "images": {TOOLS: tool_id, LEGACY: legacy_id, CAPTURE: capture_id},
                "source_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "environment_revision": "bbx-p4g-ubuntu-g1-20261107",
                "ubuntu_tools_dockerfile_sha256": hashlib.sha256(tools.read_bytes()).hexdigest(),
            },
            indent=2,
        )
    )
    shared = runpy.run_path(str(ROOT / "deploy/kubernetes/p3h/build_images.py"))
    shared["main"](candidate, tags=TAGS, sandbox_tag=LEGACY)
    path = STATE / "build-source.json"
    inventory = json.loads(path.read_text())
    inventory["images"][CAPTURE] = {
        "digest": capture_id,
        "reference": CAPTURE.rsplit(":", 1)[0] + "@" + capture_id,
    }
    path.write_text(json.dumps(inventory, indent=2) + "\n")
    runpy.run_path(str(ROOT / "deploy/kubernetes/p4g/build_images.py"))["gateway_image"]()


def revised(candidate):
    owned_cluster()
    old = json.loads((STATE / "attempt-1-source/build-source.json").read_text())
    inventory = STATE / "build-source-revised.json"
    if inventory.exists():
        raise RuntimeError("The corrected source inventory is immutable")
    runtime = candidate / "services/agent-runtime/src/bbx_runtime"
    host_context = STATE / "sidecar-context"
    for name in ("__init__.py", "sandbox_tools.py", "sandbox_network.py"):
        path = runtime / name
        staged = host_context / "source/bbx_runtime" / name
        if path.read_bytes() != staged.read_bytes():
            raise RuntimeError("An unrelated trusted Host source changed")
    path = runtime / "sandbox_mcp.py"
    shutil.copyfile(path, host_context / "source/bbx_runtime/sandbox_mcp.py")
    old["sidecar_sources"]["bbx_runtime/sandbox_mcp.py"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    closure = candidate / "services/sandbox-execution"
    helper = runpy.run_path(str(ROOT / "deploy/kubernetes/p3f/load_images.py"))
    changed = {
        CAPTURE_FIXED: build(
            CAPTURE_FIXED,
            "--network",
            "none",
            "--build-arg",
            "TOOL_IMAGE=" + TOOLS,
            "--build-arg",
            "ASSIGNMENT_NETWORK_V1=1",
            str(closure),
        ),
        HOST_FIXED: build(HOST_FIXED, str(host_context)),
    }
    for tag, digest in changed.items():
        helper["load"](tag, digest)
        old["images"][tag] = {"digest": digest, "reference": tag.rsplit(":", 1)[0] + "@" + digest}
    old["corrected_sourced_hook_sha256"] = hashlib.sha256(
        (closure / "launcher/kubernetes-setup.sh").read_bytes()
    ).hexdigest()
    inventory.write_text(json.dumps(old, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--revised-startup", action="store_true")
    args = parser.parse_args()
    (revised if args.revised_startup else main)(args.candidate.resolve())
