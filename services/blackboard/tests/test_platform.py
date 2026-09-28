"""Public configuration and secret handling boundaries."""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_blackboard.platform import McpInput, ModelInput
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


def test_model_currency_and_environment_source_validation():
    fields = dict(
        label="test",
        model="model",
        provider="openai_chat",
        base_url="https://example.invalid/v1",
        price={
            "currency": "CNY",
            "cache_hit_per_m": 0,
            "cache_miss_per_m": 1,
            "output_per_m": 2,
            "off_peak": False,
        },
    )
    assert ModelInput.model_validate(fields).credential_source == "stored"
    with pytest.raises(ValidationError, match="环境密钥"):
        ModelInput.model_validate({**fields, "credential_source": "environment"})
    fields["price"] = {"currency": "USD"}
    with pytest.raises(ValidationError, match="人民币"):
        ModelInput.model_validate(fields)


async def test_platform_credentials_are_service_only_and_validation_never_echoes_secrets():
    settings = config()
    app = create_app(settings)
    platform = AsyncMock()
    app.state.platform_store = platform
    platform.credentials.return_value = {
        "secret": "private-fixture-token",
        "credential_source": "stored",
    }
    base = "/api/platform/models/example/versions/1/credentials"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for token in (
            None,
            issue_user_token(settings, "tester"),
            issue_agent_token(settings, uuid4(), "agent-1"),
        ):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            denied = await client.get(base, headers=headers)
            assert denied.status_code in {401, 403}
            assert "private-fixture-token" not in denied.text
        assert platform.credentials.call_count == 0
        response = await client.get(base, headers={"Authorization": "Bearer test-service"})
        assert response.status_code == 200
        assert response.json()["secret"] == "private-fixture-token"
        invalid = await client.post(
            "/api/platform/models/example",
            headers={"Authorization": "Bearer test-service"},
            json={"api_key": "private-fixture-token"},
        )
        assert invalid.status_code == 422
        assert "private-fixture-token" not in invalid.text
        assert "input" not in invalid.json()["detail"][0]


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
