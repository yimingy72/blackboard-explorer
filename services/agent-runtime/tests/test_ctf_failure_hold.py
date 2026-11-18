"""Automatic failure holds preserve other writers; manual behavior stays unchanged."""

import asyncio
from typing import Any, cast

import httpx
from bbx_runtime.clients.blackboard import BlackboardClient
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import CtfCoordinator


async def test_automatic_failure_holds_admission_without_cancelling_healthy_peer():
    class Service:
        calls = []

        async def runtime_state(self, _):
            return {
                "task": {
                    "ctf_options": {"dispatch_mode": "automatic"},
                    "ctf_control": {"phase": "running"},
                    "budget": {"max_cost": "10", "max_minutes": 10},
                    "usage": {},
                },
                "members": [],
                "turns": [],
                "ready_member_ids": [],
            }

        async def runtime(self, _, operation, **body):
            self.calls.append((operation, body))
            assert operation not in {"request_finish", "finalize_close", "claim_turn"}
            return {"held": True}

    service = Service()
    coordinator = CtfCoordinator(cast(Any, None), cast(Any, None), runner=object())
    coordinator.service = cast(Any, service)

    async def failed():
        raise RuntimeError("do not persist raw exception text")

    bad = asyncio.create_task(failed())
    good = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    coordinator.active[("task", "member-1")] = (bad, {"id": "turn-1", "generation": 1})
    coordinator.active[("task", "member-2")] = (good, {"id": "turn-2", "generation": 1})
    try:
        assert await coordinator.tick("task")
        pause = next(body for op, body in service.calls if op == "pause_for_failure")
        assert pause["agent_id"] == "member-1" and pause["turn_id"] == "turn-1"
        assert "raw exception" not in str(pause)
        assert not good.done()
        assert await coordinator.tick("task")
        assert not good.done()
    finally:
        await coordinator.close()


async def test_definite_admission_pause_retries_same_tool_body(monkeypatch):
    sent = []

    async def no_wait(_):
        return None

    monkeypatch.setattr("bbx_runtime.ctf.client.asyncio.sleep", no_wait)

    def respond(request):
        sent.append(request.content)
        if len(sent) == 1:
            return httpx.Response(409, json={"detail": {"code": "ctf_admission_paused"}})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        service = CtfClient(BlackboardClient("http://test", "fake", http))
        assert await service.tool("task", "turn-token", "append_record", request_id="fixed") == {
            "ok": True
        }
    assert len(sent) == 2 and sent[0] == sent[1]
