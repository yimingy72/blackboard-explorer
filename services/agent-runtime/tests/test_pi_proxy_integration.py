"""Native Node proxy routing over local simulated model endpoints only."""

import asyncio
import json
from pathlib import Path

import pytest
from bbx_runtime import models
from bbx_runtime.models import load_runtime_profile, make_client
from bbx_runtime.pi import Message

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direct", [False, True])
async def test_pi_uses_runtime_http_proxy_and_honors_no_proxy(monkeypatch, direct):
    requests, proxy_requests = [], []
    response = (
        "data: "
        + json.dumps(
            {
                "id": "offline",
                "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        )
        + "\n\ndata: [DONE]\n\n"
    )
    payload = response.encode()

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
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n"
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
                "provider": "openai_chat",
                "model": "offline",
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
            result = await asyncio.wait_for(client.get_response([Message("user", ["hello"])]), 10)
            assert result.text == "done"
            assert len(requests) == 1
            assert len(proxy_requests) == (0 if direct else 1)
            assert requests[0]["stream"] is True
        finally:
            await client.close()
