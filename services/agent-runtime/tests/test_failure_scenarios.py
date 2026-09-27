"""Five M3b failure and boundary scenarios over the real blackboard."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from bbx_runtime.testing.scripted_client import (
    ScriptedChatClient,
    ScriptStep,
    ScriptToolCall,
    ScriptUsage,
)
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


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bbx_runtime.scheduler.loop.DEBOUNCE_SECONDS", 0.05)
    monkeypatch.setattr("bbx_runtime.scheduler.loop.FALLBACK_SECONDS", 0.2)
    monkeypatch.setattr("bbx_runtime.scheduler.loop.RECONNECT_SECONDS", 0.02)


def idle_client(aid: str, task_type: str, gate: Gate) -> GateClient:
    return GateClient([receipt_step(task_type)], aid, gate)  # type: ignore[arg-type]


async def test_04_toxic_intent_closes_inconclusive_after_three_unfinished_releases(
    runtime_infrastructure, tmp_path: Path
) -> None:
    idle = Gate(1000)

    def factory(task_type, intent_id, mode, aid, state) -> ScriptedChatClient:
        if task_type != "explore":
            return idle_client(aid, task_type, idle)
        if state["agents"][aid]["is_seed"]:
            return ScriptedChatClient(
                [
                    fact_step(aid, "Seed observation for a toxic intent"),
                    intent_step("Try a direction that cannot be completed", claim=True),
                    ScriptStep(
                        calls=(
                            ScriptToolCall("release", {"intent_id": "I1", "note": "No result yet"}),
                        )
                    ),
                    receipt_step("explore", posted=["F1", "I1"]),
                ]
            )
        assert intent_id == "I1"
        return ScriptedChatClient(
            [
                ScriptStep(
                    calls=(
                        ScriptToolCall(
                            "release", {"intent_id": intent_id, "note": "Still inconclusive"}
                        ),
                    )
                ),
                receipt_step("explore"),
            ]
        )

    async with scenario(runtime_infrastructure, tmp_path, factory) as s:
        await s.start()
        settled = await s.wait(
            lambda st: st["intents"].get("I1", {}).get("status") == "closed", seconds=20
        )
        intent = settled["intents"]["I1"]
        assert intent["result"] == "inconclusive"
        assert intent["attempts"] == 3 and intent["holder"] is None
        assert len(intent["notes"]) == 3
        events = await s.events()
        assert sum(event["type"] == "intent.released" for event in events) == 3
        assert any(
            event["type"] == "intent.closed"
            and event["payload"]["result"] == "inconclusive"
            and event["payload"]["by"] == "system"
            for event in events
        )


async def test_05_three_model_failures_fail_and_archive_without_close(
    runtime_infrastructure, tmp_path: Path
) -> None:
    idle = Gate(1000)

    def factory(task_type, intent_id, mode, aid, state) -> ScriptedChatClient:
        if task_type == "derive":
            return ScriptedChatClient([receipt_step("derive")])
        if task_type != "explore":
            return idle_client(aid, task_type, idle)
        if state["agents"][aid]["is_seed"]:
            return ScriptedChatClient(
                [
                    fact_step(aid, "A real seed observation before the model outage"),
                    intent_step("Open direction for retrying explorers"),
                    receipt_step("explore", posted=["F1", "I1"]),
                ]
            )
        assert intent_id == "I1"
        return ScriptedChatClient([])  # Actual MAF client raises; runner records runtime_error.

    async with scenario(
        runtime_infrastructure,
        tmp_path,
        factory,
        spec_overrides={"params": {"intent_max_attempts": 5}},
    ) as s:
        await s.start()
        failed = await s.wait(
            lambda st: (
                st["task"]["status"] == "failed"
                and bool(st["task"].get("workspace_uri"))
                and s.task_id in s.manager.destroyed
            ),
            seconds=30,
        )
        errors = [
            agent
            for agent in failed["agents"].values()
            if agent.get("end_reason") == "runtime_error"
        ]
        assert len(errors) >= 3
        assert all(agent["task_type"] == "explore" for agent in errors)
        assert failed["task"]["failure_streak"] == 3
        assert not [agent for agent in failed["agents"].values() if agent["task_type"] == "close"]
        assert s.manager.archives and s.manager.destroyed == [s.task_id]
        assert any(event["type"] == "task.archived" for event in await s.events())


async def test_07_atomic_claim_race_finishes_losing_scheduler_agent(
    runtime_infrastructure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idle = Gate(1000)

    def factory(task_type, intent_id, mode, aid, state) -> ScriptedChatClient:
        if task_type != "explore":
            return idle_client(aid, task_type, idle)
        if state["agents"][aid]["is_seed"]:
            return ScriptedChatClient(
                [
                    fact_step(aid, "Seed observation for a claim race"),
                    intent_step("One open intent for two claimants"),
                    receipt_step("explore", posted=["F1", "I1"]),
                ]
            )
        return idle_client(aid, task_type, idle)

    async with scenario(runtime_infrastructure, tmp_path, factory) as s:
        entered, release = asyncio.Event(), asyncio.Event()
        original_claim_for = s.board.claim_for
        losing_aid: str | None = None

        async def delayed_claim_for(task_id: str, intent_id: str, aid: str) -> list[dict[str, Any]]:
            nonlocal losing_aid
            losing_aid = aid
            entered.set()
            await release.wait()
            return await original_claim_for(task_id, intent_id, aid)

        monkeypatch.setattr(s.board, "claim_for", delayed_claim_for)
        await s.start()
        await asyncio.wait_for(entered.wait(), 20)
        competitor = await s.board.register_agent(s.task_id, "explore")
        await s.board.with_token(competitor["token"]).claim(s.task_id, "I1")
        release.set()
        assert losing_aid is not None
        settled = await s.wait(
            lambda st: st["agents"].get(losing_aid, {}).get("status") == "finished"
        )
        assert settled["intents"]["I1"]["holder"] == competitor["agent_id"]
        assert settled["agents"][losing_aid]["end_reason"] == "normal"
        claims = [event for event in await s.events() if event["type"] == "intent.claimed"]
        assert len(claims) == 1 and claims[0]["payload"]["holder"] == competitor["agent_id"]
        await s.stop()
        await s.board.finish_agent(
            s.task_id,
            competitor["agent_id"],
            {"accepted": False, "reason": "race test complete"},
            "runtime_restart",
        )


async def test_08_grace_timeout_releases_intent_with_note_and_attempt(
    runtime_infrastructure, tmp_path: Path
) -> None:
    held = Gate()
    idle = Gate(1000)

    def factory(task_type, intent_id, mode, aid, state) -> ScriptedChatClient:
        if task_type != "explore":
            return idle_client(aid, task_type, idle)
        if state["agents"][aid]["is_seed"]:
            return ScriptedChatClient(
                [
                    fact_step(aid, "Seed observation for a grace timeout"),
                    intent_step("Investigate slowly"),
                    receipt_step("explore", posted=["F1", "I1"]),
                ]
            )
        assert intent_id == "I1"
        return GateClient([receipt_step("explore")], aid, held)

    async with scenario(runtime_infrastructure, tmp_path, factory) as s:
        await s.start()
        await asyncio.wait_for(held.ready.wait(), 20)
        aid = next(iter(held.seen))
        claimed = await s.state()
        assert claimed["intents"]["I1"]["holder"] == aid
        await s.board.conclude(s.task_id, aid, "limit")
        concluding = await s.state()
        assert concluding["agents"][aid]["status"] == "concluding"
        requested = datetime.fromisoformat(
            concluding["agents"][aid]["conclude_requested_at"].replace("Z", "+00:00")
        )
        loop = s.supervisor.loops[s.task_id]
        loop.executor.request_stop(
            "runtime_restart"
        )  # Prevent reassignment without cancelling this run.
        loop._sweeper.now = lambda: requested + timedelta(minutes=loop.params.grace_timeout + 1)
        await loop._sweeper.check_once()
        settled = await s.state()
        assert settled["agents"][aid]["end_reason"] == "grace_timeout"
        intent = settled["intents"]["I1"]
        assert intent["status"] == "open" and intent["attempts"] == 1
        assert intent["notes"][-1]["by"] == aid
        assert "由系统强制释放" in intent["notes"][-1]["text"]


async def test_09_exploration_budget_exhausts_but_final_close_still_runs(
    runtime_infrastructure, tmp_path: Path
) -> None:
    idle = Gate(1000)
    costly = ScriptUsage(0, 10_000, 100, 0)

    def factory(task_type, intent_id, mode, aid, state) -> ScriptedChatClient:
        if task_type == "explore":
            assert state["agents"][aid]["is_seed"]
            return ScriptedChatClient([receipt_step("explore", usage=costly)])
        if task_type == "close" and mode == "final":
            return ScriptedChatClient(
                [
                    close_step(
                        [
                            {
                                "id": "A1",
                                "verdict": "unmet",
                                "reason": "Budget exhausted",
                                "missing": "No facts yet",
                            }
                        ],
                        report="# Budget-limited report\n\nThe acceptance item remains unmet.\n",
                    ),
                    receipt_step("close", note="Final report submitted"),
                ]
            )
        return idle_client(aid, task_type, idle)

    async with scenario(
        runtime_infrastructure,
        tmp_path,
        factory,
        spec_overrides={"budget": {"max_cost": "0.001"}},
    ) as s:
        await s.start()
        finished = await s.wait(
            lambda st: (
                st["task"]["status"] == "finished"
                and bool(st["task"].get("workspace_uri"))
                and s.task_id in s.manager.destroyed
            ),
            seconds=30,
        )
        assert float(finished["task"]["usage"]["cost"]) >= 0.001
        assert finished["task"]["report_uri"] == f"reports/{s.task_id}.md"
        assert finished["task"]["workspace_uri"] == f"workspace/{s.task_id}.tar.zst"
        assert any(
            agent["task_type"] == "close"
            and agent.get("close_mode") == "final"
            and agent["status"] == "finished"
            for agent in finished["agents"].values()
        )
        assert s.manager.archives and s.manager.destroyed == [s.task_id]
