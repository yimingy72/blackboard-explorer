"""P4f images only, from fixed upstream/HEAD inputs and a reviewed candidate."""

import argparse
import hashlib
import json
import runpy
import subprocess
from pathlib import Path

from common import HERE, NODE, ROOT, STATE, owned_cluster

TAGS = {
    role: f"bbx-p4f-{name}:20261107"
    for role, name in {"host": "sandbox-host", "runner": "runner", "server": "server-g7"}.items()
}
TOOLS = "bbx-p4f-ubuntu-tools:20261107"
SANDBOX = "bbx-p4f-sandbox:20261107"
GATEWAY = (
    "cr.agentgateway.dev/agentgateway@sha256:"
    "60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b"
)


def gateway_image():
    # Save the fixed digest without adding or deleting any host registry tag.
    digest = GATEWAY.split("@", 1)[1]
    inspect = ["docker", "image", "inspect", GATEWAY, "--format", "{{.Id}}"]
    if subprocess.run(inspect, capture_output=True).returncode:
        subprocess.run(["docker", "pull", "--platform", "linux/amd64", GATEWAY], check=True)
    if subprocess.check_output(inspect, text=True).strip() != digest:
        raise RuntimeError("The fixed gateway image identity changed")
    ctr = ["docker", "exec", NODE, "ctr", "--namespace=k8s.io", "images"]
    alias = subprocess.check_output([*ctr, "list", "--quiet", "name==" + GATEWAY]).strip()
    if not alias:
        saved = subprocess.Popen(["docker", "image", "save", GATEWAY], stdout=subprocess.PIPE)
        try:
            imported = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    NODE,
                    "ctr",
                    "--namespace=k8s.io",
                    "images",
                    "import",
                    "--platform",
                    "linux/amd64",
                    "--digests",
                    "--label",
                    "io.cri-containerd.image=managed",
                    "-",
                ],
                stdin=saved.stdout,
                capture_output=True,
            )
            assert saved.stdout is not None
            saved.stdout.close()
            if imported.returncode:
                saved.terminate()
            if saved.wait() or imported.returncode:
                raise RuntimeError("The private gateway image import failed")
        finally:
            if saved.poll() is None:
                saved.terminate()
                saved.wait()
    rows = subprocess.check_output([*ctr, "list", "name==" + GATEWAY], text=True).splitlines()
    if len(rows) != 2 or rows[1].split()[2] != digest:
        imported = [
            cols[0]
            for row in subprocess.check_output([*ctr, "list"], text=True).splitlines()[1:]
            if len(cols := row.split()) >= 3 and cols[2] == digest and cols[0] != GATEWAY
        ]
        if not imported:
            raise RuntimeError("The private node lacks the original gateway manifest")
        subprocess.run(
            [*ctr, "tag", "--force", imported[0], GATEWAY], check=True, capture_output=True
        )
        rows = subprocess.check_output([*ctr, "list", "name==" + GATEWAY], text=True).splitlines()
    if len(rows) != 2 or rows[1].split()[2] != digest:
        raise RuntimeError("The private node lacks the original gateway manifest")
    subprocess.run(
        [*ctr, "label", GATEWAY, "io.cri-containerd.image=managed"], check=True, capture_output=True
    )


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
    loader = runpy.run_path(str(HERE / "load_images.py"), run_name="p4f_sandbox_image")
    loader["load"](SANDBOX, images[SANDBOX])
    (STATE / "built-images.json").write_text(
        json.dumps(
            {
                "images": images,
                "source_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "environment_revision": "bbx-p4f-ubuntu-g1-20261107",
                "ubuntu_tools_dockerfile_sha256": hashlib.sha256(tools.read_bytes()).hexdigest(),
            },
            indent=2,
        )
    )
    shared = runpy.run_path(
        str(ROOT / "deploy/kubernetes/p3h/build_images.py"), run_name="p4f_sidecar_build"
    )
    shared["main"](candidate, tags=TAGS, sandbox_tag=SANDBOX)
    gateway_image()
    print("P4f candidate images and fixed gateway imported; no Task was launched.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    main(parser.parse_args().candidate.resolve())
