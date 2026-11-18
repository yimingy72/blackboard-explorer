"""HEAD-exported Ubuntu tools and existing official launcher, reusing local build cache."""

import hashlib
import json
import runpy
import subprocess

from common import HERE, ROOT, STATE, owned_cluster


def main():
    owned_cluster()
    tools = STATE / "tools-context/services/envd/Dockerfile"
    base = "ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852"
    tools.write_text(tools.read_text().replace("FROM ubuntu:24.04\n", "FROM " + base + "\n", 1))
    tags = ["bbx-p3g-ubuntu-tools:20261107", "bbx-p3g-sandbox:20261107"]
    commands = [
        [
            "docker",
            "build",
            "--build-arg",
            "APT_MIRROR=http://mirrors.aliyun.com/ubuntu",
            "--build-arg",
            "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple",
            "-f",
            str(tools),
            "-t",
            tags[0],
            str(STATE / "tools-context"),
        ],
        [
            "docker",
            "build",
            "--network",
            "none",
            "--build-arg",
            "TOOL_IMAGE=" + tags[0],
            "-t",
            tags[1],
            str(STATE / "sandbox-context/services/sandbox-execution"),
        ],
    ]
    for tag, command in zip(tags, commands, strict=True):
        with (STATE / (tag.split(":")[0] + ".log")).open("w") as log:
            subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    images = {
        tag: subprocess.check_output(
            ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True
        ).strip()
        for tag in tags
    }
    loader = runpy.run_path(str(HERE / "load_images.py"), run_name="p3g_image_loader")
    loader["load"](tags[1], images[tags[1]])
    receipt = {
        "source_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "images": images,
        "environment_revision": "bbx-p3g-ubuntu-g1-20261107",
        "dockerfile_sha256": hashlib.sha256(tools.read_bytes()).hexdigest(),
        "not_kali_toolset": True,
    }
    (STATE / "built-images.json").write_text(json.dumps(receipt, indent=2))
    print("Own Ubuntu verification image imported by exact digest.")


if __name__ == "__main__":
    main()
