"""Durable publication and admission under PostgreSQL, with controlled native responses."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.gateway_maintenance import admission, handle_fence, observed_generation
from bbx_contracts.gateway_models import GatewayMaintenanceFence
from fastapi import HTTPException
from test_board_flags_integration import Objects
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url

pytestmark = pytest.mark.integration


async def test_maintenance_release_does_not_accept_stale_selection_or_late_prepare(ctf):
    engine = ctf.repo.engine
    old = await observed_generation(engine)
    request_id = uuid4()
    body = GatewayMaintenanceFence(request_id=request_id, site_id="primary", action="prepare")
    assert (await handle_fence(engine, body)).state == "idle"
    with pytest.raises(HTTPException, match="maintenance"):
        await observed_generation(engine)
    release = body.model_copy(update={"action": "release"})
    assert (await handle_fence(engine, release)).state == "released"
    assert await observed_generation(engine) == old + 1
    assert (await handle_fence(engine, body)).state == "released"
    async with engine.begin() as conn:
        with pytest.raises(HTTPException, match="changed while selecting"):
            await admission(conn, old)


async def test_review_fee_never_enters_native_solver_total(ctf):
    from datetime import UTC, datetime
    from decimal import Decimal

    from bbx_blackboard.gateway_execution import review_cost_ledger
    from bbx_blackboard.store import schema as s
    from sqlalchemy import insert

    engine = ctf.repo.engine
    app = create_app(settings(), engine=engine, objects=Objects())
    native = app.state.runner_gateway.native
    tid, review = uuid4(), uuid4()
    attributes = {
        "bbx_site_id": "primary",
        "bbx_task_id": str(tid),
        "bbx_agent_id": "lead",
        "bbx_conversation_id": str(uuid4()),
        "bbx_model_alias": "fixture-route",
        "bbx_purpose": "review",
    }
    async with engine.begin() as conn:
        for log_id, attempt, amount, attrs in (
            ("solver-native", str(uuid4()), "0.05", {}),
            ("review-native", str(review), "0.02", attributes),
        ):
            await conn.execute(
                insert(s.native_request_costs).values(
                    accounting_source_id="fixture",
                    native_log_id=log_id,
                    task_id=tid,
                    attempt_id=attempt,
                    state="known",
                    cost_usd=amount,
                    evidence={"attributes": attrs},
                    created_at=datetime.now(UTC),
                )
            )
        for aid, native_log, fee_state in (
            (review, "review-native", "known"),
            (uuid4(), None, "unknown"),
        ):
            await conn.execute(
                insert(s.gateway_execution_attempts).values(
                    task_id=tid,
                    id=aid,
                    purpose="review",
                    accounting_source_id="fixture",
                    key_name="shared-review",
                    body_sha256="a" * 64,
                    attributes=attributes,
                    phase="complete",
                    native_log_id=native_log,
                    fee_state=fee_state,
                )
            )
        assert await native._total(conn, tid) == Decimal("0.05")
    review_fees = await review_cost_ledger(engine, "primary")
    assert review_fees["confirmed_usd"] == "0.02"
    assert review_fees["pending"] == 1


async def test_delayed_budget_retry_after_another_update_has_no_effect(ctf):
    from decimal import Decimal

    from bbx_blackboard.gateway_budgets import GatewayBudgets
    from bbx_blackboard.store import schema as s
    from sqlalchemy import select, update

    engine = ctf.repo.engine
    app = create_app(settings(), engine=engine, objects=Objects())
    service = app.state.board_service
    budgets = app.state.gateway_execution.budgets
    tid = await service.create_task(
        {
            "goal": "Original USD budget",
            "agent_profile": "default",
            "acceptance": [{"id": "A1", "desc": "Retain budget"}],
            "budget": {"currency": "USD", "max_cost": "0.09", "max_minutes": 10},
        }
    )
    first, second = uuid4(), uuid4()
    async with engine.begin() as conn:
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(status="running"))
        for key, total in ((first, "0.07"), (second, "0.09")):
            await GatewayBudgets.save_request(
                conn,
                tid,
                "ordinary",
                key,
                {
                    "request_id": str(key),
                    "additional_usd": "0.02",
                    "threshold_usd": total,
                    "platform_payload": {"operation": "adjust", "minutes": None, "currency": "USD"},
                },
            )
    budgets.admin.update_key = AsyncMock(side_effect=AssertionError("must not mutate a replay"))
    await budgets.increase(tid, "ordinary", first, Decimal("0.02"), Decimal("0.09"))
    assert (
        await service.adjust_budget(
            tid,
            Decimal("0.02"),
            None,
            request_id=first,
            gateway_generation=await observed_generation(engine),
        )
        == []
    )
    async with engine.connect() as conn:
        frozen = await conn.scalar(select(s.tasks.c.budget).where(s.tasks.c.id == tid))
    assert frozen["currency"] == "USD" and Decimal(frozen["max_cost"]) == Decimal("0.09")


async def test_user_may_review_finished_agent_on_running_task_but_not_change_incarnation(ctf):
    from unittest.mock import AsyncMock

    import httpx
    from bbx_blackboard.auth import issue_user_token
    from bbx_blackboard.store import schema as s
    from bbx_contracts.gateway_models import (
        GatewayModelSnapshot,
        GatewayUsdRates,
        ReviewGatewayAttempt,
    )
    from sqlalchemy import update

    engine = ctf.repo.engine
    options = settings().model_copy(update={"gateway_site_id": "primary"})
    app = create_app(options, engine=engine, objects=Objects())
    service = app.state.board_service
    tid = await service.create_task(
        {
            "goal": "Active Task with a finished Agent",
            "agent_profile": "default",
            "acceptance": [{"id": "A1", "desc": "Finish a result"}],
            "budget": {"currency": "USD", "max_cost": "0.05", "max_minutes": 10},
        }
    )
    async with engine.begin() as conn:
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(status="running"))
    aid = await service.register_agent(tid, "explore", is_seed=True)
    model = GatewayModelSnapshot(
        alias="fixture-route",
        label="Gateway model",
        catalog_revision="a" * 64,
        accounting_source_id="fixture",
        route_revision=1,
        route_digest="b" * 64,
        model_catalog_digest="c" * 64,
        context_window=32000,
        max_output_tokens=1000,
        input_modalities=["text"],
        tool_call=True,
        usd_rates=GatewayUsdRates.model_validate(
            {"input": "1", "output": "2", "cache_read": "0", "cache_write": "0"}
        ),
    )
    app.state.gateway_models.list_active = AsyncMock(return_value=[model])
    app.state.gateway_execution.review_key = AsyncMock(return_value=("isolated-test-key", "review"))
    user = {"Authorization": f"Bearer {issue_user_token(options, 'alice')}"}
    service_auth = {"Authorization": "Bearer " + options.service_token.get_secret_value()}
    live, review = uuid4(), uuid4()
    path = f"/api/tasks/{tid}/agents/{aid}/messages"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(path, headers=user, json={"id": str(live), "content": "Live input"})
        ).status_code == 200
        async with engine.begin() as conn:
            await conn.execute(
                update(s.agent_runs)
                .where(s.agent_runs.c.task_id == tid, s.agent_runs.c.id == aid)
                .values(status="finished")
            )
        denied = await client.post(
            path, headers=service_auth, json={"id": str(review), "content": "Automated review"}
        )
        assert denied.status_code == 403
        explicit = await client.post(
            path, headers=user, json={"id": str(review), "content": "What did you conclude?"}
        )
        assert explicit.status_code == 200
    with pytest.raises(HTTPException):
        await app.state.conversations.claim(tid, aid, live, "review")
    receipt = await app.state.conversations.claim(tid, aid, review, "review")
    request = ReviewGatewayAttempt(
        alias=model.alias,
        catalog_revision=model.catalog_revision,
        attempt_id=uuid4(),
        body_sha256="d" * 64,
        agent_id=aid,
        run_number=1,
        message_id=review,
        claim_token=receipt["claim_token"],
    )
    async with engine.begin() as conn:
        original = await app.state.gateway_execution._review(conn, tid, request)
        assert original == (str(review), model.model_dump(mode="json"))
        await conn.execute(
            update(s.agent_runs)
            .where(s.agent_runs.c.task_id == tid, s.agent_runs.c.id == aid)
            .values(derive_round=2)
        )
        with pytest.raises(HTTPException):
            await app.state.gateway_execution._review(conn, tid, request)


async def test_stopped_ctf_member_keeps_original_generation_across_read_only_turns(ctf):
    from bbx_blackboard.gateway_tasks import review_source
    from bbx_blackboard.store import schema as s
    from bbx_blackboard.store.repository import row
    from bbx_contracts.gateway_models import GatewayModelSnapshot
    from sqlalchemy import insert, update
    from test_ctf_integration import team

    tid = await team(ctf)
    member = await ctf.create_member(tid, "user", "Analyst", str(uuid4()))
    aid = member["id"]
    engine = ctf.repo.engine
    async with engine.begin() as conn:
        task = await row(conn, s.tasks, tid)
    assert task["status"] == "running"
    assert await review_source(engine, task, aid) is None  # Active/idle is not a finished member.
    async with engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid)
            .values(lifecycle="stopped", run_state="stopped", generation=8)
        )
    source = await review_source(engine, task, aid)
    assert source == {"generation": 8, "run_number": 1}
    model = GatewayModelSnapshot.model_validate(
        {
            "alias": "fixture-ctf",
            "label": "Fixture",
            "catalog_revision": "a" * 64,
            "accounting_source_id": "fixture",
            "route_revision": 1,
            "route_digest": "b" * 64,
            "model_catalog_digest": "c" * 64,
            "context_window": 32000,
            "max_output_tokens": 1000,
            "input_modalities": ["text"],
            "tool_call": True,
            "usd_rates": {"input": "1", "output": "2", "cache_read": "0", "cache_write": "0"},
        }
    )
    first, second = str(uuid4()), str(uuid4())
    for message in (first, second):
        await ctf.post_message(
            tid,
            "user",
            aid,
            "What did you find?",
            message,
            review_model=model,
            review_origin=source,
        )
    turn = await ctf.claim_review_turn(tid, aid, "isolated-review")
    assert turn["generation"] == 9 and turn["review_origin_generation"] == 8
    messages = await ctf.claim_messages(tid, aid, turn["id"], turn["generation"])
    assert [item["id"] for item in messages] == [first]
    async with engine.begin() as conn:
        await ctf.repo.lock(conn, tid)
        await conn.execute(
            insert(s.gateway_execution_attempts).values(
                task_id=tid,
                id=uuid4(),
                purpose="review",
                accounting_source_id="fixture",
                key_name="review",
                body_sha256="d" * 64,
                attributes={"bbx_conversation_id": turn["id"]},
                phase="dispatched",
            )
        )
        current = await row(conn, s.ctf_members, tid, aid)
        await ctf._settle_review(conn, tid, current, turn, "interrupted", "")
    assert await review_source(engine, task, aid) == source
    later = await ctf.claim_review_turn(tid, aid, "isolated-review")
    assert later["generation"] == 10 and later["assignment_message_ids"] == [second]
    async with engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid)
            .values(lifecycle="active", run_state="running", generation=11)
        )
    with pytest.raises(HTTPException):
        await ctf.authorize_review(tid, aid, later["generation"], later["id"])
