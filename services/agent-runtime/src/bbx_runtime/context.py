"""Dependencies and the pinned opening snapshot for one agent run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from bbx_contracts.model_limits import effective_max_input_tokens
from bbx_contracts.models import AgentProfile, Params
from bbx_objects import ObjectStore

from bbx_runtime.clients import BlackboardClient, EnvdClient

if TYPE_CHECKING:
    from bbx_runtime.session import SessionCheckpoint

TaskType = Literal["explore", "derive", "close"]
CloseMode = Literal["judge", "final"]


def execution_timestamp(value: datetime | str | None) -> str | None:
    """Compare execution fences across FastAPI's equivalent Z and UTC encodings."""
    if value is None:
        return None
    stamp = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    return stamp.astimezone(UTC).isoformat()


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
    expected_started_at: str | None = None

    @property
    def params(self) -> Params:
        params = Params.model_validate(self.state["task"]["params"])
        model = getattr(self.profile.models, self.task_type)
        input_limit = effective_max_input_tokens(model)
        if input_limit is not None:
            params.context_threshold = min(params.context_threshold, max(1, int(input_limit * 0.8)))
        return params
