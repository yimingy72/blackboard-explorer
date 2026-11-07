"""Real PostgreSQL, native gateway, Runtime and Pi; all model responses are synthetic."""

import asyncio
import json
from contextlib import AsyncExitStack, suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.native_admin import NativeAdmin
from bbx_blackboard.store import schema as s
from bbx_contracts.native_gateway import TRUSTED_ATTRIBUTE_HEADERS
from bbx_contracts.runner import PROTOCOL, runner_json_text
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.runner_proxy import RunnerModelProxy
from bbx_runtime.settings import Settings as RuntimeSettings
from fastapi import HTTPException
from mcp_gateway_support import native_business_gateway
from native_gateway_support import native_gateway
from runner_control_support import live_http, node_runner
from runner_gateway_support import (
    admit,
    attempt,
    call_record,
    cap,
    digest,
    dispatch,
    prepare,
    private,
    worker_scope,
)
from sqlalchemy import select, update
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_runner_control_integration import (
    SERVICE,
    auth,
    control,
    mgmt,
    node_data,
    poll_query,
    post_input,
    wait_record,
)

pytestmark = pytest.mark.integration


async def provision_native(
    app,
    http,
    scope,
    native,
    *,
    lose_ack=False,
    expected_usd="0.005000000",
    create_entered=None,
    create_release=None,
):
    fees = app.state.runner_gateway.native
    fees.settings = fees.settings.model_copy(
        update={
            "native_gateway_accounting_source_id": "test-persistent-native-db",
            "native_gateway_admin_url": native["admin"],
            "native_gateway_data_url": native["data"],
            "native_gateway_catalog_revision": "fixture-rates-and-limits-v1",
        }
    )
    await fees.admin.close()
    fees.admin = NativeAdmin(fees.settings)
    body = {
        "expected_epoch": 1,
        "accounting_source_id": "test-persistent-native-db",
        "selected_models": ["bounded-fake"],
        "exchange_rate": {
            "rate_cny_per_usd": "7",
            "source_ref": "fixture://frozen-fx",
            "observed_at": datetime.now(UTC).isoformat(),
        },
    }
    response = await http.post(
        private(scope) + "/native-budget/prepare", headers=SERVICE, json=body
    )
    assert response.status_code == 200, response.text
    assert response.json()["threshold_usd"] == expected_usd
    calls = 0
    original = fees.admin.create_key

    async def create(value):
        nonlocal calls
        calls += 1
        if create_entered is not None:
            assert create_release is not None
            create_entered.set()
            await asyncio.wait_for(create_release.wait(), 5)
        result = await original(value)
        if lose_ack:
            raise HTTPException(503, "Synthetic creation ACK loss")
        return result

    fees.admin.create_key = create
    response = await http.post(private(scope) + "/native-budget/publish", headers=SERVICE)
    if lose_ack:
        assert response.status_code == 503
        # The row containing the encrypted original key predates the only native write.
        async with fees.repo.engine.connect() as conn:
            saved = (
                (
                    await conn.execute(
                        select(s.native_task_budgets).where(
                            s.native_task_budgets.c.task_id == scope["tid"]
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert saved["publication_state"] == "creating_unknown"
        assert saved["key_ciphertext"] and "bbx-task-" not in saved["key_ciphertext"]
        response = await http.post(private(scope) + "/native-budget/publish", headers=SERVICE)
    assert response.status_code == 200, response.text
    assert response.json()["publication_state"] == "ready" and calls == 1
    assert "key_ciphertext" not in response.json() and "task_gateway_key" not in response.json()
    model = await http.get(
        private(scope) + "/native-budget/model",
        headers=SERVICE,
        params={"model_alias": "bounded-fake"},
    )
    assert model.status_code == 200, model.text
    assert model.json()["sdk_model"]["context_window"] == 16384
    return fees, response.json()


async def settle(fees, tid, expected):
    async with asyncio.timeout(12):
        while True:
            await fees.consume(tid)
            view = await fees.view(tid)
            if Decimal(view["confirmed_usd"]) == Decimal(expected):
                return view
            await asyncio.sleep(0.05)


async def test_native_key_recovery_real_runner_fees_fx_threshold_and_no_retry(
    ctf, tmp_path, record_property
):
    from test_runner_control_integration import connect

    async with native_gateway(tmp_path / "gateway") as native, connect(ctf) as (app, http):
        scope = await prepare(http, ctf, max_cost="0.056", native=False, tools=["board_challenges"])
        fees, published = await provision_native(
            app, http, scope, native, lose_ack=True, expected_usd="0.008000000"
        )
        tid, bid = scope["tid"], scope["binding"]["binding_id"]
        async with (
            app.state.business_mcp.run(),
            live_http(app, "/unused") as (base, _),
            AsyncExitStack() as mcp_stack,
        ):
            mcp_url = await mcp_stack.enter_async_context(
                native_business_gateway(tmp_path / "business", app, base, [tid])
            )
            service = BlackboardClient(base, "service-test")
            proxy = RunnerModelProxy(
                RuntimeSettings.model_construct(
                    runner_gateway_url=native["data"], runner_proxy_max_inflight=4
                ),
                service,
            )
            try:
                async with live_http(proxy.app, "/unused") as (proxy_url, _):
                    data = await node_data(ctf, tid, scope["owner"], base)
                    data["model_proxy_url"] = proxy_url
                    data["mcp_gateway_url"] = mcp_url + f"/api/tasks/{tid}/mcp"
                    data["gateway_credentials"] = {
                        bid: {
                            "model": scope["credentials"]["model"],
                            "mcp": scope["credentials"]["mcp"],
                        }
                    }
                    async with node_runner(data, tmp_path) as node:
                        registered = await wait_record(
                            ctf, s.runner_bindings, tid, bid, status="prepared"
                        )
                        message = await post_input(
                            http, ctf, tid, scope["owner"], scope["binding"], "native fee fixture"
                        )
                        await wait_record(
                            ctf,
                            s.runner_commands,
                            tid,
                            "1:" + message["request_id"],
                            status="applied",
                        )
                        try:
                            async with asyncio.timeout(12):
                                while True:
                                    async with ctf.repo.engine.connect() as conn:
                                        before_fees = (
                                            (
                                                await conn.execute(
                                                    select(s.runner_calls).where(
                                                        s.runner_calls.c.task_id == tid
                                                    )
                                                )
                                            )
                                            .mappings()
                                            .all()
                                        )
                                    models = [
                                        item for item in before_fees if item["kind"] == "model"
                                    ]
                                    tools = [item for item in before_fees if item["kind"] == "tool"]
                                    if (
                                        len(models) == 2
                                        and len(tools) == 1
                                        and tools[0]["state"] == "completed"
                                        and all(not item["concurrency_active"] for item in models)
                                    ):
                                        break
                                    await asyncio.sleep(0.02)
                            assert all(
                                item["data"]["fee_state"] == "awaiting_log" for item in models
                            )
                            first = await settle(fees, tid, "0.006")
                        except TimeoutError:
                            async with ctf.repo.engine.connect() as conn:
                                debug = (
                                    (
                                        await conn.execute(
                                            select(
                                                s.runner_calls.c.id,
                                                s.runner_calls.c.state,
                                                s.runner_calls.c.data,
                                            ).where(s.runner_calls.c.task_id == tid)
                                        )
                                    )
                                    .mappings()
                                    .all()
                                )
                            raise AssertionError(
                                {
                                    "provider_calls": len(await native["calls"]()),
                                    "runner": await node.command("status"),
                                    "calls": [dict(item) for item in debug],
                                }
                            ) from None
                        assert first["converted_cny"] == "0.042" and first["hold_reason"] is None
                        # Readback preserves the original credential and native resource.
                        assert (await fees.publish(tid))["native_resource_id"] == published[
                            "native_resource_id"
                        ]
                        async with ctf.repo.engine.connect() as conn:
                            call = (
                                (
                                    await conn.execute(
                                        select(s.runner_calls).where(
                                            s.runner_calls.c.task_id == tid,
                                            s.runner_calls.c.kind == "model",
                                        )
                                    )
                                )
                                .mappings()
                                .first()
                            )
                        assert "body_json" not in call[
                            "data"
                        ] and "native fee fixture" not in json.dumps(call["data"])
                        assert registered["sdk_session_id"] == call["data"]["sdk_session_id"]
                        await node.command("stop")
                        # A Worker summary shares the Task key, with its own smaller role cap.
                        current = await worker_scope(http, ctf, scope, output_cap=64)
                        worker_bid = current["binding"]["binding_id"]
                        second = attempt(current, purpose="summary")
                        oversized = {
                            **second,
                            "model_snapshot": {
                                **second["model_snapshot"],
                                "max_output_tokens": 129,
                            },
                        }
                        denied = await http.post(
                            f"/api/tasks/{tid}/runner-model/attempts/admit",
                            headers=cap(current),
                            content=runner_json_text(oversized).encode(),
                        )
                        assert denied.status_code == 403
                        admission = await http.post(
                            f"/api/tasks/{tid}/runner-model/attempts/admit",
                            headers=cap(current),
                            content=runner_json_text(second).encode(),
                        )
                        assert admission.status_code == 200, admission.text
                        headers = {
                            **cap(current),
                            "x-bbx-task-id": str(tid),
                            "x-bbx-binding-id": worker_bid,
                            "x-bbx-runner-id": scope["owner"]["fence"]["runner_id"],
                            "x-bbx-pod-uid": scope["owner"]["fence"]["pod_uid"],
                            "x-bbx-runtime-epoch": "1",
                            "x-bbx-sdk-session-id": current["sdk"],
                            "x-bbx-call-id": second["call_id"],
                            "x-bbx-attempt-id": second["attempt_id"],
                            "x-bbx-body-digest": second["body_digest"],
                            "x-bbx-agent-id": "forged",
                        }
                        async with httpx.AsyncClient(trust_env=False) as client:
                            responses = await asyncio.gather(
                                *(
                                    client.post(
                                        proxy_url + "/chat/completions",
                                        headers=headers,
                                        content=second["body_json"].encode(),
                                    )
                                    for _ in range(2)
                                )
                            )
                        assert sorted(response.status_code for response in responses) == [200, 409]
                        final = await settle(fees, tid, "0.009")
                        assert (
                            final["hold_reason"] == "budget_exceeded"
                            and final["overage_usd"] == "0.001000000"
                        )
                        assert len(await native["calls"]()) == 3
                        # Rereading native IDs never doubles fees or releases a hold.
                        await fees.consume(tid)
                        assert (await fees.view(tid))["confirmed_usd"] == "0.009"
                        async with ctf.repo.engine.connect() as conn:
                            rows = (
                                (
                                    await conn.execute(
                                        select(s.native_request_costs).where(
                                            s.native_request_costs.c.task_id == tid
                                        )
                                    )
                                )
                                .mappings()
                                .all()
                            )
                        assert len(rows) == 3 and len({row["native_log_id"] for row in rows}) == 3
                        assert {row["evidence"]["attributes"]["bbx_agent_id"] for row in rows} == {
                            "lead",
                            current["binding"]["agent_id"],
                        }
                        blocked = await http.post(
                            f"/api/tasks/{tid}/runner-model/attempts/admit",
                            headers=cap(current),
                            content=runner_json_text(attempt(current)).encode(),
                        )
                        assert blocked.status_code == 409
                        resources = await fees.admin.resources()
                        original = next(
                            item
                            for item in resources
                            if item["id"] == published["native_resource_id"]
                        )
                        try:
                            await NativeAdmin.create_key(fees.admin, original["value"])
                            duplicate_accepted = True
                        except HTTPException:
                            duplicate_accepted = False
                        matching = [
                            item
                            for item in await fees.admin.resources()
                            if item["value"].get("keyHash") == original["value"]["keyHash"]
                        ]
                        record_property("native_duplicate_key_resources", len(matching))
                        assert len(matching) == (2 if duplicate_accepted else 1)
                        if duplicate_accepted:
                            assert (
                                await http.post(
                                    private(scope) + "/native-budget/publish", headers=SERVICE
                                )
                            ).status_code == 409
                        original_settings = fees.settings
                        fees.settings = fees.settings.model_copy(
                            update={"native_gateway_accounting_source_id": ""}
                        )
                        assert (await fees.view(tid))["confirmed_usd"] == "0.009"
                        assert (
                            await http.get(f"/api/tasks/{tid}/runner-model/fees", headers=SERVICE)
                        ).status_code == 200
                        fees.settings = original_settings
            finally:
                await proxy.close()
                await service.close()
                await fees.admin.close()


async def test_native_unknown_missing_observation_status_rollback_and_worker_survival(
    ctf, tmp_path
):
    from test_runner_control_integration import connect

    async with native_gateway(tmp_path / "gateway") as native, connect(ctf) as (app, http):
        scope = await prepare(http, ctf, max_cost="0.035")
        fees, _ = await provision_native(app, http, scope, native)
        try:
            body = attempt(scope, marker="no_usage")
            assert (await admit(http, scope, body)).status_code == 200
            grant = (await dispatch(http, scope, body)).json()
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(
                    native["data"] + "/v1/chat/completions",
                    headers={
                        "authorization": "Bearer " + grant["task_gateway_key"],
                        **{
                            TRUSTED_ATTRIBUTE_HEADERS[key]: value
                            for key, value in grant["trusted_attributes"].items()
                        },
                    },
                    content=body["body_json"].encode(),
                )
            assert response.status_code == 200
            async with asyncio.timeout(8):
                while True:
                    await fees.consume(scope["tid"])
                    view = await fees.view(scope["tid"])
                    if view["hold_reason"] == "native_fee_pending":
                        break
                    await asyncio.sleep(0.05)
            assert Decimal(view["confirmed_usd"]) == 0 and view["pending_attempts"] == 1
            record = await call_record(ctf, scope["tid"], "model:" + body["attempt_id"])
            assert record["state"] == "unknown" and record["data"]["fee_state"] == "unknown"
            conflicting = await http.post(
                private(scope) + f"/model/{body['attempt_id']}/transport",
                headers=SERVICE,
                json={
                    "protocol": PROTOCOL,
                    "attempt_id": body["attempt_id"],
                    "dispatch_id": grant["dispatch_id"],
                    "phase": "not_sent",
                    "http_status": None,
                },
            )
            assert conflicting.status_code == 409
            payload = runner_json_text(
                {"kind": "close", "binding_id": scope["binding"]["binding_id"]}
            )
            close = await http.post(
                mgmt(scope["tid"]) + "/commands",
                headers=SERVICE,
                json={
                    "request_id": str(uuid4()),
                    "expected_epoch": 1,
                    "payload_json": payload,
                    "payload_digest": digest(payload),
                },
            )
            assert close.status_code == 200, close.text
            commands = await http.get(
                control(scope["tid"]) + "/commands",
                headers=auth(scope["owner"]),
                params=poll_query(scope["owner"]),
            )
            assert commands.status_code == 200, commands.text
            assert commands.json()["lease_renewals"] == []
            assert all(
                json.loads(item["payload_json"])["kind"] == "close"
                for item in commands.json()["commands"]
            )
            assert (
                await http.post(
                    mgmt(scope["tid"]) + "/owners/renew",
                    headers=SERVICE,
                    json={"expected_epoch": 1},
                )
            ).status_code == 200
            observation = {
                "protocol": PROTOCOL,
                "fence": scope["owner"]["fence"],
                "binding_id": scope["binding"]["binding_id"],
                "sdk_session_id": scope["sdk"],
                "call_id": body["call_id"],
                "attempt_id": body["attempt_id"],
                "outcome": "completed",
                "model_outcome": "completed",
                "usage": {"cost": "999"},
                "http_status": 200,
            }
            assert (
                await http.post(
                    f"/api/tasks/{scope['tid']}/runner-model/attempts/{body['attempt_id']}/observation",
                    headers=cap(scope),
                    json=observation,
                )
            ).status_code == 200
            assert Decimal((await fees.view(scope["tid"]))["confirmed_usd"]) == 0
            logs, log_count = fees.admin.logs, 0

            async def counted_logs(attributes: dict, cursor: str | None = None, limit: int = 100):
                nonlocal log_count
                log_count += 1
                return await logs(attributes, cursor, limit)

            fees.admin.logs = counted_logs
            for _ in range(3):
                await fees.consume(scope["tid"])
            assert log_count == 0
            budget_method = fees.admin.budget
            status = await budget_method("bbx-task-" + str(scope["tid"]))
            high = json.loads(json.dumps(status))
            high["budgets"][0]["usage"].update(used="0.006", exceeded=True)

            async def high_status(key_name: str):
                return high

            fees.admin.budget = high_status
            await fees.consume(scope["tid"])
            fees.admin.budget = budget_method
            await fees.consume(scope["tid"])
            assert (await fees.view(scope["tid"]))["native_used_high_water_usd"] == "0.006"
            assert Decimal((await fees.view(scope["tid"]))["confirmed_usd"]) == 0
            # A malformed source pauses this Task, while the shared consumer keeps running.
            invalid = json.loads(json.dumps(status))
            invalid["budgets"][0]["usage"].pop("used")

            async def invalid_status(key_name: str):
                return invalid

            fees.admin.budget = invalid_status
            instant = fees.clock()
            fees.clock = lambda: instant + timedelta(seconds=61)
            held, hold = asyncio.Event(), fees.hold

            async def observe_hold(tid, reason, **kwargs):
                await hold(tid, reason, **kwargs)
                held.set()

            fees.hold = observe_hold
            consumer = asyncio.create_task(fees.run())
            try:
                await asyncio.wait_for(held.wait(), 3)
                assert not consumer.done()
            finally:
                consumer.cancel()
                with suppress(asyncio.CancelledError):
                    await consumer
            fees.hold, fees.admin.budget = hold, budget_method
            fees.clock = lambda: datetime.now(UTC)
            # A crash after grant has no transport observation, but still reaches a bounded hold.
            lost = await prepare(http, ctf, max_cost="0.035")
            await provision_native(app, http, lost, native)
            unsent = attempt(lost)
            assert (await admit(http, lost, unsent)).status_code == 200
            missing_grant = (await dispatch(http, lost, unsent)).json()
            assert missing_grant["send_allowed"]
            original = await call_record(ctf, lost["tid"], "model:" + unsent["attempt_id"])
            assert "transport_observed_at" not in original["data"]
            cutoff = datetime.fromtimestamp(missing_grant["authorized_wait_until_ms"] / 1000, UTC)
            assert (cutoff - datetime.now(UTC)).total_seconds() <= 30
            fees.clock = lambda: cutoff + timedelta(seconds=6)
            await fees.consume(lost["tid"])
            missing = await call_record(ctf, lost["tid"], "model:" + unsent["attempt_id"])
            assert missing["data"]["fee_state"] == "missing" and missing["state"] == "dispatched"
            assert (await fees.view(lost["tid"]))["hold_reason"] == "native_log_missing"
            assert (await dispatch(http, lost, unsent)).status_code == 409
        finally:
            await fees.admin.close()


async def test_native_key_creation_retirement_and_deleted_task_consumer_races(ctf, tmp_path):
    from bbx_blackboard.conversations import Conversations
    from test_runner_control_integration import connect

    async with native_gateway(tmp_path / "gateway") as native, connect(ctf) as (app, http):
        scope = await prepare(http, ctf, max_cost="0.035")
        entered, release = asyncio.Event(), asyncio.Event()
        publishing = asyncio.create_task(
            provision_native(
                app, http, scope, native, create_entered=entered, create_release=release
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        fees = app.state.runner_gateway.native
        try:
            async with ctf.repo.engine.begin() as conn:
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == scope["tid"])
                    .values(deleting=True, cleanup_ready=True, status="stopped")
                )
            with pytest.raises(HTTPException, match="readback"):
                await fees.retire(scope["tid"])
            with pytest.raises(HTTPException, match="retirement"):
                await Conversations(ctf.repo.engine).purge(scope["tid"], ctf.objects)
            release.set()
            _, published = await publishing
            original_settings = fees.settings
            fees.settings = fees.settings.model_copy(
                update={"native_gateway_accounting_source_id": "other-source"}
            )
            with pytest.raises(HTTPException, match="source"):
                await fees.retire(scope["tid"])
            fees.settings = original_settings
            resources = await fees.admin.resources()
            original = next(
                item for item in resources if item["id"] == published["native_resource_id"]
            )
            drift = json.loads(json.dumps(original["value"]))
            drift["budgets"][0]["limit"]["amount"] = 1
            await fees.admin.request(
                "PUT", "/api/config/resources/llm.apiKey/" + original["id"], body={"value": drift}
            )
            with pytest.raises(HTTPException, match="changed"):
                await fees.retire(scope["tid"])
            async with ctf.repo.engine.connect() as conn:
                assert (
                    await conn.execute(
                        select(s.native_task_budgets.c.key_ciphertext).where(
                            s.native_task_budgets.c.task_id == scope["tid"]
                        )
                    )
                ).scalar_one()
            await fees.admin.request(
                "PUT",
                "/api/config/resources/llm.apiKey/" + original["id"],
                body={"value": original["value"]},
            )
            await fees.retire(scope["tid"])
            assert all(item["id"] != original["id"] for item in await fees.admin.resources())
            assert (await Conversations(ctf.repo.engine).purge(scope["tid"], ctf.objects))["purged"]
            # The worker may have selected a Task immediately before control deletes it.
            live = await prepare(http, ctf, max_cost="0.035")
            await provision_native(app, http, live, native)
            never_sent = attempt(live)
            assert (await admit(http, live, never_sent)).status_code == 200
            grant = (await dispatch(http, live, never_sent)).json()
            note = await http.post(
                private(live) + f"/model/{never_sent['attempt_id']}/transport",
                headers=SERVICE,
                json={
                    "protocol": PROTOCOL,
                    "attempt_id": never_sent["attempt_id"],
                    "dispatch_id": grant["dispatch_id"],
                    "phase": "not_sent",
                    "http_status": None,
                },
            )
            assert note.status_code == 200
            assert (await dispatch(http, live, never_sent)).json()["send_allowed"] is False
            unsent_record = await call_record(ctf, live["tid"], "model:" + never_sent["attempt_id"])
            assert unsent_record["state"] == "not_sent" and not unsent_record["concurrency_active"]
            assert (await fees.view(live["tid"]))["pending_attempts"] == 0
            sent = attempt(live)
            assert (await admit(http, live, sent)).status_code == 200
            sent_grant = (await dispatch(http, live, sent)).json()
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(
                    native["data"] + "/v1/chat/completions",
                    headers={
                        "authorization": "Bearer " + sent_grant["task_gateway_key"],
                        **{
                            TRUSTED_ATTRIBUTE_HEADERS[key]: value
                            for key, value in sent_grant["trusted_attributes"].items()
                        },
                    },
                    content=sent["body_json"].encode(),
                )
            assert response.status_code == 200
            transport = {
                "protocol": PROTOCOL,
                "attempt_id": sent["attempt_id"],
                "dispatch_id": sent_grant["dispatch_id"],
                "phase": "response_ended",
                "http_status": 200,
            }
            note_path = private(live) + f"/model/{sent['attempt_id']}/transport"
            assert (await http.post(note_path, headers=SERVICE, json=transport)).status_code == 200
            async with ctf.repo.engine.connect() as conn:
                assert not (
                    await conn.execute(
                        select(s.native_request_costs.c.native_log_id).where(
                            s.native_request_costs.c.task_id == live["tid"]
                        )
                    )
                ).first()
            assert (
                await http.post(note_path, headers=SERVICE, json={**transport, "phase": "not_sent"})
            ).status_code == 409
            await settle(fees, live["tid"], "0.003")
            waiting, resume, handled = asyncio.Event(), asyncio.Event(), asyncio.Event()
            budget_method, hold = fees.admin.budget, fees.hold

            async def delayed_status(key_name: str):
                result = await budget_method(key_name)
                waiting.set()
                await resume.wait()
                return result

            async def observed_hold(tid, reason, **kwargs):
                await hold(tid, reason, **kwargs)
                handled.set()

            fees.admin.budget, fees.hold = delayed_status, observed_hold
            worker = asyncio.create_task(fees.run())
            try:
                await asyncio.wait_for(waiting.wait(), 3)
                async with ctf.repo.engine.begin() as conn:
                    await conn.execute(
                        update(s.tasks)
                        .where(s.tasks.c.id == live["tid"])
                        .values(deleting=True, cleanup_ready=True, status="stopped")
                    )
                await fees.retire(live["tid"])
                assert (await Conversations(ctf.repo.engine).purge(live["tid"], ctf.objects))[
                    "purged"
                ]
                resume.set()
                await asyncio.wait_for(handled.wait(), 3)
                assert not worker.done()
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
        finally:
            release.set()
            if not publishing.done():
                publishing.cancel()
                with suppress(asyncio.CancelledError):
                    await publishing
            await fees.admin.close()
