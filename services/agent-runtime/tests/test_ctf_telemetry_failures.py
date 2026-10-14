"""Failed error telemetry cannot turn cancellation into retryable transport work."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.failures import CtfToolFailure
from bbx_runtime.ctf.telemetry import ToolObservationMiddleware


class Observations:
    def __init__(self):
        self.kinds = []

    async def runtime(self, _, operation, **payload):
        assert operation == "record_observation"
        self.kinds.append(payload["kind"])
        if payload["kind"] == "tool_result":
            raise httpx.RemoteProtocolError("synthetic private observation error")
        return {}


def middleware(service):
    return ToolObservationMiddleware(
        cast(CtfClient, service), "task", "member", {"id": "turn", "generation": 3}
    )


def context():
    return cast(
        Any,
        SimpleNamespace(
            metadata={"function_call_occurrence_id": "occurrence"},
            function=SimpleNamespace(name="synthetic_tool"),
            arguments={"opaque": "synthetic private arguments"},
            result="synthetic private result",
        ),
    )


async def test_cancelled_tool_keeps_original_cancel_and_never_reenters_dispatch(caplog):
    original = asyncio.CancelledError("synthetic private cancellation")
    observations = Observations()
    observer = middleware(observations)
    dispatches = []
    calls = []

    async def cancelled_tool():
        calls.append("tool")
        raise original

    async def dispatch():
        # Model the caller's bounded transport retry: an overwritten cancellation
        # would cause a second provider entry and another invocation of the tool.
        for attempt in range(2):
            dispatches.append("provider")
            try:
                await observer.process(context(), cancelled_tool)
                return
            except httpx.TransportError:
                if attempt:
                    raise

    with pytest.raises(asyncio.CancelledError) as raised:
        await dispatch()
    assert raised.value is original
    assert dispatches == ["provider"]
    assert calls == ["tool"]
    assert observations.kinds == ["tool_call", "tool_result"]
    assert "task=task member=member turn_id=turn generation=3" in caplog.text
    assert "error_type=CancelledError observation_error_type=RemoteProtocolError" in caplog.text
    assert "synthetic private" not in caplog.text


@pytest.mark.parametrize("original", [RuntimeError("private runtime"), ValueError("private input")])
async def test_failed_error_observation_preserves_original_non_cancel_error(original, caplog):
    async def failed_tool():
        raise original

    with pytest.raises(CtfToolFailure) as raised:
        await middleware(Observations()).process(context(), failed_tool)
    assert raised.value.original_error is original
    assert "private" not in caplog.text


async def test_successful_tool_still_requires_result_observation():
    calls = []

    async def completed_tool():
        calls.append("tool")

    with pytest.raises(CtfToolFailure) as raised:
        await middleware(Observations()).process(context(), completed_tool)
    assert isinstance(raised.value.original_error, httpx.RemoteProtocolError)
    assert calls == ["tool"]


@pytest.mark.parametrize("cancelled_tool", [True, False])
async def test_error_observation_cancellation_preserves_first_cancel_only_when_already_cancelled(
    cancelled_tool,
):
    first = asyncio.CancelledError("first cancel") if cancelled_tool else ValueError("tool error")
    second = asyncio.CancelledError("observation cancel")

    class CancelledObservations(Observations):
        async def runtime(self, _, operation, **payload):
            if payload["kind"] == "tool_result":
                raise second
            return {}

    async def failed_tool():
        raise first

    with pytest.raises(asyncio.CancelledError) as caught:
        await middleware(CancelledObservations()).process(context(), failed_tool)
    assert caught.value is (first if cancelled_tool else second)
