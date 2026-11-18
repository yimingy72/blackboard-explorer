"""Build from a clean P4h candidate using the same pinned upstream materials."""

import argparse
import runpy
from pathlib import Path

from common import ROOT

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    shared = runpy.run_path(str(ROOT / "deploy/kubernetes/p4g/build_images.py"))
    build = shared["main"]
    build.__globals__.update(
        TAGS={role: tag.replace("bbx-p4g-", "bbx-p4h-") for role, tag in shared["TAGS"].items()},
        TOOLS=shared["TOOLS"].replace("bbx-p4g-", "bbx-p4h-"),
        SANDBOX=shared["SANDBOX"].replace("bbx-p4g-", "bbx-p4h-"),
    )
    build(args.candidate.resolve())
