"""Taskless model tests use the original Pi ledger send permit and response receipt."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.store import schema as s
from sqlalchemy import delete, select, update
from test_pi_model_ledger_integration import CALLS
from test_pi_model_ledger_integration import board_service as board_service
from test_pi_model_ledger_integration import database_url as database_url
from test_pi_model_ledger_integration import pi_app as pi_app
from test_pi_model_ledger_integration import pi_provider_origin as pi_provider_origin

pytestmark = pytest.mark.integration
PATH = "/api/platform/pi-model-tests"


@pytest.fixture
async def taskless_app(pi_app):
    yield pi_app
    # The shared disposable DB fixture tests a full downgrade; production
    # intentionally refuses that downgrade once taskless receipts exist.
    async with pi_app[0].state.engine.begin() as conn:
        await conn.execute(
            delete(s.pi_model_calls).where(s.pi_model_calls.c.purpose == "playground")
        )


async def test_playground_request_owner_replay_and_unknown_use_one_attempt(
    taskless_app, monkeypatch
):
    app, http, tid, model, _, sender, _, user = taskless_app
    settings = app.state.pi_registry.settings
    settings.pi_playground_runtime_url = "http://agent-runtime:8081"
    denied = {"Authorization": "Bearer " + issue_user_token(settings, "another-user")}
    assert (await http.get("/api/platform/pi-models", headers=user)).json()["can_test"] is True
    posted = []
    held = asyncio.Event()
    release = asyncio.Event()
    lost = False
    expired = False

    async def runtime(_transport, request):
        assert request.url == httpx.URL("http://agent-runtime:8081/playground")
        assert (
            request.headers["Authorization"]
            == "Bearer " + settings.pi_model_runtime_token.get_secret_value()
        )
        invocation = json.loads(request.content)
        intent = invocation["intent"]
        assert intent["task_id"] is None and intent["proof"]["kind"] == "playground"
        posted.append(intent)
        prepared = await http.post(CALLS + "/prepare", headers=sender, json=intent)
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["allowed_tools"] == []
        attempt = {"attempt_id": intent["attempt_id"], "request_sha256": "c" * 64}
        call_path = f"{CALLS}/{intent['call_id']}"
        if expired:
            async with app.state.engine.begin() as conn:
                await conn.execute(
                    update(s.pi_model_calls)
                    .where(s.pi_model_calls.c.call_id == UUID(intent["call_id"]))
                    .values(created_at=datetime.now(UTC) - timedelta(minutes=6))
                )
        dispatched = await http.post(call_path + "/dispatch", headers=sender, json=attempt)
        if expired:
            assert dispatched.json() == {"send_allowed": False, "state": "not_sent"}
            return httpx.Response(200, json={"accepted": False})
        assert dispatched.json()["send_allowed"] is True
        replay = await http.post(call_path + "/dispatch", headers=sender, json=attempt)
        assert replay.json()["send_allowed"] is False
        if lost:
            raise httpx.ReadError("Synthetic lost sender reply")
        held.set()
        await release.wait()
        settled = await http.post(
            call_path + "/settle",
            headers=sender,
            json={**attempt, "outcome": "complete", "result_text": "Synthetic answer"},
        )
        assert settled.status_code == 200, settled.text
        return httpx.Response(200, json={"accepted": True})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", runtime)
    body = {
        "request_id": str(uuid4()),
        "model_id": model["id"],
        "model_revision": model["model_revision"],
        "prompt": "Synthetic prompt not stored",
    }
    assert (await http.post(PATH, headers=denied, json=body)).status_code == 403
    first = asyncio.create_task(http.post(PATH, headers=user, json=body))
    try:
        await asyncio.wait_for(held.wait(), timeout=5)
        duplicate = await http.post(PATH, headers=user, json=body)
        assert duplicate.json()["state"] == "dispatched"
        changed = await http.post(PATH, headers=user, json={**body, "prompt": "Different"})
        assert changed.status_code == 409
        assert len(posted) == 1
    finally:
        release.set()
    finished = await first
    assert finished.status_code == 200, finished.text
    assert finished.json()["result_text"] == "Synthetic answer"
    assert finished.json()["amount_usd"] is None
    stored = await http.get(PATH + "/" + body["request_id"], headers=user)
    assert stored.json() == finished.json() and stored.headers["cache-control"] == "no-store"
    assert (await http.get(PATH + "/" + body["request_id"], headers=denied)).status_code == 403
    lost = True
    other = {**body, "request_id": str(uuid4())}
    reply_lost = await http.post(PATH, headers=user, json=other)
    assert reply_lost.json()["state"] == "unknown" and len(posted) == 2
    assert (await http.post(PATH, headers=user, json=other)).json() == reply_lost.json()
    assert len(posted) == 2
    original = posted[-1]
    late = await http.post(
        f"{CALLS}/{original['call_id']}/settle",
        headers=sender,
        json={
            "attempt_id": original["attempt_id"],
            "request_sha256": "c" * 64,
            "outcome": "complete",
            "result_text": "The original reply arrived later",
        },
    )
    assert late.status_code == 200
    assert (await http.get(PATH + "/" + other["request_id"], headers=user)).json()[
        "state"
    ] == "complete"
    expired = True
    stale = {**body, "request_id": str(uuid4())}
    assert (await http.post(PATH, headers=user, json=stale)).json()["state"] == "not_sent"
    assert len(posted) == 3
    async with app.state.engine.begin() as conn:
        # A new listing can no longer select this model; existing request IDs remain readable.
        await conn.execute(
            update(s.platform_configs)
            .where(s.platform_configs.c.name == model["id"])
            .values(enabled=False)
        )
        rows = (await conn.execute(select(s.pi_model_calls))).mappings().all()
        assert len(rows) == 3
        assert all(row["task_id"] is None and row["purpose"] == "playground" for row in rows)
        assert all(body["prompt"] not in str(row["intent"]) for row in rows)
    assert (await http.post(PATH, headers=user, json=body)).json() == finished.json()
    task_fees = (await http.get(f"/api/tasks/{tid}/pi-fees", headers=user)).json()
    assert task_fees["agents"] == {}
    assert task_fees["solver"] == {"estimated_usd": "0", "unknown_calls": 0}
