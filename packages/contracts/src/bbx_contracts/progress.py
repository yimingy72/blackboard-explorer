"""Task-scoped progress projection and fenced runtime protocol."""

from decimal import Decimal
from typing import Annotated, Any, Literal
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
    max_output_tokens: int = Field(
        default=8192, ge=256, description="历史兼容字段，实际输出上限使用固定模型配置"
    )
    reasoning_effort: Literal["low", "medium", "high", "max"] | None = Field(
        default=None, description="历史兼容字段，实际思考强度使用固定模型配置"
    )


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
    max_output_tokens: int = Field(ge=1)


class ProgressSettle(ProgressFence):
    request_id: UUID
    usage: Usage = Field(default_factory=Usage)
    usage_known: bool = True
    executed: bool | None = True


class ProgressComplete(ProgressFence):
    content: str = Field(default="", max_length=40000)


_DiagnosticIdentifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
_ProviderRequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
_ExceptionName = Literal[
    "OtherException",
    "IncompleteModelStreamError",
    "ModelStreamError",
    "APIError",
    "APIConnectionError",
    "APIStatusError",
    "APITimeoutError",
    "ChatClientException",
    "ChatClientContentFilterException",
    "ChatClientInvalidAuthException",
    "ChatClientInvalidRequestException",
    "ContentFilterFinishReasonError",
    "RuntimeError",
    "ValueError",
    "TypeError",
    "ProxyError",
    "ConnectError",
    "ReadError",
    "RemoteProtocolError",
    "LocalProtocolError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteError",
    "WriteTimeout",
    "PoolTimeout",
]


class ProgressFailureDiagnostic(ContractModel):
    """Content-free model failure fields; provider bodies and arbitrary extras are forbidden."""

    category: Literal[
        "authentication",
        "connection",
        "conflict",
        "payment_required",
        "rate_limit",
        "server_error",
        "timeout",
        "content_filter",
        "invalid_request",
        "invalid_response",
        "unknown",
    ]
    scope: Literal["provider", "request"]
    exception_type: _ExceptionName
    cause_type: _ExceptionName | None = None
    transport_type: (
        Literal[
            "ProxyError",
            "ConnectError",
            "ReadError",
            "RemoteProtocolError",
            "LocalProtocolError",
            "ConnectTimeout",
            "ReadTimeout",
            "WriteError",
            "WriteTimeout",
            "PoolTimeout",
        ]
        | None
    ) = None
    failure_phase: Literal[
        "request_validation",
        "http_response",
        "stream_completion",
        "proxy_connect",
        "connect",
        "request_write",
        "response_read",
        "unknown",
    ]
    event_type: Literal["response.failed", "response.incomplete", "error"] | None = None
    incomplete_reason: _DiagnosticIdentifier | None = None
    finish_reason: (
        Literal["stop", "tool_calls", "length", "function_call", "content_filter"] | None
    ) = None
    transient: bool
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_code: _DiagnosticIdentifier | None = None
    request_id: _ProviderRequestId | None = None
    provider_response_id: _ProviderRequestId | None = None
    retry_after: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9, :.\-]+$")
    elapsed_ms: int = Field(default=0, ge=0, le=86_400_000)
    message_count: int = Field(default=0, ge=0, le=1_000_000)
    text_chars: int = Field(default=0, ge=0, le=1_000_000_000)
    attempt_limit: int = Field(default=1, ge=1, le=100)


class ProgressFail(ProgressFence):
    error: str = Field(min_length=1, max_length=2000)
    retryable: bool = False
    budget_exhausted: bool = False
    diagnostic: ProgressFailureDiagnostic | None = None
