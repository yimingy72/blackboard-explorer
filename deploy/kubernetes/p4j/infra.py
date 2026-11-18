"""Use the pinned P4g fixture provisioning with only P4j private common settings."""

import runpy

from common import ROOT

if __name__ == "__main__":
    runpy.run_path(str(ROOT / "deploy/kubernetes/p4g/infra.py"), run_name="__main__")
