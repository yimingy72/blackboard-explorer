"""Versioned contracts for peer Workers and per-challenge collaboration."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .models import ContractModel, Evidence, PostFactRequest

COLLABORATION_REVISION = 4
MAX_CHALLENGE_WORKERS = 10
MAX_WORKER_INTENTS = 3
ASSISTANCE_REMINDER_MS = 30 * 60 * 1000
Difficulty = Literal["easy", "medium", "hard"]


def collaboration_enabled(task) -> bool:
    return (task.get("ctf_control") or {}).get("business_tools", {}).get("revision") == (
        COLLABORATION_REVISION
    )


class ChallengeCollaboration(ContractModel):
    difficulty: Difficulty = "medium"
    multi_stage: bool = False
    assistance_target: int = Field(default=0, ge=0, lt=MAX_CHALLENGE_WORKERS)
    review_revision: int = Field(default=0, ge=0)
    review_base_revision: int = Field(default=0, ge=0)
    review_summary: str | None = None
    review_author: str | None = None
    review_fact_refs: list[str] = Field(default_factory=list)
    review_candidate_ids: list[UUID] = Field(default_factory=list)
    feedback_fact_id: str | None = None
    solve_elapsed_ms: int = Field(default=0, ge=0)
    solve_started_at: datetime | None = None
    assistance_reminded: bool = False


class WorkerContext(ContractModel):
    revision: int = Field(ge=1)
    assignment_ref: str | None
    challenge_id: UUID | None

    @model_validator(mode="after")
    def coherent_assignment(self):
        if (self.assignment_ref is None) != (self.challenge_id is None):
            raise ValueError("Worker work context requires both Assignment and challenge")
        return self


class FileFactRequest(PostFactRequest):
    expected_revision: int = Field(ge=1)
    evidence: list[Evidence] = Field(default_factory=list, max_length=0)
    satisfies: list[str] = Field(default_factory=list, max_length=0)


class CollaborativeChallengeRequest(ContractModel):
    request_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    external_id: str | None = None
    connection: str | None = None
    requirements: str | None = None
    difficulty: Difficulty | None = None
    multi_stage: bool = False
    readiness: Literal["ready", "no_external_materials", "materials_pending"] = "ready"
    priority: int = Field(default=1, ge=0, le=3, strict=True)

    @model_validator(mode="after")
    def default_difficulty(self):
        if not self.title.strip():
            raise ValueError("Challenge title is required")
        if self.difficulty is None:
            self.difficulty = "hard" if self.multi_stage else "medium"
        return self


class ChallengeMutation(ContractModel):
    request_id: UUID
    challenge_id: UUID
    expected_revision: int = Field(ge=1)


class CollaborativeChallengeUpdate(ChallengeMutation):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    connection: str | None = None
    requirements: str | None = None
    difficulty: Difficulty | None = None
    multi_stage: bool | None = None
    readiness: Literal["ready", "no_external_materials", "materials_pending"] | None = None
    priority: int | None = Field(default=None, ge=0, le=3, strict=True)

    @model_validator(mode="after")
    def explicit_patch(self):
        supplied = self.model_fields_set - ChallengeMutation.model_fields.keys()
        if not supplied or (self.title is not None and not self.title.strip()):
            raise ValueError("Specify a nonblank challenge update")
        if supplied.intersection({"difficulty", "multi_stage", "readiness", "priority"}) and any(
            getattr(self, key) is None
            for key in supplied.intersection({"difficulty", "multi_stage", "readiness", "priority"})
        ):
            raise ValueError("Challenge configuration cannot be null")
        return self


class ClaimChallengeRequest(ChallengeMutation):
    pass


class RequestAssistance(ChallengeMutation):
    additional_workers: int = Field(default=1, ge=1, lt=MAX_CHALLENGE_WORKERS, strict=True)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def nonblank_reason(self):
        if not self.reason.strip():
            raise ValueError("Explain the assistance needed")
        return self


class ClaimChallengeIntent(ChallengeMutation):
    intent_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class SubmitChallengeReview(ChallengeMutation):
    summary: str = Field(min_length=1)
    fact_refs: list[str] = Field(default_factory=list)
    candidate_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def result_evidence(self):
        if not self.summary.strip() or not (self.fact_refs or self.candidate_ids):
            raise ValueError("Review requires a result summary and its saved evidence")
        return self


class ReviewChallenge(ChallengeMutation):
    review_revision: int = Field(ge=1)
    decision: Literal["accepted", "rejected"]
    reason: str = Field(min_length=1)
    fact_refs: list[str] = Field(default_factory=list)
    candidate_ids: list[UUID] = Field(default_factory=list)
    submission_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def explain_decision(self):
        if not self.reason.strip():
            raise ValueError("Explain the verification decision")
        return self
