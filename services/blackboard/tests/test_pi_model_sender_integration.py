"""Actual Pi provider I/O, private Blackboard routes, and one disposable PostgreSQL ledger."""

import asyncio
import hashlib
import json
import socket
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import uvicorn
from bbx_blackboard.store import schema
from bbx_contracts.models import ModelConfig
from bbx_contracts.pi_models import PiCallPrepare
from bbx_runtime import pi_sender
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.models import make_task_model_client
from bbx_runtime.pi import Message
from bbx_runtime.pi.bridge import PiBridge
from bbx_runtime.settings import Settings
from pydantic import SecretStr
from sqlalchemy import select, update
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from test_pi_model_ledger_integration import intent
from test_pi_model_ledger_integration import pi_app as pi_app
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url

pytestmark = pytest.mark.integration


@pytest.fixture
async def provider(monkeypatch, tmp_path: Path):
    cert, private = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=fixture.test",
            "-addext",
            "subjectAltName=DNS:fixture.test",
            "-keyout",
            str(private),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(cert))
    posts = []

    async def complete(request: Request):
        assert request.headers.get("Authorization") == "Bearer fixture-only-key"
        raw = await request.body()
        posts.append(raw)
        assert json.loads(raw)["model"] == "fixture-chat"
        prefix = {
            "id": "fixture-request",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-chat",
        }
        chunks = [
            {
                **prefix,
                "choices": [
                    {"index": 0, "delta": {"content": "Fixture result"}, "finish_reason": None}
                ],
            },
            {**prefix, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {
                **prefix,
                "choices": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            },
        ]
        content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return Response(content, media_type="text/event-stream")

    app = Starlette(routes=[Route("/v1/chat/completions", complete, methods=["POST"])])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    origin = f"https://fixture.test:{listener.getsockname()[1]}"

    async def loopback_only(url, approved):
        assert url == origin + "/v1" and approved == (origin,)
        return ["127.0.0.1"]

    monkeypatch.setattr(pi_sender, "provider_addresses", loopback_only)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="critical",
            access_log=False,
            ssl_keyfile=str(private),
            ssl_certfile=str(cert),
        )
    )
    runner = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        yield origin, posts
    finally:
        server.should_exit = True
        await runner
        listener.close()


@pytest.fixture
def pi_provider_origin(provider):
    return provider[0]


async def test_actual_sdk_claim_and_settle_via_blackboard(pi_app, provider):
    app, http, tid, model, agents, auth, started_at, user = pi_app
    origin, posts = provider
    service = BlackboardClient("http://bbx.test", "unused-generic-token", http)
    bridge = PiBridge(use_env_proxy=False)
    sender = pi_sender.PiSender(service, auth["X-BBX-Pi-Runtime"], (origin,), bridge)
    context = {"messages": [{"role": "user", "content": "fixture", "timestamp": 1}], "tools": []}
    options = {"maxTokens": 16}
    raw = intent(tid, model, agents[0], started_at)
    raw["context_sha256"] = pi_sender.context_sha256(context, options)
    body = PiCallPrepare.model_validate(raw)
    try:
        result = await sender.invoke(body, context, options)
        assert result["message"]["content"][0]["text"] == "Fixture result"
        assert len(posts) == 1
        async with app.state.engine.connect() as conn:
            saved = (
                (
                    await conn.execute(
                        select(schema.pi_model_calls).where(
                            schema.pi_model_calls.c.call_id == UUID(raw["call_id"])
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert saved["state"] == "complete"
        assert saved["request_sha256"] == hashlib.sha256(posts[0]).hexdigest()
        fees = (await http.get(f"/api/tasks/{tid}/pi-fees", headers=user)).raise_for_status().json()
        assert float(fees["solver"]["estimated_usd"]) == pytest.approx(0.00001)
        assert fees["solver"]["unknown_calls"] == 0
        with pytest.raises(RemoteError) as replay:
            await sender.invoke(body, context, options)
        assert replay.value.status == 409
        assert len(posts) == 1
    finally:
        await bridge.close()


async def test_actual_claimed_review_and_progress_use_the_same_tls_sender(pi_app, provider):
    app, http, tid, model, agents, auth, _, user = pi_app
    origin, posts = provider
    service = BlackboardClient("http://bbx.test", "service-test", http)
    runtime = Settings(
        service_token=SecretStr("service-test"),
        minio_root_password=SecretStr("fixture-minio"),
        envd_token_secret=SecretStr("fixture-envd"),
        pi_runtime_sender_token=SecretStr(auth["X-BBX-Pi-Runtime"]),
        pi_provider_allowed_origins=(origin,),
    )
    profile = ModelConfig.model_validate(
        {
            "provider": "openai_compatible",
            "model": "logical-profile",
            "base_url": "https://unused.invalid/v1",
            "reasoning_effort": "off",
            "price": {"currency": "USD"},
        }
    )
    trusted = {
        "Authorization": "Bearer service-test",
        "X-BBX-Legacy-Execution": "legacy-agent-network-v1",
    }

    async def execute(source, purpose, proof, anchor):
        assert source["pi_model"] == {"model_id": model["id"], "model_revision": 1}
        client = make_task_model_client(
            profile,
            source=source,
            settings=runtime,
            service=service,
            task_id=str(tid),
            purpose=purpose,
            proof=proof,
            explore_max_steps=1,
            conclude_grace_calls=0,
            max_duration_seconds=30,
        )
        try:
            reply = await client.get_response(
                [Message("user", anchor, message_id=anchor)],
                options={"session_id": "fixture-" + purpose, "max_tokens": 16},
            )
            assert reply.text == "Fixture result"
        finally:
            await client.close()

    aid = agents[0]
    mid = str(uuid4())
    async with app.state.engine.begin() as conn:
        await conn.execute(
            update(schema.agent_runs)
            .where(schema.agent_runs.c.task_id == tid, schema.agent_runs.c.id == aid)
            .values(status="finished")
        )
    review_path = f"/api/tasks/{tid}/agents/{aid}/messages"
    (
        await http.post(review_path, headers=user, json={"id": mid, "content": "Fixture review"})
    ).raise_for_status()
    review = (
        (await http.post(f"{review_path}/{mid}/claim", headers=trusted, json={"mode": "review"}))
        .raise_for_status()
        .json()
    )
    await execute(
        review,
        "review",
        {
            "agent_id": aid,
            "run_number": 1,
            "message_id": mid,
            "claim_token": review["claim_token"],
        },
        mid,
    )
    assert len(posts) == 1

    (
        await http.put(
            f"/api/tasks/{tid}/progress/settings",
            headers=user,
            json={
                "enabled": True,
                "currency": "USD",
                "max_cost": "1",
                "pi_model": {"model_id": model["id"], "model_revision": 1},
                "merge_seconds": 0,
                "min_interval_seconds": 0,
            },
        )
    ).raise_for_status()
    (
        await http.post(
            f"/api/tasks/{tid}/progress/messages",
            headers=user,
            json={"id": str(uuid4()), "content": "Summarize the synthetic progress."},
        )
    ).raise_for_status()
    progress = (
        (await http.post("/api/progress/jobs/claim", headers=trusted)).raise_for_status().json()
    )
    await execute(
        progress,
        "progress",
        {
            "job_id": progress["job_id"],
            "claim_token": progress["claim_token"],
            "run_number": progress["run_number"],
        },
        "fixture-progress-batch",
    )
    assert len(posts) == 2
    async with app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(schema.pi_model_calls).where(schema.pi_model_calls.c.task_id == tid)
                )
            )
            .mappings()
            .all()
        )
    assert {row["purpose"] for row in rows} == {"review", "progress"}
    assert all(row["state"] == "complete" for row in rows)
    assert {row["request_sha256"] for row in rows} == {
        hashlib.sha256(post).hexdigest() for post in posts
    }
