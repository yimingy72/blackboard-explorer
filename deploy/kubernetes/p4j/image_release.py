"""Build a locally verified P4j Kali overlay without replacing the historical tools."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
BASE_DIGEST = "sha256:c79ded7b50e9a7ea2433e9740c48417ce08601948196ec497ad403c779da58ae"
BASE = "bbx-exec-env-kali@" + BASE_DIGEST
EXECD = "opensandbox/execd@sha256:c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c"
PLATFORM = "linux/amd64"
GIB = 1024**3
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REPOSITORY = re.compile(r"[a-z0-9][a-z0-9_.:/-]{0,199}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
ENVIRONMENT_REVISION = re.compile(r"bbx-[a-z0-9-]{1,62}\Z")
SOURCE = "services/sandbox-execution"
PROBE = "deploy/kubernetes/p4i/root_probe.py"
PROFILE = "binding-pcap-v1"
CAPS = ("NET_ADMIN", "NET_RAW", "SYS_ADMIN", "SETPCAP", "DAC_OVERRIDE", "KILL")


class ReleaseError(RuntimeError):
    pass


def run(*argv, timeout=30, output=None):
    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            stdout=output if output is not None else subprocess.PIPE,
            stderr=subprocess.STDOUT if output is not None else subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError(f"{argv[0]} did not complete the required local check") from None
    if result.returncode:
        raise ReleaseError(f"{argv[0]} refused the required local check")
    return result.stdout.strip() if output is None else ""


def digest_reference(reference, digest):
    repo, separator, found = reference.partition("@")
    if (
        not separator
        or not REPOSITORY.fullmatch(repo)
        or repo.endswith(("/", ":"))
        or ":" in repo.rsplit("/", 1)[-1]
        or not DIGEST.fullmatch(digest)
        or found != digest
    ):
        raise ReleaseError("An immutable approved repository digest is required")
    return repo


def image(docker, reference):
    try:
        value = json.loads(
            run(
                *docker,
                "image",
                "inspect",
                "--platform",
                PLATFORM,
                "--format",
                "{{json .}}",
                reference,
            )
        )
    except ReleaseError:
        raise ReleaseError(f"Required local {PLATFORM} image is unavailable: {reference}") from None
    if (
        not isinstance(value, dict)
        or (value.get("Os"), value.get("Architecture")) != ("linux", "amd64")
        or not DIGEST.fullmatch(value.get("Id", ""))
    ):
        raise ReleaseError("The loaded image does not match the required platform")
    return value


def pinned_image(docker, reference):
    wanted = reference.rsplit("@", 1)[1]
    digest_reference(reference, wanted)
    value = image(docker, reference)
    if not any(repo.rsplit("@", 1)[-1] == wanted for repo in (value.get("RepoDigests") or [])):
        raise ReleaseError("The original manifest digest is not attached to the local image")
    return value["Id"]


def verify_layer_prefix(docker, base, derived):
    parents = image(docker, base).get("RootFS", {}).get("Layers") or []
    layers = image(docker, derived).get("RootFS", {}).get("Layers") or []
    if (
        not parents
        or len(layers) <= len(parents)
        or not all(isinstance(layer, str) and DIGEST.fullmatch(layer) for layer in parents)
        or layers[: len(parents)] != parents
    ):
        raise ReleaseError("The built filesystem does not retain the approved base layers")


def collector(docker, reference, expected):
    output = run(
        *docker,
        "run",
        "--rm",
        "--pull=never",
        "--platform",
        PLATFORM,
        "--network=none",
        "--cap-drop=ALL",
        "--entrypoint",
        "/bin/sh",
        "-e",
        "LC_ALL=C",
        reference,
        "-ec",
        "test -x /usr/bin/dumpcap; /usr/bin/sha256sum /usr/bin/dumpcap; /usr/bin/dumpcap -v",
    )
    fields = output.splitlines()
    sha = re.fullmatch(r"([0-9a-f]{64})\s+/usr/bin/dumpcap", fields[0] if fields else "")
    release = re.search(r"(?m)^Dumpcap \(Wireshark\) ([0-9]+\.[0-9]+\.[0-9]+)\b", output)
    pcap = re.search(r"\blibpcap(?: version)? ([0-9]+\.[0-9]+\.[0-9]+)\b", output)
    if not sha or not release or release[1] != expected or not pcap:
        raise ReleaseError("The approved dumpcap version or libpcap evidence did not match")
    return {"sha256": sha[1], "version": release[1], "libpcap_version": pcap[1]}


def git_source(commit, destination):
    archive = destination / "source.tar"
    with archive.open("w") as output:
        run("git", "archive", "--format=tar", commit, SOURCE, PROBE, output=output)
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")
    archive.unlink()
    dockerfile = destination / SOURCE / "Dockerfile"
    if f"FROM {EXECD} AS official" not in dockerfile.read_text().splitlines():
        raise ReleaseError("The selected source does not pin the reviewed official execd")
    return destination / SOURCE


@contextmanager
def builder_address(docker, reference):
    expected = pinned_image(docker, reference)
    tag = "bbx-p4j-approved-base:" + uuid4().hex
    run(*docker, "image", "tag", reference, tag)
    try:
        if image(docker, tag)["Id"] != expected:
            raise ReleaseError("The private builder address does not match the approved image")
        yield tag
    finally:
        if image(docker, tag)["Id"] != expected:
            raise ReleaseError("The private builder address changed; it was not removed")
        run(*docker, "image", "rm", tag)


def exported_config_digest(metadata, log):
    digest = metadata.get("containerimage.config.digest", "")
    if not digest:
        exported = set(
            re.findall(r"(?m)^#\d+ exporting config (sha256:[0-9a-f]{64})(?: |$)", log.read_text())
        )
        if len(exported) == 1:
            digest = exported.pop()
    if not DIGEST.fullmatch(digest):
        raise ReleaseError("The build did not return an exact image config digest")
    return digest


def overlay(docker, base, base_address, tag, source, state, commit, environment_revision):
    base_digest = base.rsplit("@", 1)[1]
    metadata = state / "build-metadata.json"
    build_log = state / "build.log"
    with build_log.open("w") as output:
        run(
            *docker,
            "build",
            "--platform",
            PLATFORM,
            "--pull=false",
            "--network=none",
            "--progress=plain",
            "--provenance=false",
            "--metadata-file",
            str(metadata),
            "--build-arg",
            f"TOOL_IMAGE={base_address}",
            "--build-arg",
            "ASSIGNMENT_NETWORK_V1=1",
            "--label",
            "org.opencontainers.image.base.digest=" + base_digest,
            "--label",
            "org.opencontainers.image.revision=" + commit,
            "--label",
            "dev.bbx.traffic.profile=" + PROFILE,
            "--label",
            "dev.bbx.sandbox.environment-revision=" + environment_revision,
            "-t",
            tag,
            str(source),
            timeout=900,
            output=output,
        )
    value = json.loads(metadata.read_text())
    digest = value.get("containerimage.digest", "")
    if not DIGEST.fullmatch(digest):
        raise ReleaseError("The build did not return an exact manifest digest")
    reference = tag.rsplit(":", 1)[0] + "@" + digest
    platform_id = pinned_image(docker, reference)
    verify_layer_prefix(docker, base, reference)
    labels = image(docker, reference).get("Config", {}).get("Labels") or {}
    if any(
        labels.get(k) != v
        for k, v in {
            "org.opencontainers.image.base.digest": base_digest,
            "org.opencontainers.image.revision": commit,
            "dev.bbx.traffic.profile": PROFILE,
            "dev.bbx.sandbox.environment-revision": environment_revision,
        }.items()
    ):
        raise ReleaseError("Derived image provenance labels did not match")
    return reference, platform_id, exported_config_digest(value, build_log)


def smoke(docker, reference, probe, state):
    caps = [arg for cap in CAPS for arg in ("--cap-add", cap)]
    with (state / "root-lifecycle.log").open("w") as output:
        run(
            *docker,
            "run",
            "--rm",
            "--pull=never",
            "--platform",
            PLATFORM,
            "--name",
            "bbx-p4j-image-probe-" + uuid4().hex[:10],
            "--network=none",
            "--cap-drop=ALL",
            *caps,
            "--security-opt",
            "seccomp=unconfined",
            "--security-opt",
            "apparmor=unconfined",
            "--sysctl",
            "net.ipv4.ip_forward=1",
            "--mount",
            f"type=bind,source={probe},target=/fixture.py,readonly",
            "--entrypoint",
            "/usr/bin/python3",
            reference,
            "/fixture.py",
            timeout=45,
            output=output,
        )


def local_docker(context):
    name = context or run("docker", "context", "show")
    endpoint = run(
        "docker", "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}', name
    )
    if not endpoint.startswith("unix://"):
        raise ReleaseError("This verification requires a local Docker Unix endpoint")
    return ("docker", "--context", name)


def base_selection(options):
    path = options.rebuild_receipt
    if path is None:
        reference = options.base_reference or BASE
        digest_reference(reference, BASE_DIGEST)
        return reference, {"base_source": "original"}
    with path.open("rb") as file:
        content = file.read(256 * 1024 + 1)
    if len(content) > 256 * 1024:
        raise ReleaseError("The reviewed rebuild receipt exceeds its size bound")
    receipt = json.loads(content)
    if not isinstance(receipt, dict) or (
        receipt.get("result") != "reconstructed_tools_verified"
        or receipt.get("base_source") != "reconstructed"
        or receipt.get("publish_ready") is not False
        or receipt.get("platform") != PLATFORM
    ):
        raise ReleaseError("Expected a locally verified reconstructed CTF tool receipt")
    base = receipt.get("base_tool_image", "")
    digest = base.rpartition("@")[-1]
    if digest == BASE_DIGEST:
        raise ReleaseError("A rebuilt image cannot claim the historical digest")
    digest_reference(base, digest)
    upstream = receipt.get("upstream_image", "")
    if (
        digest_reference(upstream, upstream.rpartition("@")[-1])
        != "docker.io/kalilinux/kali-rolling"
    ):
        raise ReleaseError("The reconstructed base is not tied to the approved Kali source")
    new_base = receipt.get("reconstructed_base_image", "")
    digest_reference(new_base, new_base.rpartition("@")[-1])
    if not re.fullmatch(r"[0-9a-f]{40}", receipt.get("source_commit", "")) or not all(
        re.fullmatch(r"[0-9a-f]{64}", receipt.get(key, ""))
        for key in ("recipe_sha256", "bootstrap_sha256", "verification_sha256")
    ):
        raise ReleaseError("The reconstructed CTF tool recipe evidence is incomplete")
    reference = options.base_reference or base
    digest_reference(reference, digest)
    return reference, {
        "base_source": "reconstructed",
        **{
            key: receipt[key]
            for key in (
                "upstream_image",
                "reconstructed_base_image",
                "recipe_sha256",
                "bootstrap_sha256",
                "verification_sha256",
            )
        },
        "tools_rebuild_receipt_sha256": hashlib.sha256(content).hexdigest(),
    }


def execute(options):
    state = options.state.resolve()
    if not state.is_relative_to(ROOT / ".data") or state.exists():
        raise ReleaseError("Use a new task-owned state directory under repository .data")
    state.mkdir(mode=0o700, parents=True)
    receipt = {"result": "unavailable", "publish_ready": False, "platform": PLATFORM}
    try:
        base, lineage = base_selection(options)
        receipt.update(lineage)
        if not VERSION.fullmatch(options.dumpcap_version):
            raise ReleaseError("Provide the approved numeric Kali dumpcap version")
        if not ENVIRONMENT_REVISION.fullmatch(options.environment_revision):
            raise ReleaseError("Provide the approved Sandbox environment revision")
        receipt["environment_revision"] = options.environment_revision
        docker = local_docker(options.docker_context)
        receipt["base"] = {
            "reference": base,
            "platform_id": pinned_image(docker, base),
        }
        receipt["official_execd"] = {"reference": EXECD, "platform_id": pinned_image(docker, EXECD)}
        if shutil.disk_usage(state).free < 50 * GIB:
            raise ReleaseError("Fewer than 50 GiB free on the local build filesystem")
        receipt["base"]["collector"] = collector(docker, base, options.dumpcap_version)
        source = options.source
        if (
            not re.fullmatch(r"[0-9a-f]{40}", source)
            or run("git", "rev-parse", "--verify", source + "^{commit}") != source
        ):
            raise ReleaseError("A reviewed complete Git commit is required")
        receipt["source_commit"] = source
        tag = "bbx-p4j-kali-traffic:" + source[:12] + "-" + uuid4().hex[:8]
        with tempfile.TemporaryDirectory(dir=state) as temporary:
            root = Path(temporary)
            source_path = git_source(source, root)
            with builder_address(docker, base) as base_address:
                reference, platform_id, config_digest = overlay(
                    docker,
                    base,
                    base_address,
                    tag,
                    source_path,
                    state,
                    source,
                    options.environment_revision,
                )
            receipt["derived"] = {
                "reference": reference,
                "tag": tag,
                "platform_id": platform_id,
                "config_digest": config_digest,
                "base_layer_prefix_verified": True,
            }
            seen = collector(docker, reference, options.dumpcap_version)
            if seen != receipt["base"]["collector"]:
                raise ReleaseError("The overlay did not preserve the original collector binary")
            receipt["derived"]["collector"] = seen
            smoke(docker, reference, root / PROBE, state)
        receipt["result"] = "local_overlay_verified"
        receipt["required_next"] = "public_native_ctf_kubernetes_task_on_exact_derived_digest"
        return 0
    except (ReleaseError, ValueError, OSError) as error:
        receipt["reason"] = str(error)
        return 1
    finally:
        path = state / "image-release.json"
        path.write_text(json.dumps(receipt, indent=2) + "\n")
        print(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Reviewed full Git commit")
    parser.add_argument("--dumpcap-version", required=True, help="Approved Kali dumpcap version")
    parser.add_argument("--environment-revision", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--base-reference")
    parser.add_argument("--rebuild-receipt", type=Path)
    parser.add_argument("--docker-context")
    try:
        raise SystemExit(execute(parser.parse_args()))
    except ReleaseError as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
