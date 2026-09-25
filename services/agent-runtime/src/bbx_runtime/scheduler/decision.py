"""Deterministic scheduling over the blackboard's current folded state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from bbx_contracts.models import Params

from .actions import (
    Action,
    Conclude,
    EnterClosing,
    Fail,
    SpawnClose,
    SpawnDerive,
    SpawnExplore,
    SystemClose,
)


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    stamp = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def _explore_budget_exhausted(task: dict[str, Any], params: Params, now: datetime) -> bool:
    budget = task["budget"]
    usage = task.get("usage") or {}
    spent = Decimal(str(usage.get("cost", 0) or 0))
    limit = Decimal(str(budget["max_cost"])) * (Decimal(1) - params.close_reserve_ratio)
    started = _utc(task.get("started_at"))
    current = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    timed_out = started is not None and current >= started + timedelta(
        minutes=int(budget["max_minutes"])
    )
    return spent >= limit or timed_out


def _priority(intent: dict[str, Any], unmet: set[str]) -> tuple[bool, int, str]:
    return (
        not bool(unmet.intersection(intent.get("relates_to") or [])),
        int(intent.get("version") or 0),
        str(intent["id"]),
    )


def _ever_claimed(state: dict[str, Any], agent: dict[str, Any]) -> bool:
    aid = agent["id"]
    return (
        bool(agent.get("intent_id"))
        or any(
            note["by"] == aid
            for intent in state["intents"].values()
            for note in intent.get("notes", [])
        )
        or any(
            fact.get("author") == aid and fact.get("resolves") for fact in state["facts"].values()
        )
    )


def decide(state: dict[str, Any], params: Params, now: datetime) -> list[Action]:
    """Return ordered actions without changing the supplied state."""
    task = state["task"]
    status = task["status"]
    if status == "closing":
        active = [
            agent
            for agent in state["agents"].values()
            if agent["status"] in {"running", "concluding"}
        ]
        if active:
            return []
        failed_finals = sum(
            agent["task_type"] == "close"
            and agent.get("close_mode") == "final"
            and agent["status"] in {"finished", "failed"}
            and agent.get("end_reason") != "runtime_restart"
            for agent in state["agents"].values()
        )
        if failed_finals >= params.max_consecutive_failures:
            return [Fail("终结报告连续未完成")]
        return [SpawnClose("final")]
    if status != "running":
        return []

    agents = list(state["agents"].values())
    active = [agent for agent in agents if agent["status"] in {"running", "concluding"}]
    workers = [agent for agent in active if agent["task_type"] in {"explore", "derive"}]
    judging = any(agent["task_type"] == "close" for agent in active)
    if int(task.get("failure_streak") or 0) >= params.max_consecutive_failures:
        return [Fail("连续运行错误")]
    if int(task.get("seed_empty_count") or 0) >= 2:
        return [Fail("无法起步")]

    actions: list[Action] = []
    for agent in active:
        if agent["status"] != "running" or agent["task_type"] != "explore":
            continue
        limit = (
            params.seed_max_steps
            if agent.get("is_seed") and not _ever_claimed(state, agent)
            else params.explore_max_steps
        )
        if int(agent["steps"]) >= limit or int(agent["context_tokens"]) >= params.context_threshold:
            actions.append(Conclude(agent["id"], "limit"))

    intents = list(state["intents"].values())
    exhausted = [
        intent
        for intent in intents
        if intent["status"] == "open" and int(intent["attempts"]) >= params.intent_max_attempts
    ]
    if exhausted:
        actions.extend(SystemClose(intent["id"]) for intent in exhausted)
        # Closing an intent changes the board version. Re-read before deciding judge or derive.
        return actions

    acceptance = task["acceptance_state"]
    if acceptance and all(item["status"] == "met" for item in acceptance.values()):
        return [EnterClosing("accepted")]
    if (
        _explore_budget_exhausted(task, params, now)
        or int(task.get("derive_empty_streak") or 0) >= params.derive_empty_limit
    ):
        return [EnterClosing("terminated")]

    quiescent = not workers and not any(
        intent["status"] in {"open", "claimed"} for intent in intents
    )
    if not judging and (
        state.get("pending_claims")
        or (quiescent and state["last_change_version"] > state["last_judgment_version"])
    ):
        actions.append(SpawnClose("judge"))
        judging = True

    slots = int(task["budget"]["max_concurrent_agents"]) - len(workers)
    if slots <= 0:
        return actions
    if state["board_empty"] and not workers:
        actions.append(SpawnExplore(seed=True))
        return actions
    if quiescent and not judging and state["last_judgment_version"] >= state["last_change_version"]:
        return [SpawnDerive()] if params.derive_enabled else [EnterClosing("terminated")]

    unmet = {key for key, item in acceptance.items() if item["status"] != "met"}
    open_intents = sorted(
        (intent for intent in intents if intent["status"] == "open"),
        key=lambda intent: _priority(intent, unmet),
    )
    actions.extend(SpawnExplore(intent_id=intent["id"]) for intent in open_intents[:slots])
    return actions


__all__ = ["Action", "decide"]
