"""Import only a present legacy environment key during runtime startup."""

import json
from typing import cast
from unittest.mock import AsyncMock

import httpx
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.server import _import_legacy_key
from bbx_runtime.settings import SchedulerSettings
from pydantic import SecretStr


async def test_import_legacy_key_uses_private_service_api() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"imported": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        service = BlackboardClient("http://board", "service-token", http)
        settings = SchedulerSettings.model_construct(deepseek_api_key=SecretStr("fake-key"))
        await _import_legacy_key(settings, service)
    assert len(requests) == 1
    assert requests[0].url.path == "/api/platform/import-environment-key"
    assert requests[0].headers["Authorization"] == "Bearer service-token"
    assert json.loads(requests[0].content) == {"api_key": "fake-key"}


async def test_platform_only_startup_skips_import() -> None:
    service = AsyncMock(spec=BlackboardClient)
    settings = SchedulerSettings.model_construct(deepseek_api_key=SecretStr(""))
    await _import_legacy_key(settings, cast(BlackboardClient, service))
    service.import_environment_key.assert_not_awaited()
