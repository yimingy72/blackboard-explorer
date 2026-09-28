"""Dependencies and the pinned opening snapshot for one agent run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from bbx_contracts.models import AgentProfile, Params
from bbx_objects import ObjectStore

from bbx_runtime.clients import BlackboardClient, EnvdClient

if TYPE_CHECKING:
    from bbx_runtime.session import SessionCheckpoint

TaskType = Literal["explore", "derive", "close"]
CloseMode = Literal["judge", "final"]


@dataclass
class RunContext:
    task_id: str
    agent_id: str
    task_type: TaskType
    state: dict[str, Any]
    profile: AgentProfile
    service: BlackboardClient
    board: BlackboardClient
    objects: ObjectStore
    envd: EnvdClient | None = None
    intent_id: str | None = None
    mode: CloseMode | None = None
    checkpoint: SessionCheckpoint | None = None
    expected_derive_round: int | None = None

    @property
    def params(self) -> Params:
        return Params.model_validate(self.state["task"]["params"])
