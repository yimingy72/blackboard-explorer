"""A foreign reused node must cause refusal before any Docker or kubelet write."""

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
(ROOT / ".data").mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="bbx-archv2-owner-", dir=ROOT / ".data") as directory:
    root = Path(directory)
    stub = root / "docker"
    log = root / "calls"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$BBX_TEST_LOG"\n'
        'if [ "$1" != inspect ]; then exit 88; fi\n'
        'case "$*" in *--format*) printf "foreign-owner\\n" ;; esac\n'
    )
    stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/kubernetes/p1/run.sh"), "deploy-only"],
        env={"PATH": str(root) + ":/usr/bin:/bin", "BBX_TEST_LOG": str(log)},
        capture_output=True,
    )
    assert result.returncode != 0
    calls = log.read_text().splitlines()
    assert len(calls) == 2 and all(call.startswith("inspect ") for call in calls), calls
    assert not any("update" in call or "exec" in call for call in calls)
print("Foreign-node ownership refused before writes: passed")
