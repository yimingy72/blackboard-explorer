"""Fixed native HTTP shapes and uncertain create failures, without native money or DB logic."""

import asyncio
import json

import httpx
import pytest
from bbx_blackboard import native_admin
from bbx_blackboard.native_admin import KEYS_PATH, NativeAdmin
from fastapi import HTTPException
from pydantic import SecretStr
from test_ctf_api import settings


def configured():
    return settings().model_copy(
        update={
            "native_gateway_admin_url": "http://native-admin",
            "native_gateway_data_url": "http://native-data",
            "native_gateway_admin_token": SecretStr("admin-private-token"),
        }
    )


@pytest.mark.asyncio
async def test_fixed_io_keeps_key_creation_single_and_admin_separate_from_task():
    calls = []
    resource = {
        "id": "native-uuid",
        "value": {"keyHash": "sha256:" + "a" * 64, "metadata": {"name": "task-key"}},
    }

    def transport(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.host, request.url.path, body))
        if request.url.host == "native-data":
            assert request.url.path == "/v1/models"
            assert request.headers["authorization"] == "Bearer task-private-key"
            return httpx.Response(200, json={"data": [{"id": "openai/alias"}]})
        assert request.headers["authorization"] == "Bearer admin-private-token"
        if request.method == "PUT":
            assert request.url.path == KEYS_PATH and body == {
                "resources": [{"value": resource["value"]}]
            }
            return httpx.Response(200, json={"resources": [resource]})
        if request.method == "DELETE":
            assert request.url.path == KEYS_PATH + "/native-uuid"
            return httpx.Response(404)
        if request.url.path == KEYS_PATH:
            return httpx.Response(200, json={"resources": [resource]})
        if request.url.path.endswith("/status"):
            assert request.url.params["apiKeyName"] == "task-key"
            return httpx.Response(200, json={"budgets": []})
        if request.url.path.endswith("/search"):
            assert body == {
                "limit": 1,
                "includeAttributes": True,
                "filters": {"attributes": {"bbx_task_id": "task"}},
                "cursor": "opaque",
            }
            return httpx.Response(200, json={"logs": [], "nextCursor": None})
        assert body == {"id": "stable-log-id", "includePayload": False}
        return httpx.Response(200, json={"log": {"id": "stable-log-id"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = NativeAdmin(configured(), http=http)
        assert (await client.create_key(resource["value"]))["resources"] == [resource]
        assert await client.resources() == [resource]
        assert await client.models("task-private-key") == [{"id": "openai/alias"}]
        assert await client.budget("task-key") == {"budgets": []}
        assert (await client.logs({"bbx_task_id": "task"}, "opaque", 1))["logs"] == []
        assert (await client.get_log("stable-log-id"))["log"]["id"] == "stable-log-id"
        await client.delete_key("native-uuid")
        await client.close()
        assert not http.is_closed
    assert sum(method == "PUT" for method, *_ in calls) == 1


@pytest.mark.asyncio
async def test_timeout_bad_directory_and_bounded_errors_are_generic_and_not_retried(monkeypatch):
    calls = []

    def transport(request):
        calls.append(request.url.path)
        if request.method == "PUT":
            raise httpx.ReadTimeout("test-only uncertain native create ACK", request=request)
        if request.url.host == "native-data":
            return httpx.Response(200, json={"data": None})
        if request.url.path.endswith("/status"):
            return httpx.Response(503, text="secret=must-not-be-returned")
        return httpx.Response(200, content=b" " * 65)

    monkeypatch.setattr(native_admin, "MAX_RESPONSE_BYTES", 64)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        disabled = NativeAdmin(
            configured().model_copy(
                update={"native_gateway_admin_url": None, "native_gateway_data_url": None}
            ),
            http=http,
        )
        with pytest.raises(HTTPException) as no_config:
            await disabled.resources()
        assert no_config.value.status_code == 503 and calls == []
        client = NativeAdmin(configured(), http=http)
        operations = [
            client.create_key({"keyHash": "sha256:" + "b" * 64}),
            client.models("task-private-key"),
            client.budget("task-key"),
            client.resources(),
        ]
        for operation in operations:
            with pytest.raises(HTTPException) as refused:
                await operation
            assert refused.value.status_code == 503
            assert refused.value.detail == "Native gateway management is unavailable"
        assert calls.count(KEYS_PATH) == 2, (
            "one create plus one list; timeout cannot trigger create retry"
        )

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(20):
                yield b" "
                await asyncio.sleep(0.01)
            yield b"{}"

    monkeypatch.setattr(native_admin, "REQUEST_TIMEOUT_SECONDS", 0.03)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip()))
    ) as http:
        client = NativeAdmin(configured(), http=http)
        with pytest.raises(HTTPException) as deadline:
            await client.resources()
        assert deadline.value.status_code == 503

    malformed = [
        ({"resources": [{"id": 1, "value": {}}]}, "resources"),
        ({"data": [{}]}, "models"),
        ({"logs": [None], "nextCursor": 42}, "logs"),
    ]
    for payload, operation in malformed:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request, value=payload: httpx.Response(200, json=value)
            )
        ) as http:
            client = NativeAdmin(configured(), http=http)
            pending = (
                client.resources()
                if operation == "resources"
                else (client.models("task-key") if operation == "models" else client.logs({}))
            )
            with pytest.raises(HTTPException) as shape:
                await pending
            assert shape.value.status_code == 503
