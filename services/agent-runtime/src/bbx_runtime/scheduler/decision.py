"""Deterministic scheduling over the blackboard's current folded state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from bbx_contracts.completion import all_met, current_review, explicit_completion, quiescent
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
    started = _utc(task.get("active_since") or task.get("started_at"))
    current = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    timed_out = started is not None and current >= started + timedelta(
        seconds=int(budget["max_minutes"]) * 60 - int(task.get("active_seconds") or 0)
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
        failed_final_non_runtime = sum(
            agent["task_type"] == "close"
            and agent.get("close_mode") == "final"
            and agent["status"] in {"finished", "failed"}
            and agent.get("end_reason") not in {"runtime_error", "runtime_restart"}
            for agent in state["agents"].values()
        )
        if (
            failed_final_non_runtime + int(task.get("failure_streak") or 0)
            >= params.max_consecutive_failures
        ):
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

    # The state endpoint keeps these versions on task. Accept the older test fixture
    # shape as well, without mutating the supplied state.
    completion_task = {
        **task,
        "last_change_version": task.get("last_change_version", state.get("last_change_version", 0)),
        "last_judgment_version": task.get(
            "last_judgment_version", state.get("last_judgment_version", 0)
        ),
    }
    change_version = int(completion_task["last_change_version"] or 0)
    judgment_version = int(completion_task["last_judgment_version"] or 0)
    acceptance = task["acceptance_state"]
    if explicit_completion(completion_task):
        return [EnterClosing("accepted")]
    if _explore_budget_exhausted(task, params, now):
        return [EnterClosing("terminated")]

    quiet = quiescent(state["agents"], state["intents"])
    review = current_review(completion_task, state["agents"])
    review_version = int(review["finished_version"]) if review else 0
    if not judging and (
        state.get("pending_claims")
        or (quiet and max(change_version, review_version) > judgment_version)
    ):
        actions.append(SpawnClose("judge"))
        judging = True

    if quiet and not judging and judgment_version >= change_version and review:
        if all_met(completion_task):
            return [EnterClosing("accepted")]
        if (
            not params.derive_enabled
            or int(task.get("derive_empty_streak") or 0) >= params.derive_empty_limit
        ):
            return [EnterClosing("terminated")]

    slots = int(task["budget"]["max_concurrent_agents"]) - len(workers)
    if slots <= 0:
        return actions
    if state["board_empty"] and not workers:
        actions.append(SpawnExplore(seed=True))
        return actions
    if quiet and not judging and judgment_version >= change_version:
        return [SpawnDerive(review=True)]

    unmet = {key for key, item in acceptance.items() if item["status"] != "met"}
    open_intents = sorted(
        (intent for intent in intents if intent["status"] == "open"),
        key=lambda intent: _priority(intent, unmet),
    )
    actions.extend(SpawnExplore(intent_id=intent["id"]) for intent in open_intents[:slots])
    if (
        params.derive_enabled
        and not judging
        and not open_intents
        and any(agent["task_type"] == "explore" for agent in workers)
        and not any(agent["task_type"] == "derive" for agent in workers)
    ):
        latest_fact = max(
            (int(fact.get("version") or 0) for fact in state["facts"].values()), default=0
        )
        latest_derive = max(
            (
                max(
                    int(agent["derive_from_version"]),
                    int(agent.get("last_seen_version") or 0)
                    if agent.get("status") == "finished" and agent.get("end_reason") == "normal"
                    else 0,
                )
                for agent in agents
                if agent["task_type"] == "derive" and agent.get("derive_from_version") is not None
            ),
            default=0,
        )
        if latest_fact > latest_derive:
            actions.append(SpawnDerive(parallel=True))
    return actions


__all__ = ["Action", "decide"]
