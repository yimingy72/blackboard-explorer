"""Closed CTF review reuses history without execution tools or mailbox leakage."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from bbx_contracts.ctf import load_ctf_profile
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_runtime import TURN, MemoryCtf


async def test_review_removed_member_preserves_session_and_only_claims_review_mailbox():
    profile = load_ctf_profile(Path("profiles/ctf"))

    class ReviewService(MemoryCtf):
        def __init__(self):
            super().__init__()
            self.client = self
            self.finished = []
            self.reads = []
            self.billed = []

        async def state(self, _):
            return {
                "task": {
                    "status": "finished",
                    "agent_profile": "ctf",
                    "agent_profile_version": 1,
                    "budget": {"max_minutes": 1},
                }
            }

        async def get_profile(self, *_):
            return {"profile": profile.model_dump(mode="json")}

        async def runtime(self, task_id, operation, **body):
            if operation == "authorize_review":
                self.operations.append(operation)
                return {}
            if operation == "authorize_member":
                raise AssertionError("Review cannot authorize execution")
            if operation == "claim_messages":
                return [
                    row
                    for row in self.rows
                    if row.get("purpose") == "review" and row["id"] not in self.delivered
                ]
            if operation == "finish_turn":
                self.finished.append(body)
                return {}
            if operation == "bill_usage":
                self.billed.append(body)
                return {}
            return await super().runtime(task_id, operation, **body)

        async def tool(self, tid, token, operation, **body):
            assert operation in {
                "list_challenges",
                "get_challenge",
                "list_records",
                "read_artifact",
            }
            self.reads.append(operation)
            return "persisted artifact content"

    service = ReviewService()
    cp = await load_checkpoint(
        cast(CtfClient, service), "task", "member-1", TURN, "Original teammate instructions"
    )
    from agent_framework import Content, Message

    cp.session.state["message_injection.pending_messages"] = [
        Message(
            role="user",
            contents=[Content.from_text("EXECUTION MUST NOT LEAK")],
            message_id="execution",
        )
    ]
    cp.session.state["ctf_usage_outbox"] = [{"request_id": "old-execution", "usage": {}}]
    await cp.save()
    service.post("execution", "EXECUTION MUST NOT LEAK")
    service.post("review", "explain the saved result")
    service.rows[-1]["purpose"] = "review"
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(ScriptToolCall("read_artifact", {"artifact_id": "artifact"}),),
                expect_contains="explain the saved result",
            ),
            ScriptStep(text="review answer", expect_contains="persisted artifact content"),
        ]
    )
    envd = SimpleNamespace(
        prepare=AsyncMock(side_effect=AssertionError("no envd")),
        stop_member=AsyncMock(side_effect=AssertionError("no drain")),
    )
    runner = CtfRunner(
        cast(CtfClient, service),
        cast(Any, SimpleNamespace()),
        client_factory=lambda *_: client,
        envd=envd,
        platform_transport=AsyncMock(side_effect=AssertionError("no platform")),
    )
    await runner.run(
        "task",
        {
            "id": "member-1",
            "display_name": "retired member",
            "role": "teammate",
            "status": "removed",
        },
        {**TURN, "purpose": "review", "token": "review-token"},
    )
    assert service.delivered == {"review"}
    assert service.finished[0]["answer"] == "review answer"
    assert {t.name for t in client.received_options[0]["tools"]} == {
        "list_challenges",
        "get_challenge",
        "list_records",
        "read_artifact",
    }
    assert service.saved is not None
    assert service.saved["opening_instructions"] == "Original teammate instructions"
    assert service.saved["session"]["state"]["ctf_usage_outbox"][0]["request_id"] == "old-execution"
    assert all(row["request_id"] != "old-execution" for row in service.billed)
    history = str(service.saved["session"]["state"]["in_memory"]["messages"])
    assert "EXECUTION MUST NOT LEAK" not in history
    envd.prepare.assert_not_awaited()
    envd.stop_member.assert_not_awaited()


async def test_review_coordinator_does_not_provision_and_includes_removed_members():
    runner = SimpleNamespace(run=AsyncMock(), pending_checkpoints={})
    coordinator = CtfCoordinator(
        cast(Any, None),
        Settings.model_construct(),
        runner=runner,
        envd_factory=AsyncMock(side_effect=AssertionError("no execution container")),
    )
    turn = {"id": "review", "generation": 2, "purpose": "review"}
    calls = []

    async def runtime(tid, operation, **body):
        calls.append(operation)
        return turn if operation == "claim_review_turn" else None

    coordinator.service = cast(
        Any,
        SimpleNamespace(
            runtime=runtime,
            state=AsyncMock(
                return_value={
                    "task": {"status": "finished"},
                    "members": [{"id": "member-1", "status": "removed"}],
                }
            ),
        ),
    )
    await coordinator.review_tick("task")
    await coordinator.review_active[("task", "member-1")][0]
    runner.run.assert_awaited_once()
    assert calls == ["recover_reviews", "claim_review_turn"]
    assert not coordinator.envds
    await coordinator.close()


async def test_detach_waits_for_checkpoint_and_clears_previous_run_caches():
    import pytest

    key = ("task", "member-1")
    adapter = SimpleNamespace(close=AsyncMock())
    runner = SimpleNamespace(
        pending_checkpoints={key: object()},
        pending_settlements={key: object()},
        retry_checkpoints=AsyncMock(),
        envds={"task": adapter},
    )
    coordinator = CtfCoordinator(cast(Any, None), Settings.model_construct(), runner=runner)
    coordinator.envds["task"] = adapter
    coordinator.recovery_pending.add("task")
    with pytest.raises(RuntimeError, match="Unconfirmed"):
        await coordinator.detach("task")
    adapter.close.assert_not_awaited()
    runner.pending_checkpoints.clear()
    await coordinator.detach("task")
    assert not coordinator.envds and not runner.envds and not runner.pending_settlements
    assert "task" not in coordinator.recovery_pending
    adapter.close.assert_awaited_once()
