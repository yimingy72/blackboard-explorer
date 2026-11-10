"""Safe model references and trusted AgentGateway invocation contracts."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import ContractModel

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
