"""Model discovery reads metadata only and keeps stored credentials at their endpoint."""

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import model_discovery as discovery
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_blackboard.platform import ModelInput, PlatformStore
from bbx_blackboard.settings import Settings
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError


@pytest.fixture
def settings():
    return Settings.model_construct(
        postgres_password=SecretStr("test-db"),
        minio_root_password=SecretStr("test-minio"),
        service_token=SecretStr("test-service"),
        agent_token_secret=SecretStr("platform-test-signing-key-at-least-32"),
        admin_users=SecretStr("tester:test"),
    )


@pytest.fixture
def mock_http(monkeypatch):
    original = httpx.AsyncClient

    def install(handler):
        def client(**kwargs):
            if "transport" not in kwargs:
                kwargs["transport"] = httpx.MockTransport(handler)
            return original(**kwargs)

        monkeypatch.setattr(discovery.httpx, "AsyncClient", client)

    return install


async def test_discovery_authentication_and_unsaved_credentials(settings, mock_http):
    app = create_app(settings)
    app.state.platform_store = AsyncMock()
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/v1/models"
        assert request.headers["Authorization"] == "Bearer fixture-key"
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    mock_http(handler)
    body = {"provider": "openai_chat", "credentials": {"api_key": "fixture-key"}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for token in [None, issue_agent_token(settings, uuid4(), "agent-1")]:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = await client.post(
                "/api/platform/models/discover", json=body, headers=headers
            )
            assert response.status_code in {401, 403}
        assert not calls
        response = await client.post(
            "/api/platform/models/discover",
            json=body,
            headers={"Authorization": f"Bearer {issue_user_token(settings, 'tester')}"},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["models"] == [{"id": "model-a", "metadata_source": "provider_api"}]
    assert "fixture-key" not in response.text
    app.state.platform_store.save.assert_not_awaited()


@pytest.mark.parametrize(
    ("provider", "base_url", "expected"),
    [
        ("openai_chat", "https://api.openai.com/v1", 200),
        ("openai_chat", "https://other.invalid/v1", 422),
        ("openai_responses", "https://api.openai.com/v1", 422),
    ],
)
async def test_saved_credentials_only_reused_for_exact_connection(
    settings, mock_http, provider, base_url, expected
):
    app = create_app(settings)
    platform = PlatformStore(Mock(), "fixture-signing-key")
    platform.get = AsyncMock(
        return_value={
            "config": {"provider": "openai_chat", "base_url": "https://api.openai.com/v1"},
            "secret_ciphertext": platform._encrypt_credentials({"api_key": "stored-fixture-key"}),
        }
    )
    app.state.platform_store = platform
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer stored-fixture-key"
        return httpx.Response(200, json={"data": [{"id": "a"}]})

    mock_http(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/platform/models/discover",
            json={
                "provider": provider,
                "base_url": base_url,
                "saved_name": "example",
                "saved_version": 2,
            },
            headers={"Authorization": "Bearer test-service"},
        )
    assert response.status_code == expected
    assert len(requests) == (1 if expected == 200 else 0)
    assert "stored-fixture-key" not in response.text
    platform.get.assert_awaited_once_with("models", "example", 2)


async def test_changed_endpoint_uses_only_explicit_new_credentials(settings, mock_http):
    app = create_app(settings)
    platform = AsyncMock()
    platform.get.return_value = {
        "config": {
            "provider": "openai_chat",
            "base_url": "https://old.invalid/v1",
        }
    }
    platform._decrypt_model_credentials = Mock(side_effect=AssertionError("must not decrypt"))
    app.state.platform_store = platform

    def handler(request):
        assert request.url.host == "new.invalid"
        assert request.headers["Authorization"] == "Bearer new-fixture-key"
        return httpx.Response(200, json={"data": []})

    mock_http(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/platform/models/discover",
            json={
                "provider": "openai_chat",
                "base_url": "https://new.invalid/v1",
                "saved_name": "example",
                "credentials": {"api_key": "new-fixture-key"},
            },
            headers={"Authorization": "Bearer test-service"},
        )
    assert response.status_code == 200
    platform._decrypt_model_credentials.assert_not_called()


@pytest.mark.parametrize("containerized", [False, True])
@pytest.mark.parametrize("changed_endpoint", [False, True])
async def test_local_endpoint_routes_after_saved_credential_matching(
    settings, mock_http, monkeypatch, caplog, containerized, changed_endpoint
):
    monkeypatch.setattr(discovery, "in_container", lambda: containerized)
    app = create_app(settings)
    platform = PlatformStore(Mock(), "fixture-signing-key")
    original_url = "http://127.0.0.1:3425/gateway/v1"
    config = {"provider": "openai_chat", "base_url": original_url}
    platform.get = AsyncMock(
        return_value={
            "config": config,
            "secret_ciphertext": platform._encrypt_credentials({"api_key": "local-fixture-key"}),
        }
    )
    app.state.platform_store = platform
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == ("host.docker.internal" if containerized else "127.0.0.1")
        assert request.url.port == 3425
        assert request.url.path == "/gateway/v1/models"
        assert request.headers["Authorization"] == "Bearer local-fixture-key"
        return httpx.Response(200, json={"data": [{"id": "local-model"}]})

    mock_http(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/platform/models/discover",
            json={
                "provider": "openai_chat",
                "base_url": original_url.replace("127.0.0.1", "host.docker.internal")
                if changed_endpoint
                else original_url,
                "saved_name": "local-model",
                "saved_version": 2,
            },
            headers={"Authorization": "Bearer test-service"},
        )
    assert response.status_code == (422 if changed_endpoint else 200)
    assert len(requests) == (0 if changed_endpoint else 1)
    assert config["base_url"] == original_url
    assert "local-fixture-key" not in response.text + caplog.text


@pytest.mark.parametrize(
    ("provider", "payload", "expected"),
    [
        (
            "deepseek",
            {
                "data": [
                    {
                        "id": "deepseek-real",
                        "name": "DeepSeek",
                        "context_window": 1048576,
                        "max_output_tokens": 32768,
                        "input_modalities": ["text", "image"],
                    }
                ]
            },
            {"context_window": 1048576, "max_output_tokens": 32768, "supports_vision": True},
        ),
        (
            "anthropic",
            {
                "data": [
                    {
                        "id": "claude-real",
                        "max_input_tokens": 200000,
                        "max_tokens": 32000,
                        "capabilities": {"image_input": {"supported": True}},
                    }
                ]
            },
            {"max_input_tokens": 200000, "max_output_tokens": 32000, "supports_vision": True},
        ),
        (
            "gemini",
            {
                "models": [
                    {
                        "name": "models/gemini-real",
                        "inputTokenLimit": 1048576,
                        "outputTokenLimit": 65536,
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ]
            },
            {"max_input_tokens": 1048576, "max_output_tokens": 65536},
        ),
        (
            "mistral",
            {
                "data": [
                    {
                        "id": "mistral-real",
                        "max_context_length": 128000,
                        "capabilities": {"vision": False},
                    }
                ]
            },
            {"context_window": 128000, "supports_vision": False},
        ),
    ],
)
async def test_normalizes_only_declared_provider_metadata(mock_http, provider, payload, expected):
    mock_http(lambda request: httpx.Response(200, json=payload))
    result = await discovery.discover_models(
        provider, "https://provider.invalid", {"api_key": "fixture-key"}
    )
    model = result.models[0].model_dump(exclude_none=True)
    assert {key: model[key] for key in expected} == expected
    assert "price" not in model
    if provider == "gemini":
        assert model["id"] == "gemini-real"
        assert "supports_vision" not in model and "context_window" not in model


async def test_unknown_limits_capabilities_and_prices_are_not_inferred(mock_http):
    mock_http(
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "gpt-vision-1m",
                        "context_window": True,
                        "max_input_tokens": "999",
                        "max_output_tokens": -1,
                        "price": {"input": 0.1},
                        "supports_vision": "true",
                    }
                ]
            },
        )
    )
    result = await discovery.discover_models("openai_compatible", "https://gateway.invalid/v1", {})
    assert result.models[0].model_dump(exclude_none=True) == {
        "id": "gpt-vision-1m",
        "metadata_source": "provider_api",
    }
    assert result.warnings


async def test_explicit_pricing_preserves_currency_and_units(mock_http):
    mock_http(
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "priced",
                        "price": {"currency": "USD", "cache_miss_per_m": 2, "output_per_m": 8},
                    }
                ]
            },
        )
    )
    result = await discovery.discover_models("openai_compatible", "https://gateway.invalid", {})
    assert result.models[0].price is not None
    assert result.models[0].price.currency == "USD"
    assert result.models[0].price.cache_hit_per_m is None


async def test_pagination_and_ollama_metadata_only(mock_http):
    calls = []

    def paginated(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "data": [{"id": "second" if request.url.params.get("after_id") else "first"}],
                "has_more": not bool(request.url.params.get("after_id")),
                "last_id": "cursor",
            },
        )

    mock_http(paginated)
    result = await discovery.discover_models(
        "anthropic", "https://provider.invalid", {"api_key": "fixture-key"}
    )
    assert [model.id for model in result.models] == ["first", "second"]
    assert len(calls) == 2

    def ollama(request):
        if request.method == "GET":
            assert request.url.path == "/api/tags"
            return httpx.Response(200, json={"models": [{"name": "local:latest"}]})
        assert request.url.path == "/api/show" and json.loads(request.content) == {
            "model": "local:latest"
        }
        return httpx.Response(
            200,
            json={
                "model_info": {
                    "general.architecture": "llama",
                    "llama.context_length": 131072,
                },
                "capabilities": ["completion", "vision"],
            },
        )

    mock_http(ollama)
    result = await discovery.discover_models("ollama", "http://localhost:11434", {})
    assert result.models[0].context_window == 131072 and result.models[0].supports_vision is True


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"data": ["wrong"]},
        {"data": [{"id": ""}]},
        {"data": [], "has_more": True},
        {"data": [{"id": "fixture-key"}]},
    ],
)
async def test_malformed_responses_and_credential_echoes_are_safe(mock_http, payload):
    mock_http(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(HTTPException) as error:
        await discovery.discover_models(
            "openai_chat", "https://provider.invalid", {"api_key": "fixture-key"}
        )
    assert error.value.status_code == 502
    assert "fixture-key" not in error.value.detail


async def test_upstream_errors_and_redirects_do_not_expose_credentials(mock_http):
    mock_http(lambda request: httpx.Response(401, text="fixture-key"))
    with pytest.raises(HTTPException, match="502") as error:
        await discovery.discover_models(
            "openai_chat", "https://provider.invalid", {"api_key": "fixture-key"}
        )
    assert "fixture-key" not in error.value.detail
    mock_http(lambda request: httpx.Response(307, headers={"Location": "https://other.invalid"}))
    with pytest.raises(HTTPException) as error:
        await discovery.discover_models(
            "openai_chat", "https://provider.invalid", {"api_key": "fixture-key"}
        )
    assert error.value.status_code == 502


@pytest.mark.parametrize(
    "provider", ["azure_openai_chat", "bedrock", "foundry", "gemini_vertex", "anthropic_bedrock"]
)
async def test_unsupported_providers_make_no_request(mock_http, provider):
    def unexpected(request):
        pytest.fail("Unsupported discovery must not send a request")

    mock_http(unexpected)
    result = await discovery.discover_models(provider, "provider-default", {})
    assert result.supported is False and not result.models and result.warnings


def test_model_input_defaults_and_limits():
    fields = dict(
        label="test",
        provider="openai_chat",
        model="model",
        price={
            "currency": "CNY",
            "cache_hit_per_m": 0,
            "cache_miss_per_m": 1,
            "output_per_m": 2,
            "off_peak": False,
        },
    )
    assert ModelInput.model_validate(fields).context_window == 1_000_000
    assert ModelInput.model_validate({**fields, "context_window": None}).context_window == 1_000_000
    for name in ("max_input_tokens", "max_output_tokens"):
        for invalid in (0, True, 1.5, "100"):
            with pytest.raises(ValidationError):
                ModelInput.model_validate({**fields, name: invalid})


@pytest.mark.parametrize("mode", ["auto", "native", "tool_observations", "invalid", ""])
def test_responses_replay_option_is_validated_before_saving(mode):
    fields = {
        "label": "test",
        "provider": "openai_responses",
        "model": "gateway-model",
        "provider_options": {"responses_replay_mode": mode},
        "price": {
            "currency": "CNY",
            "cache_hit_per_m": 0,
            "cache_miss_per_m": 1,
            "output_per_m": 2,
            "off_peak": False,
        },
    }
    if mode not in {"auto", "native", "tool_observations"}:
        with pytest.raises(ValidationError, match="连接选项值"):
            ModelInput.model_validate(fields)
    else:
        assert ModelInput.model_validate(fields).provider_options["responses_replay_mode"] == mode


async def test_reference_catalog_requires_auth_and_never_uses_connection_credentials(
    settings, mock_http, monkeypatch
):
    app = create_app(settings)
    app.state.platform_store = AsyncMock()
    monkeypatch.setattr(discovery, "_catalog_cache", None)
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://models.dev/api.json"
        assert "Authorization" not in request.headers and "x-api-key" not in request.headers
        return httpx.Response(
            200,
            json={
                "reference": {
                    "name": "Reference provider",
                    "models": {
                        "underlying-model": {
                            "name": "Underlying model",
                            "limit": {"context": 200000, "input": 180000, "output": 20000},
                            "cost": {"input": 2, "output": 8, "cache_read": 0.2},
                            "modalities": {"input": ["text", "image"]},
                        }
                    },
                }
            },
        )

    mock_http(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/platform/model-catalog")).status_code == 401
        assert not calls
        headers = {"Authorization": "Bearer test-service"}
        listing = await client.get("/api/platform/model-catalog", headers=headers)
        assert listing.json()["providers"] == [{"id": "reference", "name": "Reference provider"}]
        selected = await client.get(
            "/api/platform/model-catalog?provider=reference", headers=headers
        )
        model = selected.json()["models"][0]
        assert model["id"] == "underlying-model" and model["metadata_source"] == "models.dev"
        assert model["context_window"] == 200000 and model["max_input_tokens"] == 180000
        assert model["max_output_tokens"] == 20000 and model["supports_vision"] is True
        assert model["price"]["currency"] == "USD"
        assert model["price"]["cache_miss_per_m"] == "2"
        assert "off_peak" not in model["price"]
        assert len(calls) == 1
        missing = await client.get("/api/platform/model-catalog?provider=missing", headers=headers)
        assert missing.status_code == 404
    app.state.platform_store.credentials.assert_not_awaited()
    app.state.platform_store.get.assert_not_awaited()


async def test_discovery_response_size_and_repeated_cursors_are_bounded(mock_http, monkeypatch):
    monkeypatch.setattr(discovery, "MAX_RESPONSE_BYTES", 50)
    # The helper's bound is set at definition time, so exercise its explicit override.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"id": "a" * 100}]})
        )
    ) as client:
        with pytest.raises(ValueError, match="too large"):
            await discovery._json(client, "GET", "https://provider.invalid", max_bytes=50)
    calls = []

    def repeated(request):
        calls.append(request)
        return httpx.Response(
            200, json={"data": [{"id": "a"}], "has_more": True, "last_id": "repeated"}
        )

    mock_http(repeated)
    with pytest.raises(HTTPException) as error:
        await discovery.discover_models(
            "anthropic", "https://provider.invalid", {"api_key": "fixture-key"}
        )
    assert error.value.status_code == 502 and len(calls) == 2


async def test_saving_changed_endpoint_cannot_retain_old_credentials():
    engine = Mock()
    connection = AsyncMock()
    transaction = AsyncMock()
    transaction.__aenter__.return_value = connection
    engine.begin.return_value = transaction
    platform = PlatformStore(engine, "fixture-signing-key")
    previous = Mock()
    previous.mappings.return_value.first.return_value = {
        "deleted": False,
        "version": 1,
        "config": {"provider": "openai_chat", "base_url": "https://old.invalid/v1"},
        "secret_ciphertext": platform._encrypt_credentials({"api_key": "old-fixture-key"}),
    }
    connection.execute.side_effect = [None, previous]
    body = ModelInput.model_validate(
        {
            "label": "changed",
            "provider": "openai_chat",
            "model": "model",
            "base_url": "https://new.invalid/v1",
            "price": {
                "currency": "CNY",
                "cache_hit_per_m": 0,
                "cache_miss_per_m": 1,
                "output_per_m": 2,
                "off_peak": False,
            },
        }
    )
    with pytest.raises(HTTPException) as error:
        await platform.save("models", "example", body, "tester")
    assert error.value.status_code == 422
    assert connection.execute.await_count == 2


def test_public_model_uses_shared_effective_limits_without_changing_snapshot():
    platform = PlatformStore(Mock(), "fixture-signing-key")
    config = {
        "provider": "openai_chat",
        "model": "model",
        "base_url": "https://provider.invalid/v1",
        "reasoning_effort": "none",
        "price": {"currency": "CNY"},
    }
    row = {
        "kind": "models",
        "name": "example",
        "version": 1,
        "label": "Example",
        "enabled": True,
        "config": config,
        "secret_ciphertext": None,
        "credential_source": "none",
    }
    public = platform.public(row)
    assert public["effective_limits"] == {
        "context_window": 1_000_000,
        "max_input_tokens": 991808,
        "max_output_tokens": 8192,
    }
    assert "context_window" not in row["config"]
    assert "max_output_tokens" not in public["config"]
