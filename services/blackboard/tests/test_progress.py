"""Progress security and graph invariants without external services."""

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_progress_token
from bbx_blackboard.progress import ProgressService, apply_candidate, fold_sources, initial
from bbx_blackboard.settings import Settings
from bbx_contracts.models import ModelConfig, Price
from bbx_contracts.progress import ProgressCandidate, ProgressNode, ProgressReserve
from fastapi import HTTPException
from pydantic import SecretStr


def settings():
    return Settings.model_construct(
        postgres_password=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        service_token=SecretStr("service-test"),
        agent_token_secret=SecretStr("a-long-test-signing-secret-at-least-32"),
        admin_users=SecretStr("admin:test"),
    )


def tree():
    return initial({"goal": "Find the cause", "run_number": 1})


def candidate(**kwargs):
    return ProgressCandidate(claim_token=uuid4(), run_number=1, expected_revision=0, **kwargs)


async def test_reservation_uses_frozen_model_limit_instead_of_legacy_role_setting(monkeypatch):
    @asynccontextmanager
    async def begin():
        yield object()

    service = ProgressService(SimpleNamespace(begin=begin))
    model = ModelConfig(
        provider="openai_compatible",
        model="synthetic",
        base_url="https://offline.invalid",
        reasoning_effort="none",
        max_output_tokens=500_000,
        price=Price(
            currency="CNY",
            cache_hit_per_m=Decimal("1"),
            cache_miss_per_m=Decimal("2"),
            output_per_m=Decimal("3"),
            off_peak=False,
        ),
    ).model_dump(mode="json")
    job = {"data": {"model": model, "settings": {"max_output_tokens": 8192}, "receipts": {}}}
    state = {
        "settings": {"enabled": True, "max_cost": "10"},
        "cost": "0",
        "reserved": "0",
        "unknown": "0",
    }
    monkeypatch.setattr(service, "job", AsyncMock(return_value=({}, job)))
    monkeypatch.setattr(service, "state", AsyncMock(return_value=state))
    monkeypatch.setattr(service, "save_job", AsyncMock())
    monkeypatch.setattr(service, "save", AsyncMock())
    body = ProgressReserve(
        claim_token=uuid4(),
        run_number=1,
        request_id=uuid4(),
        input_tokens=1000,
        max_output_tokens=500_000,
    )
    receipt = await service.reserve(uuid4(), uuid4(), body)
    assert Decimal(receipt["reserved"]) == Decimal("1.502")
    assert receipt["args"]["max_output_tokens"] == model["max_output_tokens"]
    assert job["data"]["settings"]["max_output_tokens"] == 8192
    job["data"]["receipts"].clear()
    with pytest.raises(HTTPException, match="model configuration"):
        await service.reserve(
            uuid4(),
            uuid4(),
            body.model_copy(update={"request_id": uuid4(), "max_output_tokens": 500_001}),
        )


def test_incremental_candidates_keep_stable_nodes_and_reject_cycles():
    first = apply_candidate(
        tree(),
        candidate(
            upserts=[
                ProgressNode(
                    id="direction",
                    parent_id="goal",
                    kind="direction",
                    title="Explain input behavior",
                    question="Which inputs fail?",
                )
            ]
        ),
    )
    second = apply_candidate(
        first,
        candidate(
            upserts=[
                ProgressNode(
                    id="gap",
                    parent_id="direction",
                    kind="gap",
                    title="Boundary remains unknown",
                    next_step="Test the oldest input",
                )
            ]
        ),
    )
    assert {node["id"] for node in second["nodes"]} == {"goal", "direction", "gap"}
    with pytest.raises(HTTPException, match="Tree cycle"):
        apply_candidate(
            second,
            candidate(
                upserts=[
                    ProgressNode(id="direction", parent_id="gap", kind="direction", title="Cycle")
                ]
            ),
        )
    with pytest.raises(HTTPException, match="Orphan"):
        apply_candidate(second, candidate(removed_ids=["direction"]))
    with pytest.raises(HTTPException, match="require sources"):
        apply_candidate(
            second,
            candidate(
                upserts=[
                    ProgressNode(
                        id="finding", parent_id="goal", kind="finding", title="An unsupported claim"
                    )
                ]
            ),
        )
    with pytest.raises(HTTPException, match="revision"):
        apply_candidate({**second, "revision": 2}, candidate())


def test_graph_limits_and_single_root():
    nodes = [ProgressNode(id="goal", kind="goal", title="Root")]
    for i in range(1, 9):
        nodes.append(
            ProgressNode(
                id=f"n{i}",
                parent_id="goal" if i == 1 else f"n{i - 1}",
                kind="direction",
                title=str(i),
            )
        )
    with pytest.raises(HTTPException, match="depth"):
        apply_candidate(tree(), candidate(nodes=nodes))
    with pytest.raises(HTTPException, match="one goal root"):
        apply_candidate(
            tree(), candidate(upserts=[ProgressNode(id="another", kind="goal", title="Root")])
        )


def test_historical_fold_never_uses_future_status_or_current_holder():
    events = [
        {
            "version": 1,
            "type": "fact.posted",
            "payload": {"id": "F1", "statement": "Known", "author": "a"},
        },
        {
            "version": 2,
            "type": "intent.posted",
            "payload": {"id": "I1", "statement": "Try", "author": "a"},
        },
        {"version": 3, "type": "intent.claimed", "payload": {"intent_id": "I1", "holder": "b"}},
        {"version": 4, "type": "fact.disputed", "payload": {"fact_id": "F1", "by": "F2"}},
    ]
    old = fold_sources(events[:2])
    current = fold_sources(events)
    assert old["F1"]["status"] == "proposed" and current["F1"]["status"] == "disputed"
    assert old["I1"]["holder"] is None and current["I1"]["holder"] == "b"
    assert current["I1"]["author"] == "a"


def test_unknown_reservations_continue_to_occupy_budget():
    state = {"cost": "0.5", "reserved": "0.75", "unknown": "0"}
    data = {"receipts": {"r": {"status": "reserved", "reserved": "0.75"}}}
    ProgressService.unknown_reservations(state, data)
    ProgressService.unknown_reservations(state, data)
    assert state == {"cost": "0.5", "reserved": "0.00", "unknown": "0.75"}
    assert ProgressService.occupied(state) == Decimal("1.25")


@pytest.mark.asyncio
async def test_progress_capability_denies_every_solver_write_and_unbounded_read():
    config = settings()
    tid, jid, lease = uuid4(), uuid4(), uuid4()
    app = create_app(config)
    token = issue_progress_token(config, tid, jid, lease, 1)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as client:
        for path in ("facts", "intents", "stop", "budget", "agents/a/claim", "agents/a/session"):
            result = await client.post(f"/api/tasks/{tid}/{path}", headers=headers, json={})
            assert result.status_code in {403, 404, 405, 422}
            # Existing routes must reject capabilities before reaching any store.
            if path in {"facts", "intents", "stop", "budget"}:
                assert result.status_code == 403
        assert (await client.get(f"/api/tasks/{tid}/state", headers=headers)).status_code == 403
        assert (
            await client.get(f"/api/tasks/{uuid4()}/progress", headers=headers)
        ).status_code == 403
        assert (await client.get("/api/progress/settings", headers=headers)).status_code == 403
        assert (await client.post("/api/progress/jobs/claim", headers=headers)).status_code == 403
        assert (
            await client.post(
                f"/api/tasks/{tid}/progress/jobs/{jid}/checkpoint",
                headers=headers,
                json={
                    "claim_token": str(lease),
                    "run_number": 1,
                    "session": {},
                    "expected_revision": 0,
                    "source_version": 0,
                },
            )
        ).status_code == 403


def test_disabled_tasks_have_free_skeleton_and_no_hidden_budget():
    value = tree()
    assert not value["settings"]["enabled"]
    assert value["cost"] == "0" and value["revision"] == 0
    assert [n["kind"] for n in value["nodes"]] == ["goal"]
