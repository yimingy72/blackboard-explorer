"""Real PostgreSQL control authority, with synthetic Runner metadata only."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import jwt
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.conversations import Conversations
from bbx_blackboard.runner_auth import RunnerIdentity, issue_runner_control_token
from bbx_blackboard.service import BoardService
from bbx_blackboard.store import schema as s
from bbx_blackboard.task_board import TaskBoardService
from bbx_contracts.runner import PROTOCOL, RunnerFence, runner_json_text
from fastapi import HTTPException
from runner_control_support import live_http, node_runner
from sqlalchemy import select, update
from test_board_flags_integration import bind_owner, identity, install_fake_runner, task
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url

pytestmark = pytest.mark.integration
SERVICE = {"Authorization": "Bearer service-test"}


def raw(value):
    return runner_json_text(value).encode()


def auth(owner):
    return {"Authorization": "Bearer " + owner["token"], "Content-Type": "application/json"}


def mgmt(tid):
    return f"/api/tasks/{tid}/ctf/runtime/runner-management"


def control(tid):
    return f"/api/tasks/{tid}/runner-control"


def poll_query(owner, after=0):
    return {
        **{key: owner["fence"][key] for key in ("runner_id", "pod_uid", "epoch")},
        "after_seq": after,
        "wait_seconds": 0,
    }


@asynccontextmanager
async def connect(ctf):
    app = create_app(settings(), engine=ctf.repo.engine, objects=ctf.objects)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as http:
        yield app, http


async def register_owner(http, tid, expected_epoch=0):
    response = await http.post(
        mgmt(tid) + "/owners",
        headers=SERVICE,
        json={
            "request_id": str(uuid4()),
            "runner_id": "runner-" + uuid4().hex,
            "pod_uid": "pod-" + uuid4().hex,
            "expected_epoch": expected_epoch,
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert (
        response.json()["credential_expires_at"]
        == jwt.decode(response.json()["token"], options={"verify_signature": False})["exp"]
    )
    assert response.json()["execution_enabled"] is False
    return response.json()


def registration(owner, aid="lead"):
    return {
        "request_id": str(uuid4()),
        "expected_epoch": owner["fence"]["epoch"],
        "agent_id": aid,
        "model": {
            "id": "fake-model",
            "api": "openai-completions",
            "input": ["text"],
            "reasoning": False,
            "thinking_level": "off",
            "context_window": 4096,
            "max_output_tokens": 256,
        },
        "model_credential_ref": "model-" + aid,
        "mcp_credential_ref": "mcp-" + aid,
        "allowed_tools": [],
        "instructions": "Published synthetic role",
        "profile_revision": 1,
        "prompt_revision": 1,
    }


async def setup(http, ctf):
    tid = await task(ctf)
    await install_fake_runner(ctf, tid)
    owner = await register_owner(http, tid)
    response = await http.post(mgmt(tid) + "/bindings", headers=SERVICE, json=registration(owner))
    assert response.status_code == 200, response.text
    binding = response.json()["binding"]
    response = await http.get(
        control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
    )
    assert response.status_code == 200, response.text
    command = response.json()["commands"][0]
    return tid, owner, binding, command


async def worker_binding(http, ctf, tid, owner):
    async with ctf.repo.engine.connect() as conn:
        people = (
            (await conn.execute(select(s.ctf_members).where(s.ctf_members.c.task_id == tid)))
            .mappings()
            .all()
        )
    lead = next(person for person in people if person["id"] == "lead")
    worker = next(person for person in people if person["role"] == "teammate")
    lead_identity = identity(tid, lead["id"], lead["generation"], lead["current_turn_id"])
    worker_identity = identity(tid, worker["id"], worker["generation"], worker["current_turn_id"])
    challenge = await TaskBoardService(ctf).create_challenge(
        tid, lead_identity, {"request_id": str(uuid4()), "title": "Worker scope"}
    )
    await bind_owner(ctf, tid, challenge, worker_identity)
    response = await http.post(
        mgmt(tid) + "/bindings",
        headers=SERVICE,
        json={**registration(owner, worker["id"]), "challenge_id": challenge["id"]},
    )
    assert response.status_code == 200, response.text
    binding = response.json()["binding"]
    batch = (
        await http.get(control(tid) + "/commands", headers=auth(owner), params=poll_query(owner))
    ).json()
    command = next(
        command
        for command in batch["commands"]
        if json.loads(command["payload_json"]).get("spec", {}).get("binding", {}).get("binding_id")
        == binding["binding_id"]
    )
    return binding, command


async def acquire(http, tid, owner, binding, command):
    body = {
        "protocol": PROTOCOL,
        "request_id": command["request_id"],
        "payload_digest": command["payload_digest"],
        "fence": owner["fence"],
        "binding": binding,
    }
    response = await http.post(
        control(tid) + f"/bindings/{binding['binding_id']}/acquire",
        headers=auth(owner),
        content=raw(body),
    )
    assert response.status_code == 200, response.text
    return body, response.json()


def empty_cursor():
    return {
        "offset": 0,
        "seq": 0,
        "hash": "0" * 64,
        "leaf_id": None,
        "anchor_offset": 0,
        "anchor_size": 0,
        "anchor_hash": hashlib.sha256(b"").hexdigest(),
    }


def cursor(previous, entry="entry", advance=1):
    return {
        "offset": previous["offset"] + 80,
        "seq": previous["seq"] + advance,
        "hash": hashlib.sha256(f"{previous['offset']}:{entry}".encode()).hexdigest(),
        "leaf_id": entry,
        "anchor_offset": previous["offset"],
        "anchor_size": 80,
        "anchor_hash": hashlib.sha256(entry.encode()).hexdigest(),
    }


def confirmation(owner, binding, previous, current, applications=None):
    return {
        "protocol": PROTOCOL,
        "request_id": hashlib.sha256(raw([owner["fence"], current])).hexdigest(),
        "fence": owner["fence"],
        "binding": binding,
        "sdk_session_id": "native-sdk-stable",
        "previous": previous,
        "cursor": current,
        "applied_inputs": applications or [],
    }


async def confirm(http, tid, owner, binding, body):
    response = await http.post(
        control(tid) + f"/bindings/{binding['binding_id']}/journal/confirm",
        headers=auth(owner),
        content=raw(body),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def acknowledge(http, tid, owner, command, status, **kwargs):
    body = {
        "protocol": PROTOCOL,
        "request_id": command["request_id"],
        "payload_digest": command["payload_digest"],
        "fence": owner["fence"],
        "status": status,
        **kwargs,
    }
    encoded = raw(body) + b" \n"
    response = await http.post(
        control(tid) + f"/commands/{command['request_id']}/receipts",
        headers=auth(owner),
        content=encoded,
    )
    assert response.status_code == 200, response.text
    assert response.json()["receipt_digest"] == hashlib.sha256(encoded).hexdigest()
    return body


async def post_input(http, ctf, tid, owner, binding, text, sender="user"):
    mid = str(uuid4())
    async with ctf.repo.engine.begin() as conn:
        await ctf.repo.lock(conn, tid)
        await ctf._message(conn, tid, sender, binding["agent_id"], text, mid, "instruction")
    payload = (
        ' { "kind": "input", "binding_id": '
        + json.dumps(binding["binding_id"])
        + ', "input_id": '
        + json.dumps(mid)
        + ', "content": '
        + json.dumps(text)
        + ', "mode": "follow_up" } '
    )
    body = {
        "request_id": str(uuid4()),
        "expected_epoch": owner["fence"]["epoch"],
        "payload_json": payload,
        "payload_digest": hashlib.sha256(payload.encode()).hexdigest(),
    }
    response = await http.post(mgmt(tid) + "/commands", headers=SERVICE, json=body)
    assert response.status_code == 200, response.text
    return {**response.json(), "payload_json": payload, "input_id": mid}


async def test_runner_audience_owner_history_expiry_and_closed_legacy_gate(ctf):
    tid = await task(ctf)
    async with connect(ctf) as (app, http):
        owner = await register_owner(http, tid)
        assert (
            await http.post(mgmt(tid) + "/bindings", headers=SERVICE, json=registration(owner))
        ).status_code == 403
        refreshed = await http.post(
            mgmt(tid) + "/owners/renew", headers=SERVICE, json={"expected_epoch": 1}
        )
        assert refreshed.status_code == 200 and refreshed.headers["cache-control"] == "no-store"
        assert (
            refreshed.json()["credential_expires_at"]
            == jwt.decode(refreshed.json()["token"], options={"verify_signature": False})["exp"]
        )
        assert (
            await http.post(
                mgmt(tid) + "/owners",
                headers={"Authorization": "Bearer " + issue_user_token(settings(), "admin")},
                json={
                    "request_id": str(uuid4()),
                    "runner_id": "other",
                    "pod_uid": "other",
                    "expected_epoch": 1,
                },
            )
        ).status_code == 403
        for authorization in (
            SERVICE,
            {"Authorization": "Bearer " + issue_user_token(settings(), "admin")},
        ):
            assert (
                await http.get(
                    control(tid) + "/commands", headers=authorization, params=poll_query(owner)
                )
            ).status_code == 401
        for path in (f"/api/tasks/{tid}/state", "/api/settings/ctf/workers", mgmt(tid) + "/owners"):
            assert (await http.get(path, headers=auth(owner))).status_code in {401, 405}
        assert (
            await http.get(
                control(tid) + "/commands",
                headers=auth(owner),
                params={**poll_query(owner), "pod_uid": "wrong-pod"},
            )
        ).status_code == 403
        other_tid = await task(ctf)
        assert (
            await http.get(
                control(other_tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
        ).status_code == 403
        async with ctf.repo.engine.connect() as conn:
            stored = dict(
                (
                    await conn.execute(
                        select(s.runner_owners).where(s.runner_owners.c.task_id == tid)
                    )
                )
                .mappings()
                .one()
            )
        altered = {
            **owner,
            "token": issue_runner_control_token(
                settings(), {**stored, "token_version": "wrong-version"}
            ),
        }
        assert (
            await http.get(
                control(tid) + "/commands", headers=auth(altered), params=poll_query(owner)
            )
        ).status_code == 403
        instant = app.state.runner_control.clock()
        app.state.runner_control.clock = lambda: instant + timedelta(seconds=61)
        assert (
            await http.post(
                mgmt(tid) + "/owners/renew", headers=SERVICE, json={"expected_epoch": 1}
            )
        ).status_code == 409
        renewed = await register_owner(http, tid, 1)
        assert renewed["hard_deadline"] <= owner["hard_deadline"]
        assert (
            await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
        ).status_code == 409
        async with ctf.repo.engine.begin() as conn:
            history = (
                (
                    await conn.execute(
                        select(s.runner_owners)
                        .where(s.runner_owners.c.task_id == tid)
                        .order_by(s.runner_owners.c.epoch)
                    )
                )
                .mappings()
                .all()
            )
            assert len(history) == 2 and not history[0]["current"] and history[1]["current"]
            assert await ctf.claim_turn(tid, "lead", "legacy") is None
            await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(deleting=True))
        assert (
            await http.get(
                control(tid) + "/commands", headers=auth(renewed), params=poll_query(renewed)
            )
        ).status_code == 409
        with pytest.raises(HTTPException):
            await ctf.start(tid)


async def wait_record(ctf, table, tid, oid, **expected):
    async def read():
        while True:
            async with ctf.repo.engine.connect() as conn:
                found = (
                    (
                        await conn.execute(
                            select(table).where(table.c.task_id == tid, table.c.id == oid)
                        )
                    )
                    .mappings()
                    .first()
                )
            if found and all(found[key] == value for key, value in expected.items()):
                return dict(found)
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(read(), 12)


async def node_data(ctf, tid, owner, base):
    async with ctf.repo.engine.connect() as conn:
        payloads = (
            (
                await conn.execute(
                    select(s.runner_bindings.c.payload_json).where(
                        s.runner_bindings.c.task_id == tid,
                        s.runner_bindings.c.owner_epoch == owner["fence"]["epoch"],
                        s.runner_bindings.c.status.in_(["registered", "prepared"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    return {
        "fence": owner["fence"],
        "token": owner["token"],
        "credential_expires_at": owner["credential_expires_at"],
        "control_url": base + control(tid),
        "specs": [json.loads(payload)["spec"] for payload in payloads],
    }


async def test_prepare_ack_loss_exact_bytes_journal_cas_and_owner_restore(ctf, tmp_path):
    async with connect(ctf) as (app, http):
        tid, owner, binding, command = await setup(http, ctf)
        bid = binding["binding_id"]
        ack_path = control(tid) + f"/commands/{command['request_id']}/receipts"
        acquire_path = control(tid) + f"/bindings/{bid}/acquire"
        async with live_http(app, ack_path) as (base, transport):
            async with node_runner(await node_data(ctf, tid, owner, base), tmp_path) as node:
                await asyncio.wait_for(transport.entered.wait(), 12)
                prepared = await wait_record(ctf, s.runner_bindings, tid, bid, status="prepared")
                sdk = prepared["sdk_session_id"]
                assert sdk and sdk != binding["platform_session_id"]
                await node.command("kill")
                transport.release.set()
                await node.command("start", await node_data(ctf, tid, owner, base))

                async def restored():
                    while (
                        transport.requests.get(acquire_path, 0) < 2
                        or transport.requests.get(ack_path, 0) < 2
                    ):
                        await asyncio.sleep(0.02)

                await asyncio.wait_for(restored(), 12)
                again = await wait_record(ctf, s.runner_bindings, tid, bid, status="prepared")
                assert again["sdk_session_id"] == sdk
                assert len(list((node.journal_root / bid).glob("*.jsonl"))) == 1
                replayed = (
                    await http.get(
                        control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
                    )
                ).json()["commands"][0]
                assert replayed == command
                async with ctf.repo.engine.connect() as conn:
                    first_receipt = (
                        (
                            await conn.execute(
                                select(s.runner_receipts).where(
                                    s.runner_receipts.c.task_id == tid,
                                    s.runner_receipts.c.kind == "journal",
                                    s.runner_receipts.c.owner_epoch == 1,
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                original_body = first_receipt["body_text"]
                assert (
                    await http.post(
                        control(tid) + f"/bindings/{bid}/journal/confirm",
                        headers=auth(owner),
                        content=original_body.encode(),
                    )
                ).status_code == 200
                assert (
                    await http.post(
                        control(tid) + f"/bindings/{bid}/journal/confirm",
                        headers=auth(owner),
                        content=(original_body + " ").encode(),
                    )
                ).status_code == 409
                message = await post_input(http, ctf, tid, owner, binding, "Real Runner input 原文")
                applied = await wait_record(
                    ctf, s.runner_commands, tid, "1:" + message["request_id"], status="applied"
                )
                assert applied["native_proof"]["input_id"] == message["input_id"]

                async def model_finished():
                    while (await node.command("status"))["model_receipts"] < 1:
                        await asyncio.sleep(0.02)

                await asyncio.wait_for(model_finished(), 10)
                await node.command("kill")
                before = await wait_record(ctf, s.runner_bindings, tid, bid, status="prepared")
                successor = await register_owner(http, tid, 1)
                attached = await http.post(
                    mgmt(tid) + f"/bindings/{bid}/attach",
                    headers=SERVICE,
                    json={"expected_epoch": 2},
                )
                assert attached.status_code == 200, attached.text
                next_batch = (
                    await http.get(
                        control(tid) + "/commands",
                        headers=auth(successor),
                        params=poll_query(successor),
                    )
                ).json()
                assert next_batch["commands"][0]["payload_json"] == command["payload_json"]
                assert next_batch["commands"][0]["request_id"] == command["request_id"]
                await node.command("start", await node_data(ctf, tid, successor, base))

                async def acquired_new_owner():
                    while (
                        transport.requests.get(acquire_path, 0) < 3
                        or transport.requests.get(ack_path, 0) < 3
                    ):
                        await asyncio.sleep(0.02)

                await asyncio.wait_for(acquired_new_owner(), 12)
                recovered = await wait_record(
                    ctf, s.runner_bindings, tid, bid, owner_epoch=2, status="prepared"
                )
                assert (
                    recovered["sdk_session_id"] == sdk
                    and len(list((node.journal_root / bid).glob("*.jsonl"))) == 1
                )
                assert (
                    await http.post(
                        control(tid) + f"/bindings/{bid}/journal/confirm",
                        headers=auth(owner),
                        content=original_body.encode(),
                    )
                ).status_code == 200
                invalid = confirmation(
                    successor, binding, empty_cursor(), cursor(recovered["cursor"])
                )
                invalid["sdk_session_id"] = sdk
                assert (
                    await http.post(
                        control(tid) + f"/bindings/{bid}/journal/confirm",
                        headers=auth(successor),
                        content=raw(invalid),
                    )
                ).status_code == 409
                invalid["sdk_session_id"] = "wrong-sdk"
                assert (
                    await http.post(
                        control(tid) + f"/bindings/{bid}/journal/confirm",
                        headers=auth(successor),
                        content=raw(invalid),
                    )
                ).status_code == 403
                assert before["cursor"]["seq"] <= recovered["cursor"]["seq"]
                async with ctf.repo.engine.connect() as conn:
                    owner_row = dict(
                        (
                            await conn.execute(
                                select(s.runner_owners).where(
                                    s.runner_owners.c.task_id == tid,
                                    s.runner_owners.c.current.is_(True),
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                who = RunnerIdentity(
                    RunnerFence.model_validate(successor["fence"]), owner_row["token_version"]
                )
                old_clock = app.state.runner_control.clock
                instant = old_clock()
                app.state.runner_control.clock = lambda: instant + timedelta(seconds=16)
                renewed = await asyncio.wait_for(
                    app.state.runner_control.poll(tid, who, 100000, 15), 0.5
                )
                assert renewed["lease_renewals"][0]["lease_revision"] > recovered["permit_revision"]
                app.state.runner_control.clock = old_clock
                payload = runner_json_text({"kind": "close", "binding_id": bid})
                closing = await http.post(
                    mgmt(tid) + "/commands",
                    headers=SERVICE,
                    json={
                        "request_id": str(uuid4()),
                        "expected_epoch": 2,
                        "payload_json": payload,
                        "payload_digest": hashlib.sha256(payload.encode()).hexdigest(),
                    },
                )
                assert closing.status_code == 200, closing.text
                closed = await wait_record(ctf, s.runner_bindings, tid, bid, status="closed")
                assert closed["permit_expires_at"] is None and closed["sdk_session_id"] == sdk
                waiting = asyncio.create_task(app.state.runner_control.poll(tid, who, 100000, 0.3))
                await asyncio.sleep(0.05)
                assert ctf.repo.engine.pool.checkedout() == 0
                async with ctf.repo.engine.begin() as conn:
                    message_row = (
                        (
                            await conn.execute(
                                select(s.ctf_messages).where(
                                    s.ctf_messages.c.task_id == tid,
                                    s.ctf_messages.c.id == message["input_id"],
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert (
                        message_row["status"] == "delivered"
                        and message_row["session_revision"] is None
                    )
                    await conn.execute(
                        update(s.tasks).where(s.tasks.c.id == tid).values(status="stopped")
                    )
                with pytest.raises(HTTPException):
                    await waiting


async def test_input_transaction_monotonic_prompt_and_close_evidence(ctf, monkeypatch):
    async with connect(ctf) as (app, http):
        tid, owner, binding, command = await setup(http, ctf)
        bid = binding["binding_id"]
        message = await post_input(http, ctf, tid, owner, binding, "你好：保留输入原文")
        worker, worker_command = await worker_binding(http, ctf, tid, owner)
        initial = (
            await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
        ).json()
        assert [item["command_seq"] for item in initial["commands"]] == [1, 2, 3]
        await acquire(http, tid, owner, binding, command)
        await acquire(http, tid, owner, worker, worker_command)
        first = cursor(empty_cursor())
        await confirm(
            http, tid, owner, binding, confirmation(owner, binding, empty_cursor(), first)
        )
        batch = (
            await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
        ).json()
        assert batch["commands"][0]["request_id"] == command["request_id"]
        assert batch["commands"][1]["payload_json"] == message["payload_json"]
        applied = {
            "protocol": PROTOCOL,
            "request_id": message["request_id"],
            "payload_digest": message["payload_digest"],
            "fence": owner["fence"],
            "status": "applied",
            "binding_id": bid,
            "sdk_session_id": "native-sdk-stable",
            "input_id": message["input_id"],
            "cursor": first,
        }
        assert (
            await http.post(
                control(tid) + f"/commands/{message['request_id']}/receipts",
                headers=auth(owner),
                content=raw(applied),
            )
        ).status_code == 409
        await acknowledge(
            http,
            tid,
            owner,
            message,
            "accepted",
            binding_id=bid,
            sdk_session_id="native-sdk-stable",
            input_id=message["input_id"],
        )
        current = cursor(first, "native-input", advance=2)
        application = {
            "input_id": message["input_id"],
            "entry_id": "native-input",
            "seq": current["seq"],
        }
        body = confirmation(owner, binding, first, current, [application])
        original = app.state.runner_control.repo.notify
        monkeypatch.setattr(
            app.state.runner_control.repo,
            "notify",
            AsyncMock(side_effect=RuntimeError("Injected transaction failure")),
        )
        with pytest.raises(RuntimeError, match="Injected"):
            await confirm(http, tid, owner, binding, body)
        monkeypatch.setattr(app.state.runner_control.repo, "notify", original)
        async with ctf.repo.engine.connect() as conn:
            assert (
                await conn.execute(
                    select(s.ctf_messages.c.status).where(
                        s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == message["input_id"]
                    )
                )
            ).scalar_one() == "leased"
            assert (
                await conn.execute(
                    select(s.runner_bindings.c.cursor).where(
                        s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == bid
                    )
                )
            ).scalar_one() == first
        await confirm(http, tid, owner, binding, body)
        await acknowledge(
            http,
            tid,
            owner,
            message,
            "applied",
            binding_id=bid,
            sdk_session_id="native-sdk-stable",
            input_id=message["input_id"],
            cursor=current,
        )
        await acknowledge(
            http,
            tid,
            owner,
            message,
            "accepted",
            binding_id=bid,
            sdk_session_id="native-sdk-stable",
            input_id=message["input_id"],
        )
        later = cursor(current, "later-entry")
        await confirm(http, tid, owner, binding, confirmation(owner, binding, current, later))
        await acknowledge(
            http,
            tid,
            owner,
            message,
            "applied",
            binding_id=bid,
            sdk_session_id="native-sdk-stable",
            input_id=message["input_id"],
            cursor=later,
        )
        async with ctf.repo.engine.connect() as conn:
            recorded = (
                (
                    await conn.execute(
                        select(s.runner_commands).where(
                            s.runner_commands.c.task_id == tid,
                            s.runner_commands.c.request_id == message["request_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            delivered = (
                (
                    await conn.execute(
                        select(s.ctf_messages).where(
                            s.ctf_messages.c.task_id == tid,
                            s.ctf_messages.c.id == message["input_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert recorded["status"] == "applied" and recorded["native_proof"] == application
            assert delivered["status"] == "delivered" and delivered["session_revision"] is None
            turn = (
                (
                    await conn.execute(
                        select(s.ctf_turns).where(
                            s.ctf_turns.c.task_id == tid,
                            s.ctf_turns.c.id == delivered["delivered_turn_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert turn["assignment_message_ids"].count(message["input_id"]) == 1
            assert turn["checkpoint_revision"] == 0
        worker_message = await post_input(
            http, ctf, tid, owner, worker, "Lead instruction", sender="lead"
        )
        worker_first = cursor(empty_cursor(), "worker-header")
        worker_confirm = confirmation(owner, worker, empty_cursor(), worker_first)
        worker_confirm["sdk_session_id"] = "worker-sdk"
        await confirm(http, tid, owner, worker, worker_confirm)
        worker_applied = cursor(worker_first, "worker-input", advance=2)
        worker_confirm = confirmation(
            owner,
            worker,
            worker_first,
            worker_applied,
            [
                {
                    "input_id": worker_message["input_id"],
                    "entry_id": "worker-input",
                    "seq": worker_applied["seq"],
                }
            ],
        )
        worker_confirm["sdk_session_id"] = "worker-sdk"
        await confirm(http, tid, owner, worker, worker_confirm)
        events = await BoardService(ctf.repo.engine, ctf.objects).events(tid, for_agent="lead")
        event = next(
            item
            for item in events
            if item["type"] == "ctf.message.delivered"
            and item["object_id"] == worker_message["input_id"]
        )
        assert event["addressed_to"] == sorted({"lead", worker["agent_id"]})
        assert event["payload"]["native_entry_id"] == "worker-input"
        path = control(tid) + f"/bindings/{bid}/prompt"
        assert (
            await http.get(path, headers=auth(owner), params={"known_revision": 1})
        ).status_code == 304
        assert (
            await http.put(
                mgmt(tid) + f"/bindings/{bid}/prompt",
                headers=SERVICE,
                json={
                    "expected_epoch": 1,
                    "expected_revision": 1,
                    "instructions": "Published revision two",
                },
            )
        ).status_code == 200
        prompt = await http.get(path, headers=auth(owner), params={"known_revision": 1})
        assert prompt.status_code == 200 and prompt.json()["prompt_revision"] == 2
        assert (
            await http.get(path, headers=auth(owner), params={"known_revision": 2})
        ).status_code == 304
        second = await post_input(http, ctf, tid, owner, binding, "Unconfirmed input")
        pending = confirmation(
            owner,
            binding,
            later,
            cursor(later, "pending-input"),
            [
                {
                    "input_id": second["input_id"],
                    "entry_id": "pending-input",
                    "seq": later["seq"] + 1,
                }
            ],
        )
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.ctf_members)
                .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == worker["agent_id"])
                .values(run_state="stopping", pending_operation={"kind": "stop"})
            )
        stopped_worker = await http.get(
            control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
        )
        assert stopped_worker.status_code == 200, stopped_worker.text
        assert any(
            json.loads(item["payload_json"])["kind"] == "close"
            for item in stopped_worker.json()["commands"]
        )
        assert {renewal["binding_id"] for renewal in stopped_worker.json()["lease_renewals"]} == {
            bid
        }
        await acknowledge(
            http, tid, owner, worker_command, "rejected", error_code="boundary_frozen"
        )
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.ctf_members)
                .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == "lead")
                .values(run_state="stopping", pending_operation={"kind": "stop"})
            )
        payload = runner_json_text({"kind": "close", "binding_id": bid})
        response = await http.post(
            mgmt(tid) + "/commands",
            headers=SERVICE,
            json={
                "request_id": str(uuid4()),
                "expected_epoch": 1,
                "payload_json": payload,
                "payload_digest": hashlib.sha256(payload.encode()).hexdigest(),
            },
        )
        assert response.status_code == 200, response.text
        closing = response.json()
        await acknowledge(
            http,
            tid,
            owner,
            closing,
            "closed",
            binding_id=bid,
            sdk_session_id="native-sdk-stable",
            cursor=later,
            sdk_idle=True,
            journal_confirmed=False,
            pending_confirmation=pending,
            unresolved_attempts=["unknown-attempt"],
            unresolved_invocations=["unknown-tool"],
        )
        await confirm(http, tid, owner, binding, pending)
        unknown = {**pending, "request_id": "e" * 64}
        assert (
            await http.post(
                control(tid) + f"/bindings/{bid}/journal/confirm",
                headers=auth(owner),
                content=raw(unknown),
            )
        ).status_code == 409
        assert (
            await http.post(
                mgmt(tid) + f"/bindings/{bid}/attach", headers=SERVICE, json={"expected_epoch": 1}
            )
        ).status_code == 409
        async with ctf.repo.engine.begin() as conn:
            closed = (
                (
                    await conn.execute(
                        select(s.runner_bindings).where(
                            s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == bid
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert (
                closed["status"] == "closed"
                and closed["cursor"] == later
                and closed["permit_expires_at"] is None
            )
            assert closed["payload_json"] == command["payload_json"]
            assert (
                await conn.execute(
                    select(s.ctf_messages.c.status).where(
                        s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == second["input_id"]
                    )
                )
            ).scalar_one() == "leased"
            member = (
                (
                    await conn.execute(
                        select(s.ctf_members).where(
                            s.ctf_members.c.task_id == tid, s.ctf_members.c.id == "lead"
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert member["lifecycle"] == "active" and member["run_state"] == "stopping"
            await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(status="stopped"))
        assert (
            await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
        ).status_code == 409
        archive = await Conversations(ctf.repo.engine).export_archive(tid)
        assert archive["runner_control"]["runner_receipts"] and owner["token"] not in json.dumps(
            archive
        )
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(deleting=True))
        await Conversations(ctf.repo.engine).purge(tid, ctf.objects)
        async with ctf.repo.engine.connect() as conn:
            for table in (s.runner_owners, s.runner_bindings, s.runner_commands, s.runner_receipts):
                assert not (
                    await conn.execute(select(table.c.task_id).where(table.c.task_id == tid))
                ).all()
