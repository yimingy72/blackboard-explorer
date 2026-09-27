"""Every scheduling rule is a pure, table-driven decision over folded board state."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from bbx_contracts.models import Params
from bbx_runtime.scheduler.actions import (
    Conclude,
    EnterClosing,
    Fail,
    SpawnClose,
    SpawnDerive,
    SpawnExplore,
    SystemClose,
)
from bbx_runtime.scheduler.decision import decide

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def board() -> dict[str, Any]:
    return {
        "task": {
            "status": "running",
            "budget": {"max_concurrent_agents": 3, "max_cost": "10", "max_minutes": 60},
            "usage": {"cost": 0},
            "started_at": NOW - timedelta(minutes=5),
            "acceptance_state": {"A1": {"status": "unmet"}, "A2": {"status": "unmet"}},
            "failure_streak": 0,
            "seed_empty_count": 0,
            "derive_empty_streak": 0,
        },
        "facts": {},
        "intents": {},
        "agents": {},
        "pending_claims": False,
        "board_empty": True,
        "last_change_version": 0,
        "last_judgment_version": 0,
    }


def agent(
    aid: str,
    *,
    task_type: str = "explore",
    status: str = "running",
    steps: int = 0,
    context_tokens: int = 0,
    is_seed: bool = False,
    intent_id: str | None = None,
    close_mode: str | None = None,
    end_reason: str | None = None,
    derive_from_version: int | None = None,
) -> dict[str, Any]:
    return {
        "id": aid,
        "task_type": task_type,
        "status": status,
        "steps": steps,
        "context_tokens": context_tokens,
        "is_seed": is_seed,
        "intent_id": intent_id,
        "close_mode": close_mode,
        "end_reason": end_reason,
        "derive_from_version": derive_from_version,
    }


def intent(
    iid: str,
    *,
    status: str = "open",
    attempts: int = 0,
    relates_to: list[str] | None = None,
    version: int = 1,
) -> dict[str, Any]:
    return {
        "id": iid,
        "status": status,
        "attempts": attempts,
        "relates_to": relates_to or [],
        "version": version,
    }


@pytest.mark.parametrize("status", ["created", "provisioning", "finished", "failed", "stopped"])
def test_non_running_status_has_no_actions(status: str) -> None:
    state = board()
    state["task"]["status"] = status
    assert decide(state, Params(), NOW) == []


def test_empty_board_spawns_one_seed_without_mutating_state() -> None:
    state = board()
    original = deepcopy(state)
    assert decide(state, Params(), NOW) == [SpawnExplore(seed=True)]
    assert state == original


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ([], [SpawnClose("final")]),
        ([agent("agent-1", task_type="close", close_mode="final")], []),
        ([agent("agent-1", status="concluding")], []),
        ([agent("agent-1", task_type="close", close_mode="judge")], []),
        (
            [
                agent(
                    "agent-1",
                    task_type="close",
                    close_mode="final",
                    status="finished",
                    end_reason="normal",
                )
            ],
            [SpawnClose("final")],
        ),
        (
            [
                agent(
                    "agent-1",
                    task_type="close",
                    close_mode="final",
                    status="finished",
                    end_reason="runtime_restart",
                )
            ],
            [SpawnClose("final")],
        ),
    ],
)
def test_closing_waits_then_spawns_final(
    history: list[dict[str, Any]], expected: list[Any]
) -> None:
    state = board()
    state["task"]["status"] = "closing"
    state["agents"] = {item["id"]: item for item in history}
    assert decide(state, Params(), NOW) == expected


def test_final_retries_are_bounded_and_restart_does_not_count() -> None:
    state = board()
    state["task"]["status"] = "closing"
    state["agents"] = {
        "agent-1": agent(
            "agent-1", task_type="close", close_mode="final", status="finished", end_reason="normal"
        ),
        "agent-2": agent(
            "agent-2",
            task_type="close",
            close_mode="final",
            status="finished",
            end_reason="refused",
        ),
        "agent-3": agent(
            "agent-3",
            task_type="close",
            close_mode="final",
            status="failed",
            end_reason="runtime_error",
        ),
        "agent-4": agent(
            "agent-4",
            task_type="close",
            close_mode="final",
            status="finished",
            end_reason="runtime_restart",
        ),
        "agent-5": agent(
            "agent-5", task_type="close", close_mode="judge", status="finished", end_reason="normal"
        ),
    }
    assert decide(state, Params(), NOW) == [Fail("终结报告连续未完成")]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [("failure_streak", 3, "连续运行错误"), ("seed_empty_count", 2, "无法起步")],
)
def test_failure_preempts_spawns(field: str, value: int, reason: str) -> None:
    state = board()
    state["task"][field] = value
    state["pending_claims"] = True
    assert decide(state, Params(), NOW) == [Fail(reason)]


def test_failure_streak_takes_priority_over_empty_seed() -> None:
    state = board()
    state["task"]["failure_streak"] = 3
    state["task"]["seed_empty_count"] = 2
    assert decide(state, Params(), NOW) == [Fail("连续运行错误")]


@pytest.mark.parametrize(
    ("running", "expected"),
    [
        (agent("agent-1", is_seed=True, steps=20), [Conclude("agent-1", "limit")]),
        (agent("agent-1", is_seed=True, intent_id="I1", steps=20), []),
        (agent("agent-1", steps=60), [Conclude("agent-1", "limit")]),
        (agent("agent-1", context_tokens=128000), [Conclude("agent-1", "limit")]),
        (agent("agent-1", status="concluding", steps=80), []),
        (agent("agent-1", task_type="derive", steps=80), []),
    ],
)
def test_explore_limits(running: dict[str, Any], expected: list[Any]) -> None:
    state = board()
    state["agents"] = {running["id"]: running}
    assert decide(state, Params(), NOW) == expected


def test_system_close_over_attempt_limit_preempts_stale_dispatch() -> None:
    state = board()
    state["board_empty"] = False
    state["intents"] = {
        "I1": intent("I1", attempts=3),
        "I2": intent("I2", attempts=4),
        "I3": intent("I3", attempts=0),
    }
    assert decide(state, Params(), NOW) == [SystemClose("I1"), SystemClose("I2")]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda s: s["task"]["acceptance_state"].update(
                {"A1": {"status": "met"}, "A2": {"status": "met"}}
            ),
            EnterClosing("accepted"),
        ),
        (lambda s: s["task"]["usage"].update(cost="9.5"), EnterClosing("terminated")),
        (
            lambda s: s["task"].update(started_at=NOW - timedelta(minutes=60)),
            EnterClosing("terminated"),
        ),
        (lambda s: s["task"].update(derive_empty_streak=2), EnterClosing("terminated")),
    ],
)
def test_closing_triggers(change, expected: EnterClosing) -> None:
    state = board()
    change(state)
    state["agents"] = {"agent-1": agent("agent-1", steps=100)}
    assert decide(state, Params(), NOW) == [expected]


def test_time_budget_accepts_iso_timestamp_and_zero_cost_does_not_close() -> None:
    state = board()
    state["task"]["started_at"] = (NOW - timedelta(minutes=59)).isoformat().replace("+00:00", "Z")
    state["task"]["usage"] = {"cost": 0}
    assert decide(state, Params(), NOW) == [SpawnExplore(seed=True)]
    state["task"]["started_at"] = (NOW - timedelta(minutes=61)).replace(tzinfo=None)
    assert decide(state, Params(), NOW) == [EnterClosing("terminated")]
    state["task"]["started_at"] = None
    assert decide(state, Params(), NOW) == [SpawnExplore(seed=True)]


def test_close_judgment_runs_without_worker_slot() -> None:
    state = board()
    state["board_empty"] = False
    state["pending_claims"] = True
    state["task"]["budget"]["max_concurrent_agents"] = 1
    state["agents"] = {"agent-1": agent("agent-1")}
    assert decide(state, Params(), NOW) == [SpawnClose("judge")]
    state["agents"]["agent-2"] = agent("agent-2", task_type="close", close_mode="judge")
    assert decide(state, Params(), NOW) == []


def test_quiescent_new_board_change_triggers_judgment() -> None:
    state = board()
    state["board_empty"] = False
    state["last_change_version"] = 6
    state["last_judgment_version"] = 2
    assert decide(state, Params(), NOW) == [SpawnClose("judge")]


def test_derive_requires_quiescence_and_latest_judgment() -> None:
    state = board()
    state["board_empty"] = False
    state["last_change_version"] = 6
    state["last_judgment_version"] = 6
    assert decide(state, Params(), NOW) == [SpawnDerive()]
    state["agents"] = {"agent-1": agent("agent-1")}
    assert decide(state, Params(), NOW) == []
    state["agents"] = {"agent-1": agent("agent-1", task_type="close", close_mode="judge")}
    assert decide(state, Params(), NOW) == []


def test_fact_during_explore_starts_one_derive_then_dispatches_its_intents() -> None:
    state = board()
    state["board_empty"] = False
    state["agents"] = {"seed": agent("seed", is_seed=True, intent_id="I0")}
    state["intents"] = {"I0": intent("I0", status="claimed")}
    assert decide(state, Params(), NOW) == []

    state["facts"] = {"F1": {"version": 4, "author": "seed"}}
    assert decide(state, Params(), NOW) == [SpawnDerive(parallel=True)]
    state["agents"]["derive-1"] = agent("derive-1", task_type="derive", derive_from_version=4)
    assert decide(state, Params(), NOW) == []

    state["agents"]["derive-1"]["status"] = "finished"
    state["intents"].update(
        I1=intent("I1", relates_to=["A1"], version=5),
        I2=intent("I2", relates_to=["A2"], version=6),
    )
    assert decide(state, Params(), NOW) == [SpawnExplore("I1"), SpawnExplore("I2")]


def test_parallel_derive_waits_for_a_new_fact_version() -> None:
    state = board()
    state["board_empty"] = False
    state["facts"] = {"F1": {"version": 4}}
    state["agents"] = {
        "explore": agent("explore"),
        "derive-1": agent("derive-1", task_type="derive", status="finished", derive_from_version=4),
    }
    assert decide(state, Params(), NOW) == []
    state["last_change_version"] = 8  # An unrelated board event is insufficient.
    assert decide(state, Params(), NOW) == []
    state["facts"]["F2"] = {"version": 9}
    assert decide(state, Params(), NOW) == [SpawnDerive(parallel=True)]


def test_parallel_derive_respects_slots_open_intents_and_single_baseline() -> None:
    state = board()
    state["board_empty"] = False
    state["facts"] = {"F1": {"version": 4}}
    state["agents"] = {"explore": agent("explore")}
    state["task"]["budget"]["max_concurrent_agents"] = 1
    assert decide(state, Params(), NOW) == []

    state["task"]["budget"]["max_concurrent_agents"] = 2
    assert decide(state, Params(derive_enabled=False), NOW) == []
    state["intents"] = {"I1": intent("I1")}
    assert decide(state, Params(), NOW) == [SpawnExplore("I1")]


def test_parallel_derive_waits_for_claim_judgment() -> None:
    state = board()
    state["board_empty"] = False
    state["facts"] = {"F1": {"version": 4}}
    state["agents"] = {"explore": agent("explore")}
    state["pending_claims"] = True
    assert decide(state, Params(), NOW) == [SpawnClose("judge")]


def test_disabled_derive_waits_for_seed_intents_and_latest_judgment() -> None:
    state = board()
    params = Params(derive_enabled=False)
    assert decide(state, params, NOW) == [SpawnExplore(seed=True)]

    state["board_empty"] = False
    state["last_change_version"] = 4
    assert decide(state, params, NOW) == [SpawnClose("judge")]
    state["agents"] = {"agent-1": agent("agent-1", task_type="close", close_mode="judge")}
    assert decide(state, params, NOW) == []

    state["agents"] = {}
    state["last_judgment_version"] = 4
    state["intents"] = {"I1": intent("I1")}
    assert decide(state, params, NOW) == [SpawnExplore("I1")]
    state["intents"]["I1"]["status"] = "claimed"
    assert decide(state, params, NOW) == []
    state["intents"]["I1"]["status"] = "closed"
    state["last_change_version"] = 5
    assert decide(state, params, NOW) == [SpawnClose("judge")]
    state["last_judgment_version"] = 5
    assert decide(state, params, NOW) == [EnterClosing("terminated")]


def test_open_intents_prioritize_unmet_then_oldest_and_respect_slots() -> None:
    state = board()
    state["board_empty"] = False
    state["task"]["acceptance_state"]["A2"]["status"] = "met"
    state["task"]["budget"]["max_concurrent_agents"] = 3
    state["agents"] = {"agent-1": agent("agent-1", status="concluding")}
    state["intents"] = {
        "I3": intent("I3", relates_to=["A2"], version=1),
        "I2": intent("I2", relates_to=["A1"], version=3),
        "I1": intent("I1", relates_to=["A1"], version=2),
    }
    assert decide(state, Params(), NOW) == [SpawnExplore("I1"), SpawnExplore("I2")]


def test_claimed_intent_prevents_quiescent_judgment_or_derive() -> None:
    state = board()
    state["board_empty"] = False
    state["intents"] = {"I1": intent("I1", status="claimed")}
    state["last_change_version"] = 10
    assert decide(state, Params(), NOW) == []


def test_seed_is_not_repeated_while_worker_is_active() -> None:
    state = board()
    state["agents"] = {"agent-1": agent("agent-1", task_type="derive")}
    assert decide(state, Params(), NOW) == []


def test_manual_stop_is_already_closing_and_waits_for_active_agents() -> None:
    state = board()
    state["task"]["status"] = "closing"
    state["agents"] = {"agent-1": agent("agent-1")}
    assert decide(state, Params(), NOW) == []


@pytest.mark.parametrize("history", ["released", "closed"])
def test_seed_keeps_explore_limit_after_finishing_its_first_intent(history):
    state = board()
    state["board_empty"] = False
    state["agents"] = {"agent-1": agent("agent-1", is_seed=True, steps=20)}
    if history == "released":
        state["intents"] = {
            "I1": {**intent("I1", status="closed"), "notes": [{"by": "agent-1", "text": "handoff"}]}
        }
    else:
        state["facts"] = {"F2": {"author": "agent-1", "resolves": "I1"}}
    assert Conclude("agent-1", "limit") not in decide(state, Params(), NOW)
    state["agents"]["agent-1"]["steps"] = 60
    assert Conclude("agent-1", "limit") in decide(state, Params(), NOW)
