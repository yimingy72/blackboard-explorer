"""Prepare pinned inputs and HEAD-only image build contexts; never consume envd WIP."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile

from common import HERE, ROOT, STATE

CACHE = ROOT / ".data/bbx-archv2-k8s"
COMMIT = "62a64f0b48cdd6b410f36e53c74dc7559c87719a"
CHECKS = {
    "bin/kind": "4f0b6e3b88bdc66d922c08469f05ef507d4903dd236e6319199bb9c868eed274",
    "downloads/helm.tar.gz": "09a108c0abda42e45af172be65c49125354bf7cd178dbe10435e94540e49c7b9",
    "downloads/opensandbox.tar.gz": (
        "c2ca1bd964d5f1933f69f01d210aabfe83d8174761b1487a5b1bd5f790abdf2b"
    ),
    "downloads/cilium.tgz": "39b62a72f0892dd16548bd08ee97d5db2e975f7c1f57155578a144186e22992f",
}
STATE.mkdir(parents=True, mode=0o700, exist_ok=True)
for name, digest in CHECKS.items():
    assert hashlib.sha256((CACHE / name).read_bytes()).hexdigest() == digest, name
(STATE / "bin").mkdir(exist_ok=True)
kubectl_url = "https://dl.k8s.io/release/v1.34.0/bin/darwin/amd64/kubectl"
kubectl_sha = "a5904061dd5c8e57d55e52c78fa23790e76de30924b26ba31be891e75710d7a9"
for suffix in ("", ".sha256"):
    target = STATE / "bin" / ("kubectl" + suffix)
    if not target.exists():
        subprocess.run(
            [
                "curl",
                "--proxy",
                os.environ.get("BBX_P3F_FETCH_PROXY", "http://127.0.0.1:7897"),
                "--connect-timeout",
                "10",
                "--max-time",
                "120",
                "-fsSL",
                kubectl_url + suffix,
                "-o",
                str(target),
            ],
            check=True,
        )
assert (STATE / "bin/kubectl.sha256").read_text().strip() == kubectl_sha
assert hashlib.sha256((STATE / "bin/kubectl").read_bytes()).hexdigest() == kubectl_sha
(STATE / "bin/kubectl").chmod(0o700)
shutil.copy(CACHE / "bin/kind", STATE / "bin/kind")
with tarfile.open(CACHE / "downloads/helm.tar.gz") as archive:
    source = archive.extractfile("darwin-amd64/helm")
    assert source
    (STATE / "bin/helm").write_bytes(source.read())
for name in ["kind", "helm"]:
    (STATE / "bin" / name).chmod(0o700)
shutil.copy(CACHE / "downloads/cilium.tgz", STATE / "cilium.tgz")
context = STATE / "server-context"
with tarfile.open(CACHE / "downloads/opensandbox.tar.gz") as archive:
    for member in archive.getmembers():
        name = member.name.removeprefix("OpenSandbox-" + COMMIT + "/")
        if not member.isfile():
            continue
        if name.startswith(("manifests/charts/", "kubernetes/config/crd/bases/")):
            target = STATE / "upstream" / name
        elif name in [
            "server/opensandbox_server/config.py",
            "server/opensandbox_server/main.py",
            "server/opensandbox_server/services/k8s/client.py",
            "server/opensandbox_server/tenants/http_provider.py",
            "server/configuration.md",
        ]:
            target = context / name
        else:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        assert source
        target.write_bytes(source.read())
patch = ROOT / "deploy/kubernetes/g7/patches/opensandbox-1.2.0-server-lifecycle.patch"
subprocess.run(
    ["patch", "--batch", "--forward", "-p1", "-d", str(context), "-i", str(patch)],
    check=True,
    capture_output=True,
)
original_identity = {
    "base_commit": COMMIT,
    "base_image": "opensandbox/server@sha256:"
    "25bebdcf1cf7724eb923faecbdbbf82d4038c6ab35c736151574726d14006f30",
    "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
    "files": {
        name: hashlib.sha256((context / name).read_bytes()).hexdigest()
        for name in [
            "server/opensandbox_server/config.py",
            "server/opensandbox_server/main.py",
            "server/opensandbox_server/services/k8s/client.py",
            "server/opensandbox_server/tenants/http_provider.py",
            "server/configuration.md",
        ]
    },
}
(context / "identity.json").write_text(json.dumps(original_identity, indent=2))
shutil.copy(ROOT / "deploy/kubernetes/g7/Dockerfile", context / "Dockerfile")
head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
for directory in ["services/envd", "services/sandbox-execution"]:
    target = STATE / ("tools-context" if directory.endswith("envd") else "sandbox-context")
    target.mkdir(exist_ok=True)
    result = subprocess.run(
        ["git", "archive", head, directory], cwd=ROOT, capture_output=True, check=True
    )
    archive_path = STATE / (directory.rsplit("/", 1)[1] + "-head.tar")
    archive_path.write_bytes(result.stdout)
    with tarfile.open(archive_path) as archive:
        archive.extractall(target, filter="data")
(STATE / "build-source.json").write_text(
    json.dumps(
        {"head": head, "g7_identity": original_identity, "head_only_contexts": True}, indent=2
    )
)
rendered = subprocess.check_output(
    [
        str(STATE / "bin/helm"),
        "template",
        "bbx-p3f-cilium",
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
print("Pinned source/chart/tool hashes and HEAD-only build contexts verified.")
