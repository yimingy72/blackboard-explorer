"""Ordinary final receipts survive control drops without restarting a model run."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from bbx_contracts.models import Params
from bbx_contracts.profile import load_profile
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.runner import AgentRunner
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient
from test_runner import fake_stream


class Board:
    def __init__(self, mode):
        profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
        self.mode = mode
        self.saved = None
        self.finishes = []
        self.snapshot = {
            "task": {
                "id": "task",
                "run_number": 1,
                "agent_profile": "default",
                "agent_profile_version": 1,
                "params": Params().model_dump(),
                "budget": {"max_minutes": 1},
            },
            "agents": {
                "agent-1": {
                    "id": "agent-1",
                    "task_type": "derive",
                    "status": "running",
                    "derive_round": 1,
                }
            },
        }
        self.get_profile = AsyncMock(return_value=profile.model_dump())
        self.with_token = Mock()

    async def state(self, task_id):
        return deepcopy(self.snapshot)

    async def get_agent_session(self, *args):
        if self.saved is None:
            raise RemoteError(404, "synthetic no session")
        return deepcopy(self.saved)

    async def put_agent_session(self, task_id, agent_id, **payload):
        revision = self.saved["revision"] if self.saved else 0
        assert payload["expected_revision"] == revision
        self.saved = {
            "session": deepcopy(payload["session"]),
            "opening_instructions": payload["opening_instructions"],
            "origin": "native",
            "revision": revision + 1,
        }
        return deepcopy(self.saved)

    async def finish_agent(self, task_id, agent_id, receipt, reason, **fences):
        self.finishes.append((deepcopy(receipt), reason, fences))
        assert fences == {
            "expected_derive_round": 1,
            "expected_run_number": 1,
            "expected_agent_run_id": "agent-1",
        }
        if self.mode in {"ok", "committed"}:
            self.snapshot["agents"][agent_id].update(
                status="finished", receipt=deepcopy(receipt), end_reason=reason
            )
        if self.mode == "committed" and len(self.finishes) == 1 or self.mode == "persistent":
            raise httpx.RemoteProtocolError("synthetic finish response loss")
        if self.mode == "uncommitted" and len(self.finishes) == 1:
            raise httpx.RemoteProtocolError("synthetic finish not committed")
        self.snapshot["agents"][agent_id].update(
            status="finished", receipt=deepcopy(receipt), end_reason=reason
        )
        return []


@pytest.fixture
async def scripted(monkeypatch, request):
    calls = []
    accepted = getattr(request, "param", True)
    payload = (
        {"accepted": True, "data": {"posted": [], "excluded": []}}
        if accepted
        else {"accepted": False, "reason": "Synthetic unavailable input"}
    )

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def run(self, *args, stream=False, **kwargs):
            async def respond():
                calls.append("model")
                return SimpleNamespace(text=json.dumps(payload))

            return fake_stream(respond(), stream=stream)

    async def no_wait(_):
        return None

    monkeypatch.setattr("bbx_runtime.runner.Agent", Agent)
    monkeypatch.setattr("bbx_runtime.runner.asyncio.sleep", no_wait)
    return calls


@pytest.mark.parametrize("scripted", [True, False], indirect=True)
@pytest.mark.parametrize(
    "mode,expected_finishes", [("committed", 1), ("uncommitted", 2), ("persistent", 3)]
)
async def test_final_receipt_is_saved_and_recovered_without_model_replay(
    scripted, mode, expected_finishes
):
    board = Board(mode)
    runner = AgentRunner(Settings.model_construct(), cast(Any, board), Mock(), Mock())
    result = await runner.run_agent(
        "task", "agent-1", "derive", agent_token="synthetic", client=ScriptedChatClient([])
    )
    expected_reason = "normal" if result.receipt["accepted"] else "refused"
    assert result.end_reason == expected_reason
    assert scripted == ["model"]
    assert len(board.finishes) == expected_finishes
    assert board.saved is not None
    proof = board.saved["session"]["state"]["bbx_final_receipt"]
    assert proof["receipt"] == result.receipt
    assert proof["end_reason"] == expected_reason
    if mode == "persistent":
        assert board.snapshot["agents"]["agent-1"]["status"] == "running"
        board.mode = "ok"
        assert await runner.reconcile_final_receipt(
            "task", "agent-1", board.snapshot["agents"]["agent-1"], board.snapshot["task"]
        )
        assert board.snapshot["agents"]["agent-1"]["receipt"] == result.receipt
    assert scripted == ["model"]


@pytest.mark.parametrize("change", ["run_number", "derive_round", "run_id"])
async def test_old_final_receipt_cannot_compensate_another_run(scripted, change):
    board = Board("persistent")
    runner = AgentRunner(Settings.model_construct(), cast(Any, board), Mock(), Mock())
    await runner.run_agent(
        "task", "agent-1", "derive", agent_token="synthetic", client=ScriptedChatClient([])
    )
    if change == "run_number":
        board.snapshot["task"]["run_number"] = 2
    elif change == "derive_round":
        board.snapshot["agents"]["agent-1"]["derive_round"] = 2
    else:
        board.snapshot["agents"]["agent-1"]["id"] = "another-run"
    before = len(board.finishes)
    assert not await runner.reconcile_final_receipt(
        "task", "agent-1", board.snapshot["agents"]["agent-1"], board.snapshot["task"]
    )
    assert len(board.finishes) == before
    assert scripted == ["model"]


@pytest.mark.parametrize("cancelled", [False, True])
async def test_resource_cleanup_error_preserves_original_result_or_cancellation(
    monkeypatch, caplog, cancelled
):
    import asyncio

    from pydantic import SecretStr

    original = asyncio.CancelledError("heartbeat")
    calls = []

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def run(self, *args, stream=False, **kwargs):
            async def respond():
                calls.append("model")
                if cancelled:
                    raise original
                return SimpleNamespace(text='{"accepted":true,"data":{"posted":[],"excluded":[]}}')

            return fake_stream(respond(), stream=stream)

    board = Board("ok")
    monkeypatch.setattr("bbx_runtime.runner.Agent", Agent)
    monkeypatch.setattr("bbx_runtime.runner.resolve_model_credentials", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "bbx_runtime.runner.make_client", lambda *args, **kwargs: ScriptedChatClient([])
    )
    close = AsyncMock(side_effect=RuntimeError("private synthetic cleanup failure"))
    monkeypatch.setattr("bbx_runtime.runner.close_model_client", close)
    runner = AgentRunner(
        Settings.model_construct(deepseek_api_key=SecretStr("unused-scripted")),
        cast(Any, board),
        Mock(),
        Mock(),
    )
    if cancelled:
        with pytest.raises(asyncio.CancelledError) as raised:
            await runner.run_agent("task", "agent-1", "derive", agent_token="synthetic")
        assert raised.value is original
        assert board.finishes[0][1] == "heartbeat"
    else:
        result = await runner.run_agent("task", "agent-1", "derive", agent_token="synthetic")
        assert result.receipt == board.finishes[0][0]
        assert result.end_reason == "normal"
    assert calls == ["model"] and len(board.finishes) == 1
    assert board.saved is not None
    assert board.saved["session"]["state"]["bbx_final_receipt"]["receipt"] == board.finishes[0][0]
    close.assert_awaited_once()
    assert "Agent resource cleanup failed" in caplog.text
    assert "private synthetic cleanup failure" not in caplog.text
