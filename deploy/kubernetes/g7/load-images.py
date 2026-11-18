"""Import only pinned amd64 lightweight images into the owned kind node."""

import json
import subprocess

NODE = "bbx-archv2-g7-control-plane"
IMAGE = "bbx-archv2-server-g7:bbx-g7-direct-1"
PINS = {
    "opensandbox/execd:release-1.2.0": "sha256:"
    "c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c",
    "opensandbox/controller:release-1.2.0": "sha256:"
    "7701db7eacda4c3533a961e0451f6ce067535d8ce46f15765a04dcf0ffa6e2c6",
    "opensandbox/ingress:release-1.2.0": "sha256:"
    "c7fc7faa8e48793c6c9e74bb1178b46121abdaba315b51704163a8faa1f7cc83",
    "python:3.12-slim": "sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30",
}
assert (
    subprocess.check_output(
        ["docker", "inspect", NODE, "--format", '{{index .Config.Labels "io.x-k8s.kind.cluster"}}'],
        text=True,
    ).strip()
    == "bbx-archv2-g7"
)
available = int(
    subprocess.check_output(
        ["docker", "exec", NODE, "df", "-B1", "--output=avail", "/var/lib/containerd"], text=True
    ).splitlines()[-1]
)
assert available > 8 * 1024**3, "Insufficient node disk for the lightweight G7 fixture"
for image in [IMAGE, *PINS]:
    if image in PINS:
        recorded = json.loads(
            subprocess.check_output(
                ["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"],
                text=True,
            )
        )
        assert any(value.endswith("@" + PINS[image]) for value in recorded), (
            "Cached image differs from the fixed baseline"
        )
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
            stdout=subprocess.DEVNULL,
        )
        assert export.stdout is not None
        export.stdout.close()
        if result.returncode:
            export.terminate()
        code = export.wait()
        assert result.returncode == 0 and code == 0, "G7 image import failed"
    finally:
        if export.poll() is None:
            export.terminate()
            export.wait()
    if image in PINS:
        repository = image.split(":", 1)[0]
        if "/" not in repository:
            repository = "library/" + repository
        alias = "docker.io/" + repository + "@" + PINS[image]
        command = ["docker", "exec", NODE, "ctr", "--namespace=k8s.io", "images"]
        if not subprocess.check_output([*command, "list", "--quiet", "name==" + alias]).strip():
            subprocess.run(
                [
                    *command,
                    "tag",
                    "docker.io/" + (image if "/" in image else "library/" + image),
                    alias,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        rows = subprocess.check_output([*command, "list", "name==" + alias], text=True).splitlines()
        assert len(rows) == 2 and rows[1].split()[2] == PINS[image]
        subprocess.run(
            [*command, "label", alias, "io.cri-containerd.image=managed"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
