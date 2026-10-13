"""Retired checkpoint barriers close through real PostgreSQL and service HTTP."""

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from bbx_blackboard.store import schema as s
from bbx_runtime.ctf.coordinator import CLOSE_RETRY_LIMIT, CtfCoordinator
from sqlalchemy import update
from test_ctf_cooperation_integration import cooperation_board as cooperation_board
from test_ctf_cooperation_integration import create_task
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

pytestmark = pytest.mark.integration


async def test_legacy_finished_checkpoint_barrier_closes_as_failure_over_real_http(
    cooperation_board, monkeypatch
):
    board, ctf, _, configured, probe, engine = cooperation_board
    tid = await create_task(board, ctf, configured, "10")
    turn = await ctf.runtime(tid, "claim_turn", agent_id="lead", runtime_instance="close-http")
    await ctf.runtime(
        tid,
        "request_finish",
        actor="lead",
        request_id=str(uuid4()),
        generation=turn["generation"],
        turn_id=turn["id"],
        conclusion={"end_reason": "goal_claimed", "summary": "Fake work", "lead_claim": True},
    )
    await ctf.runtime(
        tid,
        "bill_usage",
        agent_id="lead",
        turn_id=turn["id"],
        generation=turn["generation"],
        request_id=str(uuid4()),
        usage={"cost": "0.1"},
    )
    await ctf.runtime(
        tid,
        "finish_turn",
        agent_id="lead",
        turn_id=turn["id"],
        generation=turn["generation"],
        end_reason="completed",
        answer="Fake settled answer",
    )
    before = await ctf.runtime_state(tid)
    assert before["turns"] == []
    # Reproduce the old deployed contradiction without reopening its retired writer.
    async with engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == UUID(tid))
            .values(
                ctf_control={
                    **before["task"]["ctf_control"],
                    "unresolved_checkpoints": [turn["id"]],
                }
            )
        )

    class Envd:
        drains = 0

        async def drain(self, _):
            self.drains += 1
            return True

    clock = [0.0]
    monkeypatch.setattr("bbx_runtime.ctf.coordinator.monotonic", lambda: clock[0])
    envd = Envd()
    coordinator = CtfCoordinator(board, cast(Any, SimpleNamespace()), envd=envd)
    probe.operations.clear()
    assert await coordinator.tick(tid) is True
    assert probe.operations[("POST", "finalize_close")] == 0
    for _ in range(CLOSE_RETRY_LIMIT):
        clock[0] = coordinator.close_retries[tid].next_retry
        if not await coordinator.tick(tid):
            break
    else:
        pytest.fail("Retired writer barrier did not converge within finite closing ticks")

    after = await ctf.state(tid)
    assert after["task"]["ctf_control"]["phase"] == "closed"
    assert after["task"]["status"] == "failed"
    assert after["task"]["ctf_conclusion"]["end_reason"] == "system_failure"
    assert after["task"]["ctf_control"]["unresolved_checkpoints"] == []
    assert turn["id"] in after["task"]["ctf_control"]["checkpoint_failures"]
    assert after["task"]["usage"]["cost"] == "0.1"
    assert after["turns"][0]["final_answer"] == "Fake settled answer"
    assert probe.operations[("POST", "recover")] == 1
    assert probe.operations[("POST", "finalize_close")] == 1
    assert probe.operations[("POST", "checkpoint")] == 0
    assert probe.operations[("POST", "claim_turn")] == 0
    assert envd.drains == 1
