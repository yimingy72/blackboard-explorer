"""P4g images only, from fixed upstream/HEAD inputs and a reviewed candidate."""

import argparse
import hashlib
import json
import runpy
import subprocess
from pathlib import Path

from common import HERE, ROOT, STATE, owned_cluster

TAGS = {
    role: f"bbx-p4g-{name}:20261107"
    for role, name in {"host": "sandbox-host", "runner": "runner", "server": "server-g7"}.items()
}
TOOLS = "bbx-p4g-ubuntu-tools:20261107"
SANDBOX = "bbx-p4g-sandbox:20261107"
def main(candidate):
    owned_cluster()
    tools = STATE / "tools-context/services/envd/Dockerfile"
    ubuntu = "ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852"
    tools.write_text(tools.read_text().replace("FROM ubuntu:24.04\n", "FROM " + ubuntu + "\n", 1))
    builds = {
        TOOLS: [
            "--build-arg",
            "APT_MIRROR=http://mirrors.aliyun.com/ubuntu",
            "--build-arg",
            "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple",
            "-f",
            str(tools),
            str(STATE / "tools-context"),
        ],
        SANDBOX: [
            "--network",
            "none",
            "--build-arg",
            "TOOL_IMAGE=" + TOOLS,
            str(STATE / "sandbox-context/services/sandbox-execution"),
        ],
    }
    images = {}
    for tag, args in builds.items():
        with (STATE / (tag.split(":")[0] + ".log")).open("w") as log:
            subprocess.run(
                ["docker", "build", "--platform", "linux/amd64", "-t", tag, *args],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        images[tag] = subprocess.check_output(
            ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
            text=True,
        ).strip()
    loader = runpy.run_path(str(HERE / "load_images.py"), run_name="p4g_sandbox_image")
    loader["load"](SANDBOX, images[SANDBOX])
    (STATE / "built-images.json").write_text(
        json.dumps(
            {
                "images": images,
                "source_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "environment_revision": "bbx-p4g-ubuntu-g1-20261107",
                "ubuntu_tools_dockerfile_sha256": hashlib.sha256(tools.read_bytes()).hexdigest(),
            },
            indent=2,
        )
    )
    shared = runpy.run_path(
        str(ROOT / "deploy/kubernetes/p3h/build_images.py"), run_name="p4g_sidecar_build"
    )
    shared["main"](candidate, tags=TAGS, sandbox_tag=SANDBOX)
    print("P4g candidate images imported; no Task was launched.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    main(parser.parse_args().candidate.resolve())
