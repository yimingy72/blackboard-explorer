import asyncio
import hashlib
import json
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from bbx_contracts.models import ModelConfig
from bbx_contracts.pi_models import PiCallPrepare
from bbx_runtime import pi_sender
from bbx_runtime.clients.blackboard import BlackboardClient
from bbx_runtime.models import make_task_model_client
from bbx_runtime.pi import Message
from bbx_runtime.pi.bridge import PiBridge
from bbx_runtime.runner_proxy import RunnerModelProxy
from bbx_runtime.settings import Settings
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route


@asynccontextmanager
async def listen(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(4):
            while not server.started:
                await asyncio.sleep(0.01)
        yield origin
    finally:
        server.should_exit = True
        await task
        sock.close()


def sse(rows):
    return "".join("data: " + json.dumps(row) + "\n\n" for row in rows) + "data: [DONE]\n\n"


class FakeProvider:
    def __init__(self):
        self.posts = []
        self.valid_usage = True
        self.contradict_usage = False
        self.tool_once = False
        self.tool_sent = False
        self.helper_mode = False
        self.parent_delegated = False
        self.child_tool_sent = False

    async def __call__(self, request: Request):
        assert request.headers.get("authorization") == "Bearer fake-provider-only"
        self.posts.append(await request.body())
        wire = json.loads(self.posts[-1])
        assert wire["model"] == "provider-physical" and wire["stream_options"]["include_usage"]
        base = {
            "id": "actual-synthetic-id",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": wire["model"],
        }
        rows = [
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "synthetic answer"},
                        "finish_reason": None,
                    }
                ],
            },
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        if (
            self.tool_once
            and not self.tool_sent
            and any(
                tool.get("function", {}).get("name") == "remote_echo"
                for tool in wire.get("tools", [])
            )
        ):
            self.tool_sent = True
            rows[0]["choices"][0]["delta"] = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "original-tool-id",
                        "type": "function",
                        "function": {"name": "remote_echo", "arguments": '{"value":"same"}'},
                    }
                ],
            }
            rows[1]["choices"][0]["finish_reason"] = "tool_calls"
        if self.helper_mode:
            names = {tool.get("function", {}).get("name") for tool in wire.get("tools", [])}
            tool_call = None
            if "delegate_helper_background" in names and not self.parent_delegated:
                self.parent_delegated = True
                tool_call = (
                    "delegate_helper_background",
                    {
                        "goal": "Read the scoped test marker.",
                        "input_refs": [],
                        "expected_output": "One marker.",
                    },
                )
            elif names == {"exec_command"} and not self.child_tool_sent:
                self.child_tool_sent = True
                tool_call = ("exec_command", {"value": "marker"})
            if tool_call:
                rows[0]["choices"][0]["delta"] = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "synthetic-helper-tool-id",
                            "type": "function",
                            "function": {
                                "name": tool_call[0],
                                "arguments": json.dumps(tool_call[1]),
                            },
                        }
                    ],
                }
                rows[1]["choices"][0]["finish_reason"] = "tool_calls"
        if self.valid_usage:
            rows.append(
                {
                    **base,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 6,
                        "total_tokens": 16,
                        "prompt_tokens_details": {"cached_tokens": 2},
                        "completion_tokens_details": {"reasoning_tokens": 1},
                    },
                }
            )
        if self.contradict_usage:
            rows[-1]["usage"]["prompt_cache_hit_tokens"] = 3
            rows[-1]["usage"]["prompt_cache_miss_tokens"] = 7
        return Response(sse(rows), media_type="text/event-stream")


class FakeAuthority:
    def __init__(self, provider, origin):
        self.provider = provider
        self.origin = origin
        self.calls = {}
        self.rows = []
        self.lose_next_settlement_ack = False

    def __call__(self, request):
        assert request.headers["x-bbx-pi-runtime"] == "synthetic-runtime-sender"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        action = request.url.path.rsplit("/", 1)[1]
        if action == "prepare":
            call_id = body["call_id"]
            allowed = None
            if body["proof"]["kind"] == "native":
                binding = body["proof"]["binding"]["binding_id"]
                assert request.headers["x-bbx-runner-model"] in {
                    "Bearer runner-short-only",
                    f"Bearer private-model-{binding}",
                }
                allowed = {
                    "parent": [
                        "exec_command",
                        "delegate_helper_background",
                        "background_helper_status",
                        "cancel_helper",
                    ],
                    "child": ["exec_command"],
                }.get(binding, ["remote_echo"])
            if call_id not in self.calls:
                self.calls[call_id] = {"prepare": body, "posts_before": len(self.provider.posts)}
            else:
                assert self.calls[call_id]["prepare"] == body
            return httpx.Response(
                200,
                json={
                    "call_id": call_id,
                    "attempt_id": body["attempt_id"],
                    "api_key": "fake-provider-only",
                    "allowed_tools": allowed,
                    "model": {
                        "id": body["model_id"],
                        "model_revision": body["model_revision"],
                        "label": "Fixture",
                        "upstream_model": "provider-physical",
                        "base_url": self.origin + "/v1",
                        "api": "openai-completions",
                        "provider": "openai",
                        "context_window": 4096,
                        "max_output_tokens": 80,
                        "input_modalities": ["text"],
                        "has_secret": True,
                        "usd_rates": {
                            "input": "2",
                            "output": "3",
                            "cache_read": "1",
                            "cache_write": "2",
                        },
                        "pricing_source": "synthetic configured rate",
                    },
                },
            )
        call_id = request.url.path.split("/")[-2]
        original = self.calls[call_id]
        assert original["prepare"]["attempt_id"] == body["attempt_id"]
        if action == "dispatch":
            if "dispatch" in original:
                assert original["dispatch"] == body
                return httpx.Response(200, json={"send_allowed": False, "state": "complete"})
            assert not any(
                hashlib.sha256(post).hexdigest() == body["request_sha256"]
                for post in self.provider.posts
            )
            original["dispatch"] = body
            return httpx.Response(200, json={"send_allowed": True, "state": "dispatched"})
        assert action == "settle"
        assert body["request_sha256"] == original["dispatch"]["request_sha256"]
        assert (
            sum(
                hashlib.sha256(post).hexdigest() == body["request_sha256"]
                for post in self.provider.posts
            )
            == 1
        )
        self.rows.append(body)
        if self.lose_next_settlement_ack:
            self.lose_next_settlement_ack = False
            raise httpx.ReadError("synthetic settlement ACK lost", request=request)
        return httpx.Response(
            200,
            json={"call_id": call_id, "attempt_id": body["attempt_id"], "state": body["outcome"]},
        )


@pytest.fixture
def approve_fixture_loopback(monkeypatch):
    async def resolve(url, origins):
        assert url.startswith("http://127.0.0.1:")
        assert origins == ("https://approved.example",)
        return ["127.0.0.1"]

    monkeypatch.setattr(pi_sender, "provider_addresses", resolve)


@pytest.mark.asyncio
async def test_trusted_sdk_actual_send_and_unknown_usage(approve_fixture_loopback):
    provider = FakeProvider()
    async with listen(
        Starlette(routes=[Route("/v1/chat/completions", provider.__call__, methods=["POST"])])
    ) as origin:
        authority = FakeAuthority(provider, origin)
        async with httpx.AsyncClient(transport=httpx.MockTransport(authority)) as internal:
            service = BlackboardClient("http://authority.fixture", "not-the-sender", internal)
            bridge = PiBridge(use_env_proxy=False)
            sender = pi_sender.PiSender(
                service, "synthetic-runtime-sender", ("https://approved.example",), bridge
            )
            context = {
                "messages": [{"role": "user", "content": "fixture", "timestamp": 1}],
                "tools": [],
            }
            options = {"maxTokens": 20}

            def prepared():
                return PiCallPrepare.model_validate(
                    {
                        "task_id": str(uuid4()),
                        "call_id": str(uuid4()),
                        "attempt_id": str(uuid4()),
                        "logical_call_id": uuid4().hex * 2,
                        "model_id": "fixture",
                        "model_revision": 2,
                        "action": "turn",
                        "proof": {
                            "kind": "ordinary",
                            "agent_id": "agent",
                            "run_number": 1,
                            "expected_started_at": "2026-11-10T01:00:00+00:00",
                        },
                        "context_sha256": pi_sender.context_sha256(context, options),
                    }
                )

            try:
                first = prepared()
                known = await sender.invoke(first, context, options)
                assert known["message"]["content"][0]["text"] == "synthetic answer"
                assert authority.rows[-1]["outcome"] == "complete"
                assert authority.rows[-1]["usage"]["reasoning"] == 1
                assert float(authority.rows[-1]["amount_usd"]) == pytest.approx(0.000036)
                with pytest.raises(ValueError, match="closed"):
                    await sender.invoke(first, context, options)
                assert len(provider.posts) == 1 and len(authority.rows) == 1
                provider.valid_usage = False
                context = {
                    "messages": [{"role": "user", "content": "fixture no usage", "timestamp": 1}],
                    "tools": [],
                }
                second = await sender.invoke(prepared(), context, options)
                assert second["message"]["stopReason"] == "stop"
                assert authority.rows[-1]["outcome"] == "complete"
                assert authority.rows[-1]["amount_usd"] is authority.rows[-1]["usage"] is None
                assert len(provider.posts) == 2
                provider.valid_usage = True
                provider.contradict_usage = True
                context = {
                    "messages": [
                        {"role": "user", "content": "contradictory usage", "timestamp": 1}
                    ],
                    "tools": [],
                }
                await sender.invoke(prepared(), context, options)
                assert authority.rows[-1]["outcome"] == "complete"
                assert authority.rows[-1]["amount_usd"] is authority.rows[-1]["usage"] is None
                assert len(provider.posts) == 3
            finally:
                await bridge.close()


@pytest.mark.asyncio
async def test_actual_pi_15_receives_runtime_events_after_settlement(approve_fixture_loopback):
    provider = FakeProvider()
    async with listen(
        Starlette(routes=[Route("/v1/chat/completions", provider.__call__, methods=["POST"])])
    ) as origin:
        authority = FakeAuthority(provider, origin)
        async with httpx.AsyncClient(transport=httpx.MockTransport(authority)) as internal:
            service = BlackboardClient("http://authority.fixture", "not-the-sender", internal)
            settings = Settings(
                service_token=SecretStr("synthetic-service-only"),
                minio_root_password=SecretStr("fake-minio"),
                envd_token_secret=SecretStr("fake-envd"),
                pi_runtime_sender_token=SecretStr("synthetic-runtime-sender"),
                pi_provider_allowed_origins=("https://approved.example",),
            )
            runtime = RunnerModelProxy(settings, service, http=internal)
            async with listen(runtime.app) as runtime_origin:
                node = Path(__file__).resolve().parents[1] / "task-runner"
                proc = await asyncio.create_subprocess_exec(
                    "node",
                    "--input-type=module",
                    "-",
                    cwd=node,
                    env={
                        "PATH": __import__("os").environ["PATH"],
                        "PI_TEST_RUNTIME": runtime_origin,
                    },
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, error = await proc.communicate(b"""
import assert from "node:assert/strict";
import {randomUUID, createHash} from "node:crypto";
import {stream} from "@earendil-works/pi-ai/api/pi-messages";
const task_id = randomUUID(), call_id = randomUUID(), attempt_id = randomUUID();
const logical_call_id = createHash("sha256").update(JSON.stringify([
  "bbx-native-pi-v1", task_id, "turn", "session", "a".repeat(64), 1, "turn",
])).digest("hex");
const context = {messages:[{role:"user",content:"fixture",timestamp:1}],tools:[]};
const model = {id:"fixture", name:"fixture", provider:"openai", api:"pi-messages",
  baseUrl:process.env.PI_TEST_RUNTIME, input:["text"], reasoning:false, contextWindow:4096,
  maxTokens:80, cost:{input:0,output:0,cacheRead:0,cacheWrite:0}};
const response = stream(model, context, {apiKey:"runner-short-only", maxTokens:20,
  fetch:(url,init)=>fetch(url,{...init,redirect:"error"}),
  onPayload:payload=>({...payload, bbx:{task_id,call_id,attempt_id,logical_call_id,
    model_id:"fixture",model_revision:2,
    action:"turn",proof:{kind:"native",binding:{binding_id:"binding",agent_id:"agent",platform_session_id:"turn",business_contract:"challenge-board-v1",
      assignment_ref:null,generation:1,journal_generation:"journal"},
      fence:{task_id,runner_id:"runner",pod_uid:"pod",epoch:1},sdk_session_id:"session",
      journal:{offset:1,seq:1,hash:"a".repeat(64),anchor_offset:0,anchor_size:1,anchor_hash:"a".repeat(64)},profile_revision:1,prompt_revision:1}}})});
const message=await response.result();
assert.equal(message.stopReason,"stop");
assert.equal(message.content[0].text,"synthetic answer");
assert.equal(message.usage.reasoning,1);
assert.equal(message.responseId,"actual-synthetic-id");
console.log("private Pi1.5 terminal verified");
""")
                assert proc.returncode == 0, error.decode()
                assert "private Pi1.5 terminal verified" in output.decode()
            assert len(provider.posts) == 1 and authority.rows[0]["outcome"] == "complete"
            await runtime.close()


@pytest.mark.asyncio
async def test_native_runner_tool_and_summaries_use_actual_provider(approve_fixture_loopback):
    provider = FakeProvider()
    provider.tool_once = True
    async with listen(
        Starlette(routes=[Route("/v1/chat/completions", provider.__call__, methods=["POST"])])
    ) as origin:
        authority = FakeAuthority(provider, origin)
        async with httpx.AsyncClient(transport=httpx.MockTransport(authority)) as internal:
            service = BlackboardClient("http://authority.fixture", "not-the-sender", internal)
            settings = Settings(
                service_token=SecretStr("synthetic-service-only"),
                minio_root_password=SecretStr("fake-minio"),
                envd_token_secret=SecretStr("fake-envd"),
                pi_runtime_sender_token=SecretStr("synthetic-runtime-sender"),
                pi_provider_allowed_origins=("https://approved.example",),
            )
            runtime = RunnerModelProxy(settings, service, http=internal)
            async with listen(runtime.app) as runtime_origin:
                node = Path(__file__).resolve().parents[1] / "task-runner"
                proc = await asyncio.create_subprocess_exec(
                    "node",
                    "--test",
                    "test/pi-direct.test.mjs",
                    cwd=node,
                    env={"PATH": os.environ["PATH"], "BBX_TEST_PI_RUNTIME": runtime_origin},
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, error = await proc.communicate()
                assert proc.returncode == 0, (output.decode(), error.decode())
            assert provider.tool_sent
            assert (
                sum(row["prepare"]["action"] == "summary" for row in authority.calls.values()) >= 2
            )
            assert len(provider.posts) == len(authority.rows) == len(authority.calls)
            assert all(row["outcome"] == "complete" for row in authority.rows)
            await runtime.close()


@pytest.mark.asyncio
async def test_ordinary_call_with_lost_settlement_ack_does_not_replay(approve_fixture_loopback):
    provider = FakeProvider()
    async with listen(
        Starlette(routes=[Route("/v1/chat/completions", provider.__call__, methods=["POST"])])
    ) as origin:
        authority = FakeAuthority(provider, origin)
        async with httpx.AsyncClient(transport=httpx.MockTransport(authority)) as internal:
            service = BlackboardClient("http://authority.fixture", "not-the-sender", internal)
            settings = Settings(
                service_token=SecretStr("synthetic-service-only"),
                minio_root_password=SecretStr("fake-minio"),
                envd_token_secret=SecretStr("fake-envd"),
                pi_runtime_sender_token=SecretStr("synthetic-runtime-sender"),
                pi_provider_allowed_origins=("https://approved.example",),
            )
            task_id = str(uuid4())
            profile = ModelConfig.model_validate(
                {
                    "provider": "openai_compatible",
                    "model": "profile-placeholder",
                    "base_url": "https://old-model.invalid/v1",
                    "reasoning_effort": "off",
                    "price": {"currency": "USD"},
                    "context_window": 4096,
                    "max_output_tokens": 80,
                }
            )

            def client():
                return make_task_model_client(
                    profile,
                    source={"pi_model": {"model_id": "fixture", "model_revision": 2}},
                    settings=settings,
                    service=service,
                    task_id=task_id,
                    purpose="ordinary",
                    proof={
                        "agent_id": "agent",
                        "run_number": 1,
                        "expected_started_at": "2026-11-10T01:00:00+00:00",
                    },
                    explore_max_steps=3,
                    conclude_grace_calls=0,
                    max_duration_seconds=120,
                )

            message = Message("user", "one durable prompt", message_id="original-ordinary-input")
            options = {"session_id": "original-ordinary-session", "max_tokens": 20}
            first = client()
            authority.lose_next_settlement_ack = True
            try:
                with pytest.raises(httpx.ReadError):
                    await first.get_response([message], options=options)
            finally:
                await first.close()
            assert len(provider.posts) == len(authority.rows) == 1
            assert authority.rows[0]["outcome"] == "complete"
            second = client()
            try:
                with pytest.raises(ValueError, match="closed"):
                    await second.get_response([message], options=options)
            finally:
                await second.close()
            assert len(provider.posts) == len(authority.rows) == len(authority.calls) == 1


@pytest.mark.asyncio
async def test_native_hosted_helper_actual_provider(approve_fixture_loopback):
    provider = FakeProvider()
    provider.helper_mode = True
    async with listen(
        Starlette(routes=[Route("/v1/chat/completions", provider.__call__, methods=["POST"])])
    ) as origin:
        authority = FakeAuthority(provider, origin)
        async with httpx.AsyncClient(transport=httpx.MockTransport(authority)) as internal:
            service = BlackboardClient("http://authority.fixture", "not-the-sender", internal)
            settings = Settings(
                service_token=SecretStr("synthetic-service-only"),
                minio_root_password=SecretStr("fake-minio"),
                envd_token_secret=SecretStr("fake-envd"),
                pi_runtime_sender_token=SecretStr("synthetic-runtime-sender"),
                pi_provider_allowed_origins=("https://approved.example",),
            )
            runtime = RunnerModelProxy(settings, service, http=internal)
            async with listen(runtime.app) as runtime_origin:
                node = Path(__file__).resolve().parents[1] / "task-runner"
                proc = await asyncio.create_subprocess_exec(
                    "node",
                    "--test",
                    "--test-name-pattern=direct P6a",
                    "test/helper-background.test.mjs",
                    cwd=node,
                    env={"PATH": os.environ["PATH"], "BBX_TEST_PI_RUNTIME": runtime_origin},
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, error = await proc.communicate()
                assert proc.returncode == 0, output.decode() + error.decode()
            assert provider.parent_delegated and provider.child_tool_sent
            assert len(provider.posts) == len(authority.rows) == len(authority.calls)
            assert (
                sum(
                    call["prepare"]["proof"]["binding"]["binding_id"] == "child"
                    for call in authority.calls.values()
                )
                == 2
            )
            await runtime.close()


async def test_legacy_review_model_metadata_does_not_choose_pi():
    from bbx_runtime.settings import Settings

    old_snapshot = {
        "alias": "original-review-model",
        "catalog_revision": "a" * 64,
        "label": "Original model",
        "context_window": 4096,
        "max_output_tokens": 80,
        "input_modalities": ["text"],
        "tool_call": True,
        "usd_rates": {"input": "1", "output": "1", "cache_read": "1", "cache_write": "1"},
        "accounting_source_id": "original-gateway",
        "route_revision": 1,
        "route_digest": "b" * 64,
        "model_catalog_digest": "c" * 64,
    }
    profile = ModelConfig.model_validate(
        {
            "provider": "openai_compatible",
            "model": "unused",
            "base_url": "https://unused.invalid/v1",
            "reasoning_effort": "off",
            "price": {"currency": "USD"},
        }
    )
    settings = Settings(
        service_token=SecretStr("fixture"),
        minio_root_password=SecretStr("fixture"),
        envd_token_secret=SecretStr("fixture"),
        runner_gateway_url="https://original-gateway.invalid",
    )
    async with BlackboardClient("http://authority.fixture", "fixture") as service:
        client = make_task_model_client(
            profile,
            source={"review_model": old_snapshot, "gateway_model": old_snapshot},
            settings=settings,
            service=service,
            task_id="original-task",
            purpose="review",
            proof={},
            explore_max_steps=1,
            conclude_grace_calls=0,
            max_duration_seconds=30,
        )
        try:
            assert client.gateway is not None
            assert client.trusted_model is None
        finally:
            await client.close()
