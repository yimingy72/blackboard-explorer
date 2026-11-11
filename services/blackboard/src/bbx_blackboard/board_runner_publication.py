"""Independent release evidence for the ordinary shared Runner environment."""

import hashlib
from typing import Any, Literal

from bbx_contracts.legacy_network import LegacyNetworkPolicy, snapshot
from bbx_contracts.models import ContractModel
from bbx_contracts.runner import Digest
from bbx_contracts.runner_bootstrap import (
    BINDING_CAPTURE_CONFIG_FIELDS,
    board_network_policy,
    management_document_digest,
)
from bbx_contracts.runner_inputs import INPUT_PROFILE
from fastapi import HTTPException
from pydantic import Field

from bbx_blackboard.legacy_network import deployment_snapshot
from bbx_blackboard.native_ctf_publication import AcceptanceReference, _bounded_file
from bbx_blackboard.settings import Settings


class BlackboardRunnerAcceptance(ContractModel):
    schema_version: Literal[1]
    execution_contract: Literal["exploration-board-v1"]
    pi_model_protocol: Literal["pi-messages-v1"]
    mcp_transport: Literal["direct-v2"]
    runtime_config_digest: Digest
    runtime_artifact_sha256: Digest
    runner_image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    sandbox_host_image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    network_mode: Literal["default", "vpn", "socks5"]
    ordinary_task_and_close: Literal[True]
    input_and_archive: Literal[True]
    no_network_fallback: Literal[True]


class BlackboardRunnerPublication(ContractModel):
    revision: int = Field(ge=1, strict=True)
    runtime_resource_config: dict[str, Any]
    acceptance: AcceptanceReference

    def frozen(self):
        return {
            "revision": self.revision,
            "runtime_config_digest": management_document_digest(self.runtime_resource_config),
            "inputs_profile": INPUT_PROFILE,
            "board_network_mode": self.runtime_resource_config["board_network"]["mode"],
        }


def load_board_publication(settings: Settings) -> BlackboardRunnerPublication | None:
    path = settings.board_runner_publication_file
    if path is None:
        return None
    from bbx_blackboard.native_runner_bootstrap import NativeRunnerBootstrap

    try:
        published = BlackboardRunnerPublication.model_validate_json(_bounded_file(path))
        raw = _bounded_file(path.parent / published.acceptance.file)
        if hashlib.sha256(raw).hexdigest() != published.acceptance.sha256:
            return None
        receipt = BlackboardRunnerAcceptance.model_validate_json(raw)
        config = published.runtime_resource_config
        NativeRunnerBootstrap._configuration(config)
        network = board_network_policy(config)
        if network is None:
            return None
        original = deployment_snapshot(settings)
        expected = (
            snapshot(LegacyNetworkPolicy())
            if network["mode"] == "default"
            else snapshot(LegacyNetworkPolicy.model_validate(network["legacy_policy"]))
        )
        if original != expected:
            return None
        if (
            not BINDING_CAPTURE_CONFIG_FIELDS <= config.keys()
            or {"execution_contract", "mcp_transport"} & config.keys()
            or receipt.runtime_config_digest != management_document_digest(config)
            or receipt.runner_image != config.get("runner_image")
            or receipt.sandbox_host_image != config.get("sandbox_host_image")
            or receipt.runtime_artifact_sha256 != config.get("runtime_artifact_sha256")
            or receipt.network_mode != config.get("board_network", {}).get("mode")
        ):
            return None
        return published
    except (OSError, TypeError, ValueError, AttributeError, HTTPException):
        return None
