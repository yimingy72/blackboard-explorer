"""Reconstruct the unavailable CTF tool image as a new, attributed image."""

import argparse
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path

from image_release import (
    GIB,
    PLATFORM,
    ROOT,
    ReleaseError,
    image,
    local_docker,
    pinned_image,
    run,
    verify_layer_prefix,
)

HERE = Path(__file__).resolve().parent
UPSTREAM = (
    "docker.io/kalilinux/kali-rolling@"
    "sha256:5cc5e418c51098ae3c48f9a1d5b3bc74a508588900765a3964259da783652b03"
)
UPSTREAM_INDEX = "sha256:6ec743d9fc5cd397451eeddeff3869bcfab5de9bd5dec5b1f34cbfbd80313073"
RECIPE = "services/envd/Dockerfile.kali"
VERIFY = "scripts/verify-exec-env-kali-inner.sh"


def build(docker, stage, dockerfile, source, state, tag, *args):
    metadata = state / f"{stage}-metadata.json"
    with (state / f"{stage}.log").open("w") as log:
        run(
            *docker,
            "build",
            "--platform",
            PLATFORM,
            "--pull=false",
            "--provenance=false",
            "--metadata-file",
            str(metadata),
            "--file",
            str(dockerfile),
            "-t",
            tag,
            *args,
            str(source),
            timeout=3600,
            output=log,
        )
    digest = json.loads(metadata.read_text())["containerimage.digest"]
    reference = tag.rsplit(":", 1)[0] + "@" + digest
    pinned_image(docker, reference)
    return reference


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def source_tree(commit, state):
    destination = state / "source"
    destination.mkdir(mode=0o700)
    tar = state / "recipe.tar"
    with tar.open("w") as output:
        run(
            "git",
            "archive",
            "--format=tar",
            commit,
            "services/envd",
            ".dockerignore",
            VERIFY,
            output=output,
        )
    with tarfile.open(tar) as archive:
        archive.extractall(destination, filter="data")
    tar.unlink()
    shutil.copyfile(HERE / "Dockerfile.kali-base", destination / "Dockerfile.kali-base")
    return destination


def file_digest(path):
    with path.open("rb") as content:
        return hashlib.file_digest(content, "sha256").hexdigest()


def keep_debian_mpmath(recipe):
    text = recipe.read_text()
    anchor = "ARG PYPI_UPSTREAM=https://pypi.org/simple\nRUN python3 -m pip install "
    if text.count(anchor) != 1:
        raise ReleaseError("The reviewed historical analysis-tool install changed")
    # Install the exact compatible version under /usr/local without removing apt files.
    checksum = "a0b2b9fe80bbcd81a6647ff13108738cfb482d481d826cc0e02f5b35e5c88d2c"
    wheel = f"mpmath==1.3.0 --hash=sha256:{checksum}"
    overlay = r"""ARG PYPI_UPSTREAM=https://pypi.org/simple
RUN printf '%s\n' \
        '@WHEEL@' \
        > /tmp/p4j-mpmath.txt \
    && python3 -m pip install --break-system-packages --ignore-installed --no-deps \
        --no-cache-dir --only-binary=:all: --require-hashes --index-url "${PYPI_UPSTREAM}" \
        -r /tmp/p4j-mpmath.txt \
    && python3 -c 'import mpmath; assert mpmath.__version__ == "1.3.0"' \
    && rm /tmp/p4j-mpmath.txt
RUN python3 -m pip install """
    recipe.write_text(text.replace(anchor, overlay.replace("@WHEEL@", wheel), 1))


def offline_pwndbg(recipe, package, tree):
    text = recipe.read_text()
    pin = re.search(r"(?m)^ARG PWNDBG_SHA256=([0-9a-f]{64})$", text)
    if not pin or file_digest(package) != pin[1]:
        raise ReleaseError("The local pwndbg asset does not match the historical recipe SHA")
    copied = tree / "p4j-pwndbg.deb"
    shutil.copyfile(package, copied)
    changed, count = re.subn(
        r"(?m)^RUN curl [^\n]+-o /tmp/pwndbg\.deb [^\n]*\n"
        r"[^\n]+pwndbg_[^\n]+\n    && echo ",
        lambda _: (
            "RUN --mount=type=bind,source=p4j-pwndbg.deb,target=/tmp/p4j-pwndbg.deb,ro "
            "cp /tmp/p4j-pwndbg.deb /tmp/pwndbg.deb \\\n    && echo "
        ),
        text,
    )
    if count != 1:
        raise ReleaseError("The reviewed pwndbg transport line changed")
    recipe.write_text(changed)
    return pin[1]


def keep_angr_parser(recipe):
    text = recipe.read_text()
    anchor = "\nEXPOSE 8080\n"
    if text.count(anchor) != 1:
        raise ReleaseError("The reviewed analysis image footer changed")
    checksum = "b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992"
    wheel = f"pycparser==3.0 --hash=sha256:{checksum}"
    overlay = r"""
RUN printf '%s\n' '@WHEEL@' > /tmp/p4j-pycparser.txt \
    && python3 -m pip install --break-system-packages --no-deps --no-cache-dir \
        --only-binary=:all: --require-hashes --index-url "${PYPI_UPSTREAM}" \
        -r /tmp/p4j-pycparser.txt \
    && python3 -c \
        'import angr; assert angr.Project("/bin/true",auto_load_libs=False).arch.name == "AMD64"' \
    && rm /tmp/p4j-pycparser.txt
"""
    recipe.write_text(text.replace(anchor, overlay.replace("@WHEEL@", wheel) + anchor, 1))


def execute(options):
    state = options.state.resolve()
    if not state.is_relative_to(ROOT / ".data") or state.exists():
        raise ReleaseError("Use a new task-owned state directory under repository .data")
    state.mkdir(mode=0o700, parents=True)
    receipt = {
        "result": "unavailable",
        "base_source": "reconstructed",
        "publish_ready": False,
        "platform": PLATFORM,
        "upstream_image": UPSTREAM,
        "upstream_index_digest": UPSTREAM_INDEX,
    }
    try:
        source = options.source
        if (
            not re.fullmatch(r"[0-9a-f]{40}", source)
            or run("git", "rev-parse", "--verify", source + "^{commit}") != source
        ):
            raise ReleaseError("A reviewed full Git commit is required")
        if shutil.disk_usage(state).free < 50 * GIB:
            raise ReleaseError("Fewer than 50 GiB free on the build filesystem")
        docker = local_docker(options.docker_context)
        upstream = pinned_image(docker, UPSTREAM)
        tree = source_tree(source, state)
        bootstrap = tree / "Dockerfile.kali-base"
        recipe = tree / RECIPE
        original_recipe = file_digest(recipe)
        keep_debian_mpmath(recipe)
        if options.pwndbg_package is not None:
            receipt["prefetched_pwndbg_sha256"] = offline_pwndbg(
                recipe, options.pwndbg_package, tree
            )
        keep_angr_parser(recipe)
        receipt.update(
            source_commit=source,
            upstream_platform_id=upstream,
            historical_recipe_sha256=original_recipe,
            recipe_sha256=file_digest(recipe),
            bootstrap_sha256=file_digest(bootstrap),
            verification_sha256=file_digest(tree / VERIFY),
        )
        suffix = source[:12] + "-" + state.name
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,75}", suffix):
            raise ReleaseError("Use a short lowercase run directory name")
        base_tag = "bbx-p4j-kali-base:" + suffix
        base = build(docker, "kali-base", bootstrap, tree, state, base_tag)
        verify_layer_prefix(docker, UPSTREAM, base)
        receipt.update(reconstructed_base_image=base, reconstructed_base_tag=base_tag)
        expected_base = pinned_image(docker, base)
        if image(docker, base_tag)["Id"] != expected_base:
            raise ReleaseError("The private build address changed before use")
        ref = build(
            docker,
            "ctf-tools",
            recipe,
            tree,
            state,
            "bbx-p4j-ctf-tools:" + suffix,
            "--build-arg",
            f"BASE_IMAGE={base_tag}",
            "--build-arg",
            "GITHUB_MIRROR=",
            "--label",
            "org.opencontainers.image.base.digest=" + base.rsplit("@", 1)[1],
        )
        if image(docker, base_tag)["Id"] != expected_base:
            raise ReleaseError("The private build address changed during the build")
        verify_layer_prefix(docker, base, ref)
        receipt["base_tool_image"] = ref
        receipt["base_layer_prefix_verified"] = True
        (state / "dpkg-versions.txt").write_text(
            run(
                *docker,
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--entrypoint",
                "/usr/bin/dpkg-query",
                ref,
                "-W",
                "-f",
                "${binary:Package}\t${Version}\n",
            )
            + "\n"
        )
        (state / "python-versions.txt").write_text(
            run(
                *docker,
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--entrypoint",
                "/usr/bin/python3",
                ref,
                "-m",
                "pip",
                "freeze",
            )
            + "\n"
        )
        (state / "collector-version.txt").write_text(
            run(
                *docker,
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--entrypoint",
                "/usr/bin/dumpcap",
                ref,
                "-v",
            )
            + "\n"
        )
        with (state / "tools-verification.log").open("w") as log:
            run(
                *docker,
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--mount",
                f"type=bind,source={tree / VERIFY},target=/verify-tools,readonly",
                "--entrypoint",
                "/bin/bash",
                ref,
                "/verify-tools",
                timeout=600,
                output=log,
            )
        receipt["tool_platform_id"] = image(docker, ref)["Id"]
        receipt["result"] = "reconstructed_tools_verified"
        return 0
    except (ReleaseError, ValueError, OSError) as error:
        receipt["reason"] = str(error)
        return 1
    finally:
        save(state / "tools-rebuild.json", receipt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--docker-context")
    parser.add_argument("--pwndbg-package", type=Path, help="Exact upstream deb already downloaded")
    try:
        raise SystemExit(execute(parser.parse_args()))
    except ReleaseError as error:
        parser.exit(1, str(error) + "\n")
