"""Import only the local amd64 tool image; kind 0.30 --all-platforms loses absent blobs."""

import json
import subprocess
from pathlib import Path

NODE = "bbx-archv2-k8s-control-plane"
IMAGE = "bbx-archv2-sandbox-k8s:20261106"
labels = json.loads(
    subprocess.check_output(["docker", "inspect", NODE, "--format", "{{json .Config.Labels}}"])
).get("io.x-k8s.kind.cluster")
assert labels == "bbx-archv2-k8s"
available = int(
    subprocess.check_output(
        ["docker", "exec", NODE, "df", "-B1", "--output=avail", "/var/lib/containerd"]
    )
    .decode()
    .splitlines()[-1]
)
assert available > 50 * 1024**3, "Insufficient node disk for the fixed CTF tool image"
images = [
    IMAGE,
    "opensandbox/server:release-1.2.0",
    "opensandbox/execd:release-1.2.0",
    "opensandbox/controller:release-1.2.0",
    "opensandbox/ingress:release-1.2.0",
]
versions = json.loads((Path(__file__).parent / "versions.json").read_text())
for image in images:
    if image == IMAGE:
        expected = subprocess.check_output(
            [
                "docker",
                "image",
                "inspect",
                "--platform",
                "linux/amd64",
                image,
                "--format",
                "{{.Id}}",
            ],
            text=True,
        ).strip()
        rows = subprocess.check_output(
            [
                "docker",
                "exec",
                NODE,
                "ctr",
                "--namespace=k8s.io",
                "images",
                "list",
                "name==docker.io/library/" + IMAGE,
            ],
            text=True,
        ).splitlines()
        if len(rows) == 2:
            digest = rows[1].split()[2]
            document = json.loads(
                subprocess.check_output(
                    ["docker", "exec", NODE, "ctr", "--namespace=k8s.io", "content", "get", digest]
                )
            )
            if "manifests" in document:
                digest = next(
                    entry["digest"]
                    for entry in document["manifests"]
                    if entry.get("platform", {}).get("os") == "linux"
                    and entry["platform"].get("architecture") == "amd64"
                )
            if digest == expected:
                continue
    else:
        recorded = json.loads(
            subprocess.check_output(
                ["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}} "]
            )
        )
        assert any(
            value.endswith("@" + versions["official_images"][image]) for value in recorded
        ), "Official cached image does not match the fixed BOM"
    export = subprocess.Popen(["docker", "image", "save", image], stdout=subprocess.PIPE)
    try:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "--privileged",
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
        )
        assert export.stdout is not None
        export.stdout.close()
        if result.returncode:
            export.terminate()
        code = export.wait()
        assert result.returncode == 0 and code == 0, "Tool image import failed"
    finally:
        if export.poll() is None:
            export.terminate()
            export.wait()
    if image != IMAGE:
        # CRI looks up digest references by their canonical repo@digest name.
        alias = "docker.io/" + image.split(":", 1)[0] + "@" + versions["official_images"][image]
        command = ["docker", "exec", NODE, "ctr", "--namespace=k8s.io", "images"]
        if not subprocess.check_output([*command, "list", "--quiet", "name==" + alias]).strip():
            subprocess.run([*command, "tag", "docker.io/" + image, alias], check=True)
        rows = subprocess.check_output([*command, "list", "name==" + alias], text=True).splitlines()
        assert len(rows) == 2 and rows[1].split()[2] == versions["official_images"][image]
        subprocess.run([*command, "label", alias, "io.cri-containerd.image=managed"], check=True)
