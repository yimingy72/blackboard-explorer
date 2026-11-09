"""Public configuration and secret handling boundaries."""

from unittest.mock import AsyncMock

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
        ).status_code == 403


async def test_mcp_discovery_lists_all_pages_without_executing_tools_or_leaking_errors(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import bbx_blackboard.platform as module

    settings = config()
    app = create_app(settings)
    platform = AsyncMock()
    app.state.platform_store = platform
    platform.get.return_value = {
        "config": {"url": "https://example.invalid/mcp", "auth_header": "X-Key", "auth_scheme": ""}
    }
    platform.credentials.return_value = {"secret": "fixture-mcp-secret"}
    calls = []

    @asynccontextmanager
    async def transport(url, *, http_client):
        assert http_client.headers["X-Key"] == "fixture-mcp-secret"
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
                        name="first" if cursor is None else "second", description="Tool"
                    )
                ],
                nextCursor="next" if cursor is None else None,
            )

    monkeypatch.setattr(module, "streamable_http_client", transport)
    monkeypatch.setattr(module, "ClientSession", Session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/platform/mcp-servers/test/versions/1/tools",
            headers={"Authorization": "Bearer test-service"},
        )
        assert [tool["name"] for tool in response.json()["tools"]] == ["first", "second"]
        assert calls == [None, "next"]
        assert "fixture-mcp-secret" not in response.text

        async def broken(self, cursor=None):
            raise RuntimeError("fixture-mcp-secret")

        monkeypatch.setattr(Session, "list_tools", broken)
        failure = await client.get(
            "/api/platform/mcp-servers/test/versions/1/tools",
            headers={"Authorization": "Bearer test-service"},
        )
        assert failure.status_code == 502
        assert "fixture-mcp-secret" not in failure.text
