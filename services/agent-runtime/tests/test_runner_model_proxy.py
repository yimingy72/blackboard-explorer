"""The Runtime is an identity/key/transport seam; native gateway records own money."""

import asyncio
import gzip
import hashlib
import json
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from bbx_contracts.native_gateway import TRUSTED_ATTRIBUTE_HEADERS, NativeTransportObservation
from bbx_contracts.runner import PROTOCOL
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.runner_proxy import RunnerModelProxy
from bbx_runtime.settings import Settings
from pydantic import SecretStr
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request


def digest(value):
    return hashlib.sha256(value).hexdigest()


def incoming(raw, headers):
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/chat/completions",
            "scheme": "http",
            "server": ("runtime", 80),
            "query_string": b"",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        },
        receive,
    )


@asynccontextmanager
async def fixture(mode="opaque", capacity=8, hold=False):
    state = SimpleNamespace(
        claimed=False,
        calls=[],
        notes=[],
        responses=[],
        note_entered=asyncio.Event(),
        note_gate=asyncio.Event(),
    )
    if not hold:
        state.note_gate.set()
    fence = {"task_id": "task", "runner_id": "runner", "pod_uid": "pod", "epoch": 1}
    binding = {
        "business_contract": "challenge-board-v1",
        "binding_id": "binding",
        "platform_session_id": "platform",
        "agent_id": "stored-agent",
        "generation": 1,
        "journal_generation": "journal",
        "assignment_ref": None,
    }
    raw = (
        '{"model":"openai/probe","stream":true,'
        '"messages":[{"role":"user","content":"你好"}],"max_completion_tokens":32}'
    ).encode()
    wire = b"opaque\x00wire\xffbytes"
    if mode == "compressed":
        wire = gzip.compress(wire)
    elif mode in {"budget429", "provider429"}:
        wire = json.dumps(
            {"error": {"code": "budget_exceeded" if mode == "budget429" else "rate_limit_exceeded"}}
        ).encode()
    attributes = {
        "bbx_task_id": "task",
        "bbx_binding_id": "binding",
        "bbx_epoch": "1",
        "bbx_agent_id": "stored-agent",
        "bbx_attempt_id": "attempt",
        "bbx_call_id": "call",
        "bbx_sdk_session_id": "sdk",
    }
    headers = {
        "authorization": "Bearer binding-short-token",
        "x-bbx-task-id": "task",
        "x-bbx-runner-id": "runner",
        "x-bbx-pod-uid": "pod",
        "x-bbx-runtime-epoch": "1",
        "x-bbx-binding-id": "binding",
        "x-bbx-sdk-session-id": "sdk",
        "x-bbx-call-id": "call",
        "x-bbx-attempt-id": "attempt",
        "x-bbx-body-digest": digest(raw),
        "x-bbx-agent-id": "forged-agent",
        "x-bbx-epoch": "999",
        "x-request-id": "forged",
    }
    cursor = {
        "offset": 10,
        "seq": 1,
        "hash": "1" * 64,
        "leaf_id": "entry",
        "anchor_offset": 0,
        "anchor_size": 10,
        "anchor_hash": "2" * 64,
    }
    metadata = {
        "protocol": PROTOCOL,
        "fence": fence,
        "binding": binding,
        "sdk_session_id": "sdk",
        "call_id": "call",
        "attempt_id": "attempt",
        "journal": cursor,
        "purpose": "turn",
        "profile_revision": 1,
        "prompt_revision": 1,
        "model_snapshot": {
            "id": "openai/probe",
            "api": "openai-completions",
            "input": ["text"],
            "reasoning": False,
            "thinking_level": "off",
            "context_window": 4096,
            "max_output_tokens": 32,
        },
        "body_json": raw.decode(),
        "body_digest": digest(raw),
    }

    class Wire(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield wire[:4]
            if mode == "disconnect":
                raise httpx.ReadError("synthetic native connection lost")
            yield wire[4:]

    async def transport(request):
        if request.url.host == "native":
            assert request.url.path == "/v1/chat/completions" and request.content == raw
            assert request.headers["authorization"] == "Bearer task-private-key"
            assert "x-request-id" not in request.headers
            assert {
                name: request.headers[header] for name, header in TRUSTED_ATTRIBUTE_HEADERS.items()
            } == attributes
            state.calls.append(request)
            response = httpx.Response(
                429 if mode in {"budget429", "provider429"} else 200,
                headers={
                    "content-type": "application/octet-stream",
                    **({"content-encoding": "gzip"} if mode == "compressed" else {}),
                },
                stream=Wire(),
            )
            state.responses.append(response)
            return response
        assert request.url.host == "board"
        body = json.loads(request.content)
        if request.url.path.endswith("/attempts/admit"):
            assert (
                body == metadata
                and request.headers["authorization"] == "Bearer binding-short-token"
            )
            return httpx.Response(
                200,
                json={
                    "protocol": PROTOCOL,
                    "confirmed": True,
                    "attempt_id": "attempt",
                    "call_id": "call",
                    "request_digest": digest(request.content),
                },
            )
        if request.url.path.endswith("/dispatch"):
            assert request.headers["authorization"] == "Bearer runtime-service"
            assert request.headers["x-bbx-runner-authorization"] == "Bearer binding-short-token"
            if body["fence"]["epoch"] != 1 or body["body_digest"] != digest(raw):
                return httpx.Response(409, json={"detail": "changed body or old epoch"})
            grant = {
                "protocol": PROTOCOL,
                "confirmed": True,
                "send_allowed": not state.claimed,
                "attempt_id": "attempt",
                "state": "dispatched",
                "body_digest": digest(raw),
            }
            if not state.claimed:
                state.claimed = True
                grant.update(
                    dispatch_id="dispatch",
                    native_gateway_url="http://other"
                    if mode == "wrong_gateway"
                    else "http://native",
                    task_gateway_key="task-private-key",
                    accounting_source_id="stable-source",
                    trusted_attributes=attributes,
                    authorized_wait_until_ms=int((time.time() + 5) * 1000),
                )
            return httpx.Response(200, json=grant)
        if request.url.path.endswith("/transport"):
            note = NativeTransportObservation.model_validate(body)
            assert request.headers["authorization"] == "Bearer runtime-service"
            state.notes.append(note)
            state.note_entered.set()
            await state.note_gate.wait()
            return httpx.Response(
                200,
                json={
                    "protocol": PROTOCOL,
                    "confirmed": True,
                    "attempt_id": "attempt",
                    "dispatch_id": "dispatch",
                    "request_digest": digest(request.content),
                },
            )
        raise AssertionError(
            "No provider credentials, prices, route proofs or capture endpoints may be consulted: "
            + request.url.path
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
    service = BlackboardClient("http://board", "runtime-service", http_client=http)
    settings = Settings(
        service_token=SecretStr("runtime-service"),
        minio_root_password=SecretStr("test"),
        envd_token_secret=SecretStr("test"),
        runner_gateway_url="http://native",
        runner_proxy_max_inflight=capacity,
    )
    proxy = RunnerModelProxy(settings, service, http=http)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://runtime"
        ) as client:
            result = await client.post(
                "/attempts/admit",
                headers={"authorization": "Bearer binding-short-token"},
                json=metadata,
            )
            assert result.status_code == 200 and "task-private-key" not in result.text
        yield proxy, state, raw, headers, wire
    finally:
        state.note_gate.set()
        await proxy.close()
        await http.aclose()


@pytest.mark.asyncio
async def test_native_bytes_keys_attributes_and_status_are_opaque():
    for mode in ("opaque", "compressed", "budget429", "provider429"):
        async with fixture(mode) as (proxy, state, raw, headers, wire):
            response = await proxy.dispatch(incoming(raw, headers))
            stream = cast(AsyncGenerator[bytes, None], response.body_iterator)
            first = await anext(stream)
            assert first == wire[:4], (
                "transport forwards deltas without waiting or interpreting SSE"
            )
            assert first + b"".join([chunk async for chunk in stream]) == wire
            assert response.status_code == (429 if mode in {"budget429", "provider429"} else 200)
            if mode == "compressed":
                assert response.headers["content-encoding"] == "gzip"
            assert len(state.calls) == len(state.notes) == 1
            assert state.notes[0].phase == "response_ended"
            assert set(state.notes[0].model_dump()) == {
                "protocol",
                "attempt_id",
                "dispatch_id",
                "http_status",
                "phase",
            }


@pytest.mark.asyncio
async def test_once_dispatch_rejects_changed_body_epoch_and_unpinned_gateway():
    async with fixture() as (proxy, state, raw, headers, _):
        first = await proxy.dispatch(incoming(raw, headers))
        for body, extra in [
            (raw, {}),
            (raw + b" ", {"x-bbx-body-digest": digest(raw + b" ")}),
            (raw, {"x-bbx-runtime-epoch": "2"}),
        ]:
            with pytest.raises(HTTPException) as denied:
                await proxy.dispatch(incoming(body, {**headers, **extra}))
            assert denied.value.status_code == 409
        assert not state.notes, "refusals cannot observe or clear the concurrent granted original"
        _ = b"".join(
            [chunk async for chunk in cast(AsyncGenerator[bytes, None], first.body_iterator)]
        )
        assert len(state.calls) == len(state.notes) == 1
    async with fixture("wrong_gateway", capacity=1) as (proxy, state, raw, headers, _):
        with pytest.raises(HTTPException) as denied:
            await proxy.dispatch(incoming(raw, headers))
        assert denied.value.status_code == 502 and not state.calls
        assert state.notes[0].phase == "not_sent" and not proxy.active and not proxy.slots.locked()


@pytest.mark.asyncio
async def test_disconnect_response_start_and_runtime_close_observe_once():
    for mode in ("disconnect", "cancel", "start_failure", "runtime_close"):
        async with fixture(mode, capacity=1, hold=mode == "runtime_close") as (
            proxy,
            state,
            raw,
            headers,
            _,
        ):
            response = await proxy.dispatch(incoming(raw, headers))
            if mode in {"start_failure", "runtime_close"}:
                started, release = asyncio.Event(), asyncio.Event()

                async def failed_send(
                    message, entered=started, gate=release, closing=mode == "runtime_close"
                ):
                    assert message["type"] == "http.response.start"
                    entered.set()
                    if closing:
                        await gate.wait()
                    raise OSError("synthetic response start failed")

                scope = dict(incoming(raw, headers).scope)
                scope["asgi"] = {"version": "3.0", "spec_version": "2.4"}
                serving = asyncio.create_task(
                    response(scope, incoming(raw, headers).receive, failed_send)
                )
                await started.wait()
                if mode == "runtime_close":
                    closing = asyncio.create_task(proxy.close())
                    await state.note_entered.wait()
                    release.set()
                    assert len(state.notes) == len(proxy.active) == 1
                    state.note_gate.set()
                    await closing
                with pytest.raises((ClientDisconnect, asyncio.CancelledError)):
                    await serving
            else:
                stream = cast(AsyncGenerator[bytes, None], response.body_iterator)
                await anext(stream)
                if mode == "cancel":
                    await stream.aclose()
                else:
                    with pytest.raises(httpx.ReadError):
                        await anext(stream)
            assert state.responses[0].is_closed and not proxy.slots.locked() and not proxy.active
            assert (
                len(state.notes) == len(state.calls) == 1 and state.notes[0].phase == "disconnected"
            )
