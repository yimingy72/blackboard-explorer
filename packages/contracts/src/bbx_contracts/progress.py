"""Task-scoped progress projection and fenced runtime protocol."""

from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from .models import ContractModel, Usage


class ProgressSourceRef(ContractModel):
    kind: Literal["task", "fact", "intent", "event", "evidence", "acceptance"]
    id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=0)
    evidence_uri: str | None = Field(default=None, max_length=1000)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ProgressKeyStep(ContractModel):
    text: str = Field(min_length=1, max_length=2000)
    source_refs: list[ProgressSourceRef] = Field(min_length=1, max_length=30)


class ProgressNode(ContractModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    parent_id: str | None = Field(default=None, max_length=100)
    order: int = Field(default=0, ge=0, le=10000)
    kind: Literal["goal", "direction", "finding", "attempt", "gap"]
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(default="", max_length=2000)
    summary: str = Field(default="", max_length=4000)
    significance: str = Field(default="", max_length=2000)
    next_step: str = Field(default="", max_length=2000)
    limitations: str = Field(default="", max_length=2000)
    assessment: str = Field(default="", max_length=1000)
    key_steps: list[ProgressKeyStep] = Field(default_factory=list, max_length=20)
    source_refs: list[ProgressSourceRef] = Field(default_factory=list, max_length=50)
    archived: bool = False


class ProgressSettings(ContractModel):
    enabled: bool = False
    max_cost: Decimal = Field(default=Decimal("2"), ge=0, le=10000)
    model_id: str | None = None
    model_version: int | None = Field(default=None, ge=1)
    system_prompt: str = Field(default="", max_length=40000)
    merge_seconds: int = Field(default=10, ge=0, le=3600)
    min_interval_seconds: int = Field(default=60, ge=0, le=86400)
    max_output_tokens: int = Field(default=8192, ge=256, le=32768)


class ProgressGlobalSettings(ContractModel):
    revision: int | None = Field(default=None, ge=0)
    expected_revision: int = Field(ge=0)
    prompt: str = Field(default="", max_length=40000)
    model_id: str | None = None
    enabled_tools: list[str] = Field(
        default_factory=lambda: [
            "read_progress_sources",
            "get",
            "search",
            "read_evidence",
            "get_progress_tree",
            "update_progress_tree",
        ]
    )


class ProgressRefresh(ContractModel):
    request_id: UUID


class ProgressQuestion(ContractModel):
    id: UUID
    content: str = Field(min_length=1, max_length=20000)
    node_id: str | None = Field(default=None, max_length=100)


class ProgressFence(ContractModel):
    claim_token: UUID
    run_number: int = Field(ge=1)


class ProgressCandidate(ProgressFence):
    expected_revision: int = Field(ge=0)
    overview: str | None = Field(default=None, max_length=6000)
    upserts: list[ProgressNode] = Field(default_factory=list, max_length=300)
    removed_ids: list[str] = Field(default_factory=list, max_length=300)
    nodes: list[ProgressNode] | None = Field(default=None, max_length=300)


class ProgressCheckpoint(ProgressFence):
    session: dict[str, Any]
    opening_instructions: str = ""
    origin: str = "progress"
    expected_revision: int = Field(ge=0)
    source_version: int = Field(ge=0)
    knowledge_version: int | None = Field(default=None, ge=0)


class ProgressReserve(ProgressFence):
    request_id: UUID
    input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=1, le=32768)


class ProgressSettle(ProgressFence):
    request_id: UUID
    usage: Usage = Field(default_factory=Usage)
    usage_known: bool = True
    executed: bool | None = True


class ProgressComplete(ProgressFence):
    content: str = Field(default="", max_length=40000)


class ProgressFail(ProgressFence):
    error: str = Field(min_length=1, max_length=2000)
    retryable: bool = False
