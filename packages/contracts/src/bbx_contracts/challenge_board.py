"""Challenge knowledge contracts; writer identity is bound by the server."""

from uuid import UUID

from pydantic import Field

from .models import ContractModel, Event, Fact, Intent, PostFactRequest

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
