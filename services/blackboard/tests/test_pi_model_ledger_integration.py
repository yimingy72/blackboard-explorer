"""Exercise actual P6a registry and ledger routes against disposable PostgreSQL."""

import asyncio
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.pi_registry import display_pi_model
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.store import schema as s
from bbx_contracts.legacy_network import (
    LEGACY_NETWORK_HEADER,
    LEGACY_NETWORK_VERSION,
    LegacyNetworkSnapshot,
)
from bbx_contracts.pi_models import PiModelInput, PiModelVersion
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.models import make_task_model_client
from bbx_runtime.settings import Settings as RuntimeSettings
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import insert, select, update
from test_ctf_api import settings
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url

pytestmark = pytest.mark.integration
CALLS = "/api/runtime/pi-model-calls"


@pytest.fixture
def pi_provider_origin():
    return "https://fixture.test"


@pytest.fixture
async def pi_app(board_service, pi_provider_origin, monkeypatch):
    async def fixture_public_host(_registry, _url):
        # Actual provider dial policy is exercised by the sender integration.
        return None

    from bbx_blackboard.pi_registry import PiRegistry

    monkeypatch.setattr(PiRegistry, "verify_public_host", fixture_public_host)
    engine = board_service.repo.engine
    conf = settings().model_copy(
        update={
            "pi_site_id": "primary",
            "pi_model_primary_site": "primary",
            "pi_model_authority": "fixture-authority",
            "pi_model_operator": "alice",
            "pi_model_allowed_origins": [pi_provider_origin, "https://other.test"],
            "pi_model_runtime_token": SecretStr("fixture-model-runtime-" + "x" * 32),
            "pi_model_authority_token": SecretStr("fixture-model-authority-" + "y" * 32),
        }
    )
    await ProfileStore(engine).ensure_default(
        Path(__file__).resolve().parents[3] / "profiles/default"
    )
    app = create_app(conf, engine=engine, objects=board_service.objects)
    app.state.legacy_network_publication = LegacyNetworkSnapshot(
        mode="default", policy_digest="a" * 64
    )
    user = {"Authorization": "Bearer " + issue_user_token(conf, "alice")}
    sender = {"X-BBX-Pi-Runtime": conf.pi_model_runtime_token.get_secret_value()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bbx.test"
    ) as http:
        model_input = {
            "label": "Fixture",
            "upstream_model": "fixture-chat",
            "base_url": pi_provider_origin + "/v1",
            "context_window": 32768,
            "max_output_tokens": 1024,
            "input_modalities": ["text"],
            "usd_rates": {"input": "1", "output": "1", "cache_read": "1", "cache_write": "1"},
            "pricing_source": "Synthetic USD rates",
            "secret": "fixture-only-key",
        }
        denied = await http.post(
            "/api/platform/pi-models",
            json=model_input,
            headers={"Authorization": "Bearer " + conf.service_token.get_secret_value()},
        )
        assert denied.status_code == 403
        created = await http.post("/api/platform/pi-models", json=model_input, headers=user)
        assert created.status_code == 200, created.text
        assert "fixture-only-key" not in created.text
        model = created.json()
        edit = await http.put(
            f"/api/platform/pi-models/{model['id']}",
            headers=user,
            json={
                **{key: value for key, value in model_input.items() if key != "secret"},
                "base_url": "https://other.test/v1",
                "expected_revision": 1,
            },
        )
        assert edit.status_code == 422
        exposed = await http.get(
            f"/api/platform/pi-models/{model['id']}/versions/1/credentials",
            headers={"Authorization": "Bearer " + conf.service_token.get_secret_value()},
        )
        assert exposed.status_code == 422
        opened = await http.post(
            "/api/tasks",
            headers=user,
            json={
                "goal": "Confirm synthetic model accounting",
                "acceptance": [{"id": "A1", "desc": "Confirm"}],
                "budget": {"currency": "USD", "max_cost": "1", "max_minutes": 60},
                "agent_profile": "default",
                "pi_model": {"model_id": model["id"], "model_revision": 1},
            },
        )
        assert opened.status_code in {200, 201}, opened.text
        tid = UUID(opened.json()["id"])
        started = await http.post(f"/api/tasks/{tid}/start", headers=user)
        assert started.status_code == 200, started.text
        async with engine.begin() as conn:
            await conn.execute(
                insert(s.legacy_execution_receipts).values(
                    task_id=tid,
                    run_number=1,
                    receipt={"phase": "ready"},
                )
            )
        await app.state.board_service.transition(tid, "running")
        agents = [await app.state.board_service.register_agent(tid, "explore") for _ in range(3)]
        started_at = datetime.now(UTC)
        async with engine.begin() as conn:
            await conn.execute(
                update(s.agent_runs)
                .where(s.agent_runs.c.task_id == tid)
                .values(status="running", started_at=started_at)
            )
        yield app, http, tid, model, agents, sender, started_at, user


def intent(tid, model, aid, started_at, *, logical=None, run_number=1):
    return {
        "task_id": str(tid),
        "call_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "logical_call_id": logical or hashlib.sha256(uuid4().bytes).hexdigest(),
        "context_sha256": "a" * 64,
        "model_id": model["id"],
        "model_revision": model["model_revision"],
        "action": "turn",
        "proof": {
            "kind": "ordinary",
            "agent_id": aid,
            "run_number": run_number,
            "expected_started_at": started_at.isoformat(),
        },
    }


async def test_pi_registry_and_concurrent_site_ledger(pi_app):
    app, http, tid, model, agents, auth, started_at, user = pi_app
    digest = "b" * 64
    usage = {"input": 100, "output": 20, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 120}

    async def prepare(aid):
        data = intent(tid, model, aid, started_at)
        result = await http.post(CALLS + "/prepare", headers=auth, json=data)
        assert result.status_code == 200, result.text
        assert result.json()["api_key"] == "fixture-only-key"
        return data

    async def send(call):
        return await http.post(
            f"{CALLS}/{call['call_id']}/dispatch",
            headers=auth,
            json={"attempt_id": call["attempt_id"], "request_sha256": digest},
        )

    async def finish(call, amount, outcome="complete"):
        return await http.post(
            f"{CALLS}/{call['call_id']}/settle",
            headers=auth,
            json={
                "attempt_id": call["attempt_id"],
                "request_sha256": digest,
                "outcome": outcome,
                "usage": usage if outcome == "complete" else None,
                "amount_usd": amount,
            },
        )

    first = await prepare(agents[0])
    assert (await send(first)).json()["send_allowed"] is True
    assert (await send(first)).json()["send_allowed"] is False
    assert (await finish(first, "0.9")).status_code == 200
    assert (await finish(first, "0.9")).status_code == 200
    late_replay = {**first, "call_id": str(uuid4()), "attempt_id": str(uuid4())}
    assert (await http.post(CALLS + "/prepare", headers=auth, json=late_replay)).status_code == 409
    a, b = await asyncio.gather(prepare(agents[0]), prepare(agents[1]))
    results = await asyncio.gather(send(a), send(b))
    assert all(result.json()["send_allowed"] for result in results)
    assert (await finish(a, "0.2")).status_code == 200
    assert (await send(await prepare(agents[2]))).status_code == 429
    missing = await http.post(
        f"/api/tasks/{tid}/budget", headers=user, json={"additional_cost": "1", "currency": "USD"}
    )
    assert missing.status_code == 422
    request_a, request_b = str(uuid4()), str(uuid4())
    for ident, amount in ((request_a, "1"), (request_b, "1"), (request_a, "1.00")):
        increased = await http.post(
            f"/api/tasks/{tid}/budget",
            headers=user,
            json={
                "request_id": ident,
                "additional_cost": amount,
                "currency": "USD",
            },
        )
        assert increased.status_code == 200, increased.text
    conflict = await http.post(
        f"/api/tasks/{tid}/budget",
        headers=user,
        json={
            "request_id": request_a,
            "additional_cost": "2",
            "currency": "USD",
        },
    )
    assert conflict.status_code == 409
    assert (await finish(b, None, "unknown")).status_code == 200
    candidate = await prepare(agents[2])
    held = await send(candidate)
    assert held.status_code == 409, held.text

    async def terminal_state(terminal):
        async with app.state.engine.begin() as conn:
            await conn.execute(
                update(s.agent_runs)
                .where(s.agent_runs.c.task_id == tid)
                .values(status="finished" if terminal else "running")
            )
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    status="finished" if terminal else "running",
                    cleanup_ready=terminal,
                    workspace_uri=f"workspace/{tid}.tar.zst" if terminal else None,
                )
            )
            await conn.execute(
                update(s.legacy_execution_receipts)
                .where(
                    s.legacy_execution_receipts.c.task_id == tid,
                    s.legacy_execution_receipts.c.run_number == 1,
                )
                .values(
                    receipt={"phase": "exited" if terminal else "ready", "capture_closed": terminal}
                )
            )

    await terminal_state(True)
    with pytest.raises(HTTPException, match="原 Pi 调用尚未确认"):
        await app.state.board_service.resume(tid, uuid4(), Decimal(0), 10)
    await terminal_state(False)
    assert (await finish(b, "0.3")).status_code == 200
    assert (await send(candidate)).json()["send_allowed"] is True
    assert (await finish(candidate, None, "not_sent")).status_code == 200
    totals = await http.get(f"/api/tasks/{tid}/pi-fees", headers=user)
    assert totals.status_code == 200, totals.text
    assert Decimal(totals.json()["solver"]["estimated_usd"]) == Decimal("1.4")
    assert totals.json()["solver"]["unknown_calls"] == 0
    assert Decimal(totals.json()["threshold_usd"]) == Decimal(3)
    async with app.state.engine.connect() as conn:
        task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
        assert Decimal(task["usage"]["cost"]) == Decimal("1.4")
    await terminal_state(True)
    await app.state.board_service.resume(tid, uuid4(), Decimal(0), 10)
    async with app.state.engine.begin() as conn:
        resumed = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
        assert resumed["run_number"] == 2 and Decimal(resumed["usage"]["cost"]) == Decimal("1.4")
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(status="running"))
        await conn.execute(
            update(s.agent_runs)
            .where(s.agent_runs.c.task_id == tid)
            .values(status="running", started_at=started_at)
        )
    different_epoch = intent(
        tid, model, agents[0], started_at, logical=first["logical_call_id"], run_number=2
    )
    assert (
        await http.post(CALLS + "/prepare", headers=auth, json=different_epoch)
    ).status_code == 409


async def assert_runtime_uses_claim(http, tid, priced, claimed, sender, purpose, proof):
    runtime = RuntimeSettings.model_construct(
        service_token=SecretStr("service-test"),
        pi_runtime_sender_token=SecretStr(sender["X-BBX-Pi-Runtime"]),
        pi_provider_allowed_origins=(),
    )
    client = make_task_model_client(
        display_pi_model(PiModelVersion.model_validate(priced)),
        source=claimed,
        settings=runtime,
        service=BlackboardClient("http://bbx.test", "service-test", http_client=http),
        task_id=str(tid),
        purpose=purpose,
        proof=proof,
        explore_max_steps=1,
        conclude_grace_calls=0,
        max_duration_seconds=30,
    )
    assert client.trusted_model is not None
    assert client.trusted_model.reference.model_dump() == claimed["pi_model"]
    await client.close()


async def test_completed_unpriced_review_does_not_hold_solver(pi_app):
    app, http, tid, priced, agents, sender, started_at, user = pi_app
    editable = {
        key: value
        for key, value in priced.items()
        if key
        not in {
            "id",
            "model_revision",
            "has_secret",
        }
    }
    models = (await http.get("/api/platform/pi-models", headers=user)).json()["models"]
    for entry in models:
        if not entry["enabled"]:
            continue
        disabled = await http.put(
            f"/api/platform/pi-models/{entry['id']}",
            headers=user,
            json={
                **{
                    key: value
                    for key, value in entry.items()
                    if key not in {"id", "model_revision", "has_secret"}
                },
                "expected_revision": entry["model_revision"],
                "enabled": False,
            },
        )
        assert disabled.status_code == 200, disabled.text
    unpriced = await http.post(
        "/api/platform/pi-models",
        headers=user,
        json={
            **editable,
            "label": "Unpriced review",
            "usd_rates": None,
            "pricing_source": "",
            "secret": "fixture-only-unpriced-key",
        },
    )
    assert unpriced.status_code == 200, unpriced.text
    aid = agents[0]
    async with app.state.engine.begin() as conn:
        await conn.execute(
            update(s.agent_runs)
            .where(s.agent_runs.c.task_id == tid, s.agent_runs.c.id == aid)
            .values(status="finished")
        )
    mid = uuid4()
    submitted = await http.post(
        f"/api/tasks/{tid}/agents/{aid}/messages",
        headers=user,
        json={"id": str(mid), "content": "A synthetic read-only follow-up"},
    )
    assert submitted.status_code == 200, submitted.text
    claim = await http.post(
        f"/api/tasks/{tid}/agents/{aid}/messages/{mid}/claim",
        headers={
            "Authorization": "Bearer service-test",
            LEGACY_NETWORK_HEADER: LEGACY_NETWORK_VERSION,
        },
        json={"mode": "review"},
    )
    assert claim.status_code == 200, claim.text
    claimed = claim.json()
    ref = claimed["pi_model"]
    assert ref == {"model_id": unpriced.json()["id"], "model_revision": 1}
    assert "gateway_model" not in claimed
    call = intent(
        tid, {"id": ref["model_id"], "model_revision": ref["model_revision"]}, aid, started_at
    )
    call["proof"] = {
        "kind": "review",
        "agent_id": aid,
        "run_number": 1,
        "message_id": str(mid),
        "claim_token": str(claimed["claim_token"]),
    }
    await assert_runtime_uses_claim(http, tid, priced, claimed, sender, "review", call["proof"])
    unauthorized = await http.post(
        CALLS + "/prepare", headers={"Authorization": "Bearer service-test"}, json=call
    )
    assert unauthorized.status_code == 401
    prepared = await http.post(CALLS + "/prepare", headers=sender, json=call)
    assert prepared.status_code == 200, prepared.text
    attempt = {"attempt_id": call["attempt_id"], "request_sha256": "c" * 64}
    grant = await http.post(f"{CALLS}/{call['call_id']}/dispatch", headers=sender, json=attempt)
    assert grant.json()["send_allowed"] is True
    final = await http.post(
        f"{CALLS}/{call['call_id']}/settle",
        headers=sender,
        json={
            **attempt,
            "outcome": "complete",
            "amount_usd": None,
            "usage": {"input": 9, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 10},
        },
    )
    assert final.status_code == 200 and final.json()["state"] == "complete"
    costs = await http.get("/api/platform/pi-review-fees", headers=user)
    assert costs.status_code == 200 and costs.json()["unknown_calls"] == 1
    fees = (await http.get(f"/api/tasks/{tid}/pi-fees", headers=user)).json()
    assert fees["solver"] == {"estimated_usd": "0", "unknown_calls": 0}


async def test_new_review_prefers_original_model_id_after_price_revision(pi_app):
    app, http, tid, model, agents, _, _, user = pi_app
    data = {
        key: value
        for key, value in model.items()
        if key not in {"id", "model_revision", "has_secret"}
    }
    edited = await http.put(
        f"/api/platform/pi-models/{model['id']}",
        headers=user,
        json={
            **data,
            "label": "Updated display",
            "expected_revision": 1,
            "usd_rates": {"input": "2", "output": "1", "cache_read": "1", "cache_write": "1"},
        },
    )
    assert edited.status_code == 200 and edited.json()["model_revision"] == 2
    # A sorted-first alternate must not win merely because the original has a new revision.
    alternate = await app.state.pi_registry.save(
        "a-fixture-alternate",
        PiModelInput.model_validate(
            {
                **data,
                "label": "Alternate provider",
                "base_url": "https://other.test/v1",
                "secret": "fixture-alt-key",
            }
        ),
        "alice",
    )
    assert alternate.id < model["id"]
    aid = agents[0]
    async with app.state.engine.begin() as conn:
        await conn.execute(
            update(s.agent_runs)
            .where(s.agent_runs.c.task_id == tid, s.agent_runs.c.id == aid)
            .values(status="finished")
        )
    mid = uuid4()
    result = await http.post(
        f"/api/tasks/{tid}/agents/{aid}/messages",
        headers=user,
        json={"id": str(mid), "content": "Use the original provider after its price edit"},
    )
    assert result.status_code == 200, result.text
    claimed = await http.post(
        f"/api/tasks/{tid}/agents/{aid}/messages/{mid}/claim",
        headers={
            "Authorization": "Bearer service-test",
            LEGACY_NETWORK_HEADER: LEGACY_NETWORK_VERSION,
        },
        json={"mode": "review"},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["pi_model"] == {"model_id": model["id"], "model_revision": 2}


async def test_native_terminal_review_uses_confirmed_exit_without_workspace_tar(pi_app):
    from bbx_contracts.challenge_board import BOARD_CONTRACT

    app, http, _, priced, _, sender, _, user = pi_app
    settings = app.state.settings
    editable = {
        key: value
        for key, value in priced.items()
        if key not in {"id", "model_revision", "has_secret"}
    }
    unpriced = await http.put(
        f"/api/platform/pi-models/{priced['id']}",
        headers=user,
        json={
            **editable,
            "expected_revision": priced["model_revision"],
            "usd_rates": None,
            "pricing_source": "",
        },
    )
    assert unpriced.status_code == 200, unpriced.text
    model = PiModelVersion.model_validate(priced)
    ctf = app.state.ctf_service
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Review the retained Native task",
            "budget": {"currency": "USD", "max_cost": "1", "max_minutes": 10},
            "ctf_options": {"max_teammates": 1},
        },
        business_contract=BOARD_CONTRACT,
        pi_model=(settings.pi_model_authority, model),
    )
    operation_id, stamp = str(uuid4()), datetime.now(UTC)
    resources = s.task_runner_resources
    owners = s.runner_owners
    async with app.state.engine.begin() as conn:
        control = await conn.scalar(select(s.tasks.c.ctf_control).where(s.tasks.c.id == tid))
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                status="stopped", ctf_control={**control, "phase": "closed"}, cleanup_ready=False
            )
        )
        await conn.execute(
            insert(resources).values(
                task_id=tid,
                operation_id=operation_id,
                request_id=operation_id,
                request_digest="fixture",
                request={},
                controller_generation=1,
                revision=1,
                namespace="synthetic-retained",
                state="stopped",
                desired="stopped",
                hard_deadline=stamp,
                sandbox_create_claimed=True,
                tenant_key_hash="fixture-" + operation_id,
                tenant_key_ciphertext="fixture",
                observation={},
                data={"exit_confirmed": False},
                next_check_at=stamp,
                created_at=stamp,
            )
        )
        await conn.execute(
            insert(owners).values(
                task_id=tid,
                epoch=1,
                current=True,
                revoked=False,
                runner_id="fixture-runner",
                pod_uid="fixture-pod",
                token_version="fixture",
                expires_at=stamp,
                hard_deadline=stamp,
                registration_id=operation_id,
                registration_digest="fixture",
                next_seq=0,
            )
        )
    svc = {"Authorization": "Bearer service-test"}
    path = f"/api/tasks/{tid}/ctf/runtime"
    body = {"agent_id": "lead", "runtime_instance": "synthetic-native-review"}

    async def can_claim():
        pending = await http.get("/api/ctf/reviews/pending-tasks", headers=svc)
        assert pending.status_code == 200, pending.text
        claimed = await http.post(path + "/claim_review_turn", headers=svc, json=body)
        assert claimed.status_code == 200, claimed.text
        return str(tid) in pending.json()["task_ids"], claimed.json()

    mid = uuid4()
    submitted = await http.post(
        f"/api/tasks/{tid}/agents/lead/messages",
        headers=user,
        json={"id": str(mid), "content": "Review the saved records."},
    )
    assert submitted.status_code == 200, submitted.text
    assert await can_claim() == (False, None)
    async with app.state.engine.begin() as conn:
        await conn.execute(
            update(resources)
            .where(resources.c.task_id == tid)
            .values(data={"exit_confirmed": True})
        )
    assert await can_claim() == (False, None)
    async with app.state.engine.begin() as conn:
        await conn.execute(
            update(owners).where(owners.c.task_id == tid).values(current=False, revoked=True)
        )
    pending, turn = await can_claim()
    assert pending and turn["purpose"] == "review"
    assert turn["pi_model"]["model_revision"] == unpriced.json()["model_revision"]
    claim = await http.post(
        path + "/claim_messages",
        headers=svc,
        json={"agent_id": "lead", "turn_id": turn["id"], "generation": turn["generation"]},
    )
    assert claim.status_code == 200, claim.text
    proof = {
        "kind": "review",
        "agent_id": "lead",
        "run_number": 1,
        "turn_id": turn["id"],
        "generation": turn["generation"],
    }
    await assert_runtime_uses_claim(http, tid, priced, turn, sender, "review", proof)
    call = {
        "task_id": str(tid),
        "call_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "logical_call_id": "e" * 64,
        "context_sha256": "a" * 64,
        "action": "turn",
        **turn["pi_model"],
        "proof": proof,
    }
    prepared = await http.post(CALLS + "/prepare", headers=sender, json=call)
    assert prepared.status_code == 200 and prepared.json()["model"]["usd_rates"] is None
    dispatch = {"attempt_id": call["attempt_id"], "request_sha256": "b" * 64}
    call_path = f"{CALLS}/{call['call_id']}"
    async with app.state.engine.begin() as conn:
        await conn.execute(
            update(owners).where(owners.c.task_id == tid).values(current=True, revoked=False)
        )
    assert (
        await http.post(call_path + "/dispatch", headers=sender, json=dispatch)
    ).status_code == 409
    async with app.state.engine.begin() as conn:
        await conn.execute(
            update(owners).where(owners.c.task_id == tid).values(current=False, revoked=True)
        )
    allowed = await http.post(call_path + "/dispatch", headers=sender, json=dispatch)
    assert allowed.status_code == 200 and allowed.json()["send_allowed"] is True
    settled = await http.post(
        call_path + "/settle",
        headers=sender,
        json={
            **dispatch,
            "outcome": "complete",
            "usage": {"input": 3, "output": 2, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 5},
        },
    )
    assert settled.status_code == 200, settled.text
    async with app.state.engine.connect() as conn:
        record = (
            (await conn.execute(select(s.pi_model_calls).where(s.pi_model_calls.c.task_id == tid)))
            .mappings()
            .one()
        )
        assert record["purpose"] == "review" and record["amount_usd"] is None
        assert not await conn.scalar(select(s.tasks.c.cleanup_ready).where(s.tasks.c.id == tid))
