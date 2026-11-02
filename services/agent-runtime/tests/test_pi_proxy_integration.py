"""Native Node proxy routing over local simulated model endpoints only."""

import asyncio
import json
from pathlib import Path

import pytest
from bbx_runtime import models
from bbx_runtime.model_errors import ModelStreamError, model_error_metadata
from bbx_runtime.models import load_runtime_profile, make_client
from bbx_runtime.pi import Message

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize(
    "provider,status", [("openai_chat", 200), ("gemini", 200), ("gemini", 429)]
)
async def test_pi_uses_runtime_http_proxy_and_honors_no_proxy(
    monkeypatch, direct, provider, status
):
    requests, proxy_requests = [], []
    response = (
        "data: "
        + json.dumps(
            {
                "id": "offline",
                "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
            if provider == "openai_chat"
            else {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "done"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 12,
                },
            }
        )
        + "\n\n"
    )
    payload = (
        response.encode()
        if status == 200
        else json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}).encode()
    )

    async def handle(reader, writer, *, proxy=False):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            if proxy:
                proxy_requests.append(header.split(b"\r\n", 1)[0])
            if header.startswith(b"CONNECT "):
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in header.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            body = await reader.readexactly(length)
            requests.append(json.loads(body))
            content_type = "text/event-stream" if status == 200 else "application/json"
            reason = "OK" if status == 200 else "Too Many Requests"
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n".encode()
                + b"Connection: close\r\nRetry-After: 1\r\nX-Request-Id: req-safe\r\n"
                + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def proxy_handler(reader, writer):
        await handle(reader, writer, proxy=True)

    async with (
        await asyncio.start_server(proxy_handler, "127.0.0.1", 0) as proxy,
        await asyncio.start_server(handle, "127.0.0.1", 0) as endpoint,
    ):
        proxy_port = proxy.sockets[0].getsockname()[1]
        endpoint_port = endpoint.sockets[0].getsockname()[1]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.setenv(key, f"http://127.0.0.1:{proxy_port}")
        for key in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(key, "localhost,127.0.0.1")
        monkeypatch.setattr(models, "in_container", lambda: False)
        model = load_runtime_profile(
            Path(__file__).resolve().parents[3] / "profiles/default"
        ).models.explore
        model = model.model_copy(
            update={
                "provider": provider,
                "model": "gemini-test" if provider == "gemini" else "offline",
                "base_url": f"http://127.0.0.1:{endpoint_port}/v1"
                if direct
                else "http://model.invalid/v1",
            }
        )
        client = make_client(
            model,
            api_key="test-only",
            explore_max_steps=1,
            conclude_grace_calls=0,
            max_duration_seconds=10,
            max_retries=0,
        )
        try:
            call = client.get_response([Message("user", ["hello"])])
            if status == 200:
                result = await asyncio.wait_for(call, 10)
                assert result.text == "done"
            else:
                with pytest.raises(ModelStreamError) as caught:
                    await asyncio.wait_for(call, 10)
                metadata = model_error_metadata(caught.value)
                assert metadata["http_status"] == 429 and metadata["transient"] is True
                assert metadata["request_id"] == "req-safe" and metadata["retry_after"] == "1"
            assert len(requests) == 1
            assert len(proxy_requests) == (0 if direct else 1)
            if provider == "openai_chat":
                assert requests[0]["stream"] is True
            else:
                assert requests[0]["contents"][0]["parts"][0]["text"] == "hello"
        finally:
            await client.close()
