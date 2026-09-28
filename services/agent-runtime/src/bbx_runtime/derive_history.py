"""Bound derive model input without discarding its native session history."""

from typing import Any

from agent_framework import InMemoryHistoryProvider, Message

from bbx_runtime.image_view import hydrate_image_messages
from bbx_runtime.session import CheckpointHistoryProvider, SessionCheckpoint


class DeriveHistoryProvider(CheckpointHistoryProvider):
    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        history = await InMemoryHistoryProvider.get_messages(
            self, session_id, state=state, **kwargs
        )
        start = int(self.checkpoint.session.state.get("bbx_derive_segment_start") or 0)
        view = history[start:]

        async def read_evidence(uri: str) -> bytes:
            return await self.checkpoint.service.read_evidence(uri)

        await hydrate_image_messages(
            self.checkpoint.task_id, self.checkpoint.session, read_evidence, view
        )
        return view


def start_derive_segment(checkpoint: SessionCheckpoint, round_number: int) -> int:
    """Start a fresh input view while retaining all prior messages for review."""
    history = checkpoint.session.state.get("in_memory", {}).get("messages", [])
    start = len(history)
    checkpoint.session.state["bbx_derive_segment_start"] = start
    checkpoint.session.state["bbx_derive_segment_round"] = round_number
    return start
