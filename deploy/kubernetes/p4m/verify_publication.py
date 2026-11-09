"""Read the normal publication only after both isolated physical modes have passed."""

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from bbx_blackboard.native_ctf_publication import load_publication
from bbx_blackboard.settings import Settings
from bbx_contracts.runner_vpn import VPN_DYNAMIC_PROFILE
from common import ROOT, STATE
from pydantic import SecretStr


def verify(branches: Path, lifecycle: Path):
    raw = (lifecycle / "evidence.json").read_bytes()
    evidence = json.loads(raw)
    required = {
        "remote_tls_dynamic_tun_and_api_returned_worker_target",
        "original_inputs_applied_before_pause",
        "pause_resume_same_tunnel_new_binding",
        "lead_disconnected_original_vpn_before_stop",
        "original_four_cids_and_pcap_archives",
    }
    assert evidence["completed"] and all(evidence["checks"].get(key) for key in required)
    identity = json.loads((branches / "same-frozen-publication-proof.json").read_text())
    assert identity["same_frozen_publication"] and len(set(identity["task_ids"])) == 2
    branch_raw = (branches / "ctf-network-branches-proof.json").read_bytes()
    branch = json.loads(branch_raw)
    assert branch["same_frozen_publication"]
    for mode, label in (("default", "default"), ("vpn", "worker")):
        observed = branch[mode]
        assert observed["task_id"] in identity["task_ids"]
        assert observed["exit_confirmed"] and len(set(observed["original_four_cids"])) == 4
        pcap = (branches / (label + "-sealed.pcap")).read_bytes()
        assert hashlib.sha256(pcap).hexdigest() == observed["pcap_sha256"]
    assert branch["default"]["vpn_operation_id"] is None and branch["vpn"]["vpn_operation_id"]
    config_path = lifecycle / "test-only-publication.json"
    publication = json.loads(config_path.read_text())
    assert publication["release"] == "test_only"
    assert publication["vpn_profile"] == VPN_DYNAMIC_PROFILE
    resources = publication["runtime_resource_config"]
    vpn = resources["vpn"]
    first = json.loads((branches / "test-only-publication.json").read_text())
    original = first["runtime_resource_config"]
    assert all(
        original[k] == resources[k] for k in ("vpn", "sandbox_host_image", "traffic_sandbox_image")
    )
    images = json.loads((STATE / "public-verified-inputs.json").read_text())["images"]
    refs = {image["reference"] for image in images.values()}
    assert {vpn["egress_image"], vpn["sandbox_image"], resources["sandbox_host_image"]} <= refs
    baseline = ROOT / ".data/p4j-native-ctf-product-20261108/kali-rebuild/sixth/tools-rebuild.json"
    tools = json.loads(baseline.read_text())
    assert tools["result"] == "reconstructed_tools_verified"
    receipt = {
        "schema_version": 1,
        "base_source": "reconstructed",
        "base_tool_image": tools["base_tool_image"],
        "recipe_sha256": tools["recipe_sha256"],
        "traffic_sandbox_image": resources["traffic_sandbox_image"],
        "catalog_revision": "p4m-synthetic-catalog-v1",
        "model": publication["model"],
        "public_create_and_archive": True,
        "vpn_profile": VPN_DYNAMIC_PROFILE,
        "vpn_egress_image": vpn["egress_image"],
        "vpn_sandbox_image": vpn["sandbox_image"],
        "vpn_host_image": resources["sandbox_host_image"],
        "vpn_roundtrip_and_disconnect": True,
        "task_network_default_and_vpn": True,
    }
    settings = Settings.model_construct(
        native_ctf_publication_file=config_path,
        native_gateway_catalog_revision=receipt["catalog_revision"],
        agent_token_secret=SecretStr("isolated-p4m-verified-publication-dry-readback"),
    )
    assert load_publication(settings).publication is None
    with TemporaryDirectory(dir=STATE) as target:
        accepted = Path(target) / "acceptance.json"
        spec = Path(target) / "publication.json"
        options = settings.model_copy(update={"native_ctf_publication_file": spec})

        def load(value):
            accepted.write_text(json.dumps(value, indent=2) + "\n")
            digest = hashlib.sha256(accepted.read_bytes()).hexdigest()
            spec.write_text(
                json.dumps(
                    {
                        **publication,
                        "release": "verified",
                        "acceptance": {"file": accepted.name, "sha256": digest},
                    }
                )
            )
            return load_publication(options), digest

        positive, digest = load(receipt)
        assert positive.publication is not None
        wrong, _ = load({**receipt, "vpn_egress_image": "different@sha256:" + "f" * 64})
        assert wrong.publication is None
        missing, _ = load({k: v for k, v in receipt.items() if k != "task_network_default_and_vpn"})
        assert missing.publication is None
    (lifecycle / "verified-acceptance.json").write_text(json.dumps(receipt, indent=2) + "\n")
    (lifecycle / "normal-publication-readback.json").write_text(
        json.dumps(
            {
                "accepted_two_actual_modes_and_original_images": True,
                "rejected_test_only_wrong_image_or_missing_two_mode_proof": True,
                "same_publication_task_ids": identity["task_ids"],
                "lifecycle_task_id": evidence["task_id"],
                "branch_evidence_sha256": hashlib.sha256(branch_raw).hexdigest(),
                "lifecycle_evidence_sha256": hashlib.sha256(raw).hexdigest(),
                "acceptance_sha256": digest,
                "created_another_task": False,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("branches", type=Path)
    parser.add_argument("lifecycle", type=Path)
    args = parser.parse_args()
    verify(args.branches, args.lifecycle)
