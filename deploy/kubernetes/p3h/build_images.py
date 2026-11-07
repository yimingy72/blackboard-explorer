"""Build the reviewed sidecar closure, Runner and official G7 overlay from isolated inputs."""

import argparse
import hashlib
import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path

from common import HERE, KUBE_STATE, STATE, owned_cluster

TAGS = {
    "host": "bbx-p3h-sandbox-host:20261107",
    "runner": "bbx-p3h-runner:20261107",
    "server": "bbx-p3h-server-g7:20261107",
}


def main(candidate, *, tags=TAGS, sandbox_tag="bbx-p3g-sandbox:20261107"):
    owned_cluster()
    if candidate == Path(__file__).resolve().parents[3] or not (candidate / "uv.lock").is_file():
        raise RuntimeError("A clean reviewed candidate is required")
    context = STATE / "sidecar-context"
    source = context / "source"
    (source / "bbx_runtime").mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ["__init__.py", "sandbox_mcp.py", "sandbox_tools.py"]:
        original = candidate / "services/agent-runtime/src/bbx_runtime" / name
        shutil.copyfile(original, source / "bbx_runtime" / name)
        files["bbx_runtime/" + name] = hashlib.sha256(original.read_bytes()).hexdigest()
    for path, name in [
        ("packages/contracts/src/bbx_contracts", "bbx_contracts"),
        ("services/sandbox-execution/src/bbx_sandbox_execution", "bbx_sandbox_execution"),
    ]:
        shutil.copytree(
            candidate / path,
            source / name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    shutil.copyfile(Path(__file__).with_name("Dockerfile"), context / "Dockerfile")
    deps = context / "requirements.in"
    deps.write_text("opensandbox\nmcp\nhttpx\nuvicorn\nstarlette\n")
    constraints = context / "workspace-constraints.txt"
    env = {**os.environ, "UV_CACHE_DIR": str(STATE / "uv-cache")}
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--all-packages",
            "--no-emit-local",
            "--no-dev",
            "--no-hashes",
            "--output-file",
            str(constraints),
        ],
        cwd=candidate,
        env=env,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "compile",
            str(deps),
            "--constraint",
            str(constraints),
            "--python-version",
            "3.12",
            "--python-platform",
            "x86_64-unknown-linux-gnu",
            "--generate-hashes",
            "--output-file",
            str(context / "requirements.txt"),
        ],
        cwd=candidate,
        env=env,
        capture_output=True,
        check=True,
    )
    runner = STATE / "runner-context"
    runner.mkdir(exist_ok=True)
    original = candidate / "services/agent-runtime/task-runner"
    for name in ["Dockerfile", "package.json", "package-lock.json", "protocol.schema.json"]:
        shutil.copyfile(original / name, runner / name)
    shutil.copytree(original / "src", runner / "src", dirs_exist_ok=True)
    loader = runpy.run_path(str(HERE / "load_images.py"), run_name="p3h_images")
    images = {}
    for role, path in [
        ("host", context),
        ("runner", runner),
        ("server", KUBE_STATE / "server-context"),
    ]:
        tag = tags[role]
        with (STATE / f"build-{role}.log").open("w") as log:
            subprocess.run(
                ["docker", "build", "--platform", "linux/amd64", "-t", tag, str(path)],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        digest = subprocess.check_output(
            ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True
        ).strip()
        loader["load"](tag, digest)
        images[tag] = {"digest": digest, "reference": tag.rsplit(":", 1)[0] + "@" + digest}
    inherited = json.loads((KUBE_STATE / "built-images.json").read_text())
    sandbox = sandbox_tag
    digest = inherited["images"][sandbox]
    images[sandbox] = {"digest": digest, "reference": sandbox.rsplit(":", 1)[0] + "@" + digest}
    identity = json.loads((KUBE_STATE / "server-context/identity.json").read_text())
    receipt = {
        "candidate": str(candidate),
        "uv_lock_sha256": hashlib.sha256((candidate / "uv.lock").read_bytes()).hexdigest(),
        "sidecar_sources": files,
        "requirements_sha256": hashlib.sha256(
            (context / "requirements.txt").read_bytes()
        ).hexdigest(),
        "images": images,
        "environment_revision": inherited["environment_revision"],
        "g7_identity": identity,
    }
    (STATE / "build-source.json").write_text(json.dumps(receipt, indent=2))
    print("Exact sidecar/Runner/G7 images imported; original reviewed Ubuntu Sandbox reused.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    main(parser.parse_args().candidate.resolve())
