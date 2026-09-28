"""Rule and calculated-state checks without database or network."""

from decimal import Decimal

import pytest
from bbx_blackboard.domain import (
    BoardState,
    RuleViolation,
    calculate_cost,
    decide,
    dispute_fields,
    pending_claims,
)
from bbx_contracts.models import Price, Usage


def board():
    return BoardState(
        task={
            "status": "running",
            "version": 4,
            "acceptance": [{"id": "A1", "desc": "Proof"}],
            "acceptance_state": {"A1": {"status": "unmet", "evidence_facts": []}},
            "last_judgment_version": 0,
            "usage": {},
            "budget": {"max_cost": "10"},
            "failure_streak": 0,
            "seed_empty_count": 0,
            "params": {
                "dispute_notify_depth": 2,
                "intent_max_attempts": 2,
                "conclude_grace_calls": 2,
            },
        },
        agents={
            "agent-1": {"status": "running", "task_type": "explore"},
            "agent-2": {"status": "running", "task_type": "explore"},
            "agent-3": {
                "status": "running",
                "task_type": "close",
                "close_mode": "judge",
                "judge_from_version": 4,
            },
            "agent-4": {"status": "concluding", "task_type": "explore", "grace_calls_left": 1},
        },
        facts={
            "F1": {
                "id": "F1",
                "kind": "observation",
                "statement": "proof",
                "author": "agent-1",
                "derived_from": [],
                "disputes": [],
                "satisfies": [],
                "version": 3,
            }
        },
        intents={
            "I1": {
                "id": "I1",
                "status": "open",
                "holder": None,
                "attempts": 0,
                "result": None,
                "method": "check",
                "author": "agent-1",
            },
            "I2": {
                "id": "I2",
                "status": "closed",
                "holder": None,
                "attempts": 1,
                "result": "inconclusive",
                "method": "old",
                "author": "agent-1",
            },
        },
        counters={"F": 1, "I": 2, "agent": 4},
    )


def fact(**kwargs):
    return {
        "kind": "observation",
        "statement": "specific result",
        "evidence": [{"uri": "e1"}],
        "derived_from": [],
        "disputes": [],
        "resolves": None,
        "result": None,
        "satisfies": [],
        **kwargs,
    }


def intent(**kwargs):
    return {
        "statement": "investigate",
        "based_on": ["F1"],
        "expected": "evidence",
        "method": "measure",
        "relates_to": ["A1"],
        "retry_of": None,
        "claim": False,
        **kwargs,
    }


@pytest.mark.parametrize(
    "command,actor,data,kind",
    [
        ("post_fact", "agent-1", fact(), "fact.posted"),
        ("post_fact", "agent-1", fact(kind="inference", derived_from=["F1"]), "fact.posted"),
        ("post_fact", "agent-1", fact(disputes=["F1"]), "fact.posted"),
        ("post_fact", "agent-1", fact(satisfies=["A1"]), "fact.posted"),
        ("post_intent", "agent-1", intent(), "intent.posted"),
        ("post_intent", "agent-1", intent(retry_of="I2"), "intent.posted"),
        ("claim", "agent-1", {"intent_id": "I1"}, "intent.claimed"),
        ("claim_for", "scheduler", {"intent_id": "I1", "agent_id": "agent-2"}, "intent.claimed"),
        ("register_agent", "scheduler", {"task_type": "explore", "is_seed": True}, "agent.spawned"),
        (
            "heartbeat",
            "agent-1",
            {
                "agent_id": "agent-1",
                "steps": 1,
                "context_tokens": 10,
                "last_seen_version": 4,
                "usage": {},
            },
            "agent.progress",
        ),
        (
            "conclude",
            "scheduler",
            {"agent_id": "agent-1", "reason": "limit"},
            "agent.conclude_requested",
        ),
        ("take_grace", "agent-4", {"agent_id": "agent-4"}, "agent.progress"),
        ("record_tool_call", "agent-1", {"id": "c1", "agent_id": "agent-1"}, "tool_call.recorded"),
        ("transition", "scheduler", {"status": "closing"}, "task.closing"),
    ],
)
def test_allowed_commands(command, actor, data, kind):
    assert decide(board(), command, actor, data)[0]["type"] == kind


@pytest.mark.parametrize(
    "command,actor,data,code",
    [
        ("post_fact", "agent-1", fact(evidence=[]), "evidence_required"),
        ("post_fact", "agent-1", fact(kind="inference"), "derived_from_required"),
        ("post_fact", "agent-1", fact(derived_from=["F99"]), "invalid_reference"),
        ("post_fact", "agent-1", fact(disputes=["F99"]), "invalid_reference"),
        ("post_fact", "agent-1", fact(satisfies=["A99"]), "invalid_reference"),
        ("post_fact", "agent-1", fact(resolves="I1", result="confirmed"), "not_holder"),
        ("post_fact", "agent-1", fact(resolves="I1"), "result_required"),
        ("post_intent", "agent-1", intent(based_on=[]), "intent_incomplete"),
        ("post_intent", "agent-1", intent(relates_to=[]), "intent_incomplete"),
        ("post_intent", "agent-1", intent(expected=""), "intent_incomplete"),
        ("post_intent", "agent-1", intent(based_on=["F99"]), "invalid_reference"),
        ("post_intent", "agent-1", intent(relates_to=["A99"]), "invalid_reference"),
        ("post_intent", "agent-1", intent(retry_of="I1"), "invalid_retry"),
        ("post_intent", "agent-1", intent(retry_of="I2", method="old"), "invalid_retry"),
        ("release", "agent-1", {"intent_id": "I1", "note": "handoff"}, "not_holder"),
        ("system_close", "system", {"intent_id": "I1"}, "close_not_allowed"),
        ("take_grace", "agent-1", {"agent_id": "agent-1"}, "grace_exhausted"),
        ("transition", "scheduler", {"status": "finished"}, "invalid_transition"),
    ],
)
def test_rejected_commands(command, actor, data, code):
    with pytest.raises(RuleViolation) as exc:
        decide(board(), command, actor, data)
    assert exc.value.code == code
    assert str(exc.value)


@pytest.mark.parametrize("status", ["finished", "failed", "stopped"])
def test_terminal_state_is_stable_and_blocks_new_agents(status: str) -> None:
    state = board()
    state.task["status"] = status
    assert decide(state, "transition", "scheduler", {"status": status}) == []
    for other in {"finished", "failed", "stopped", "running"} - {status}:
        with pytest.raises(RuleViolation) as exc:
            decide(state, "transition", "scheduler", {"status": other})
        assert exc.value.code == "invalid_transition"
    with pytest.raises(RuleViolation) as exc:
        decide(state, "register_agent", "scheduler", {"task_type": "explore"})
    assert exc.value.code == "task_terminal"


def test_created_task_can_still_register_agent() -> None:
    state = board()
    state.task["status"] = "created"
    assert (
        decide(state, "register_agent", "scheduler", {"task_type": "explore"})[0]["type"]
        == "agent.spawned"
    )


@pytest.mark.parametrize("mode", ["judge", "final"])
def test_only_one_active_close_agent_may_be_registered(mode: str) -> None:
    state = board()
    with pytest.raises(RuleViolation) as exc:
        decide(state, "register_agent", "scheduler", {"task_type": "close", "close_mode": mode})
    assert exc.value.code == "close_already_running"
    assert (
        decide(state, "register_agent", "scheduler", {"task_type": "explore"})[0]["type"]
        == "agent.spawned"
    )
    assert (
        decide(state, "register_agent", "scheduler", {"task_type": "derive"})[0]["type"]
        == "agent.spawned"
    )
    state.agents["agent-3"]["status"] = "finished"
    assert (
        decide(state, "register_agent", "scheduler", {"task_type": "close", "close_mode": mode})[0][
            "type"
        ]
        == "agent.spawned"
    )


def test_resolve_and_claim_release():
    state = board()
    state.intents["I1"]["status"] = "claimed"
    state.intents["I1"]["holder"] = "agent-1"
    posted = decide(state, "post_fact", "agent-1", fact(resolves="I1", result="confirmed"))
    assert [x["type"] for x in posted] == ["fact.posted", "intent.closed"]
    released = decide(state, "release", "agent-1", {"intent_id": "I1", "note": "handoff"})
    assert [x["type"] for x in released] == ["intent.released"]
    state.intents["I1"]["attempts"] = 1
    assert [
        x["type"]
        for x in decide(state, "release", "agent-1", {"intent_id": "I1", "note": "handoff"})
    ] == ["intent.released", "intent.closed"]


@pytest.mark.parametrize(
    "status, conclude_reason, counted",
    [
        ("running", None, True),
        ("concluding", "limit", True),
        ("concluding", "closing", False),
    ],
)
def test_manual_release_counts_except_during_closing_handoff(
    status: str, conclude_reason: str | None, counted: bool
) -> None:
    state = board()
    state.intents["I1"].update(status="claimed", holder="agent-1", attempts=1)
    state.agents["agent-1"].update(status=status, conclude_reason=conclude_reason)
    events = decide(
        state,
        "release",
        "agent-1",
        {"intent_id": "I1", "note": "交接已完成"},
    )
    assert events[0]["type"] == "intent.released"
    assert events[0]["payload"]["counted"] is counted
    assert events[0]["payload"]["note"] == "交接已完成"
    assert [event["type"] for event in events[1:]] == (["intent.closed"] if counted else [])


def test_dispute_recursion_retraction_and_notification():
    state = board()
    for number in range(2, 6):
        state.facts[f"F{number}"] = {
            "id": f"F{number}",
            "author": f"agent-{number % 2 + 1}",
            "derived_from": [],
            "disputes": [f"F{number - 1}"],
            "satisfies": [],
            "version": number,
        }
    fields = dispute_fields(state.facts)
    assert [fields[f"F{x}"]["status"] for x in range(1, 6)] == [
        "proposed",
        "disputed",
        "proposed",
        "disputed",
        "proposed",
    ]
    assert fields["F5"]["depth"] == 4
    state.task["acceptance_state"]["A1"] = {"status": "met", "evidence_facts": ["F1"]}
    state.facts = {"F1": state.facts["F1"]}
    changes = decide(state, "post_fact", "agent-1", fact(disputes=["F1"]))
    assert [x["type"] for x in changes] == ["fact.posted", "fact.disputed", "acceptance.reverted"]
    assert changes[1]["addressed_to"] == ["agent-1"]  # own fact withdrawal
    state.facts["F2"] = {**changes[0]["payload"], "version": 5}
    state.counters["F"] = 2
    reply = decide(state, "post_fact", "agent-2", fact(disputes=["F2"]))
    assert [x["type"] for x in reply] == ["fact.posted", "fact.undisputed", "fact.disputed"]
    assert reply[-1]["addressed_to"] == ["agent-1"]


def test_pending_claims_cost_and_finish_rules():
    state = board()
    state.facts["F1"]["satisfies"] = ["A1"]
    assert pending_claims(state)
    state.task["last_judgment_version"] = 3
    assert not pending_claims(state)
    assert calculate_cost(Usage(cache_hit_tokens=1_000_000), Price())[1] == "价格未配置"
    assert (
        calculate_cost(
            Usage(cache_hit_tokens=1_000_000),
            Price(
                cache_hit_per_m=Decimal("1"),
                cache_miss_per_m=Decimal("2"),
                output_per_m=Decimal("3"),
            ),
        )[0]
        == 1
    )
    state.agents["agent-1"]["task_type"] = "derive"
    state.intents["I1"].update(status="claimed", holder="agent-1")
    kinds = [
        x["type"]
        for x in decide(
            state,
            "finish_agent",
            "agent-1",
            {
                "agent_id": "agent-1",
                "end_reason": "normal",
                "receipt": {"accepted": True, "data": {"posted": [], "excluded": ["x"]}},
            },
        )
    ]
    assert kinds == ["intent.released", "derive.result", "agent.finished"]
    assert (
        decide(
            state,
            "finish_agent",
            "agent-1",
            {"agent_id": "agent-1", "end_reason": "runtime_restart", "receipt": {}},
        )[0]["payload"]["counted"]
        is False
    )


def test_close_verdict_validation():
    state = board()
    verdict = {"id": "A1", "verdict": "met", "reason": "proved", "evidence_facts": ["F1"]}
    assert (
        decide(state, "submit_close", "agent-3", {"verdicts": [verdict]})[0]["type"]
        == "acceptance.judged"
    )
    state.facts["F2"] = {
        "id": "F2",
        "author": "agent-2",
        "derived_from": [],
        "disputes": ["F1"],
        "satisfies": [],
        "version": 5,
    }
    with pytest.raises(RuleViolation, match="支撑事实"):
        decide(state, "submit_close", "agent-3", {"verdicts": [verdict]})


def test_budget_and_failure_thresholds():
    state = board()
    heartbeat = decide(
        state,
        "heartbeat",
        "agent-1",
        {
            "agent_id": "agent-1",
            "steps": 1,
            "context_tokens": 10,
            "last_seen_version": 4,
            "usage": {"cost": 11.0, "output_tokens": 10},
        },
    )
    assert [x["type"] for x in heartbeat] == ["agent.progress", "budget.updated"]
    assert heartbeat[1]["payload"]["exhausted"]
    state.task["failure_streak"] = 2
    failure = decide(
        state,
        "finish_agent",
        "agent-1",
        {"agent_id": "agent-1", "end_reason": "runtime_error", "receipt": {}},
    )
    assert [x["type"] for x in failure] == ["agent.finished", "task.failed"]
    state.agents["agent-1"]["is_seed"] = True
    state.task["failure_streak"] = 0
    state.task["seed_empty_count"] = 1
    state.facts.clear()
    state.intents.clear()
    seed = decide(
        state,
        "finish_agent",
        "agent-1",
        {"agent_id": "agent-1", "end_reason": "normal", "receipt": {}},
    )
    assert [x["type"] for x in seed] == ["agent.finished", "task.failed"]


def test_restart_does_not_count_second_empty_seed_or_fail_task() -> None:
    state = board()
    state.task["seed_empty_count"] = 1
    state.facts.clear()
    state.intents.clear()
    state.agents["agent-1"].update(is_seed=True, status="concluding", conclude_reason="limit")
    events = decide(
        state,
        "finish_agent",
        "agent-1",
        {"agent_id": "agent-1", "end_reason": "runtime_restart", "receipt": {}},
    )
    assert [event["type"] for event in events] == ["agent.finished"]


@pytest.mark.parametrize("terminal", ["finished", "failed", "stopped"])
def test_terminal_finish_does_not_append_another_task_failed(terminal: str) -> None:
    state = board()
    state.task.update(status=terminal, failure_streak=2, seed_empty_count=1)
    state.facts.clear()
    state.intents.clear()
    state.agents["agent-1"]["is_seed"] = True
    events = decide(
        state,
        "finish_agent",
        "agent-1",
        {"agent_id": "agent-1", "end_reason": "runtime_error", "receipt": {}},
    )
    assert [event["type"] for event in events] == ["agent.finished"]


@pytest.mark.parametrize(
    "reason,concluding,counted",
    [
        ("refused", False, True),
        ("grace_timeout", False, True),
        ("heartbeat", False, True),
        ("runtime_error", False, True),
        ("limit", True, True),
        ("limit", False, False),
        ("normal", False, False),
        ("runtime_restart", False, False),
    ],
)
def test_finish_release_attempt_rules(reason, concluding, counted):
    state = board()
    state.intents["I1"].update(status="claimed", holder="agent-1")
    if concluding:
        state.agents["agent-1"]["status"] = "concluding"
    result = decide(
        state,
        "finish_agent",
        "agent-1",
        {"agent_id": "agent-1", "end_reason": reason, "receipt": {}},
    )
    assert result[0]["type"] == "intent.released"
    assert result[0]["payload"]["counted"] is counted


@pytest.mark.parametrize(
    "end_reason,conclude_reason,counted",
    [
        ("normal", "limit", True),
        ("grace_timeout", "limit", True),
        ("normal", "closing", False),
        ("grace_timeout", "closing", False),
        ("refused", "closing", False),
        ("runtime_error", "closing", True),
        ("runtime_restart", "limit", False),
        ("runtime_restart", "closing", False),
    ],
)
def test_finish_respects_conclude_reason(end_reason, conclude_reason, counted):
    state = board()
    state.intents["I1"].update(status="claimed", holder="agent-1")
    state.agents["agent-1"].update(status="concluding", conclude_reason=conclude_reason)
    result = decide(
        state,
        "finish_agent",
        "agent-1",
        {"agent_id": "agent-1", "end_reason": end_reason, "receipt": {}},
    )
    assert result[0]["type"] == "intent.released"
    assert result[0]["payload"]["counted"] is counted


@pytest.mark.parametrize(
    "command,data",
    [
        ("post_fact", fact()),
        ("post_intent", intent()),
        ("record_tool_call", {"id": "c1", "agent_id": "agent-1"}),
    ],
)
def test_finished_agent_cannot_write(command, data):
    state = board()
    state.agents["agent-1"]["status"] = "finished"
    with pytest.raises(RuleViolation) as exc:
        decide(state, command, "agent-1", data)
    assert exc.value.code == "agent_inactive"


@pytest.mark.parametrize(
    "end_reason,receipt",
    [
        ("runtime_error", {"accepted": False, "reason": "model unavailable"}),
        ("runtime_restart", {}),
        ("normal", {"accepted": False, "reason": "cannot start"}),
        ("normal", {"accepted": True, "data": {"note": "invalid receipt"}}),
    ],
)
def test_derive_failure_is_not_an_empty_result(end_reason, receipt):
    state = board()
    state.agents["agent-1"]["task_type"] = "derive"
    result = decide(
        state,
        "finish_agent",
        "agent-1",
        {"agent_id": "agent-1", "end_reason": end_reason, "receipt": receipt},
    )
    assert "derive.result" not in [event["type"] for event in result]


def test_review_registration_and_accepted_transition_guards():
    state = board()
    state.task.update(
        last_change_version=3,
        last_judgment_version=3,
        derive_empty_streak=0,
        budget={"max_cost": "10", "max_minutes": 60, "max_concurrent_agents": 4},
    )
    state.agents.clear()
    state.intents.clear()
    state.task["acceptance_state"]["A1"].update(
        status="met", evidence_facts=["F1"], completion_basis="inferred"
    )
    spawned = decide(
        state, "register_agent", "scheduler", {"task_type": "derive", "derive_review": True}
    )[0]["payload"]
    assert spawned["derive_from_version"] == 3
    assert spawned["derive_parallel"] is False
    with pytest.raises(RuleViolation) as stale:
        decide(state, "transition", "scheduler", {"status": "closing", "reason": "accepted"})
    assert stale.value.code == "stale_acceptance"
    with pytest.raises(RuleViolation) as invalid:
        decide(
            state,
            "register_agent",
            "scheduler",
            {"task_type": "derive", "derive_review": True, "derive_parallel": True},
        )
    assert invalid.value.code == "invalid_derive_mode"
    state.agents["agent-1"] = {
        "task_type": "derive",
        "derive_review": True,
        "status": "finished",
        "end_reason": "normal",
        "derive_from_version": 3,
        "finished_version": 8,
        "receipt": {"accepted": True, "data": {"posted": [], "excluded": ["checked"]}},
    }
    state.task["last_judgment_version"] = 8
    assert (
        decide(state, "transition", "scheduler", {"status": "closing", "reason": "accepted"})[0][
            "type"
        ]
        == "task.closing"
    )
    state.task["last_change_version"] = 9
    with pytest.raises(RuleViolation, match="验收完成条件"):
        decide(state, "transition", "scheduler", {"status": "closing", "reason": "accepted"})
    state.task["last_change_version"] = 3
    state.task["acceptance_state"]["A1"].update(
        completion_basis="explicit", completion_reason="The evidence directly covers the full scope"
    )
    state.agents.clear()
    assert (
        decide(state, "transition", "scheduler", {"status": "closing", "reason": "accepted"})[0][
            "type"
        ]
        == "task.closing"
    )
    with pytest.raises(RuleViolation) as unnecessary:
        decide(state, "register_agent", "scheduler", {"task_type": "derive", "derive_review": True})
    assert unnecessary.value.code == "stale_derive"


def test_review_receipt_must_match_authored_intents_and_explain_empty_result():
    state = board()
    state.agents["agent-1"].update(task_type="derive", derive_review=True, derive_from_version=4)
    state.task["last_change_version"] = 4
    state.intents.clear()
    for receipt in (
        {"accepted": True, "data": {"posted": [], "excluded": []}},
        {"accepted": True, "data": {"posted": [], "excluded": [" "]}},
        {"accepted": True, "data": {}},
        {"accepted": False, "reason": "refused"},
    ):
        result = decide(
            state,
            "finish_agent",
            "agent-1",
            {"agent_id": "agent-1", "end_reason": "normal", "receipt": receipt},
        )
        assert [item["type"] for item in result] == ["agent.finished"]
        assert result[0]["payload"]["end_reason"] == "runtime_error"
    refused = decide(
        state,
        "finish_agent",
        "agent-1",
        {"agent_id": "agent-1", "end_reason": "refused", "receipt": {"accepted": False}},
    )
    assert refused[0]["payload"]["end_reason"] == "runtime_error"
    state.intents["I3"] = {"status": "closed", "holder": None, "author": "agent-1"}
    mismatched = decide(
        state,
        "finish_agent",
        "agent-1",
        {
            "agent_id": "agent-1",
            "end_reason": "normal",
            "receipt": {"accepted": True, "data": {"posted": [], "excluded": ["checked"]}},
        },
    )
    assert mismatched[-1]["payload"]["end_reason"] == "runtime_error"
    valid = decide(
        state,
        "finish_agent",
        "agent-1",
        {
            "agent_id": "agent-1",
            "end_reason": "normal",
            "receipt": {"accepted": True, "data": {"posted": ["I3"], "excluded": []}},
        },
    )
    assert valid[0]["payload"]["posted"] == ["I3"]


def test_legacy_empty_streak_does_not_block_first_completion_review():
    state = board()
    state.task.update(
        last_change_version=3,
        last_judgment_version=3,
        derive_empty_streak=2,
        budget={"max_cost": "10", "max_minutes": 60, "max_concurrent_agents": 4},
        params={**state.task["params"], "derive_enabled": False},
    )
    state.agents.clear()
    state.intents.clear()
    spawned = decide(
        state, "register_agent", "scheduler", {"task_type": "derive", "derive_review": True}
    )[0]["payload"]
    assert spawned["derive_review"] is True
    assert spawned["derive_from_version"] == 3


def test_ordinary_derive_failure_preserves_existing_end_reason():
    state = board()
    state.agents["agent-1"]["task_type"] = "derive"
    for end_reason, receipt in (
        ("refused", {"accepted": False, "reason": "cannot start"}),
        ("normal", {"accepted": True, "data": {"note": "invalid"}}),
    ):
        result = decide(
            state,
            "finish_agent",
            "agent-1",
            {"agent_id": "agent-1", "end_reason": end_reason, "receipt": receipt},
        )
        assert result[-1]["payload"]["end_reason"] == end_reason


def test_explicit_verdict_requires_direct_evidence_and_reason():
    state = board()
    verdict = {
        "id": "A1",
        "verdict": "met",
        "reason": "proved",
        "evidence_facts": ["F1"],
        "completion_basis": "explicit",
        "completion_reason": " ",
    }
    with pytest.raises(RuleViolation) as invalid:
        decide(state, "submit_close", "agent-3", {"verdicts": [verdict]})
    assert invalid.value.code == "invalid_explicit_completion"
    verdict["completion_reason"] = "Evidence directly covers the full scope"
    assert (
        decide(state, "submit_close", "agent-3", {"verdicts": [verdict]})[0]["payload"]["verdicts"][
            0
        ]["completion_basis"]
        == "explicit"
    )


def test_dispute_depth_and_relied_by():
    state = board()
    for number in range(2, 5):
        state.facts[f"F{number}"] = {
            "id": f"F{number}",
            "author": "agent-2",
            "derived_from": ["F1"] if number == 2 else [],
            "disputes": [f"F{number - 1}"],
            "satisfies": [],
            "version": number,
        }
    fields = dispute_fields(state.facts)
    assert fields["F1"]["relied_by"] == 1
    state.counters["F"] = 4
    events = decide(state, "post_fact", "agent-1", fact(disputes=["F4"]))
    changed = [x for x in events if x["type"] in {"fact.disputed", "fact.undisputed"}]
    assert changed
    assert all(x["addressed_to"] is None for x in changed)


def test_one_intent_per_agent():
    state = board()
    state.intents["I2"].update(status="claimed", holder="agent-1")
    with pytest.raises(RuleViolation) as claimed:
        decide(state, "claim", "agent-1", {"intent_id": "I1"})
    assert claimed.value.code == "already_holding"
    with pytest.raises(RuleViolation) as posted:
        decide(state, "post_intent", "agent-1", intent(claim=True))
    assert posted.value.code == "already_holding"
