"""Build one pinned Server-only overlay without editing the cached official source."""

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
STATE = ROOT / ".data/bbx-archv2-g7"
COMMIT = "62a64f0b48cdd6b410f36e53c74dc7559c87719a"
ARCHIVE = ROOT / ".data/bbx-archv2-k8s/downloads/opensandbox.tar.gz"
assert (
    hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    == "c2ca1bd964d5f1933f69f01d210aabfe83d8174761b1487a5b1bd5f790abdf2b"
)
files = [
    "server/opensandbox_server/config.py",
    "server/opensandbox_server/main.py",
    "server/opensandbox_server/services/k8s/client.py",
    "server/opensandbox_server/tenants/http_provider.py",
    "server/configuration.md",
]
context = STATE / "context"
context.mkdir(parents=True, exist_ok=True)
with tarfile.open(ARCHIVE) as archive:
    prefix = "OpenSandbox-" + COMMIT + "/"
    for member in archive.getmembers():
        name = member.name.removeprefix(prefix)
        if not member.isfile() or not (
            name in files or name.startswith(("manifests/charts/", "kubernetes/config/crd/bases/"))
        ):
            continue
        target = (context if name in files else STATE / "upstream") / name
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        assert source is not None
        target.write_bytes(source.read())
patch = HERE / "patches/opensandbox-1.2.0-server-lifecycle.patch"
subprocess.run(
    ["patch", "--dry-run", "--batch", "--forward", "-p1", "-d", str(context), "-i", str(patch)],
    check=True,
)
subprocess.run(
    ["patch", "--batch", "--forward", "-p1", "-d", str(context), "-i", str(patch)], check=True
)
identity = {
    "base_commit": COMMIT,
    "base_image": "opensandbox/server@sha256:"
    "25bebdcf1cf7724eb923faecbdbbf82d4038c6ab35c736151574726d14006f30",
    "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
    "files": {name: hashlib.sha256((context / name).read_bytes()).hexdigest() for name in files},
}
(context / "identity.json").write_text(json.dumps(identity, indent=2))
(STATE / "identity.json").write_text(json.dumps(identity, indent=2))
subprocess.run(
    [
        "docker",
        "build",
        "--network",
        "none",
        "-f",
        str(HERE / "Dockerfile"),
        "-t",
        "bbx-archv2-server-g7:bbx-g7-direct-1",
        str(context),
    ],
    check=True,
)
