"""Validate existing project-local caches; extract only this fixture's official inputs."""

import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
STATE = ROOT / ".data/bbx-archv2-g1-k8s"
CACHE = ROOT / ".data/bbx-archv2-k8s"
COMMIT = "62a64f0b48cdd6b410f36e53c74dc7559c87719a"
SOURCE_SHA256 = "c2ca1bd964d5f1933f69f01d210aabfe83d8174761b1487a5b1bd5f790abdf2b"
CHECKS = {
    "bin/kind": "4f0b6e3b88bdc66d922c08469f05ef507d4903dd236e6319199bb9c868eed274",
    "downloads/helm.tar.gz": "09a108c0abda42e45af172be65c49125354bf7cd178dbe10435e94540e49c7b9",
    "downloads/cilium.tgz": "39b62a72f0892dd16548bd08ee97d5db2e975f7c1f57155578a144186e22992f",
    "downloads/opensandbox.tar.gz": SOURCE_SHA256,
}


def prepare():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    (STATE / "bin").mkdir(exist_ok=True)
    for name, digest in CHECKS.items():
        assert hashlib.sha256((CACHE / name).read_bytes()).hexdigest() == digest, name
    shutil.copyfile(CACHE / "bin/kind", STATE / "bin/kind")
    with tarfile.open(CACHE / "downloads/helm.tar.gz") as archive:
        binary = archive.extractfile("darwin-amd64/helm")
        assert binary is not None
        (STATE / "bin/helm").write_bytes(binary.read())
    for name in ("kind", "helm"):
        (STATE / "bin" / name).chmod(0o700)
    shutil.copyfile(CACHE / "downloads/cilium.tgz", STATE / "cilium.tgz")
    with tarfile.open(CACHE / "downloads/opensandbox.tar.gz") as archive:
        for member in archive.getmembers():
            name = member.name.removeprefix("OpenSandbox-" + COMMIT + "/")
            if not member.isfile() or not name.startswith(
                ("manifests/charts/", "kubernetes/config/crd/bases/")
            ):
                continue
            target = STATE / "upstream" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            content = archive.extractfile(member)
            assert content is not None
            target.write_bytes(content.read())
    rendered = subprocess.check_output(
        [
            str(STATE / "bin/helm"),
            "template",
            "bbx-archv2-g1-k8s-cilium",
            str(STATE / "cilium.tgz"),
            "--kube-version",
            "1.34.0",
            "-n",
            "kube-system",
            "-f",
            str(HERE / "values/cilium.yaml"),
        ],
        text=True,
    )
    images = sorted(set(re.findall(r'^\s*image:\s*"(quay.io/cilium/[^"\s]+)"', rendered, re.M)))
    assert len(images) == 2 and all(":v1.18.2@sha256:" in image for image in images)
    (STATE / "cilium-images.json").write_text(json.dumps(images, indent=2))
    print("Validated fixed tools/source/chart and the two rendered Cilium image references.")


if __name__ == "__main__":
    prepare()
