"""Control transport recovery without model or execution calls."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from agent_framework import Agent
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.ctf.tool_results import json_tool_result
from bbx_runtime.ctf.tools import build_tools
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_coordination import Runtime


@pytest.mark.parametrize("owner", ["runtime-one", "runtime-other"])
async def test_lost_claim_response_reads_owner_and_only_repeats_owned_claim(owner):
    posts = 0

    def response(request):
        nonlocal posts
        if request.method == "POST":
            posts += 1
            if posts == 1:
                raise httpx.RemoteProtocolError("reply lost")
            return httpx.Response(200, json={"id": "saved-turn", "token": "fenced-token"})
        return httpx.Response(
            200,
            json={
                "task": {"ctf_control": {"phase": "running"}},
                "turns": [
                    {
                        "id": "saved-turn",
                        "agent_id": "lead",
                        "runtime_instance": owner,
                        "status": "running",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        client = CtfClient(BlackboardClient("http://board", "service", http))
        if owner == "runtime-one":
            result = await client.runtime(
                "task", "claim_turn", agent_id="lead", runtime_instance=owner
            )
            assert result == {"id": "saved-turn", "token": "fenced-token"}
            assert posts == 2
        else:
            with pytest.raises(httpx.RemoteProtocolError):
                await client.runtime(
                    "task", "claim_turn", agent_id="lead", runtime_instance="runtime-one"
                )
            assert posts == 1


async def test_control_transport_retries_keep_healthy_teammate_running(monkeypatch):
    from bbx_runtime.ctf import coordinator as module

    service = SimpleNamespace(
        runtime_state=AsyncMock(return_value={"task": {"ctf_control": {"phase": "running"}}}),
        runtime=AsyncMock(),
    )
    instance = CtfCoordinator(cast(Any, None), cast(Any, None), runner=object(), poll_seconds=0)
    instance.service = cast(Any, service)
    healthy = asyncio.create_task(asyncio.Event().wait())
    instance.active[("task", "teammate")] = (healthy, {"id": "healthy-turn"})
    attempts, delays = [], []

    async def tick(_):
        attempts.append(1)
        assert not healthy.done()
        if len(attempts) <= 2:
            raise httpx.ReadError("control disconnected")
        return False

    async def wait(awaitable, timeout):
        awaitable.close()
        delays.append(timeout)
        await asyncio.sleep(0)
        raise TimeoutError

    monkeypatch.setattr(instance, "tick", tick)
    monkeypatch.setattr(module.asyncio, "wait_for", wait)
    try:
        await instance.run("task")
        assert len(attempts) == 3 and delays == [1, 2]
        assert [call.args[1] for call in service.runtime.call_args_list] == ["start"]
        assert not healthy.done()
    finally:
        healthy.cancel()
        await asyncio.gather(healthy, return_exceptions=True)


async def test_confirmation_conflict_http_metadata_is_bounded_and_body_free():
    identifier = "00000000-0000-0000-0000-000000000001"
    response = httpx.Response(
        409,
        json={
            "detail": {
                "code": "message_confirmation_conflict",
                "message": "private-sentinel",
                "conflicts": [
                    {
                        "id": identifier,
                        "status": "delivered",
                        "deferred": False,
                        "body": "private-sentinel",
                    }
                ]
                * 101,
            }
        },
    )
    with pytest.raises(RemoteError) as caught:
        await BlackboardClient._check(response)
    result = json.loads(await json_tool_result(AsyncMock(side_effect=caught.value)()))
    conflicts = result["error"]["conflicts"]
    assert len(conflicts) == 100
    assert set(conflicts[0]) == {"id", "status", "deferred"}
    assert "private-sentinel" not in json.dumps(result)


@pytest.mark.parametrize(
    "status,code",
    [(403, "message_confirmation_conflict"), (500, None), (409, "generation_revoked")],
)
async def test_confirmation_security_and_unknown_errors_propagate(status, code):
    error = RemoteError(status, "private-sentinel", code)
    with pytest.raises(RemoteError) as caught:
        await json_tool_result(AsyncMock(side_effect=error)())
    assert caught.value is error


async def test_native_maf_wrong_challenge_id_refreshes_ids_before_retry():
    calls = []

    async def request(_, token, operation, **body):
        calls.append((operation, body))
        if operation == "list_records":
            if body["challenge_id"] == "wrong":
                raise RemoteError(404, "Challenge not found")
            assert body["challenge_id"] == "real-id"
            return {"records": []}
        assert operation == "list_challenges"
        return {"challenges": [{"id": "real-id", "title": "Fake task"}]}

    service = SimpleNamespace(tool=request)
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("list_records", {"challenge_id": "wrong"}),)),
            ScriptStep(
                calls=(ScriptToolCall("list_challenges", {}),),
                expect_contains="challenge_not_found",
            ),
            ScriptStep(
                calls=(ScriptToolCall("list_records", {"challenge_id": "real-id"}),),
                expect_contains="real-id",
            ),
            ScriptStep(text="Recovered", expect_contains='"records": []'),
        ]
    )
    async with Agent(
        client=client, tools=build_tools(cast(Any, service), "task", {"token": "token"}, "lead")
    ) as agent:
        assert (await agent.run("Read the fake task records")).text == "Recovered"
    assert [operation for operation, _ in calls] == [
        "list_records",
        "list_challenges",
        "list_records",
    ]


@pytest.mark.parametrize(
    "status,message",
    [(404, "Unknown private object"), (403, "Challenge not found"), (500, "Challenge not found")],
)
async def test_challenge_feedback_only_recovers_exact_missing_challenge(status, message):
    error = RemoteError(status, message)
    with pytest.raises(RemoteError) as caught:
        await json_tool_result(AsyncMock(side_effect=error)())
    assert caught.value is error


async def test_concurrent_ticks_start_only_one_writer_for_idempotent_claim():
    service = Runtime()
    service.state["ready_member_ids"] = ["lead"]

    async def running(*_):
        await asyncio.Event().wait()

    instance = CtfCoordinator(cast(Any, None), cast(Any, None), runner=SimpleNamespace(run=running))
    instance.service = cast(Any, service)
    try:
        await asyncio.gather(instance.tick("task"), instance.tick("task"))
        assert sum(operation == "claim_turn" for operation, _ in service.calls) == 1
        assert list(instance.active) == [("task", "lead")]
    finally:
        await instance.close()


@pytest.mark.parametrize("reconciled", [True, False])
async def test_coordinator_reconciles_confirmed_usage_before_budget_close(reconciled):
    class Service(Runtime):
        async def runtime(self, _, operation, **body):
            result = await super().runtime(_, operation, **body)
            if operation == "reconcile_budget" and reconciled:
                self.state["task"]["ctf_control"].pop("budget_unknown_reservations")
            return result

    service = Service()
    service.state["task"]["ctf_control"]["budget_unknown_reservations"] = {
        "pending": {"cost": "10"}
    }
    instance = CtfCoordinator(cast(Any, None), cast(Any, None), runner=object())
    instance.service = cast(Any, service)
    assert await instance.tick("task")
    operations = [operation for operation, _ in service.calls]
    assert operations[0] == "reconcile_budget"
    assert ("request_finish" in operations) is (not reconciled)
    if not reconciled:
        assert service.calls[-1][1]["conclusion"]["end_reason"] == "budget_exhausted"


async def test_idle_watchdog_is_throttled_and_failed_checks_do_not_advance_due(monkeypatch):
    from bbx_runtime.ctf import coordinator as module

    class Service(Runtime):
        fail = False

        async def runtime(self, _, operation, **body):
            result = await super().runtime(_, operation, **body)
            if operation == "idle_watchdog" and self.fail:
                self.fail = False
                raise httpx.ReadError("lost watchdog reply")
            return result

    service = Service()
    instance = CtfCoordinator(cast(Any, None), cast(Any, None), runner=object())
    instance.service = cast(Any, service)
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    for clock[0] in (0.0, 0.2, 1.0, 4.9):
        await instance.tick("task")
    assert service.calls == [("idle_watchdog", {})]
    clock[0] = 5.0
    service.fail = True
    with pytest.raises(httpx.ReadError):
        await instance.tick("task")
    assert instance.watchdog_checks["task"] == 5.0
    await instance.tick("task")
    assert len(service.calls) == 3
    assert instance.watchdog_checks["task"] == 10.0
    await instance.close()
    assert not instance.watchdog_checks


async def test_control_wait_stage_is_visible_while_pending_and_cleared_after_tick():
    service = Runtime()
    entered, release = asyncio.Event(), asyncio.Event()

    async def runtime_state(_):
        entered.set()
        await release.wait()
        return service.state

    service.runtime_state = runtime_state
    instance = CtfCoordinator(cast(Any, None), cast(Any, None), runner=object())
    instance.service = cast(Any, service)
    task = asyncio.create_task(instance.tick("task"))
    await entered.wait()
    assert instance.control_waits["task"][0] == "runtime_state"
    release.set()
    assert await task
    assert instance.control_waits == {}
