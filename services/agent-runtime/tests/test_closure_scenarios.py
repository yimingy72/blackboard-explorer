"""M3b scenarios 1, 2, 3, and 6 over real blackboard state."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from scenario_support import (
    Gate,
    GateClient,
    close_step,
    fact_step,
    intent_step,
    receipt_step,
    scenario,
)

pytestmark = pytest.mark.integration


def unmet(reason: str = "More evidence is needed") -> dict[str, Any]:
    return {"id": "A1", "verdict": "unmet", "reason": reason, "missing": reason}


def met(fact_id: str) -> dict[str, Any]:
    return {
        "id": "A1",
        "verdict": "met",
        "reason": "Supported by an undisputed fact",
        "evidence_facts": [fact_id],
        "completion_basis": "explicit",
        "completion_reason": "Direct evidence resolves the defined goal without uncertainty",
    }


async def test_scenario_1_successful_parallel_closure(
    runtime_infrastructure, tmp_path: Path
) -> None:
    leader = Gate()
    others = Gate(2)

    def script(task_type, intent_id, mode, aid, state):
        if task_type == "explore" and state["agents"][aid]["is_seed"]:
            return ScriptedChatClient(
                [
                    fact_step(aid, "Seed observation"),
                    *(intent_step(f"Investigate direction {index}") for index in range(1, 4)),
                    receipt_step("explore", posted=["F1", "I1", "I2", "I3"]),
                ]
            )
        if task_type == "explore" and intent_id == "I1":
            return GateClient(
                [
                    fact_step(
                        aid, "Verified result", resolves="I1", result="confirmed", satisfies=["A1"]
                    ),
                    receipt_step("explore", posted=["F2"], intent_result="confirmed"),
                ],
                aid,
                leader,
            )
        if task_type == "explore" and intent_id in {"I2", "I3"}:
            return GateClient(
                [
                    ScriptStep(calls=(ScriptToolCall("search", {"q": "Seed"}),)),
                    ScriptStep(
                        calls=(
                            ScriptToolCall(
                                "release", {"intent_id": intent_id, "note": "Closing handoff"}
                            ),
                        )
                    ),
                    receipt_step("explore", note="Handed off"),
                ],
                aid,
                others,
                at_call=2,
            )
        if task_type == "derive":
            return ScriptedChatClient(
                [receipt_step("derive", excluded=["No supported new direction"])]
            )
        if task_type == "close" and mode == "judge":
            support = next(
                fid
                for fid, fact in state["facts"].items()
                if "A1" in fact.get("satisfies", []) and fact["status"] == "proposed"
            )
            return ScriptedChatClient([close_step([met(support)]), receipt_step("close")])
        if task_type == "close" and mode == "final":
            support = state["task"]["acceptance_state"]["A1"]["evidence_facts"][0]
            return ScriptedChatClient(
                [
                    close_step(
                        [met(support)], report="# Final report\n\nA1 verified by the evidence."
                    ),
                    receipt_step("close"),
                ]
            )
        raise AssertionError(f"Unexpected agent {task_type}/{intent_id}/{mode}")

    async with scenario(runtime_infrastructure, tmp_path, script) as s:
        await s.start()
        await asyncio.wait_for(asyncio.gather(leader.ready.wait(), others.ready.wait()), 20)
        concurrent = await s.state()
        running = {
            aid
            for aid, agent in concurrent["agents"].items()
            if agent["task_type"] == "explore" and agent["status"] == "running"
        }
        assert running == leader.seen | others.seen
        assert len(running) == 3
        leader.release.set()
        await s.wait(
            lambda st: (
                st["task"]["status"] == "closing"
                and all(st["agents"][aid]["status"] == "concluding" for aid in others.seen)
            )
        )
        events = await s.events()
        concluded = {
            event["payload"]["agent_id"]
            for event in events
            if event["type"] == "agent.conclude_requested"
        }
        assert others.seen <= concluded
        others.release.set()
        finished = await s.wait(
            lambda st: (
                st["task"]["status"] == "finished" and st["task"].get("workspace_uri") is not None
            ),
            seconds=30,
        )
        assert finished["task"]["acceptance_state"]["A1"]["status"] == "met"
        assert finished["task"]["report_uri"] == f"reports/{s.task_id}.md"
        assert await s.objects.exists(finished["task"]["report_uri"])
        assert await s.objects.exists(finished["task"]["workspace_uri"])
        assert s.manager.destroyed == [s.task_id]
        for iid in ("I2", "I3"):
            handed_off = finished["intents"][iid]
            assert handed_off["status"] == "open"
            assert handed_off["holder"] is None
            assert handed_off["attempts"] == 0
            assert handed_off["notes"][-1]["text"] == "Closing handoff"
        all_events = await s.events()
        releases = [
            event
            for event in all_events
            if event["type"] == "intent.released" and event["payload"]["intent_id"] in {"I2", "I3"}
        ]
        assert {event["payload"]["intent_id"] for event in releases} == {"I2", "I3"}
        assert all(event["payload"]["counted"] is False for event in releases)
        assert all(event["payload"]["note"] == "Closing handoff" for event in releases)
        types = [event["type"] for event in all_events]
        assert "acceptance.judged" in types
        assert types[-1] == "task.archived"


async def test_scenario_2_failed_judgments_feed_explore_and_derive(
    runtime_infrastructure, tmp_path: Path
) -> None:
    feedback_gate = Gate()
    judge_count = 0
    derive_count = 0
    first_explore: ScriptedChatClient | None = None
    derive_client: ScriptedChatClient | None = None

    def script(task_type, intent_id, mode, aid, state):
        nonlocal judge_count, derive_count, first_explore, derive_client
        if task_type == "explore" and state["agents"][aid]["is_seed"]:
            return ScriptedChatClient(
                [
                    fact_step(aid, "Initial observation"),
                    intent_step("Inspect the initial claim"),
                    receipt_step("explore", posted=["F1", "I1"]),
                ]
            )
        if task_type == "explore" and intent_id == "I1":
            first_explore = GateClient(
                [
                    fact_step(aid, "Preliminary claim", satisfies=["A1"]),
                    ScriptStep(calls=(ScriptToolCall("search", {"q": "claim"}),)),
                    replace(
                        fact_step(
                            aid,
                            "Further evidence after feedback",
                            resolves="I1",
                            result="confirmed",
                            satisfies=["A1"],
                        ),
                        expect_contains="[裁定]",
                    ),
                    receipt_step("explore", posted=["F2", "F3"], intent_result="confirmed"),
                ],
                aid,
                feedback_gate,
                at_call=2,
            )
            return first_explore
        if task_type == "close" and mode == "judge":
            judge_count += 1
            if judge_count == 1:
                verdict = unmet("Need deeper evidence")
            elif judge_count == 2:
                verdict = unmet("Need new direction")
            else:
                support = max(
                    (fact for fact in state["facts"].values() if "A1" in fact.get("satisfies", [])),
                    key=lambda fact: fact["version"],
                )["id"]
                verdict = met(support)
            return ScriptedChatClient([close_step([verdict]), receipt_step("close")])
        if task_type == "derive":
            if state["agents"][aid]["derive_parallel"]:
                return ScriptedChatClient(
                    [receipt_step("derive", excluded=["No supported new direction"])]
                )
            derive_count += 1
            derive_client = ScriptedChatClient(
                [
                    replace(
                        intent_step("Try a new direction", based_on=["F3"]),
                        expect_contains="Need new direction",
                    ),
                    receipt_step("derive", posted=["I2"]),
                ]
            )
            return derive_client
        if task_type == "explore" and intent_id == "I2":
            return ScriptedChatClient(
                [
                    fact_step(
                        aid,
                        "Definitive proof",
                        resolves="I2",
                        result="confirmed",
                        satisfies=["A1"],
                    ),
                    receipt_step("explore", posted=["F4"], intent_result="confirmed"),
                ]
            )
        if task_type == "close" and mode == "final":
            support = state["task"]["acceptance_state"]["A1"]["evidence_facts"][0]
            return ScriptedChatClient(
                [
                    close_step([met(support)], report="# Final report\n\nA1 met after feedback."),
                    receipt_step("close"),
                ]
            )
        raise AssertionError(f"Unexpected agent {task_type}/{intent_id}/{mode}")

    async with scenario(runtime_infrastructure, tmp_path, script) as s:
        await s.start()
        await asyncio.wait_for(feedback_gate.ready.wait(), 20)
        first_judged = await s.wait(
            lambda st: (
                st["task"]["last_judgment_version"] > 0
                and st["task"]["acceptance_state"]["A1"]["status"] == "unmet"
            )
        )
        assert first_judged["agents"][next(iter(feedback_gate.seen))]["status"] == "running"
        assert judge_count == 1
        feedback_gate.release.set()
        finished = await s.wait(
            lambda st: (
                st["task"]["status"] == "finished" and st["task"].get("workspace_uri") is not None
            ),
            seconds=35,
        )
        assert judge_count == 3
        assert derive_count == 1
        assert first_explore is not None and len(first_explore.received_messages) >= 3
        assert derive_client is not None and derive_client.received_messages
        assert finished["task"]["acceptance_state"]["A1"]["status"] == "met"
        assert finished["task"]["acceptance_state"]["A1"]["evidence_facts"] == ["F4"]
        assert s.manager.destroyed == [s.task_id]
        judged = [event for event in await s.events() if event["type"] == "acceptance.judged"]
        assert [item["payload"]["verdicts"][0]["verdict"] for item in judged] == [
            "unmet",
            "unmet",
            "met",
            "met",
        ]


async def test_scenario_3_two_empty_derivations_close_with_missing_report(
    runtime_infrastructure, tmp_path: Path
) -> None:
    derive_runs = 0
    second_client: ScriptedChatClient | None = None

    def script(task_type, intent_id, mode, aid, state):
        nonlocal derive_runs, second_client
        if task_type == "explore" and state["agents"][aid]["is_seed"]:
            return ScriptedChatClient(
                [
                    fact_step(aid, "Seed fact without a direction"),
                    receipt_step("explore", posted=["F1"]),
                ]
            )
        if task_type == "close" and mode == "judge":
            return ScriptedChatClient(
                [close_step([unmet("A1 still lacks a proof")]), receipt_step("close")]
            )
        if task_type == "derive":
            derive_runs += 1
            if derive_runs == 1:
                return ScriptedChatClient(
                    [receipt_step("derive", excluded=["First excluded direction"])]
                )
            response = receipt_step("derive", excluded=["Second excluded direction"])
            second_client = ScriptedChatClient(
                [ScriptStep(text=response.text, expect_contains="First excluded direction")]
            )
            return second_client
        if task_type == "close" and mode == "final":
            return ScriptedChatClient(
                [
                    close_step(
                        [unmet("A1 remains unverified")],
                        report="# Final report\n\nA1 unmet: no reproducible proof.",
                    ),
                    receipt_step("close"),
                ]
            )
        raise AssertionError(f"Unexpected agent {task_type}/{intent_id}/{mode}")

    async with scenario(runtime_infrastructure, tmp_path, script) as s:
        await s.start()
        finished = await s.wait(
            lambda st: (
                st["task"]["status"] == "finished" and st["task"].get("workspace_uri") is not None
            ),
            seconds=30,
        )
        assert derive_runs == 2
        assert second_client is not None and second_client.received_messages
        assert finished["task"]["derive_empty_streak"] == 2
        assert finished["task"]["acceptance_state"]["A1"]["status"] == "unmet"
        report = await s.objects.get(f"reports/{s.task_id}.md")
        assert b"A1 unmet" in report
        assert s.manager.destroyed == [s.task_id]


async def test_scenario_6_two_empty_seeds_fail_without_close(
    runtime_infrastructure, tmp_path: Path
) -> None:
    def script(task_type, intent_id, mode, aid, state):
        assert task_type == "explore" and state["agents"][aid]["is_seed"]
        return ScriptedChatClient([receipt_step("explore", note="No direction found")])

    async with scenario(runtime_infrastructure, tmp_path, script) as s:
        await s.start()
        failed = await s.wait(
            lambda st: (
                st["task"]["status"] == "failed" and st["task"].get("workspace_uri") is not None
            ),
            seconds=30,
        )
        seeds = [agent for agent in failed["agents"].values() if agent["is_seed"]]
        assert len(seeds) == 2
        assert all(agent["status"] == "finished" for agent in seeds)
        assert failed["task"]["seed_empty_count"] == 2
        assert failed["facts"] == failed["intents"] == {}
        assert s.manager.destroyed == [s.task_id]
        types = [event["type"] for event in await s.events()]
        assert "acceptance.judged" not in types
        assert "task.archived" in types
