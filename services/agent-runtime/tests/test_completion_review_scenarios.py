"""Completion review uses the real board with scripted agents."""

from __future__ import annotations

from pathlib import Path

import pytest
from bbx_runtime.testing.scripted_client import ScriptedChatClient
from scenario_support import close_step, fact_step, intent_step, receipt_step, scenario

pytestmark = pytest.mark.integration


def inferred(fact_id: str) -> dict:
    return {
        "id": "A1",
        "verdict": "met",
        "reason": "The current evidence supports this result, subject to review",
        "evidence_facts": [fact_id],
        "completion_basis": "inferred",
    }


async def test_inferred_completion_reviews_and_rejudges_before_acceptance(
    runtime_infrastructure, tmp_path: Path
) -> None:
    judges = 0

    def script(task_type, intent_id, mode, aid, state):
        nonlocal judges
        if task_type == "explore":
            assert state["agents"][aid]["is_seed"]
            return ScriptedChatClient(
                [
                    fact_step(aid, "Direct observation", satisfies=["A1"]),
                    receipt_step("explore", posted=["F1"]),
                ]
            )
        if task_type == "derive":
            assert state["agents"][aid]["derive_review"] is True
            return ScriptedChatClient(
                [receipt_step("derive", excluded=["No further evidence-backed direction exists"])]
            )
        if task_type == "close" and mode == "judge":
            judges += 1
            return ScriptedChatClient([close_step([inferred("F1")]), receipt_step("close")])
        if task_type == "close" and mode == "final":
            return ScriptedChatClient(
                [
                    close_step([inferred("F1")], report="# Final report\n\nA1 is met."),
                    receipt_step("close"),
                ]
            )
        raise AssertionError(f"Unexpected run: {task_type}/{mode}/{intent_id}")

    async with scenario(runtime_infrastructure, tmp_path, script) as run:
        await run.start()
        finished = await run.wait(lambda st: st["task"]["status"] == "finished", seconds=30)
        reviews = [agent for agent in finished["agents"].values() if agent["task_type"] == "derive"]
        assert len(reviews) == 1 and reviews[0]["derive_review"] is True
        assert judges == 2
        assert finished["task"]["last_judgment_version"] >= reviews[0]["finished_version"]
        events = await run.events()
        types = [event["type"] for event in events]
        assert types.index("agent.finished") < types.index("task.closing")


async def test_review_direction_returns_to_exploration_then_reviews_again(
    runtime_infrastructure, tmp_path: Path
) -> None:
    reviews = 0
    judges = 0

    def script(task_type, intent_id, mode, aid, state):
        nonlocal reviews, judges
        if task_type == "explore" and state["agents"][aid]["is_seed"]:
            return ScriptedChatClient(
                [
                    fact_step(aid, "Initial evidence", satisfies=["A1"]),
                    receipt_step("explore", posted=["F1"]),
                ]
            )
        if task_type == "explore" and intent_id == "I1":
            return ScriptedChatClient(
                [
                    fact_step(
                        aid, "Follow-up result", resolves="I1", result="confirmed", satisfies=["A1"]
                    ),
                    receipt_step("explore", posted=["F2"], intent_result="confirmed"),
                ]
            )
        if task_type == "derive":
            assert state["agents"][aid]["derive_review"] is True
            reviews += 1
            if reviews == 1:
                return ScriptedChatClient(
                    [
                        intent_step("Check a remaining direction"),
                        receipt_step("derive", posted=["I1"]),
                    ]
                )
            return ScriptedChatClient(
                [receipt_step("derive", excluded=["The remaining directions lack support"])]
            )
        if task_type == "close" and mode == "judge":
            judges += 1
            return ScriptedChatClient(
                [
                    close_step([inferred("F2" if "F2" in state["facts"] else "F1")]),
                    receipt_step("close"),
                ]
            )
        if task_type == "close" and mode == "final":
            return ScriptedChatClient(
                [
                    close_step([inferred("F2")], report="# Final report\n\nA1 is met."),
                    receipt_step("close"),
                ]
            )
        raise AssertionError(f"Unexpected run: {task_type}/{mode}/{intent_id}")

    async with scenario(runtime_infrastructure, tmp_path, script) as run:
        await run.start()
        finished = await run.wait(lambda st: st["task"]["status"] == "finished", seconds=35)
        assert reviews == 2 and judges >= 3
        assert finished["intents"]["I1"]["status"] == "closed"
        review_runs = [agent for agent in finished["agents"].values() if agent.get("derive_review")]
        assert len(review_runs) == 1 and review_runs[0]["derive_round"] == 2
        assert finished["task"]["last_judgment_version"] >= review_runs[0]["finished_version"]
        events = await run.events()
        assert sum(event["type"] == "derive.result" for event in events) == 2


async def test_invalid_empty_review_never_counts_as_acceptance(
    runtime_infrastructure, tmp_path: Path
) -> None:
    def script(task_type, intent_id, mode, aid, state):
        if task_type == "explore":
            return ScriptedChatClient(
                [
                    fact_step(aid, "Initial support", satisfies=["A1"]),
                    receipt_step("explore", posted=["F1"]),
                ]
            )
        if task_type == "close" and mode == "judge":
            return ScriptedChatClient([close_step([inferred("F1")]), receipt_step("close")])
        if task_type == "derive":
            assert state["agents"][aid]["derive_review"] is True
            return ScriptedChatClient([receipt_step("derive", excluded=[])])
        raise AssertionError(f"Invalid review must not reach {task_type}/{mode}/{intent_id}")

    async with scenario(runtime_infrastructure, tmp_path, script) as run:
        await run.start()
        failed = await run.wait(lambda st: st["task"]["status"] == "failed", seconds=30)
        reviews = [agent for agent in failed["agents"].values() if agent.get("derive_review")]
        assert len(reviews) >= 1
        assert all(agent["end_reason"] == "runtime_error" for agent in reviews)
        assert not any(
            event["type"] == "task.closing" and event["payload"]["reason"] == "accepted"
            for event in await run.events()
        )
