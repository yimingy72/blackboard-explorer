"""One administrator-published native CTF creation environment."""

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from bbx_contracts.models import ContractModel
from bbx_contracts.native_gateway import FrozenExchangeRate, native_budget_threshold_usd
from bbx_contracts.runner import Digest
from bbx_contracts.runner_bootstrap import (
    BINDING_CAPTURE_CONFIG_FIELDS,
    NativeCtfEnvironment,
    TaskCreationCapabilities,
    management_document_digest,
)
from bbx_contracts.runner_inputs import INPUT_PROFILE, NativeSharedInputLimits
from fastapi import HTTPException
from pydantic import Field

from bbx_blackboard.settings import Settings

LIMIT = 256 * 1024


class PublishedModel(ContractModel):
    id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1, strict=True)
    gateway_alias: str = Field(min_length=1, max_length=512)


ORIGINAL_KALI = (
    "bbx-exec-env-kali@sha256:c79ded7b50e9a7ea2433e9740c48417ce08601948196ec497ad403c779da58ae"
)


class AcceptanceReference(ContractModel):
    file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    sha256: Digest


class CaptureAcceptance(ContractModel):
    schema_version: Literal[1]
    base_source: Literal["original", "reconstructed"]
    base_tool_image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    recipe_sha256: Digest | None = None
    traffic_sandbox_image: str
    catalog_revision: str = Field(min_length=1)
    model: PublishedModel
    public_create_and_archive: Literal[True]
    inputs_profile: Literal["shared-inputs-v1"] | None = None
    inputs_host_image: str | None = None


class NativeCtfPublication(ContractModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    revision: int = Field(ge=1, strict=True)
    label: str = Field(min_length=1, max_length=100)
    release: Literal["verified", "test_only"]
    ctf_profile_version: int = Field(ge=1, strict=True)
    model: PublishedModel
    exchange_rate: FrozenExchangeRate
    runtime_resource_config: dict[str, Any]
    acceptance: AcceptanceReference | None = None
    inputs_profile: Literal["shared-inputs-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def public(self, stamp: str) -> NativeCtfEnvironment:
        return NativeCtfEnvironment(
            id=self.id,
            revision=self.revision,
            publication_stamp=stamp,
            label=self.label,
            model_id=self.model.id,
            model_version=self.model.version,
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
        }

    def validate_budget(self, cny: Decimal) -> None:
        try:
            native_budget_threshold_usd(cny, self.exchange_rate.rate_cny_per_usd)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error


@dataclass(frozen=True)
class PublicationState:
    exclusive: bool = False
    publication: NativeCtfPublication | None = None
    stamp: str = ""

    def capabilities(self, *, available: bool = True) -> TaskCreationCapabilities:
        return TaskCreationCapabilities(
            mode="native-ctf" if self.exclusive else "legacy",
            native_ctf=self.publication.public(self.stamp)
            if available and self.publication
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
        if publication.release == "test_only":
            if not allow_test_only or publication.acceptance is not None:
                raise ValueError("Fixture publications are not accepted by the application")
        else:
            ref = publication.acceptance
            if ref is None:
                raise ValueError("Original image publication requires acceptance evidence")
            raw = _bounded_file(path.parent / ref.file)
            if hashlib.sha256(raw).hexdigest() != ref.sha256:
                raise ValueError("Original image acceptance evidence changed")
            accepted = CaptureAcceptance.model_validate_json(raw)
            original_digest = ORIGINAL_KALI.rsplit("@", 1)[-1]
            uses_original = accepted.base_tool_image.rsplit("@", 1)[-1] == original_digest
            if (
                (accepted.base_source == "original" and not uses_original)
                or (
                    accepted.base_source == "reconstructed"
                    and (uses_original or accepted.recipe_sha256 is None)
                )
                or accepted.traffic_sandbox_image
                != publication.runtime_resource_config.get("traffic_sandbox_image")
                or accepted.base_tool_image.rsplit("@", 1)[-1]
                == accepted.traffic_sandbox_image.rsplit("@", 1)[-1]
                or accepted.catalog_revision != settings.native_gateway_catalog_revision
                or accepted.model != publication.model
                or accepted.inputs_profile != publication.inputs_profile
                or (
                    publication.inputs_profile == INPUT_PROFILE
                    and accepted.inputs_host_image
                    != publication.runtime_resource_config.get("sandbox_host_image")
                )
            ):
                raise ValueError("Original image acceptance does not match this deployment")
        if publication.exchange_rate.observed_at.tzinfo is None:
            raise ValueError("Native publication exchange rate must have a timezone")
        config = publication.runtime_resource_config
        if "retirement_single_apiserver" in config:
            raise ValueError("Expected the exact Runtime claim configuration")
        if not BINDING_CAPTURE_CONFIG_FIELDS <= config.keys():
            raise ValueError("Published native CTF requires binding capture")
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
