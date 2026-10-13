"""Offline scheduling overhead and lease renewal regressions."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
from bbx_runtime.clients.blackboard import BlackboardClient
from bbx_runtime.ctf import coordinator as module
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import CtfCoordinator


class Runtime:
    def __init__(self, *, review=False):
        self.state = {
            "task": {
                "status": "finished" if review else "running",
                "ctf_control": {"phase": "closed" if review else "running"},
                "budget": {"max_cost": "10", "max_minutes": 10},
                "usage": {},
            },
            "members": [
                {"id": "lead", "lifecycle": "active"},
                {"id": "member-1", "lifecycle": "active"},
                {"id": "stopped", "lifecycle": "stopped"},
            ],
            "turns": [],
            "ready_member_ids": [],
            "review_ready_member_ids": [],
        }
        self.calls = []
        self.reads = 0

    async def runtime_state(self, _):
        self.reads += 1
        return self.state

    async def runtime(self, _, operation, **body):
        self.calls.append((operation, body))
        if operation in {"claim_turn", "claim_review_turn"}:
            return {"id": "turn", "generation": 1, "agent_id": body["agent_id"]}
        return {}


def coordinator(service):
    instance = CtfCoordinator(cast(Any, None), cast(Any, None), runner=object())
    instance.service = cast(CtfClient, service)
    return instance


async def test_runtime_client_keeps_full_state_and_lightweight_state_distinct():
    seen = []

    def response(request):
        seen.append(request)
        return httpx.Response(200, json={"task": {}, "members": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        client = CtfClient(BlackboardClient("http://test", "service-test", http))
        await client.runtime_state("task")
        await client.state("task")
    assert [request.url.path for request in seen] == [
        "/api/tasks/task/ctf/runtime-state",
        "/api/tasks/task/ctf/state",
    ]
    assert all(request.headers["Authorization"] == "Bearer service-test" for request in seen)


async def test_idle_ticks_do_not_claim_empty_mailboxes():
    service = Runtime()
    instance = coordinator(service)
    for _ in range(100):
        assert await instance.tick("task")
    assert service.reads == 100
    assert service.calls == [("idle_watchdog", {})]


async def test_dispatch_uses_ready_recipients_and_preserves_stopped_members():
    service = Runtime()
    service.state["ready_member_ids"] = ["member-1", "stopped"]
    instance = coordinator(service)

    async def running(*_):
        await asyncio.Event().wait()

    instance.runner = SimpleNamespace(run=running)
    try:
        await instance.tick("task")
        assert [
            body["agent_id"] for operation, body in service.calls if operation == "claim_turn"
        ] == ["member-1"]
        assert set(instance.active) == {("task", "member-1")}
    finally:
        await instance.close()


async def test_long_turn_renews_without_waiting_for_model_and_new_generation_renews_immediately(
    monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    service = Runtime()
    instance = coordinator(service)
    future = asyncio.create_task(asyncio.Event().wait())
    turn = {"id": "old-turn", "generation": 1}
    instance.active[("task", "lead")] = (future, turn)
    try:
        for clock[0] in (0.0, 0.2, 59.9, 60.0, 120.0, 180.0, 240.0, 300.0, 360.0):
            await instance.tick("task")
        renewals = [body for operation, body in service.calls if operation == "renew_turn"]
        assert len(renewals) == 7
        assert all(body["turn_id"] == "old-turn" for body in renewals)
        clock[0] = 360.2
        instance.active[("task", "lead")] = (future, {"id": "new-turn", "generation": 2})
        await instance.tick("task")
        assert service.calls[-1][1]["turn_id"] == "new-turn"
        assert service.calls[-1][1]["generation"] == 2
    finally:
        await instance.close()
    assert instance.renewals == {}


async def test_review_only_claims_user_ready_mailboxes_and_throttles_renewal(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    service = Runtime(review=True)
    instance = coordinator(service)
    instance.review_recovered.add("task")
    future = asyncio.create_task(asyncio.Event().wait())
    instance.review_active[("task", "lead")] = (future, {"id": "review", "generation": 1})
    try:
        await instance.review_tick("task")
        clock[0] = 0.2
        await instance.review_tick("task")
        assert [operation for operation, _ in service.calls] == ["renew_turn"]
        clock[0] = 60
        await instance.review_tick("task")
        assert [operation for operation, _ in service.calls] == ["renew_turn", "renew_turn"]
    finally:
        await instance.close()


async def test_detach_and_recovery_clear_renewal_deadlines():
    service = Runtime()
    instance = coordinator(service)
    instance.runner = SimpleNamespace(
        pending_checkpoints={}, pending_settlements={}, retry_checkpoints=AsyncMock(), envds={}
    )
    await instance.renew("task", "lead", {"id": "old", "generation": 1})
    instance.watchdog_checks["task"] = 50
    await instance.detach("task")
    assert instance.renewals == {}
    assert instance.watchdog_checks == {}
    await instance.renew("task", "lead", {"id": "new", "generation": 2})
    instance.watchdog_checks["task"] = 50
    await instance.recover("task")
    assert instance.renewals == {}
    assert instance.watchdog_checks == {}


async def test_large_history_avoids_serializing_metadata_and_keeps_full_history(monkeypatch):
    from pathlib import Path

    from agent_framework import AgentSession, Content, Message
    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.ctf.context import message_signature
    from bbx_runtime.ctf.middleware import CtfHistoryProvider

    messages = []
    for index in range(100):
        messages.extend(
            [
                Message(
                    role="user", message_id=f"old-{index}", contents=[Content.from_text("x" * 2000)]
                ),
                Message(role="assistant", contents=[Content.from_text("done")]),
            ]
        )
    recent = Message(role="user", message_id="recent", contents=[Content.from_text("new")])
    messages.append(recent)
    session = AgentSession()
    session.state["in_memory"] = {"messages": messages}
    checkpoint = SimpleNamespace(
        session=session,
        task_id="task",
        service=SimpleNamespace(),
        confirmed_history=tuple(message_signature(message) for message in messages),
    )
    model = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf").model
    service = SimpleNamespace(
        runtime_state=AsyncMock(
            return_value={
                "task": {"goal": "Synthetic task"},
                "members": [],
            }
        )
    )
    provider = CtfHistoryProvider(
        cast(Any, service), cast(Any, checkpoint), {"id": "turn"}, model, 1000
    )
    serialized_ids = []
    original = Message.to_dict

    def counted(self, *args, **kwargs):
        serialized_ids.append(self.message_id)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Message, "to_dict", counted)
    view = await provider.get_messages(session.session_id, state=session.state["in_memory"])
    assert view[-1:] == [recent]
    assert "Synthetic task" in view[0].text
    # Only the new stable recovery marker is serialized, not 201 history
    # entries with potentially enormous trace metadata on every call.
    assert serialized_ids == ["ctf-context-recovery:turn"]
    service.runtime_state.assert_awaited_once()
    assert len(session.state["in_memory"]["messages"]) == 201
