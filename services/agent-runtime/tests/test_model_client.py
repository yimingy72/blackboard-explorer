"""Profile snapshot and DeepSeek client construction, without network calls."""

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from agent_framework import Agent, Content, FunctionInvocationLayer, FunctionTool, Message
from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient
from bbx_contracts.providers import PROVIDERS
from bbx_runtime.models import (
    close_model_client,
    load_runtime_profile,
    make_client,
    model_api_key,
    model_run_options,
    resolve_model_credentials,
)

PROFILE_DIR = Path(__file__).resolve().parents[3] / "profiles" / "default"


def test_profile_snapshot_keeps_template_bodies():
    local = load_runtime_profile(PROFILE_DIR)
    pinned = load_runtime_profile(local.model_dump(mode="json"))
    wrapped = load_runtime_profile(
        {"name": "default", "version": 7, "profile": local.model_dump(mode="json")}
    )
    assert pinned.prompt_templates.explore == local.prompt_templates.explore
    assert pinned.prompt_templates.derive == local.prompt_templates.derive
    assert pinned.prompt_templates.close == local.prompt_templates.close
    assert pinned.prompt_templates.explore
    assert wrapped.prompt_templates == pinned.prompt_templates


def test_make_client_is_explicit_and_bounded(monkeypatch):
    import agent_framework._settings as maf_settings

    monkeypatch.setattr(
        maf_settings,
        "dotenv_values",
        lambda **_kwargs: pytest.fail("make_client must not read a dotenv file"),
    )
    model = load_runtime_profile(PROFILE_DIR).models.explore
    client = cast(
        Any,
        make_client(
            model,
            api_key="test-only-key",
            explore_max_steps=60,
            conclude_grace_calls=3,
            max_duration_seconds=3600,
        ),
    )
    assert isinstance(client, OpenAIChatCompletionClient)
    assert isinstance(client, FunctionInvocationLayer)
    assert client.function_invocation_configuration.get("max_iterations") == 68
    assert client.function_invocation_configuration.get("max_duration_seconds") == 3600
    assert client.client.timeout == 120
    assert model_run_options(model) == {"reasoning_effort": "high"}


def test_make_client_rejects_invalid_configuration():
    model = load_runtime_profile(PROFILE_DIR).models.explore
    with pytest.raises(ValueError, match="key"):
        make_client(
            model,
            api_key="",
            explore_max_steps=60,
            conclude_grace_calls=3,
            max_duration_seconds=3600,
        )
    with pytest.raises(ValueError, match="Unsupported"):
        make_client(
            model.model_copy(update={"provider": "other"}),
            api_key="test-only-key",
            explore_max_steps=60,
            conclude_grace_calls=3,
            max_duration_seconds=3600,
        )


@pytest.mark.parametrize(
    ("provider", "client_type"),
    [
        ("deepseek", OpenAIChatCompletionClient),
        ("openai_chat", OpenAIChatCompletionClient),
        ("openai_compatible", OpenAIChatCompletionClient),
        ("openai_responses", OpenAIChatClient),
    ],
)
def test_platform_provider_uses_pinned_endpoint(provider, client_type):
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": provider,
            "model": "snapshot-model",
            "base_url": "https://model.test/v1",
        }
    )
    client = cast(
        Any,
        make_client(
            model,
            api_key="stored-test-key",
            explore_max_steps=4,
            conclude_grace_calls=1,
            max_duration_seconds=180,
        ),
    )
    assert isinstance(client, client_type)
    assert client.model == "snapshot-model"
    assert str(client.client.base_url) == "https://model.test/v1/"
    assert model_run_options(model) == (
        {"reasoning": {"effort": "high"}}
        if provider == "openai_responses"
        else {"reasoning_effort": "high"}
    )
    assert model_run_options(model.model_copy(update={"reasoning_effort": "off"})) == {}


async def test_model_credentials_use_pinned_version_and_environment_only_for_deepseek():
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={"platform_id": "model-a", "platform_version": 3}
    )
    service = AsyncMock()
    service.get_model_credentials.return_value = {
        "secret": "stored-test-key",
        "credential_source": "stored",
    }
    assert await model_api_key(service, model, "legacy-test-key") == "stored-test-key"
    service.get_model_credentials.assert_awaited_once_with("model-a", 3)
    service.get_model_credentials.return_value = {
        "secret": None,
        "credential_source": "environment",
    }
    assert await model_api_key(service, model, "legacy-test-key") == "legacy-test-key"
    service.get_model_credentials.return_value = {"secret": None, "credential_source": "none"}
    assert await model_api_key(service, model, "legacy-test-key") == "not-required"
    service.get_model_credentials.return_value = {
        "secret": None,
        "credential_source": "environment",
    }
    with pytest.raises(ValueError, match="DeepSeek"):
        await model_api_key(service, model.model_copy(update={"provider": "openai_chat"}), "legacy")
    service.get_model_credentials.return_value = {"secret": None, "credential_source": "stored"}
    with pytest.raises(ValueError, match="unavailable"):
        await model_api_key(service, model, "legacy")


def _service_account_json() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "test-project",
            "private_key_id": "test-id",
            "private_key": private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
            "client_email": "test@test-project.iam.gserviceaccount.com",
            "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
async def test_every_provider_constructs_and_converts_tools_and_history(provider):
    """Offline proof that each advertised provider accepts local tools and resumed turns."""
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": provider,
            "model": "test-model",
            "base_url": "https://model.test/v1",
            "provider_options": {
                "api_version": "2025-01-01-preview",
                "region": "us-east-1",
                "project": "test-project",
                "location": "us-east5",
            },
        }
    )
    credentials = {
        "api_key": "test-only-key",
        "access_key_id": "test-access-key",
        "secret_access_key": "test-secret-key",
        "tenant_id": "test-tenant",
        "client_id": "test-client",
        "client_secret": "test-client-secret",
        "service_account_json": _service_account_json(),
    }
    client = cast(
        Any,
        make_client(
            model,
            credentials=credentials,
            explore_max_steps=4,
            conclude_grace_calls=1,
            max_duration_seconds=180,
        ),
    )
    assert isinstance(client, FunctionInvocationLayer)
    assert client.function_invocation_configuration.get("max_iterations") == 10
    native = cast(Any, client)
    messages = [
        Message(role="user", contents=[Content.from_text("question")]),
        Message(
            role="assistant",
            contents=[Content.from_function_call("call-1", "lookup", arguments={"name": "a"})],
        ),
        Message(role="tool", contents=[Content.from_function_result("call-1", result="found")]),
        Message(role="assistant", contents=[Content.from_text("answer")]),
    ]

    def lookup(name: str) -> str:
        return name

    tool = FunctionTool(name="lookup", description="Find an item", func=lookup)
    if provider.startswith("anthropic"):
        prepared_messages = native._prepare_messages_for_anthropic(messages)
        prepared_tools = native._prepare_tools_for_anthropic({"tools": [tool]})
    elif provider == "bedrock":
        prepared_messages = native._prepare_bedrock_messages(messages)
        prepared_tools = native._prepare_tools([tool])
    elif provider.startswith("gemini"):
        prepared_messages = native._prepare_gemini_messages(messages)
        prepared_tools = native._prepare_tools({"tools": [tool]})
    elif provider == "ollama":
        prepared_messages = native._prepare_messages_for_ollama(messages)
        prepared_tools = native._prepare_tools_for_ollama([tool])
    elif provider == "mistral":
        prepared_messages = native._prepare_mistral_messages(messages)
        prepared_tools = native._prepare_tools([tool])
    else:
        prepared_messages = native._prepare_messages_for_openai(messages)
        prepared_tools = native._prepare_tools_for_openai([tool])
    assert "question" in str(prepared_messages)
    assert "found" in str(prepared_messages)
    assert "lookup" in str(prepared_tools)
    await close_model_client(cast(Any, client))


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
async def test_provider_auth_requirement_matches_catalog(provider):
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": provider,
            "base_url": "https://model.test/v1",
            "provider_options": {
                "api_version": "2025-01-01-preview",
                "region": "us-east-1",
                "project": "test-project",
                "location": "us-east5",
            },
        }
    )
    if PROVIDERS[provider]["allow_no_auth"]:
        client = make_client(
            model,
            credentials={},
            explore_max_steps=1,
            conclude_grace_calls=0,
            max_duration_seconds=10,
        )
        await close_model_client(client)
    else:
        with pytest.raises(ValueError, match="Missing model credential"):
            make_client(
                model,
                credentials={},
                explore_max_steps=1,
                conclude_grace_calls=0,
                max_duration_seconds=10,
            )


async def test_structured_credentials_and_legacy_deepseek_reference():
    model = load_runtime_profile(PROFILE_DIR).models.explore
    service = AsyncMock()
    service.get_model_credentials.return_value = {
        "secret": None,
        "credential_source": "stored",
        "credentials": {"access_key_id": "test-access", "secret_access_key": "test-secret"},
    }
    assert await resolve_model_credentials(service, model, "legacy") == {
        "access_key_id": "test-access",
        "secret_access_key": "test-secret",
    }
    service.get_model_credentials.assert_awaited_once_with("deepseek-default", 1)


async def test_compatible_client_runs_local_tool_over_mock_http(monkeypatch):
    from openai import AsyncOpenAI

    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        message = (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"name":"a"}'},
                    }
                ],
            }
            if len(requests) == 1
            else {"role": "assistant", "content": "found a"}
        )
        return httpx.Response(
            200,
            json={
                "id": f"response-{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "bbx_runtime.models.AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport)),
    )
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": "openai_compatible",
            "model": "test-model",
            "base_url": "https://model.test/v1",
        }
    )
    client = make_client(
        model,
        credentials={"api_key": "test-only-key"},
        explore_max_steps=2,
        conclude_grace_calls=0,
        max_duration_seconds=10,
    )

    def lookup(name: str) -> str:
        return f"found {name}"

    async with Agent(client=client, tools=[lookup]) as agent:
        result = await agent.run("Find a")
    await close_model_client(client)
    assert result.text == "found a"
    assert len(requests) == 2
    assert requests[0]["tools"][0]["function"]["name"] == "lookup"
    assert requests[1]["messages"][-1]["content"] == "found a"
