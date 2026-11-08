import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "image_release.py"
SPEC = importlib.util.spec_from_file_location("p4j_image_release", PATH)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def opts(tmp_path, **fields):
    return Namespace(
        **{
            "state": tmp_path / ".data/p4j",
            "base_reference": release.BASE,
            "rebuild_receipt": None,
            "docker_context": "private",
            "dumpcap_version": "4.6.0",
            "source": "a" * 40,
            "environment_revision": "bbx-kali-traffic",
            **fields,
        }
    )


@pytest.mark.parametrize("base_ref", ["bbx-exec-env-kali:latest", "bbx@sha256:" + "1" * 64])
def test_mutable_or_other_base_is_rejected_before_docker(monkeypatch, tmp_path, base_ref):
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "run", lambda *args, **kw: pytest.fail("Docker called"))
    options = opts(tmp_path, base_reference=base_ref)
    assert release.execute(options) == 1
    value = json.loads((options.state / "image-release.json").read_text())
    assert value["result"] == "unavailable" and value["publish_ready"] is False


def test_missing_fixed_execd_cannot_start_a_build(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(release, "ROOT", tmp_path)

    def docker(*argv, **_):
        calls.append(argv)
        if "context" in argv and "inspect" in argv:
            return "unix:///p4j-private.sock"
        if argv[-1] == release.BASE:
            return json.dumps(
                {
                    "Id": "sha256:" + "b" * 64,
                    "Os": "linux",
                    "Architecture": "amd64",
                    "RepoDigests": [release.BASE],
                }
            )
        if argv[-1] == release.EXECD:
            raise release.ReleaseError("Unavailable fixed source")
        pytest.fail("An absent fixed FROM ref must prevent any run/build")

    monkeypatch.setattr(release, "run", docker)
    options = opts(tmp_path)
    assert release.execute(options) == 1
    assert not any("run" in command or "build" in command for command in calls)
    value = json.loads((options.state / "image-release.json").read_text())
    assert value["publish_ready"] is False and release.EXECD in value["reason"]


def test_collector_requires_exact_explicit_version_and_stats_library(monkeypatch):
    output = (
        "a" * 64 + "  /usr/bin/dumpcap\n"
        "Dumpcap (Wireshark) 4.6.0 (local package)\n"
        "Running on Linux, with libpcap version 1.10.5."
    )
    monkeypatch.setattr(release, "run", lambda *args, **kw: output)
    assert release.collector(("docker",), release.BASE, "4.6.0")["libpcap_version"] == "1.10.5"
    with pytest.raises(release.ReleaseError, match="version"):
        release.collector(("docker",), release.BASE, "4.2.2")


def test_reconstructed_tool_digest_must_match_its_success_receipt(tmp_path):
    path = tmp_path / "rebuild.json"
    reference = "bbx-p4j-ctf-tools@sha256:" + "1" * 64
    path.write_text(
        json.dumps(
            {
                "result": "reconstructed_tools_verified",
                "base_source": "reconstructed",
                "publish_ready": False,
                "platform": release.PLATFORM,
                "base_tool_image": reference,
                "upstream_image": "docker.io/kalilinux/kali-rolling@sha256:" + "2" * 64,
                "reconstructed_base_image": "bbx-p4j-kali-base@sha256:" + "3" * 64,
                "source_commit": "a" * 40,
                "recipe_sha256": "1" * 64,
                "bootstrap_sha256": "2" * 64,
                "verification_sha256": "3" * 64,
            }
        )
    )
    selected, lineage = release.base_selection(
        opts(tmp_path, rebuild_receipt=path, base_reference=None)
    )
    assert selected == reference and lineage["base_source"] == "reconstructed"
    with pytest.raises(release.ReleaseError, match="approved repository digest"):
        release.base_selection(opts(tmp_path, rebuild_receipt=path))


def test_builder_output_must_retain_the_pinned_rootfs_prefix(monkeypatch):
    base = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
    expected = base + ["sha256:" + "c" * 64]
    layers = {"base": base, "derived": expected}
    monkeypatch.setattr(release, "image", lambda docker, ref: {"RootFS": {"Layers": layers[ref]}})
    release.verify_layer_prefix(("docker",), "base", "derived")
    layers["derived"] = expected[1:]
    with pytest.raises(release.ReleaseError, match="approved base layers"):
        release.verify_layer_prefix(("docker",), "base", "derived")


def test_oci_config_digest_fallback_requires_one_real_export(tmp_path):
    log = tmp_path / "build.log"
    exact = "sha256:" + "1" * 64
    other = "sha256:" + "2" * 64
    log.write_text(f"#17 exporting config {exact} done\n")
    assert release.exported_config_digest({}, log) == exact
    log.write_text(f"#17 exporting config {exact} done\n#18 exporting config {other} done\n")
    with pytest.raises(release.ReleaseError, match="exact image config digest"):
        release.exported_config_digest({}, log)
    assert release.exported_config_digest({"containerimage.config.digest": exact}, log) == exact


def test_rebuild_repairs_keep_the_original_tool_recipe_pins(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.syspath_prepend(str(PATH.parent))
    from rebuild_tools import RECIPE, keep_angr_parser, keep_debian_mpmath

    original = subprocess.check_output(
        ["git", "show", f"HEAD:{RECIPE}"], cwd=release.ROOT, text=True
    )
    staged = tmp_path / "Dockerfile.kali"
    staged.write_text(original)
    keep_debian_mpmath(staged)
    keep_angr_parser(staged)
    changed = staged.read_text()
    assert changed.index("mpmath==1.3.0") < changed.index("angr==10.0.4.post1")
    assert changed.index("pycparser==3.0") > changed.index("PWNDBG_SHA256")
    for line in original.splitlines():
        if line.strip():
            assert line in changed
    assert "--ignore-installed --no-deps" in changed
    assert 'angr.Project("/bin/true",auto_load_libs=False)' in changed
