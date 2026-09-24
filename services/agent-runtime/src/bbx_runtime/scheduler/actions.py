"""Pure scheduler decisions for the action executor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class SpawnExplore:
    intent_id: str | None = None
    seed: bool = False


@dataclass(frozen=True, slots=True)
class SpawnDerive:
    pass


@dataclass(frozen=True, slots=True)
class SpawnClose:
    mode: Literal["judge", "final"]


@dataclass(frozen=True, slots=True)
class Conclude:
    agent_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class SystemClose:
    intent_id: str


@dataclass(frozen=True, slots=True)
class EnterClosing:
    reason: str


@dataclass(frozen=True, slots=True)
class Fail:
    reason: str


type Action = SpawnExplore | SpawnDerive | SpawnClose | Conclude | SystemClose | EnterClosing | Fail
