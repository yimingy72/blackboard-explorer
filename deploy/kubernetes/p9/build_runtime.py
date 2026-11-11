"""Build and import a P9 App or Runtime artifact from an exact Git revision."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

from data_transfer import NODE, checked, digest_file

NODE_IMAGE = "node@sha256:25b3eb23a00590b7499f2a2ce939322727fcce1b15fdd69754fcd09536a3ae2c"
PYTHON_IMAGE = "python@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30"


def manifest_digest(path, tag):
    with tarfile.open(path) as bundle:

        def get(name):
            with bundle.extractfile(name) as file:
                return file.read()

        index = json.loads(get("index.json"))
        found = [
            item
            for item in index["manifests"]
            if item.get("annotations", {}).get("org.opencontainers.image.ref.name")
            in {tag, "docker.io/library/" + tag}
        ]
        if not found and len(index["manifests"]) == 1:
            found = index["manifests"]
        assert len(found) == 1, "Ambiguous OCI image export"
        descriptor = found[0]
        while True:
            sha = descriptor["digest"].removeprefix("sha256:")
            assert re.fullmatch(r"[a-f0-9]{64}", sha)
            content = get(f"blobs/sha256/{sha}")
            assert hashlib.sha256(content).hexdigest() == sha
            manifest = json.loads(content)
            if "manifests" not in manifest:
                assert manifest.get("config") and manifest.get("layers")
                return descriptor["digest"]
            candidates = [
                item
                for item in manifest["manifests"]
                if item.get("platform", {}).get("os") == "linux"
                and item.get("platform", {}).get("architecture") == "amd64"
            ]
            assert len(candidates) == 1, "Expected one Linux amd64 Runtime"
            descriptor = candidates[0]


def build(git_dir, revision, output, component="runtime"):
    assert re.fullmatch(r"[a-f0-9]{40}", revision)
    os.umask(0o077)
    output.mkdir(mode=0o700)
    source, source_tar, image_tar = (
        output / name for name in ("source", "source.tar", f"{component}.oci.tar")
    )
    source.mkdir(mode=0o700)
    git = ("git", f"--git-dir={git_dir}")
    tree = checked((*git, "rev-parse", f"{revision}^{{tree}}")).decode().strip()
    with source_tar.open("wb") as file:
        subprocess.run((*git, "archive", "--format=tar", revision), stdout=file, check=True)
    # The enclosing artifact directory stays private. The image source and generated
    # web assets must be readable by the workload's non-root user.
    previous_umask = os.umask(0o022)
    try:
        with tarfile.open(source_tar) as bundle:
            bundle.extractall(source, filter="data")
        if component == "blackboard":
            with (output / "web-build.log").open("wb") as log:
                for command in (("install", "--frozen-lockfile"), ("build",)):
                    subprocess.run(
                        ("pnpm", "--dir", str(source / "web"), *command),
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                    )
            assert (source / "web/dist/index.html").is_file()
    finally:
        os.umask(previous_umask)
    assert not (source / ".git").exists() and not (source / ".env").exists()
    tag = f"bbx-p9-{component}:{revision[:12]}"
    service = "agent-runtime" if component == "runtime" else "blackboard"
    options = (
        ("--target=kubernetes", "--build-arg", f"NODE_IMAGE={NODE_IMAGE}")
        if component == "runtime"
        else ()
    )
    with (output / "build.log").open("wb") as log:
        subprocess.run(
            (
                "docker",
                "buildx",
                "build",
                "--load",
                "--provenance=false",
                "--platform=linux/amd64",
                "--label",
                f"org.opencontainers.image.revision={revision}",
                "--build-arg",
                f"PYTHON_IMAGE={PYTHON_IMAGE}",
                *options,
                "-f",
                str(source / "services" / service / "Dockerfile"),
                "-t",
                tag,
                str(source),
            ),
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    probe = (
        "import bbx_runtime.native_runner"
        if component == "runtime"
        else "import bbx_blackboard.api; from pathlib import Path; "
        "assert Path('/app/web/dist/index.html').is_file()"
    )
    checked(
        (
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--user=10001:10001",
            "--entrypoint=python",
            tag,
            "-c",
            probe,
        )
    )
    checked(("docker", "image", "save", "--output", str(image_tar), tag))
    sha = manifest_digest(image_tar, tag)
    alias = f"docker.io/library/bbx-p9-{component}@{sha}"
    ctr = ("docker", "exec", NODE, "ctr", "--namespace=k8s.io", "images")
    with image_tar.open("rb") as file:
        subprocess.run(
            (
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
            ),
            stdin=file,
            stdout=subprocess.DEVNULL,
            check=True,
        )
    if not checked((*ctr, "list", "--quiet", f"name=={alias}")).strip():
        checked((*ctr, "tag", f"docker.io/library/{tag}", alias))
    rows = checked((*ctr, "list", f"name=={alias}")).decode().splitlines()
    assert len(rows) == 2 and rows[1].split()[2] == sha
    checked((*ctr, "label", alias, "io.cri-containerd.image=managed"))
    cri = json.loads(checked(("docker", "exec", NODE, "crictl", "inspecti", alias)))["status"]
    assert cri["id"] and alias in cri["repoDigests"]
    receipt = {
        "commit": revision,
        "tree": tree,
        "source_archive_sha256": digest_file(source_tar),
        "image": alias,
        "cri_id": cri["id"],
        "non_root_import_verified": True,
        **({"node_base": NODE_IMAGE} if component == "runtime" else {}),
        "python_base": PYTHON_IMAGE,
    }
    (output / f"{component}-image.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Use Python without -O for artifact integrity checks")
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--git-dir", required=True, type=Path)
    cli.add_argument("--revision", required=True)
    cli.add_argument("--output", required=True, type=Path)
    cli.add_argument("--component", choices=("runtime", "blackboard"), default="runtime")
    args = cli.parse_args()
    print(json.dumps(build(args.git_dir, args.revision, args.output, args.component), indent=2))
