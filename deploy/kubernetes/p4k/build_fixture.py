"""Build only the new attachment layer and reviewed P4k fixture images."""

import argparse
import hashlib
import json
import runpy
from pathlib import Path

from common import ROOT, STATE, owned_cluster

PARENT = "bbx-p4j-ctf-tools@sha256:c0f841d8d0ac5c4d8e99372a777bfa2bdc071bb0bb0e8c48e347cb13072d44c4"
CAPTURE = "bbx-p4k-shared-inputs:20261108"
REVISION = "bbx-p4k-shared-inputs-v1"


def main(candidate):
    owned_cluster()
    release = runpy.run_path(str(ROOT / "deploy/kubernetes/p4j/image_release.py"))
    docker = release["local_docker"](None)
    root = STATE / "input-image"
    root.mkdir(mode=0o700)
    source = candidate / "services/sandbox-execution"
    with release["builder_address"](docker, PARENT) as alias, (root / "build.log").open("w") as log:
        release["run"](
            *docker,
            "build",
            "--platform",
            "linux/amd64",
            "--pull=false",
            "--network=none",
            "--progress=plain",
            "--build-arg",
            f"TOOL_IMAGE={alias}",
            "--build-arg",
            "ASSIGNMENT_NETWORK_V1=1",
            "--metadata-file",
            str(root / "metadata.json"),
            "-t",
            CAPTURE,
            str(source),
            timeout=180,
            output=log,
        )
    meta = json.loads((root / "metadata.json").read_text())
    digest = meta["containerimage.digest"]
    reference = CAPTURE.rsplit(":", 1)[0] + "@" + digest
    release["pinned_image"](docker, reference)
    release["verify_layer_prefix"](docker, PARENT, reference)
    installer = (source / "launcher/materialize-inputs.py").read_bytes()
    output = release["run"](
        *docker,
        "run",
        "--rm",
        "--network=none",
        "--entrypoint",
        "/usr/bin/sha256sum",
        reference,
        "/opt/bbx/control/materialize-inputs.py",
    )
    installer_digest = hashlib.sha256(installer).hexdigest()
    assert output.split()[0] == installer_digest
    with (root / "installer-probe.log").open("w") as log:
        probe = ROOT / "deploy/kubernetes/p4k/installer_probe.py"
        release["run"](
            *docker,
            "run",
            "--rm",
            "--network=none",
            "--cap-drop=ALL",
            "--cap-add=SETUID",
            "--cap-add=SETGID",
            "--cap-add=SETPCAP",
            "--cap-add=DAC_OVERRIDE",
            "--tmpfs",
            "/workspace:rw,size=18m,mode=0755,uid=10000,gid=10000",
            "--volume",
            f"{probe}:/probe.py:ro",
            "--entrypoint",
            "/usr/bin/python3",
            reference,
            "-I",
            "-S",
            "/probe.py",
            timeout=40,
            output=log,
        )
    (root / "receipt.json").write_text(
        json.dumps(
            {
                "tag": CAPTURE,
                "reference": reference,
                "config_digest": release["exported_config_digest"](meta, root / "build.log"),
                "tool_parent": PARENT,
                "installer_sha256": installer_digest,
                "environment_revision": REVISION,
            },
            indent=2,
        )
        + "\n"
    )

    shared = runpy.run_path(str(ROOT / "deploy/kubernetes/p4i/build_images.py"))
    build = shared["main"]
    build.__globals__.update(
        TOOLS="bbx-p4k-ubuntu-tools:20261107",
        LEGACY="bbx-p4k-sandbox:20261107",
        CAPTURE="bbx-p4k-capture-sandbox:20261107",
        TAGS={role: tag.replace("bbx-p4i-", "bbx-p4k-") for role, tag in shared["TAGS"].items()},
    )
    build(candidate, capture_source=(CAPTURE, digest))
    path = STATE / "build-source.json"
    inventory = json.loads(path.read_text())
    inventory["environment_revision"] = REVISION
    path.write_text(json.dumps(inventory, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    main(parser.parse_args().candidate.resolve())
