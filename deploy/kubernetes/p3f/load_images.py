"""Import fixed amd64 images into this node, with exact canonical CRI digest aliases."""

import json
import subprocess
import sys

from common import NODE, STATE, owned_cluster

IMAGE = "bbx-p3f-sandbox:20261106"

OFFICIAL = {
    "opensandbox/server:release-1.2.0": "sha256:"
    "25bebdcf1cf7724eb923faecbdbbf82d4038c6ab35c736151574726d14006f30",
    "opensandbox/execd:release-1.2.0": "sha256:"
    "c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c",
    "opensandbox/controller:release-1.2.0": "sha256:"
    "7701db7eacda4c3533a961e0451f6ce067535d8ce46f15765a04dcf0ffa6e2c6",
    "opensandbox/ingress:release-1.2.0": "sha256:"
    "c7fc7faa8e48793c6c9e74bb1178b46121abdaba315b51704163a8faa1f7cc83",
}


def repository(tag):
    name = tag.rsplit(":", 1)[0]
    if "/" not in name:
        return "docker.io/library/" + name
    return name if "." in name.split("/", 1)[0] else "docker.io/" + name


def load(tag, digest, *, allow_pull=False):
    reference = tag + "@" + digest
    if subprocess.run(["docker", "image", "inspect", reference], capture_output=True).returncode:
        if not allow_pull:
            raise RuntimeError("Required fixed local image is unavailable")
        subprocess.run(["docker", "pull", "--platform", "linux/amd64", reference], check=True)
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode:
        subprocess.run(["docker", "tag", reference, tag], check=True)
    recorded = json.loads(
        subprocess.check_output(
            [
                "docker",
                "image",
                "inspect",
                tag,
                "--format",
                "{{json .RepoDigests}}",
            ],
            text=True,
        )
    )
    assert any(value.endswith("@" + digest) for value in recorded), "Foreign cached tag refused"
    export = subprocess.Popen(["docker", "image", "save", tag], stdout=subprocess.PIPE)
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
            stdin=export.stdout,
            stdout=subprocess.DEVNULL,
        )
        assert export.stdout is not None
        export.stdout.close()
        if imported.returncode:
            export.terminate()
        assert export.wait() == imported.returncode == 0, "Owned image import failed"
    finally:
        if export.poll() is None:
            export.terminate()
            export.wait()
    command = ["docker", "exec", NODE, "ctr", "--namespace=k8s.io", "images"]
    alias = repository(tag) + "@" + digest
    if not subprocess.check_output([*command, "list", "--quiet", "name==" + alias]).strip():
        subprocess.run(
            [*command, "tag", repository(tag) + ":" + tag.rsplit(":", 1)[1], alias],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    rows = subprocess.check_output([*command, "list", "name==" + alias], text=True).splitlines()
    assert len(rows) == 2 and rows[1].split()[2] == digest
    subprocess.run(
        [*command, "label", alias, "io.cri-containerd.image=managed"],
        check=True,
        stdout=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    owned_cluster()
    if sys.argv[1:] == ["control"]:
        for tag, digest in OFFICIAL.items():
            load(tag, digest)
        for reference in json.loads((STATE / "cilium-images.json").read_text()):
            tag, digest = reference.split("@", 1)
            load(tag, digest, allow_pull=True)
    elif sys.argv[1:] == ["sandbox"]:
        available = int(
            subprocess.check_output(
                [
                    "docker",
                    "exec",
                    NODE,
                    "df",
                    "-B1",
                    "--output=avail",
                    "/var/lib/containerd",
                ],
                text=True,
            ).splitlines()[-1]
        )
        assert available > 50 * 1024**3, "Insufficient node disk; do not export CTF layers"
        digest = subprocess.check_output(
            ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], text=True
        ).strip()
        (STATE / "sandbox-image.json").write_text(
            json.dumps(
                {"image": IMAGE, "digest": digest, "available_before_import": available}, indent=2
            )
        )
        load(IMAGE, digest)
    else:
        raise ValueError("Expected control or sandbox")
