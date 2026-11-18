"""Build the isolated Ubuntu test fixture; never produce a Kali release."""

import argparse
import runpy
from pathlib import Path

from common import ROOT

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    shared = runpy.run_path(str(ROOT / "deploy/kubernetes/p4i/build_images.py"))
    build = shared["main"]
    build.__globals__.update(
        TOOLS="bbx-p4j-ubuntu-tools:20261107",
        LEGACY="bbx-p4j-sandbox:20261107",
        CAPTURE="bbx-p4j-capture-sandbox:20261107",
        TAGS={role: tag.replace("bbx-p4i-", "bbx-p4j-") for role, tag in shared["TAGS"].items()},
    )
    build(args.candidate.resolve())
