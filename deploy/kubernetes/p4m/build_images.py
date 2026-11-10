"""Assemble the private P4m images from the exact accepted tool and Host bases."""

import argparse
import hashlib
import json
import runpy
import shutil

from common import SOURCE, STATE, owned_cluster

VPN = "bbx-p4m-vpn-sandbox:20261109"
HOST = "bbx-p4m-sandbox-host:20261107"
RUNNER = "bbx-p4m-runner:20261107"
SERVER = "bbx-p4m-server-g7:20261107"
LEGACY = "bbx-p4m-sandbox:20261107"
CAPTURE = "bbx-p4m-capture-sandbox:20261107"
PLUGIN = "squat/generic-device-plugin:p4l-probe-0.2.0"
PLUGIN_DIGEST = "sha256:bf312a67334be792d3a3d3042c99225aec469957dc1737911970aebbf82d1545"
VPN_BASE = (
    "bbx-p4l-vpn-sandbox:20261108-aead@sha256:"
    "082f630d60d602a9867e456e273f46c6f4ac528f8d87d88c27a0b4e8d0902695"
)
HOST_BASE = (
    "bbx-p4l-sandbox-host:20261107@sha256:"
    "10a36e5a44bef6555d9a2f425dd9042353498cb27b647c9ad127a4249c8b6cfa"
)
TOOLS = (
    "bbx-p4j-ctf-tools:eae04778af44-sixth@sha256:"
    "c0f841d8d0ac5c4d8e99372a777bfa2bdc071bb0bb0e8c48e347cb13072d44c4"
)
CAPTURE_BASE = (
    "bbx-p4j-kali-traffic:eae04778af44-1026cd2c@sha256:"
    "c7386ae9055d1a31580bb92224b062aa62ef24f18d6301bd9a1f42dd60344f71"
)


def build(*, refresh=False):
    owned_cluster()
    helper = runpy.run_path(str(SOURCE / "deploy/kubernetes/p4i/build_images.py"))["build"]
    loader = runpy.run_path(str(SOURCE / "deploy/kubernetes/p3f/load_images.py"))["load"]
    staged = STATE / "p4m-image-sources"
    staged.mkdir(exist_ok=True)
    launcher = SOURCE / "services/sandbox-execution/launcher"
    vpn = staged / "vpn"
    vpn.mkdir(exist_ok=True)
    host = staged / "host"
    runtime = host / "bbx_runtime"
    runtime.mkdir(exist_ok=True, parents=True)
    sources = {}
    for src, dst in [
        *[(launcher / name, vpn / name) for name in ("task-vpn.py", "assignment-network.py")],
        *[
            (SOURCE / "services/agent-runtime/src/bbx_runtime" / name, runtime / name)
            for name in ("sandbox_mcp.py", "sandbox_vpn.py")
        ],
    ]:
        shutil.copyfile(src, dst)
        sources[str(src.relative_to(SOURCE))] = hashlib.sha256(src.read_bytes()).hexdigest()
    (vpn / "Dockerfile").write_text(
        f"FROM {VPN_BASE}\n"
        "COPY --chmod=0555 task-vpn.py assignment-network.py /opt/bbx/control/\n"
        "RUN install -m 0444 /dev/null /opt/bbx/control/task-network-choice-v1\n"
    )
    contracts = SOURCE / "packages/contracts/src/bbx_contracts"
    shutil.copytree(
        contracts,
        host / "bbx_contracts",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    sources[str((contracts / "runner_vpn.py").relative_to(SOURCE))] = hashlib.sha256(
        (contracts / "runner_vpn.py").read_bytes()
    ).hexdigest()
    (host / "Dockerfile").write_text(
        f"FROM {HOST_BASE}\nCOPY bbx_runtime/ /opt/bbx-sandbox-host/bbx_runtime/\n"
        "COPY bbx_contracts/ /opt/bbx-sandbox-host/bbx_contracts/\n"
    )
    runner = staged / "runner"
    runner.mkdir(exist_ok=True)
    original = SOURCE / "services/agent-runtime/task-runner"
    for name in ("Dockerfile", "package.json", "package-lock.json", "protocol.schema.json"):
        shutil.copyfile(original / name, runner / name)
    for name in ("src", "plugin"):
        shutil.copytree(original / name, runner / name, dirs_exist_ok=True)
    path = STATE / "build-source.json"
    existing = json.loads(path.read_text()) if refresh else {}
    images = dict(existing.get("images", {}))
    builds = {
        VPN: ["--network", "none", str(vpn)],
        HOST: ["--network", "none", str(host)],
        RUNNER: [str(runner)],
        SERVER: [str(STATE / "server-context")],
        LEGACY: [
            "--network",
            "none",
            "--build-arg",
            "TOOL_IMAGE=" + TOOLS,
            str(SOURCE / "services/sandbox-execution"),
        ],
    }
    if refresh and not {VPN, HOST, RUNNER, SERVER, LEGACY} <= images.keys():
        raise RuntimeError("Refresh requires a complete earlier private image inventory")
    for name in (VPN, HOST) if refresh else builds:
        tag = name + "-final" if refresh else name
        digest = helper(tag, *builds[name])
        loader(tag, digest)
        images[name] = {"digest": digest, "reference": tag.rsplit(":", 1)[0] + "@" + digest}
    digest = CAPTURE_BASE.split("@", 1)[1]
    # Capture image is declared as the separate legacy capability but never starts on this Task.
    images[CAPTURE] = {"digest": digest, "reference": CAPTURE_BASE}
    if not refresh:
        loader(PLUGIN, PLUGIN_DIGEST)
    path.write_text(
        json.dumps(
            {
                "candidate": str(SOURCE),
                "images": images,
                "sidecar_sources": sources,
                "environment_revision": "bbx-p4m-dynamic-vpn-20261109",
                "g7_identity": json.loads((STATE / "server-context/identity.json").read_text()),
                "vpn_source": {"plugin": PLUGIN.rsplit(":", 1)[0] + "@" + PLUGIN_DIGEST},
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    build(refresh=parser.parse_args().refresh)
