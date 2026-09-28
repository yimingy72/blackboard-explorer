"""The single-worker baseline still completes existing work before final closure."""

from __future__ import annotations

import asyncio
from pathlib import Path

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


@pytest.mark.asyncio
async def test_single_profile_explores_open_intent_then_reviews_before_closing(
    runtime_infrastructure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bbx_runtime.scheduler.loop.FALLBACK_SECONDS", 0.25)
    monkeypatch.setattr("bbx_runtime.scheduler.loop.DEBOUNCE_SECONDS", 0.05)
    worker_gate = Gate()

    def factory(task_type, intent_id, mode, aid, state):
        if task_type == "explore" and state["agents"][aid]["is_seed"]:
            return ScriptedChatClient(
                [
                    fact_step(aid, "Seed observation"),
                    intent_step("Investigate the seed observation"),
                    receipt_step("explore", posted=["F1", "I1"]),
                ]
            )
        if task_type == "explore" and intent_id == "I1":
            return GateClient(
                [
                    fact_step(
                        aid, "Investigation remains uncertain", resolves="I1", result="inconclusive"
                    ),
                    receipt_step("explore", posted=["F2"], intent_result="inconclusive"),
                ],
                aid,
                worker_gate,
            )
        if task_type == "close" and mode == "judge":
            return ScriptedChatClient(
                [
                    close_step(
                        [
                            {
                                "id": "A1",
                                "verdict": "unmet",
                                "reason": "More evidence needed",
                                "missing": "Independent confirmation",
                            }
                        ]
                    ),
                    receipt_step("close"),
                ]
            )
        if task_type == "derive":
            assert state["agents"][aid]["derive_review"] is True
            return ScriptedChatClient(
                [receipt_step("derive", excluded=["Independent confirmation is unavailable"])]
            )
        if task_type == "close" and mode == "final":
            return ScriptedChatClient(
                [
                    close_step(
                        [
                            {
                                "id": "A1",
                                "verdict": "unmet",
                                "reason": "Still uncertain",
                                "missing": "Independent confirmation",
                            }
                        ],
                        report="# Final report\n\nThe remaining uncertainty needs human review.",
                    ),
                    receipt_step("close"),
                ]
            )
        raise AssertionError(f"Unexpected agent {task_type}/{intent_id}/{mode}")

    async with scenario(
        runtime_infrastructure,
        tmp_path,
        factory,
        {"agent_profile": "single", "budget": {"max_concurrent_agents": 1}},
    ) as run:
        assert (await run.state())["task"]["params"]["derive_enabled"] is False
        await run.start()
        await asyncio.wait_for(worker_gate.ready.wait(), 20)
        working = await run.state()
        assert working["task"]["status"] == "running"
        assert working["intents"]["I1"]["status"] == "claimed"
        assert not any(agent["task_type"] == "close" for agent in working["agents"].values())

        worker_gate.release.set()
        finished = await run.wait(
            lambda state: (
                state["task"]["status"] == "finished"
                and state["task"].get("workspace_uri") is not None
            ),
            seconds=30,
        )
        assert finished["intents"]["I1"]["status"] == "closed"
        assert finished["task"]["acceptance_state"]["A1"]["status"] == "unmet"
        modes = [
            agent["close_mode"]
            for agent in finished["agents"].values()
            if agent["task_type"] == "close"
        ]
        assert modes == ["judge", "judge", "final"]
        reviews = [agent for agent in finished["agents"].values() if agent["task_type"] == "derive"]
        assert len(reviews) == 1 and reviews[0]["derive_review"] is True
        events = await run.events()
        kinds = [event["type"] for event in events]
        assert kinds.index("intent.closed") < kinds.index("acceptance.judged")
        assert kinds.index("acceptance.judged") < kinds.index("task.closing")
