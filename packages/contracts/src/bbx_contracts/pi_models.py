"""Frozen platform models and the trusted Pi actual-send receipts."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator

from .models import ContractModel
from .runner import Digest, JournalCursor, RunnerBinding, RunnerFence, RunnerId

ModelId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Revision = Annotated[int, Field(ge=1, strict=True)]
Rate = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=24, decimal_places=15)]
Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=42, decimal_places=24)]
Tokens = Annotated[int, Field(ge=0, le=9007199254740991, strict=True)]
CallState = Literal["prepared", "dispatched", "complete", "unknown", "not_sent"]


class PiModelRef(ContractModel):
    model_id: ModelId
    model_revision: Revision


class PiUsdRates(ContractModel):
    input: Rate
    output: Rate
    cache_read: Rate
    cache_write: Rate


class PiModelConfig(ContractModel):
    label: str = Field(min_length=1, max_length=160)
    upstream_model: str = Field(min_length=1, max_length=512)
    api: Literal["openai-completions"] = "openai-completions"
    provider: Literal["openai"] = "openai"
    base_url: str = Field(min_length=1, max_length=2048)
    context_window: int = Field(ge=1024, strict=True)
    max_output_tokens: int = Field(ge=1, strict=True)
    input_modalities: list[Literal["text", "image"]] = Field(min_length=1, max_length=2)
    reasoning: bool = False
    tool_call: bool = True
    usd_rates: PiUsdRates | None = None
    pricing_source: str = Field(default="", max_length=512)
    confirmed_free: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def validate_model(self):
        if (
            "text" not in self.input_modalities
            or len(set(self.input_modalities)) != len(self.input_modalities)
            or self.max_output_tokens >= self.context_window
            or self.label != self.label.strip()
            or self.upstream_model != self.upstream_model.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in self.label + self.upstream_model)
        ):
            raise ValueError("Invalid model name or capabilities")
        if self.usd_rates is not None:
            all_zero = not any(self.usd_rates.model_dump().values())
            if not self.pricing_source.strip() or all_zero != self.confirmed_free:
                raise ValueError(
                    "USD prices require a source and explicit confirmation of free use"
                )
        elif self.confirmed_free:
            raise ValueError("Missing rates cannot be declared free")
        return self


class PiModelInput(PiModelConfig):
    secret: SecretStr | None = None
    clear_secret: bool = False
    expected_revision: Revision | None = None


class PiModelVersion(PiModelConfig):
    id: ModelId
    model_revision: Revision
    has_secret: bool

    def reference(self) -> PiModelRef:
        return PiModelRef(model_id=self.id, model_revision=self.model_revision)


class PiModelDirectory(ContractModel):
    enabled: bool
    can_manage: bool
    models: list[PiModelVersion]


class PiReviewSelection(ContractModel):
    authority: str
    pi_model: PiModelVersion


class PiCallOrdinaryProof(ContractModel):
    kind: Literal["ordinary"]
    agent_id: RunnerId
    run_number: Revision
    expected_started_at: datetime
    expected_derive_round: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("expected_started_at")
    @classmethod
    def aware_start(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Agent start time requires a time zone")
        return value


class PiCallProgressProof(ContractModel):
    kind: Literal["progress"]
    job_id: UUID
    claim_token: UUID
    run_number: Revision
    reservation_id: UUID | None = None


class PiCallReviewProof(ContractModel):
    kind: Literal["review"]
    agent_id: RunnerId
    run_number: Revision
    message_id: UUID | None = None
    claim_token: UUID | None = None
    turn_id: UUID | None = None
    generation: Revision | None = None

    @model_validator(mode="after")
    def exact_claim(self):
        if not (
            self.message_id
            and self.claim_token
            and not (self.turn_id or self.generation)
            or self.turn_id
            and self.generation
            and not (self.message_id or self.claim_token)
        ):
            raise ValueError("An exact review claim is required")
        return self


class PiCallNativeProof(ContractModel):
    kind: Literal["native"]
    binding: RunnerBinding
    fence: RunnerFence
    sdk_session_id: RunnerId
    journal: JournalCursor
    profile_revision: Revision
    prompt_revision: Revision


PiCallProof = Annotated[
    PiCallOrdinaryProof | PiCallProgressProof | PiCallReviewProof | PiCallNativeProof,
    Field(discriminator="kind"),
]


class PiCallPrepare(PiModelRef):
    task_id: UUID
    call_id: UUID
    attempt_id: UUID
    logical_call_id: Digest
    context_sha256: Digest
    action: Literal["turn", "summary"]
    proof: PiCallProof


class PiCallPrepared(ContractModel):
    model: PiModelVersion
    api_key: str = Field(repr=False)
    call_id: UUID
    attempt_id: UUID
    allowed_tools: list[str] | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)


class PiCallDispatch(ContractModel):
    attempt_id: UUID
    request_sha256: Digest


class PiCallDispatchResult(ContractModel):
    send_allowed: bool
    state: CallState


class PiUsage(ContractModel):
    input: Tokens
    output: Tokens
    cacheRead: Tokens
    cacheWrite: Tokens
    totalTokens: Tokens
    cacheWrite1h: Tokens | None = None
    reasoning: Tokens | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.totalTokens != self.input + self.output + self.cacheRead + self.cacheWrite
            or (self.cacheWrite1h or 0) > self.cacheWrite
            or (self.reasoning or 0) > self.output
        ):
            raise ValueError("Invalid normalized Pi usage")
        return self


class PiCallSettle(ContractModel):
    attempt_id: UUID
    request_sha256: Digest | None = None
    outcome: Literal["complete", "not_sent", "unknown"]
    usage: PiUsage | None = None
    amount_usd: Amount | None = None
    provider_request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:/-]{1,256}$")

    @model_validator(mode="after")
    def settlement_matches_evidence(self):
        if self.outcome == "complete":
            if self.request_sha256 is None or (self.amount_usd is not None and self.usage is None):
                raise ValueError(
                    "A complete reply requires an original request; a charge also needs usage"
                )
        elif self.amount_usd is not None:
            raise ValueError("Unconfirmed usage cannot settle a charge")
        return self
