"""Focused real-filesystem attachment installer checks in an isolated container."""

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

script = "/opt/bbx/control/materialize-inputs.py"
spec = importlib.util.spec_from_file_location("materialize_inputs", script)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
# The public K8s probe exercises actual official context; this fixture isolates local FS edges.
module.original_context = lambda _: None
os.umask(0o077)


def prepare(files, *, fifo=False):
    scope = {
        "task_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "resource_generation": 1,
        "workspace_pvc_uid": str(uuid4()),
        "sandbox_id": str(uuid4()),
        "sandbox_pod_uid": str(uuid4()),
        "sandbox_container_id": "containerd://" + "a" * 64,
        "execution_context_id": "b" * 64,
    }
    rows = []
    for name, content in files:
        file_id = str(uuid4())
        rows.append(
            {
                "id": file_id,
                "filename": name,
                "path": f"/workspace/shared/inputs/{file_id}/{name}",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    raw = json.dumps(
        {"scope": scope, "files": rows}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(raw).hexdigest()
    assert module.root("prepare", digest) == {"result": "prepared"}
    assert module.root("status", digest) == {"result": "unconfirmed"}
    stage = Path(module.CONTROL) / digest
    (stage / "manifest.json").write_bytes(raw)
    for item, (_, content) in zip(rows, files, strict=True):
        path = stage / (item["id"] + ".part")
        if fifo:
            os.mkfifo(path)
        else:
            path.write_bytes(content)
    return digest, rows


binary = b"\x00\xffbinary"
digest, rows = prepare([("a\u6d4b.bin", binary), ("zero bytes", b"")])
receipt = module.root("install", digest)
assert receipt["result"] == "materialized" and module.root("status", digest) == receipt
for row, expected in zip(rows, (binary, b""), strict=True):
    path = Path(row["path"])
    assert path.read_bytes() == expected
    assert path.stat().st_uid == path.stat().st_gid == 10000
    assert path.stat().st_mode & 0o777 == 0o600
path = Path(rows[0]["path"])
subprocess.run(
    [
        "/usr/bin/setpriv",
        "--reuid=10000",
        "--regid=10000",
        "--clear-groups",
        "/usr/bin/python3",
        "-I",
        "-S",
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'edited')",
        str(path),
    ],
    check=True,
)
assert module.root("status", digest) == receipt and path.read_bytes() == b"edited"
new, _ = prepare([("unrelated.txt", b"must not replace")])
assert module.root("install", new)["result"] == "workspace_conflict"
assert path.read_bytes() == b"edited"
shared = Path("/workspace/shared")
shared.rename("/workspace/old-shared")
outside = Path("/tmp/input-sentinel")
outside.mkdir()
shared.symlink_to(outside, target_is_directory=True)
new, _ = prepare([("escape.txt", b"bad")])
assert module.root("install", new)["result"] == "workspace_conflict"
assert list(outside.iterdir()) == []
shared.unlink()
new, _ = prepare([("fifo", b"x")], fifo=True)
assert module.root("install", new)["result"] == "integrity_mismatch"
new, bad = prepare([("hardlink", b"original")])
sentinel = outside / "same-inode"
sentinel.write_bytes(b"original")
part = Path(module.CONTROL) / new / (bad[0]["id"] + ".part")
part.unlink()
os.link(sentinel, part)
assert module.root("install", new)["result"] == "integrity_mismatch"
assert sentinel.read_bytes() == b"original"
new, bad = prepare([("same-size", b"good")])
(Path(module.CONTROL) / new / (bad[0]["id"] + ".part")).write_bytes(b"evil")
assert module.root("install", new)["result"] == "integrity_mismatch"
assert not (shared / "inputs").exists()
new, _ = prepare([("too much", b"x" * (3 * 1024 * 1024))])
assert module.root("install", new)["result"] == "space_unavailable"
assert not (shared / "inputs").exists()
print("actual UID ownership, readonly receipt, no-clobber, nofollow, FIFO and space guards passed")
