"""Challenge knowledge contracts; writer identity is bound by the server."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator

from .models import ContractModel, Event, Fact, Intent, KnowledgeAssignment, PostFactRequest

BOARD_CONTRACT = "challenge-board-v1"
BOARD_LEGACY = "legacy"
BOARD_EVENTS = frozenset(
    {
        "board.announcement.published",
        "board.flag.candidate",
        "board.flag.submission",
        "board.flag.dispatched",
        "board.flag.unknown",
        "board.flag.receipt",
        "board.flag.cancelled",
        "board.request.linked",
    }
)
FLAG_EVENTS = BOARD_EVENTS - {"board.announcement.published", "board.request.linked"}


def business_contract(task) -> str:
    return task.get("business_contract") or (task.get("ctf_control") or {}).get(
        "business_contract", BOARD_LEGACY
    )


DISPATCH_TOOL = "board_dispatch_challenge"


def native_dispatch_enabled(task) -> bool:
    manifest = (task.get("ctf_control") or {}).get("business_tools", {})
    return manifest.get("revision") == 2 and DISPATCH_TOOL in manifest.get("roles", {}).get(
        "lead", []
    )


def business_manifest(*, automatic=False) -> dict:
    common = [
        "board_read",
        "board_search",
        "board_challenges",
        "board_announcements",
        "board_flags",
        "board_flag_value",
    ]
    return {
        "contract": BOARD_CONTRACT,
        "revision": 2 if automatic else 1,
        "transport": "/api/tasks/{task_id}/ctf/knowledge/tools/{operation}",
        "roles": {
            "lead": [
                *common,
                "board_publish_announcement",
                "board_create_challenge",
                *([DISPATCH_TOOL] if automatic else []),
                "board_submit_flag",
                "board_finish_task",
            ],
            "teammate": [*common, "board_post_fact", "board_post_intent", "board_record_flag"],
        },
    }


CHALLENGE_KNOWLEDGE_EVENTS = frozenset(
    {"fact.posted", "intent.posted", "intent.closed", "fact.disputed", "fact.undisputed"}
)


class ChallengeFactRequest(PostFactRequest):
    request_id: UUID
    expected_revision: int = Field(ge=1, description="题目元信息 revision；知识追加不递增此值")
    satisfies: list[str] = Field(default_factory=list, max_length=0)


class ChallengeIntentRequest(ContractModel):
    request_id: UUID
    expected_revision: int = Field(ge=1, description="题目元信息 revision；知识追加不递增此值")
    statement: str = Field(min_length=1)
    based_on: list[str] = Field(min_length=1)
    expected: str = Field(min_length=1)
    method: str = Field(min_length=1)
    retry_of: str | None = None


class ChallengeKnowledge(ContractModel):
    task_id: UUID
    challenge_id: UUID
    challenge_revision: int = Field(ge=1)
    board_version: int = Field(ge=0, description="Task 持久事件游标，不是知识 CAS 版本")
    facts: dict[str, Fact]
    intents: dict[str, Intent]


class ChallengeKnowledgeWrite(ContractModel):
    id: str
    object: Fact | Intent
    challenge_revision: int = Field(ge=1, description="本次写入核对的题目元信息 revision")
    board_version: int = Field(ge=1, description="本次写入最后一个 Task 事件游标")
    events: list[Event]


class ChallengeKnowledgeObject(ContractModel):
    object: Fact | Intent
    related: dict[str, Fact | Intent]


class AnnouncementRequest(ContractModel):
    request_id: UUID
    expected_revision: int = Field(ge=0, description="上一公告版本；第一版为 0")
    body: str = Field(min_length=1, max_length=16000)

    @model_validator(mode="after")
    def nonblank(self):
        if not self.body.strip():
            raise ValueError("Announcement body is required")
        return self


class AnnouncementReadRequest(ContractModel):
    after_revision: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


class BoardFinishRequest(ContractModel):
    request_id: UUID
    conclusion: dict


class BoardPageRequest(ContractModel):
    limit: int = Field(default=100, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class Announcement(ContractModel):
    id: UUID
    task_id: UUID
    revision: int = Field(ge=1)
    body: str
    author_id: str
    environment_revision: str | None = None
    supersedes: UUID | None = None
    published_at: datetime
    created_version: int


class BoardChallengeRequest(ContractModel):
    request_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    external_id: str | None = None
    connection: str | None = None
    requirements: str | None = None


class BoardDispatchChallengeRequest(ContractModel):
    request_id: UUID
    challenge_id: UUID
    expected_revision: int = Field(ge=1)
    action: Literal["ready", "withdraw"]
    readiness: Literal["no_external_materials", "materials_pending"] | None = None
    priority: int = Field(default=1, ge=0, le=3, strict=True)

    @model_validator(mode="after")
    def explicit_readiness(self):
        if (self.action == "ready") != (self.readiness is not None):
            raise ValueError("Ready requires an explicit material state; withdraw does not")
        return self


class FlagCandidateRequest(ContractModel):
    request_id: UUID
    expected_revision: int = Field(ge=1, description="题目元信息 revision，不是 Flag 状态版本")
    value: SecretStr = Field(min_length=1, max_length=4096)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=32)
    fact_refs: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def evidence_required(self):
        if not self.evidence_refs and not self.fact_refs:
            raise ValueError("Candidate evidence is required")
        return self


class FlagCandidate(ContractModel):
    id: UUID
    task_id: UUID
    challenge_id: UUID
    verification_round: str
    value_digest: str
    author_id: str
    assignment_ref: KnowledgeAssignment
    evidence_refs: list[dict] = Field(default_factory=list)
    fact_refs: list[str] = Field(default_factory=list)
    created_at: datetime
    created_version: int


class FlagValueDetail(ContractModel):
    candidate_id: UUID
    value: str = Field(description="敏感值；仅后端明确授权的详情可返回")


class FlagSource(ContractModel):
    server_name: str = Field(min_length=1, max_length=64)
    server_version: int = Field(ge=1)
    tool_name: str = Field(min_length=1, max_length=200)


class FlagSubmissionRequest(ContractModel):
    request_id: UUID
    expected_revision: int = Field(ge=1)
    candidate_id: UUID
    source: FlagSource


class FlagResult(ContractModel):
    status: Literal["accepted", "rejected", "duplicate", "unknown"]
    correct_count: int | None = Field(default=None, ge=0)
    total_count: int | None = Field(default=None, ge=1)
    score: Decimal | None = Field(default=None, ge=0)
    external_ref: str | None = None

    @model_validator(mode="after")
    def counts_match(self):
        if (
            self.correct_count is not None
            and self.total_count is not None
            and self.correct_count > self.total_count
        ):
            raise ValueError("Correct count exceeds total")
        if self.status == "unknown" and any(
            value is not None for value in (self.correct_count, self.total_count, self.score)
        ):
            raise ValueError("Unknown result cannot assert progress")
        return self


class FlagSubmission(ContractModel):
    id: UUID
    operation_id: UUID
    task_id: UUID
    challenge_id: UUID
    candidate_id: UUID
    verification_round: str
    source: FlagSource
    target_ref: dict
    request_digest: str
    author_id: str
    assignment_ref: KnowledgeAssignment
    status: Literal[
        "prepared", "dispatched", "accepted", "rejected", "duplicate", "unknown", "cancelled"
    ]
    platform_call_id: str | None = None
    result: FlagResult | None = None
    response_uri: str | None = None
    response_sha256: str | None = None
    upstream_response_uri: str | None = None
    upstream_response_sha256: str | None = None
    progress_applied: bool = False
    late: bool = False
    created_at: datetime
    created_version: int
    updated_version: int


class FlagDispatchRequest(ContractModel):
    request_id: UUID
    platform_call_id: str = Field(min_length=1, max_length=200)


class FlagReceiptRequest(ContractModel):
    request_id: UUID
    response_uri: str = Field(min_length=1)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FlagCaptureRequest(ContractModel):
    request_id: UUID
    platform_call_id: str = Field(min_length=1, max_length=200)
    source: FlagSource
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_text: str = Field(max_length=65536)


class FlagUnknownRequest(ContractModel):
    request_id: UUID
    platform_call_id: str = Field(min_length=1, max_length=200)


class FlagReceipt(ContractModel):
    operation_id: UUID
    submission_id: UUID
    candidate_id: UUID
    platform_call_id: str
    source: FlagSource
    target_ref: dict
    request_digest: str
    verification_round: str
    upstream_response_uri: str
    upstream_response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: FlagResult


class FlagProgress(ContractModel):
    correct_count: int | None = None
    total_count: int | None = None
    score: Decimal | None = None


class FlagBoard(ContractModel):
    verification_round: str
    candidates: list[FlagCandidate]
    submissions: list[FlagSubmission]
    progress: FlagProgress
