"""Public configuration and secret handling boundaries."""

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.platform import McpInput
from bbx_blackboard.settings import Settings
from pydantic import SecretStr, ValidationError


def config():
    return Settings.model_construct(
        postgres_password=SecretStr("test-db"),
        minio_root_password=SecretStr("test-minio"),
        service_token=SecretStr("test-service"),
        agent_token_secret=SecretStr("platform-test-signing-key-at-least-32"),
        admin_users=SecretStr("tester:test"),
    )


@pytest.mark.parametrize(
    "url", ["file:///tmp/socket", "https://user:pass@host/mcp", "https://host/mcp?token=abc"]
)
def test_reject_credentials_in_connection_url(url):
    with pytest.raises(ValidationError):
        McpInput(label="test", url=url)


async def test_platform_has_no_provider_model_write_surface():
    options = config()
    app = create_app(options)
    headers = {"Authorization": f"Bearer {issue_user_token(options, 'tester')}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in (
            "/platform/models",
            "/platform/providers",
            "/platform/import-environment-key",
            "/platform/gateway-models/publications",
            "/platform/gateway-models/maintenance",
        ):
            result = await client.post("/api" + path, headers=headers, json={"api_key": "fixture"})
            assert result.status_code in {404, 405}
        assert (
            await client.post("/api/internal/gateway-review-key/prepare", headers=headers)
        ).status_code == 404


async def test_mcp_discovery_lists_all_pages_without_executing_tools(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import bbx_blackboard.mcp_catalog as module

    calls = []

    @asynccontextmanager
    async def transport(url, *, http_client):
        assert http_client.headers["X-API-Key"] == "fixture-mcp-secret"
        assert url == "https://example.invalid/mcp"
        yield None, None, None

    class Session:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def initialize(self):
            pass

        async def list_tools(self, cursor=None):
            calls.append(cursor)
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="first" if cursor is None else "second",
                        description="Tool",
                        inputSchema={"type": "object", "required": ["value"]},
                        outputSchema=None,
                        annotations=None,
                    )
                ],
                nextCursor="next" if cursor is None else None,
            )

    monkeypatch.setattr(module, "public_address", AsyncMock(return_value="1.1.1.1"))
    monkeypatch.setattr(module, "streamable_http_client", transport)
    monkeypatch.setattr(module, "ClientSession", Session)
    tools = await module.discover_tools(
        "https://example.invalid/mcp", {"X-API-Key": "fixture-mcp-secret"}, "fixture-mcp-secret"
    )
    assert [tool.name for tool in tools] == ["first", "second"]
    assert tools[0].input_schema["required"] == ["value"]
    assert calls == [None, "next"]
