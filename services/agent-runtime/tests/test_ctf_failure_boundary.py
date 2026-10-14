"""Explicit tool failures restore only their original platform error in the runner."""

from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from agent_framework import MiddlewareFailure
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.execution import UnknownCommandOutcome
from bbx_runtime.ctf.failures import CtfToolFailure, propagate_tool_failure, tool_failure_cause
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_provider_boundary import DispatchService


def test_tool_failure_bridge_restores_exact_error_without_provider_cause_search():
    original = httpx.ReadError("synthetic private transport detail")
    original_cause = OSError("synthetic underlying disconnect")
    original.__cause__ = original_cause
    boundary = CtfToolFailure(original)
    assert tool_failure_cause(boundary) is original
    assert tool_failure_cause(MiddlewareFailure("synthetic unrelated boundary")) is None
    provider_error = RuntimeError("synthetic provider error")
    provider_error.__cause__ = original
    assert tool_failure_cause(provider_error) is None
    with pytest.raises(httpx.ReadError) as caught:
        with propagate_tool_failure():
            raise boundary
    assert caught.value is original
    assert caught.value.__cause__ is original_cause
    assert "private transport detail" not in str(boundary)


@pytest.mark.parametrize(
    "original,interrupted",
    [
        (httpx.ReadError("synthetic tool disconnect"), True),
        (RemoteError(403, "synthetic revoked tool"), True),
        (RemoteError(409, "synthetic stopped tool"), True),
        (RemoteError(503, "synthetic unavailable tool"), False),
        (UnknownCommandOutcome("synthetic unknown command"), False),
    ],
)
async def test_runner_unwraps_tool_failure_without_repeating_or_changing_error_classification(
    original, interrupted
):
    service = DispatchService()
    service.profile.options.context_threshold = 100000
    calls = []
    provider_calls_at_failure = []

    async def execute_call(*args):
        calls.append(args)
        provider_calls_at_failure.append(len(client.received_messages))
        raise CtfToolFailure(original) from original

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "synthetic"}),)),
            ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "synthetic"}),)),
            ScriptStep(text="must not run"),
        ]
    )
    runner = CtfRunner(
        cast(Any, service),
        cast(Any, SimpleNamespace()),
        client_factory=lambda *_: client,
        envd=SimpleNamespace(execute_call=execute_call),
    )
    arguments = (
        "task",
        {"id": "member-1", "display_name": "Synthetic", "role": "teammate"},
        {"id": "turn-1", "generation": 1, "token": "synthetic"},
    )
    if interrupted:
        await runner.run(*arguments)
        assert len(service.finished) == 1
        assert service.finished[0]["end_reason"] == "interrupted"
    else:
        with pytest.raises(type(original)) as caught:
            await runner.run(*arguments)
        assert caught.value is original
        assert not service.finished
    assert len(calls) == 1
    # MAF's local-history sentinel triggers cleanup too, never another tool batch.
    cleanup_options = client.received_options[provider_calls_at_failure[0] :]
    assert len(cleanup_options) == 1
    assert cleanup_options[0]["tool_choice"] == "none"
    assert not any(observation["kind"] == "model_error" for observation in service.observations)
