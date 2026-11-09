"""Safe model references and trusted AgentGateway invocation contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .models import ContractModel

GATEWAY_EXECUTION_ATTRIBUTE_HEADERS = {
    "bbx_site_id": "x-bbx-site-id",
    "bbx_task_id": "x-bbx-task-id",
    "bbx_agent_id": "x-bbx-agent-id",
    "bbx_attempt_id": "x-bbx-attempt-id",
    "bbx_purpose": "x-bbx-purpose",
    "bbx_conversation_id": "x-bbx-conversation-id",
    "bbx_run_number": "x-bbx-run-number",
    "bbx_model_alias": "x-bbx-model-alias",
    "bbx_reservation_id": "x-bbx-reservation-id",
}

GatewayAlias = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
GatewayRevision = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GatewayRate = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=24, decimal_places=15)]


class GatewayModelRef(ContractModel):
    alias: GatewayAlias
    catalog_revision: GatewayRevision


class GatewayUsdRates(ContractModel):
    """USD rates per million tokens for each named usage class."""

    input: GatewayRate
    output: GatewayRate
    cache_read: GatewayRate
    cache_write: GatewayRate


class GatewayCatalogEntry(ContractModel):
    label: str = Field(min_length=1, max_length=160)
    context_window: int = Field(ge=1024, strict=True)
    max_output_tokens: int = Field(ge=1, strict=True)
    max_input_tokens: int | None = Field(default=None, ge=1, strict=True)
    input_modalities: list[Literal["text", "image"]] = Field(min_length=1, max_length=2)
    tool_call: bool
    reasoning: bool | None = None
    usd_rates: GatewayUsdRates

    @model_validator(mode="after")
    def valid_model_limits(self):
        if (
            self.label != self.label.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in self.label)
            or "text" not in self.input_modalities
            or len(set(self.input_modalities)) != len(self.input_modalities)
            or self.max_output_tokens >= self.context_window
            or self.max_input_tokens is not None
            and self.max_input_tokens > self.context_window
        ):
            raise ValueError("Inconsistent gateway model capabilities")
        return self


class GatewayModelView(GatewayModelRef, GatewayCatalogEntry):
    pass


class GatewayModelSnapshot(GatewayModelView):
    accounting_source_id: str = Field(min_length=1, max_length=160)
    route_revision: int = Field(ge=1, strict=True)
    route_digest: GatewayRevision
    model_catalog_digest: GatewayRevision

    def public(self) -> GatewayModelView:
        return GatewayModelView.model_validate(
            {key: getattr(self, key) for key in GatewayModelView.model_fields}
        )


class OrdinaryGatewayAttempt(GatewayModelRef):
    attempt_id: UUID
    body_sha256: GatewayRevision
    agent_id: str = Field(min_length=1, max_length=160)
    run_number: int = Field(ge=1, strict=True)
    expected_started_at: datetime
    expected_derive_round: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("expected_started_at")
    @classmethod
    def aware_start_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Agent start time requires a time zone")
        return value


class ProgressGatewayAttempt(GatewayModelRef):
    reservation_id: UUID
    attempt_id: UUID
    body_sha256: GatewayRevision
    job_id: UUID
    claim_token: UUID
    run_number: int = Field(ge=1, strict=True)


class GatewayDirectory(ContractModel):
    status: Literal["ready", "unavailable"]
    models: list[GatewayModelView]
    management_url: str | None = None
    maintenance: bool = False


class ReviewGatewayAttempt(GatewayModelRef):
    attempt_id: UUID
    body_sha256: GatewayRevision
    agent_id: str = Field(min_length=1, max_length=160)
    run_number: int = Field(ge=1, strict=True)
    message_id: UUID | None = None
    claim_token: UUID | None = None
    turn_id: UUID | None = None
    generation: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def exact_review_claim(self):
        ordinary = self.message_id is not None and self.claim_token is not None
        native = self.turn_id is not None and self.generation is not None
        if not (
            ordinary
            and self.turn_id is None
            and self.generation is None
            or native
            and self.message_id is None
            and self.claim_token is None
        ):
            raise ValueError("An exact review claim is required")
        return self


class GatewayExecutionGrant(ContractModel):
    gateway_url: str
    key: str = Field(repr=False)
    headers: dict[str, str]
    attempt_id: UUID
    body_sha256: GatewayRevision


class GatewayExecutionResult(ContractModel):
    body_sha256: GatewayRevision
    outcome: Literal["complete", "unknown"]
    status_code: int | None = Field(default=None, ge=100, le=599, strict=True)

    @model_validator(mode="after")
    def completion_needs_status(self):
        if self.outcome == "complete" and self.status_code is None:
            raise ValueError("Completed transport needs an HTTP status")
        return self


class GatewayMaintenanceFence(ContractModel):
    request_id: UUID
    site_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    action: Literal["prepare", "release"]


class GatewayMaintenanceAck(ContractModel):
    request_id: UUID
    site_id: str
    state: Literal["idle", "busy", "released"]
