"""Prepare only the pinned P4m kind cluster and exact original public tools."""

import runpy
import shutil

from common import ROOT, SOURCE, STATE

if __name__ == "__main__":
    (STATE / "bin").mkdir(parents=True, mode=0o700, exist_ok=True)
    pinned = ROOT / ".data/p4l-native-vpn-20261108/k8s/bin"
    for name in ("kubectl", "kubectl.sha256"):
        target = STATE / "bin" / name
        if not target.exists():
            shutil.copyfile(pinned / name, target)
    runpy.run_path(str(SOURCE / "deploy/kubernetes/p4g/infra.py"), run_name="__main__")
