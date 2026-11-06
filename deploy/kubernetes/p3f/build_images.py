"""Build this slice from exported HEAD inputs and the two owned Runner additions."""

import hashlib
import json
import shutil
import subprocess
import tarfile

from common import ROOT, STATE, owned_cluster
from load_images import load


def run(command, log):
    with (STATE / log).open("w") as output:
        subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, check=True)


def digest(tag):
    return subprocess.check_output(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True
    ).strip()


source = json.loads((STATE / "build-source.json").read_text())
ubuntu = "ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852"
run(["docker", "pull", "--platform", "linux/amd64", ubuntu], "pull-ubuntu.log")
run(
    [
        "docker",
        "pull",
        "--platform",
        "linux/amd64",
        "node:22.21.1-bookworm-slim@sha256:25b3eb23a00590b7499f2a2ce939322727fcce1b15fdd69754fcd09536a3ae2c",
    ],
    "pull-runner-node.log",
)
tools = STATE / "tools-context/services/envd/Dockerfile"
text = tools.read_text()
if "FROM ubuntu:24.04\n" in text:
    tools.write_text(text.replace("FROM ubuntu:24.04\n", "FROM " + ubuntu + "\n", 1))
run(
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
        "bbx-p3f-legacy-tools:20261106",
        str(STATE / "tools-context"),
    ],
    "build-tools.log",
)
tools_digest = digest("bbx-p3f-legacy-tools:20261106")
run(
    [
        "docker",
        "build",
        "--network",
        "none",
        "--build-arg",
        "TOOL_IMAGE=bbx-p3f-legacy-tools:20261106",
        "-t",
        "bbx-p3f-sandbox:20261106",
        str(STATE / "sandbox-context/services/sandbox-execution"),
    ],
    "build-sandbox.log",
)
assert digest("bbx-p3f-legacy-tools:20261106") == tools_digest
runner = STATE / "runner-context"
runner.mkdir(exist_ok=True)
archive_path = STATE / "runner-head.tar"
archive_path.write_bytes(
    subprocess.check_output(
        ["git", "archive", source["head"], "services/agent-runtime/task-runner"], cwd=ROOT
    )
)
with tarfile.open(archive_path) as archive:
    archive.extractall(runner, filter="data")
for name in ["Dockerfile", "src/bootstrap.mjs"]:
    shutil.copyfile(
        ROOT / "services/agent-runtime/task-runner" / name,
        runner / "services/agent-runtime/task-runner" / name,
    )
run(
    [
        "docker",
        "build",
        "-t",
        "bbx-p3f-runner:20261106",
        str(runner / "services/agent-runtime/task-runner"),
    ],
    "build-runner.log",
)
run(
    [
        "docker",
        "build",
        "--network",
        "none",
        "-t",
        "bbx-p3f-server-g7:20261106",
        str(STATE / "server-context"),
    ],
    "build-server.log",
)
images = {
    tag: {"digest": digest(tag), "reference": tag.split(":")[0] + "@" + digest(tag)}
    for tag in [
        "bbx-p3f-legacy-tools:20261106",
        "bbx-p3f-sandbox:20261106",
        "bbx-p3f-runner:20261106",
        "bbx-p3f-server-g7:20261106",
    ]
}
source["runner_additions_sha256"] = {
    name: hashlib.sha256(
        (ROOT / "services/agent-runtime/task-runner" / name).read_bytes()
    ).hexdigest()
    for name in ["Dockerfile", "src/bootstrap.mjs"]
}
source["images"] = images
source["environment_revision"] = "bbx-p3f-ubuntu-g1-20261106"
(STATE / "build-source.json").write_text(json.dumps(source, indent=2))
owned_cluster()
for tag, value in images.items():
    if tag != "bbx-p3f-legacy-tools:20261106":
        load(tag, value["digest"])
print("HEAD-only images built and imported; source and final digests recorded.")
