"""M3b scenarios 10-13 through the real board and scripted MAF tool loops."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from bbx_runtime.testing.scripted_client import ScriptedChatClient
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
TWO_ACCEPTANCE = [
    {"id": "A1", "desc": "Show the first supported cause"},
    {"id": "A2", "desc": "Show the second supported cause"},
]


def fast_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bbx_runtime.scheduler.loop.FALLBACK_SECONDS", 0.25)
    monkeypatch.setattr("bbx_runtime.scheduler.loop.DEBOUNCE_SECONDS", 0.05)


def verdict(item_id: str, met: bool, evidence: list[str] | None = None) -> dict[str, Any]:
    if met:
        return {
            "id": item_id,
            "verdict": "met",
            "reason": "Scripted evidence supports this acceptance item",
            "evidence_facts": evidence or [],
        }
    return {
        "id": item_id,
        "verdict": "unmet",
        "reason": "The available evidence is insufficient",
        "missing": "Add an undisputed reproducible observation",
        "evidence_facts": [],
    }


@pytest.mark.asyncio
async def test_s10_disputed_support_reverts_met_and_is_judged_again(
    runtime_infrastructure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast_ticks(monkeypatch)
    worker_gate = Gate()
    judge_count = 0

    def factory(task_type, intent_id, mode, aid, state):
        nonlocal judge_count
        if task_type == "explore" and intent_id is None:
            return ScriptedChatClient(
                [
                    fact_step(aid, "F1 supports A1", satisfies=["A1"]),
                    intent_step("Check conflicting evidence", relates_to=["A2"]),
                    receipt_step("explore", posted=["F1", "I1"]),
                ]
            )
        if task_type == "explore":
            assert intent_id == "I1"
            return GateClient(
                [
                    fact_step(
                        aid,
                        "F2 disputes the original support",
                        disputes=["F1"],
                        resolves="I1",
                        result="rejected",
                    ),
                    receipt_step("explore", posted=["F2"], intent_result="rejected"),
                ],
                aid,
                worker_gate,
            )
        if task_type == "close" and mode == "judge":
            judge_count += 1
            return ScriptedChatClient(
                [
                    close_step([verdict("A1", judge_count == 1, ["F1"]), verdict("A2", False)]),
                    receipt_step("close"),
                ]
            )
        if task_type == "derive":
            return ScriptedChatClient([receipt_step("derive")])
        if task_type == "close" and mode == "final":
            return ScriptedChatClient(
                [
                    close_step(
                        [verdict("A1", False), verdict("A2", False)],
                        report="# Final report\nBoth acceptance items remain unmet.",
                    ),
                    receipt_step("close"),
                ]
            )
        raise AssertionError(f"Unexpected run: {task_type}/{mode}")

    async with scenario(
        runtime_infrastructure, tmp_path, factory, {"acceptance": TWO_ACCEPTANCE}
    ) as run:
        await run.start()
        await asyncio.wait_for(worker_gate.ready.wait(), 20)
        await run.wait(lambda state: state["task"]["acceptance_state"]["A1"]["status"] == "met")
        before = await run.events()
        assert len([event for event in before if event["type"] == "acceptance.judged"]) == 1
        worker_gate.release.set()
        await run.wait(lambda state: state["facts"].get("F1", {}).get("status") == "disputed")
        await run.wait(lambda state: state["task"]["acceptance_state"]["A1"]["status"] == "unmet")
        async with asyncio.timeout(20):
            while True:
                events = await run.events()
                judgments = [
                    event
                    for event in events
                    if event["type"] == "acceptance.judged" and event["payload"]["mode"] == "judge"
                ]
                if len(judgments) >= 2:
                    break
                await asyncio.sleep(0.05)
        await run.wait(
            lambda state: any(
                agent["task_type"] == "derive" and not agent["derive_parallel"]
                for agent in state["agents"].values()
            )
        )
        events = await run.events()
        assert len(judgments) == 2
        assert judgments[0]["payload"]["verdicts"][0]["verdict"] == "met"
        assert judgments[1]["payload"]["verdicts"][0]["verdict"] == "unmet"
        reverted = [event for event in events if event["type"] == "acceptance.reverted"]
        assert len(reverted) == 1 and reverted[0]["payload"]["fact_id"] == "F1"
        assert judgments[0]["version"] < reverted[0]["version"] < judgments[1]["version"]
        settled = await run.state()
        assert settled["task"]["acceptance_state"]["A1"]["status"] == "unmet"
        quiescent_derives = [
            event
            for event in events
            if event["type"] == "agent.spawned"
            and event["payload"]["task_type"] == "derive"
            and not event["payload"].get("derive_parallel")
        ]
        assert quiescent_derives
        assert all(event["version"] > judgments[1]["version"] for event in quiescent_derives)


@pytest.mark.asyncio
async def test_s11_new_satisfies_during_judge_waits_for_next_single_judge(
    runtime_infrastructure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast_ticks(monkeypatch)
    worker_gate = Gate()
    first_judge_gate = Gate()
    judge_count = 0

    def factory(task_type, intent_id, mode, aid, state):
        nonlocal judge_count
        if task_type == "explore" and intent_id is None:
            return ScriptedChatClient(
                [
                    fact_step(aid, "F1 supports A1", satisfies=["A1"]),
                    intent_step("Find A2 evidence", relates_to=["A2"]),
                    receipt_step("explore", posted=["F1", "I1"]),
                ]
            )
        if task_type == "explore":
            assert intent_id == "I1"
            return GateClient(
                [
                    fact_step(
                        aid,
                        "F2 supports A2",
                        resolves="I1",
                        result="confirmed",
                        satisfies=["A2"],
                    ),
                    receipt_step("explore", posted=["F2"], intent_result="confirmed"),
                ],
                aid,
                worker_gate,
            )
        if task_type == "close" and mode == "judge":
            judge_count += 1
            steps = [
                close_step(
                    [verdict("A1", True, ["F1"]), verdict("A2", judge_count == 2, ["F2"])],
                ),
                receipt_step("close"),
            ]
            return (
                GateClient(steps, aid, first_judge_gate)
                if judge_count == 1
                else ScriptedChatClient(steps)
            )
        if task_type == "close" and mode == "final":
            return ScriptedChatClient(
                [
                    close_step(
                        [verdict("A1", True, ["F1"]), verdict("A2", True, ["F2"])],
                        report="# Final report\nBoth acceptance items have evidence.",
                    ),
                    receipt_step("close"),
                ]
            )
        if task_type == "derive":
            return ScriptedChatClient([receipt_step("derive")])
        raise AssertionError(f"Unexpected run: {task_type}/{mode}")

    async with scenario(
        runtime_infrastructure, tmp_path, factory, {"acceptance": TWO_ACCEPTANCE}
    ) as run:
        await run.start()
        await asyncio.wait_for(first_judge_gate.ready.wait(), 20)
        await asyncio.wait_for(worker_gate.ready.wait(), 20)
        held = await run.state()
        assert (
            sum(
                agent["task_type"] == "close" and agent["close_mode"] == "judge"
                for agent in held["agents"].values()
            )
            == 1
        )
        worker_gate.release.set()
        await run.wait(
            lambda state: "F2" in state["facts"] and state["intents"]["I1"]["status"] == "closed"
        )
        while_judging = await run.state()
        assert (
            sum(
                agent["task_type"] == "close" and agent["close_mode"] == "judge"
                for agent in while_judging["agents"].values()
            )
            == 1
        )
        first_judge_gate.release.set()
        await run.wait(lambda state: state["task"]["status"] == "finished")
        events = await run.events()
        judge_events = [
            event
            for event in events
            if event["type"] == "acceptance.judged" and event["payload"]["mode"] == "judge"
        ]
        assert len(judge_events) == 2 and judge_count == 2
        assert judge_events[0]["payload"]["verdicts"][1]["verdict"] == "unmet"
        assert judge_events[1]["payload"]["verdicts"][1]["verdict"] == "met"
        assert judge_events[0]["version"] < judge_events[1]["version"]
        assert (await run.state())["task"]["acceptance_state"]["A2"]["status"] == "met"


@pytest.mark.asyncio
async def test_s12_restart_reassigns_intent_without_attempt_increment(
    runtime_infrastructure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast_ticks(monkeypatch)
    held_worker = Gate()

    def first_factory(task_type, intent_id, mode, aid, state):
        if task_type == "explore" and intent_id is None:
            return ScriptedChatClient(
                [
                    fact_step(aid, "F1 starts investigation"),
                    intent_step("Investigate after restart"),
                    receipt_step("explore", posted=["F1", "I1"]),
                ]
            )
        if task_type == "explore":
            return GateClient([receipt_step("explore")], aid, held_worker)
        return GateClient([receipt_step(task_type)], aid, Gate())

    def recovered_factory(task_type, intent_id, mode, aid, state):
        if task_type == "explore":
            assert intent_id == "I1"
            return ScriptedChatClient(
                [
                    fact_step(aid, "F2 resolves after restart", resolves="I1", result="confirmed"),
                    receipt_step("explore", posted=["F2"], intent_result="confirmed"),
                ]
            )
        return GateClient([receipt_step(task_type)], aid, Gate())

    async with scenario(runtime_infrastructure, tmp_path, first_factory) as run:
        await run.start()
        await asyncio.wait_for(held_worker.ready.wait(), 20)
        before = await run.state()
        old_holder = before["intents"]["I1"]["holder"]
        assert old_holder in held_worker.seen and before["intents"]["I1"]["attempts"] == 0
        await run.stop()
        interrupted = await run.state()
        assert interrupted["agents"][old_holder]["end_reason"] == "runtime_restart"
        assert interrupted["intents"]["I1"]["status"] == "open"
        assert interrupted["intents"]["I1"]["attempts"] == 0
        run.new_supervisor(recovered_factory)
        await run.start()
        restored = await run.wait(lambda state: state["intents"]["I1"]["status"] == "closed")
        assert restored["intents"]["I1"]["attempts"] == 0
        new_holder_events = [
            event
            for event in await run.events()
            if event["type"] == "intent.claimed" and event["payload"]["intent_id"] == "I1"
        ]
        assert len(new_holder_events) == 2
        assert new_holder_events[0]["payload"]["holder"] == old_holder
        assert new_holder_events[1]["payload"]["holder"] != old_holder
        assert run.manager.destroyed == []


@pytest.mark.asyncio
async def test_s13_manual_stop_finishes_with_report_and_archive(
    runtime_infrastructure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast_ticks(monkeypatch)
    seed_gate = Gate()
    report = "# Final report\nThe task stopped before acceptance was met."

    def factory(task_type, intent_id, mode, aid, state):
        if task_type == "explore":
            return GateClient(
                [
                    fact_step(aid, "Evidence saved during handoff"),
                    receipt_step("explore", posted=["F1"]),
                ],
                aid,
                seed_gate,
            )
        assert task_type == "close" and mode == "final"
        return ScriptedChatClient(
            [close_step([verdict("A1", False)], report=report), receipt_step("close")]
        )

    async with scenario(runtime_infrastructure, tmp_path, factory) as run:
        await run.start()
        await asyncio.wait_for(seed_gate.ready.wait(), 20)
        stopped = await run.board.stop_task(run.task_id)
        assert stopped["status"] == "closing"
        await run.wait(
            lambda state: state["agents"][next(iter(seed_gate.seen))]["status"] == "concluding"
        )
        seed_gate.release.set()
        finished = await run.wait(lambda state: state["task"]["status"] == "finished")
        assert finished["task"]["acceptance_state"]["A1"]["status"] == "unmet"
        assert finished["task"]["report_uri"] == f"reports/{run.task_id}.md"
        assert (await run.objects.get(f"reports/{run.task_id}.md")).decode() == report
        await run.wait(lambda state: state["task"].get("workspace_uri") is not None)
        async with asyncio.timeout(20):
            while run.task_id not in run.manager.destroyed:
                await asyncio.sleep(0.05)
        assert len(run.manager.archives) == 1
        archive = run.manager.archives[0]
        assert archive.uri == f"workspace/{run.task_id}.tar.zst"
        assert archive.size > 0 and await run.objects.exists(archive.uri)
        events = await run.events()
        types = [event["type"] for event in events]
        for expected in (
            "task.closing",
            "agent.conclude_requested",
            "task.report",
            "task.finished",
            "task.archived",
        ):
            assert expected in types
        assert types.index("task.closing") < types.index("agent.conclude_requested")
        assert types.index("task.finished") < types.index("task.archived")
