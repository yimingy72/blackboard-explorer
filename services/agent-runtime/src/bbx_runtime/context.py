"""Dependencies and the pinned opening snapshot for one agent run."""

from dataclasses import dataclass
from typing import Any, Literal

from bbx_contracts.models import AgentProfile, Params
from bbx_objects import ObjectStore

from bbx_runtime.clients import BlackboardClient, EnvdClient

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

    @property
    def params(self) -> Params:
        return Params.model_validate(self.state["task"]["params"])
