"""One administrator-published native CTF creation environment."""

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from bbx_contracts.models import ContractModel
from bbx_contracts.native_gateway import native_usd_amount
from bbx_contracts.runner import Digest
from bbx_contracts.runner_bootstrap import (
    BINDING_CAPTURE_CONFIG_FIELDS,
    BlackboardCreationCapability,
    NativeCtfEnvironment,
    TaskCreationCapabilities,
    management_document_digest,
)
from bbx_contracts.runner_inputs import INPUT_PROFILE, NativeSharedInputLimits
from fastapi import HTTPException
from pydantic import Field

from bbx_blackboard.settings import Settings

LIMIT = 256 * 1024


class AcceptanceReference(ContractModel):
    file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    sha256: Digest


class CtfExecutionAcceptance(ContractModel):
    schema_version: Literal[4]
    execution_profile: Literal["ctf-direct-v1"]
    pi_model_protocol: Literal["pi-messages-v1"]
    mcp_transport: Literal["direct-v2"]
    runner_image: str
    sandbox_host_image: str
    sandbox_image: str
    runtime_artifact_sha256: Digest
    runtime_config_digest: Digest
    public_create_and_archive: Literal[True]
    inputs_profile: Literal["shared-inputs-v1"] | None = None


class NativeCtfPublication(ContractModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    revision: int = Field(ge=1, strict=True)
    label: str = Field(min_length=1, max_length=100)
    release: Literal["verified", "test_only"]
    ctf_profile_version: int = Field(ge=1, strict=True)
    budget_currency: Literal["USD"] = "USD"
    runtime_resource_config: dict[str, Any]
    acceptance: AcceptanceReference | None = None
    inputs_profile: Literal["shared-inputs-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    execution_profile: Literal["ctf-direct-v1"] = "ctf-direct-v1"
    collaboration_profile: Literal["peer-workers-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def public(self, stamp: str) -> NativeCtfEnvironment:
        return NativeCtfEnvironment(
            id=self.id,
            revision=self.revision,
            publication_stamp=stamp,
            label=self.label,
            shared_inputs=NativeSharedInputLimits()
            if self.inputs_profile == INPUT_PROFILE
            else None,
        )

    def frozen(self) -> dict:
        return {
            "id": self.id,
            "revision": self.revision,
            "runtime_config_digest": management_document_digest(self.runtime_resource_config),
            **({"inputs_profile": self.inputs_profile} if self.inputs_profile else {}),
            "execution_profile": self.execution_profile,
            **(
                {"collaboration_profile": self.collaboration_profile}
                if self.collaboration_profile
                else {}
            ),
        }

    def validate_budget(self, usd: Decimal) -> None:
        try:
            native_usd_amount(usd)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error


@dataclass(frozen=True)
class PublicationState:
    exclusive: bool = False
    publication: NativeCtfPublication | None = None
    stamp: str = ""

    def capabilities(
        self, *, blackboard: BlackboardCreationCapability, native_available: bool = True
    ) -> TaskCreationCapabilities:
        return TaskCreationCapabilities(
            mode="native-ctf" if self.exclusive else "legacy",
            blackboard=blackboard,
            native_ctf=self.publication.public(self.stamp)
            if native_available and self.publication
            else None,
        )


def _bounded_file(path: Path) -> bytes:
    with path.open("rb") as file:
        data = file.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("Native publication exceeds its size bound")
    return data


def load_publication(settings: Settings, *, allow_test_only: bool = False) -> PublicationState:
    """Invalid explicit config stays exclusive and unavailable; it never falls back to legacy."""
    path = settings.native_ctf_publication_file
    if path is None:
        return PublicationState()
    from bbx_blackboard.native_runner_bootstrap import NativeRunnerBootstrap

    try:
        publication = NativeCtfPublication.model_validate_json(_bounded_file(path))
        config = publication.runtime_resource_config
        if (
            config.get("execution_profile") != "ctf-direct-v1"
            or (BINDING_CAPTURE_CONFIG_FIELDS | {"vpn", "board_network", "traffic_profile"})
            & config.keys()
        ):
            raise ValueError("CTF uses the shared Runtime without VPN or network capture")
        if publication.release == "test_only":
            if not allow_test_only or publication.acceptance is not None:
                raise ValueError("Fixture publications are not accepted by the application")
        else:
            ref = publication.acceptance
            if ref is None:
                raise ValueError("Runtime publication requires execution acceptance evidence")
            raw = _bounded_file(path.parent / ref.file)
            if hashlib.sha256(raw).hexdigest() != ref.sha256:
                raise ValueError("Execution acceptance evidence changed")
            accepted = CtfExecutionAcceptance.model_validate_json(raw)
            if (
                any(
                    getattr(accepted, name) != config.get(name)
                    for name in (
                        "execution_profile",
                        "runner_image",
                        "sandbox_host_image",
                        "sandbox_image",
                        "runtime_artifact_sha256",
                    )
                )
                or accepted.runtime_config_digest != management_document_digest(config)
                or (accepted.inputs_profile != publication.inputs_profile)
            ):
                raise ValueError("Execution acceptance does not match this deployment")
        config = publication.runtime_resource_config
        if {
            "retirement_single_apiserver",
            "mcp_transport",
            "board_network",
            "execution_contract",
            "mcp_gateway_url",
            "runner_gateway_url",
            "native_mcp_gateway_url",
            "native_gateway_data_url",
        } & config.keys():
            raise ValueError("Expected the exact direct Runtime claim configuration")
        NativeRunnerBootstrap._configuration(config)
        if any(
            config[key].endswith("/") for key in ("runner_blackboard_url", "runner_model_proxy_url")
        ):
            raise ValueError("Expected canonical Runtime control origins")
        management_document_digest(config)
    except (OSError, TypeError, ValueError, HTTPException):
        return PublicationState(exclusive=True)
    # Keep low-entropy private targets out of an offline guessing oracle; this is not auth.
    stamp = hmac.digest(
        settings.agent_token_secret.get_secret_value().encode(),
        (
            "native-ctf-publication:"
            + management_document_digest(publication.model_dump(mode="json"))
        ).encode(),
        "sha256",
    ).hex()
    return PublicationState(exclusive=True, publication=publication, stamp=stamp)
