"""Measure cooperative scheduling with real PG/HTTP/MAF and scripted model responses."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from agent_framework import ResponseStream
from bbx_blackboard.api import create_app
from bbx_blackboard.platform import ModelInput
from bbx_blackboard.settings import Settings as BoardSettings
from bbx_blackboard.store import schema as s
from bbx_contracts.ctf import CtfAgentProfile, load_ctf_profile
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.ctf.budget import ctf_call_reservation
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptUsage
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from test_ctf_vertical_integration import ROOT, MemoryObjects, call, run_ready_turns
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

pytestmark = pytest.mark.integration


class HeldScriptedClient(ScriptedChatClient):
    """Hold each native streaming response after budget admission and model entry."""

    def __init__(self, member_id, entered, release):
        super().__init__(
            [
                ScriptStep(
                    text=f"Completed arithmetic for {member_id}",
                    expect_contains=f"Compute arithmetic for {member_id}",
                    usage=ScriptUsage(prompt_cache_miss_tokens=100, completion_tokens=100),
                )
            ]
        )
        self.member_id, self.entered, self.release = member_id, entered, release

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        assert stream
        assert options["max_tokens"] == 64000
        self.entered.append(self.member_id)

        async def response():
            await asyncio.wait_for(self.release.wait(), 15)
            result = super(HeldScriptedClient, self)._inner_get_response(
                messages=messages, stream=True, options=options, **kwargs
            )
            assert isinstance(result, ResponseStream)
            return result

        return ResponseStream.from_awaitable(response())


class HttpProbe:
    def __init__(self):
        self.operations = Counter()
        self.waits = []
        self.replayed_bills = set()
        self.http: httpx.AsyncClient | None = None

    async def request(self, request):
        self.operations[(request.method, request.url.path.rsplit("/", 1)[-1])] += 1

    async def response(self, response):
        await response.aread()
        request = response.request
        operation = request.url.path.rsplit("/", 1)[-1]
        if operation == "authorize_member" and response.status_code == 409:
            detail = response.json()["detail"]
            if detail["code"] == "ctf_budget_wait":
                self.waits.append(json.loads(request.content))
        if operation != "bill_usage" or response.status_code != 200:
            return
        payload = json.loads(request.content)
        if payload["agent_id"] == "lead" or payload["request_id"] in self.replayed_bills:
            return
        self.replayed_bills.add(payload["request_id"])
        assert self.http is not None
        replay = await self.http.post(request.url, headers=request.headers, json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json() == response.json()


@pytest.fixture
async def cooperation_board(ctf_vertical_database):
    settings = BoardSettings.model_construct(
        postgres_password=SecretStr("isolated-test"),
        minio_root_password=SecretStr("unused-test"),
        service_token=SecretStr("ctf-cooperation-test-service"),
        agent_token_secret=SecretStr("ctf-cooperation-isolated-test-signing-key"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    engine = create_async_engine(ctf_vertical_database)
    app = create_app(settings, engine=engine, objects=MemoryObjects())
    probe = HttpProbe()
    try:
        await app.state.profile_store.ensure_bundled(
            settings.profiles_dir, app.state.platform_store
        )
        data = load_ctf_profile(ROOT / "profiles/ctf").model_dump()
        data["model"]["context_window"] = 128000
        data["model"]["price"] = {
            "billing_mode": "fixed",
            "currency": "CNY",
            "cache_hit_per_m": "1",
            "cache_miss_per_m": "1",
            "output_per_m": "1",
            "off_peak": False,
        }
        profile = CtfAgentProfile.model_validate(data)
        configured = await app.state.profile_store.create(
            f"ctf-cooperation-{uuid4().hex[:8]}", profile, "test"
        )
        model = await app.state.platform_store.save(
            "models",
            f"cooperation-model-{uuid4().hex[:8]}",
            ModelInput(
                label="Scripted cooperation model",
                provider=profile.model.provider,
                model=profile.model.model,
                base_url=profile.model.base_url,
                reasoning_effort=profile.model.reasoning_effort,
                context_window=profile.model.context_window,
                price=profile.model.price,
                api_key=SecretStr("unused-scripted"),
            ),
            "test",
        )
        configured["model_id"], configured["model_version"] = model["name"], model["version"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://board",
            trust_env=False,
            event_hooks={"request": [probe.request], "response": [probe.response]},
        ) as http:
            probe.http = http
            board = BlackboardClient("http://board", "ctf-cooperation-test-service", http)
            yield board, CtfClient(board), profile, configured, probe, engine
    finally:
        await app.state.workspace_cache.close()
        await engine.dispose()


async def wait_until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


async def create_task(board, ctf, configured, budget):
    task = await board.create_task(
        {
            "mode": "ctf",
            "goal": "Coordinate three independent arithmetic workers",
            "budget": {"max_cost": str(budget), "max_minutes": 5},
            "agent_profile": configured["name"],
            "profile_version": configured["version"],
            "model_id": configured["model_id"],
            "model_version": configured["model_version"],
        }
    )
    tid = task["id"]
    await board.start_task(tid)
    await ctf.runtime(tid, "start")
    return tid


async def test_budget_wait_keeps_turn_and_reduces_polling_operations(cooperation_board, capsys):
    board, ctf, profile, configured, probe, engine = cooperation_board
    reservation = ctf_call_reservation(
        profile.model, context_tokens=profile.options.context_threshold
    )
    assert reservation is not None
    tid = await create_task(board, ctf, configured, reservation * Decimal("2.5"))
    entered = []
    releases = {f"member-{i}": asyncio.Event() for i in range(1, 4)}
    clients = {}

    def factory(member, _profile):
        mid = member["id"]
        assert mid not in clients, "Waiting must never reopen the worker's turn"
        if mid == "lead":
            steps = [call("create_teammate", display_name=f"Worker {i}") for i in range(1, 4)]
            steps += [
                call(
                    "send_message",
                    recipient_id=f"member-{i}",
                    body=f"Compute arithmetic for member-{i}",
                    instruction=True,
                )
                for i in range(1, 4)
            ]
            client = ScriptedChatClient([*steps, ScriptStep(text="Workers assigned")])
        else:
            client = HeldScriptedClient(mid, entered, releases[mid])
        clients[mid] = client
        return client

    settings = Settings.model_construct(deepseek_api_key=SecretStr("unused-scripted"))
    coordinator = CtfCoordinator(board, settings, runner=CtfRunner(ctf, settings, factory))
    try:
        await run_ready_turns(coordinator, tid)
        await coordinator.tick(tid)
        await wait_until(lambda: len(entered) == 2 and bool(probe.waits))
        waiting_id = ({"member-1", "member-2", "member-3"} - set(entered)).pop()
        state = await ctf.state(tid)
        worker_turns = {t["agent_id"]: t for t in state["turns"] if t["agent_id"] != "lead"}
        assert len(worker_turns) == 3
        waiting_turn = worker_turns[waiting_id]
        reservations = state["task"]["ctf_control"]["budget_reservations"]
        assert len(reservations) == 2
        assert sum(Decimal(r["cost"]) for r in reservations.values()) == reservation * 2
        assert all(t["status"] == "running" for t in worker_turns.values())
        inputs = {m["recipient_id"]: m for m in state["messages"] if m["sender_id"] == "lead"}
        assert inputs[waiting_id]["status"] == "queued"
        await coordinator.tick(tid)
        ticks = 6
        before = probe.operations.copy()
        # Execute the former hot-loop pattern as an explicit HTTP reference workload.
        for _ in range(ticks):
            await ctf.state(tid)
            for mid, turn in worker_turns.items():
                await ctf.runtime(
                    tid,
                    "renew_turn",
                    agent_id=mid,
                    turn_id=turn["id"],
                    generation=turn["generation"],
                )
        baseline = probe.operations - before
        assert baseline[("GET", "state")] == ticks
        assert baseline[("POST", "renew_turn")] == ticks * 3
        leased = await ctf.state(tid)
        deadlines = {
            m["recipient_id"]: m["lease_until"]
            for m in leased["messages"]
            if m["recipient_id"] in entered
        }
        for deadline in deadlines.values():
            seconds = (datetime.fromisoformat(deadline) - datetime.now(UTC)).total_seconds()
            assert 290 < seconds <= 300
        before = probe.operations.copy()
        for _ in range(ticks):
            assert await coordinator.tick(tid)
            await asyncio.sleep(0.21)
        measured = probe.operations - before
        assert measured[("GET", "runtime-state")] == ticks
        assert measured[("GET", "state")] == 0
        assert measured[("POST", "renew_turn")] == 0
        assert measured[("POST", "claim_turn")] == 0
        assert measured[("POST", "finish_turn")] == 0
        held = await ctf.state(tid)
        assert len(held["turns"]) == len(state["turns"])
        assert len(entered) == 2
        assert not [m for m in held["messages"] if m["notification_purpose"] == "assignment_result"]
        assert {
            m["recipient_id"]: m["lease_until"]
            for m in held["messages"]
            if m["recipient_id"] in deadlines
        } == deadlines
        waits = [w for w in probe.waits if w["agent_id"] == waiting_id]
        assert len(waits) >= 2
        assert {w["turn_id"] for w in waits} == {waiting_turn["id"]}
        assert {w["reservation_id"] for w in waits} == {f"{waiting_turn['id']}:1"}

        releases[entered[0]].set()
        await wait_until(lambda: len(entered) == 3)
        resumed = await ctf.state(tid)
        assert (
            next(m for m in resumed["members"] if m["id"] == waiting_id)["current_turn_id"]
            == waiting_turn["id"]
        )
        assert len(resumed["turns"]) == len(state["turns"])
        for release in releases.values():
            release.set()
        await asyncio.wait_for(
            asyncio.gather(*(future for future, _ in coordinator.active.values())), 10
        )
        final = await ctf.state(tid)
        assert len(final["turns"]) == len(state["turns"])
        assert all(t["end_reason"] == "completed" for t in final["turns"])
        assert not final["task"]["ctf_control"].get("budget_reservations")
        assert not final["task"]["ctf_control"].get("budget_unknown_reservations")
        assert Decimal(final["task"]["usage"]["cost"]) == Decimal("0.0006")
        assert len(probe.replayed_bills) == 3
        results = [m for m in final["messages"] if m["notification_purpose"] == "assignment_result"]
        assert len(results) == 3
        assert {m["source_turn_id"] for m in results} == {t["id"] for t in worker_turns.values()}
        for mid, original in inputs.items():
            delivered = next(m for m in final["messages"] if m["id"] == original["id"])
            assert delivered["status"] == "delivered"
            assert delivered["delivered_turn_id"] == worker_turns[mid]["id"]
            messages = clients[mid].received_messages
            assert len(messages) == 1
            assert sum(m.message_id == original["id"] for m in messages[0]) == 1
            saved = await board.get_agent_session(tid, mid)
            history = saved["session"]["state"]["in_memory"]["messages"]
            assert sum(m.get("message_id") == original["id"] for m in history) == 1
        async with engine.connect() as conn:
            events = (
                (
                    await conn.execute(
                        select(s.events.c.payload).where(
                            s.events.c.task_id == UUID(tid),
                            s.events.c.type == "ctf.message.delivered",
                        )
                    )
                )
                .scalars()
                .all()
            )
        for original in inputs.values():
            assert sum(e["id"] == original["id"] for e in events) == 1

        # These are scheduling operation counts, not a measurement of problem-solving speed.
        metrics = {
            "ticks": ticks,
            "held_workers": 3,
            "before_full_state_gets": baseline[("GET", "state")],
            "after_full_state_gets": measured[("GET", "state")],
            "after_runtime_state_gets": measured[("GET", "runtime-state")],
            "before_lease_renewals": baseline[("POST", "renew_turn")],
            "after_lease_renewals": measured[("POST", "renew_turn")],
            "waiting_turns_created": 1,
            "waiting_model_calls_before_release": 0,
        }
        with capsys.disabled():
            print("cooperation_operations=" + json.dumps(metrics, sort_keys=True))
    finally:
        for release in releases.values():
            release.set()
        await coordinator.detach(tid, deleting=True)


async def test_permanent_budget_shortfall_closes_without_model_call(cooperation_board):
    board, ctf, profile, configured, _probe, _engine = cooperation_board
    reservation = ctf_call_reservation(
        profile.model, context_tokens=profile.options.context_threshold
    )
    assert reservation is not None
    tid = await create_task(board, ctf, configured, reservation / 2)
    client = ScriptedChatClient([ScriptStep(text="Must never run")])
    settings = Settings.model_construct(deepseek_api_key=SecretStr("unused-scripted"))
    coordinator = CtfCoordinator(
        board, settings, runner=CtfRunner(ctf, settings, lambda *_: client)
    )
    try:
        await run_ready_turns(coordinator, tid)
        assert client.received_messages == []
        assert not await coordinator.tick(tid)
        final = await ctf.state(tid)
        assert final["task"]["ctf_control"]["phase"] == "closed"
        assert final["task"]["ctf_conclusion"]["end_reason"] == "budget_exhausted"
        assert len(final["turns"]) == 1
        assert final["turns"][0]["end_reason"] == "budget_exhausted"
        assert Decimal(final["task"]["usage"].get("cost", 0)) == 0
        assert not [
            m for m in final["messages"] if m["notification_purpose"] == "assignment_result"
        ]
    finally:
        await coordinator.detach(tid, deleting=True)
