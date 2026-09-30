"""Public model provider form fields shared by the API and UI."""

from __future__ import annotations

from typing import TypedDict


class ProviderField(TypedDict):
    name: str
    label: str
    required: bool


class ProviderSpec(TypedDict):
    supports_reasoning_effort: bool
    reasoning_efforts: list[str]
    id: str
    label: str
    options_fields: list[ProviderField]
    credential_fields: list[ProviderField]
    allow_no_auth: bool
    default_base_url: str
    base_url_required: bool
    base_url_label: str


def _field(name: str, label: str, required: bool = True) -> ProviderField:
    return {"name": name, "label": label, "required": required}


_API_KEY = [_field("api_key", "API Key")]
_AWS = [
    _field("access_key_id", "AWS Access Key ID"),
    _field("secret_access_key", "AWS Secret Access Key"),
    _field("session_token", "AWS Session Token", False),
]
_AZURE_IDENTITY = [
    _field("tenant_id", "Azure Tenant ID"),
    _field("client_id", "Azure Client ID"),
    _field("client_secret", "Azure Client Secret"),
]
_GOOGLE_IDENTITY = [_field("service_account_json", "Google 服务账号 JSON")]


def _provider(
    provider_id: str,
    label: str,
    *,
    options: list[ProviderField] | None = None,
    credentials: list[ProviderField] | None = None,
    allow_no_auth: bool = False,
    default_base_url: str | None = None,
    base_url_required: bool = False,
    base_url_label: str = "API 地址",
) -> ProviderSpec:
    efforts = (
        ["low", "high", "max"]
        if provider_id == "deepseek"
        else ["minimal", "low", "medium", "high", "xhigh"]
        if provider_id
        in {
            "openai_chat",
            "openai_responses",
            "openai_compatible",
            "azure_openai_chat",
            "azure_openai_responses",
            "mistral",
        }
        else []
    )
    return {
        "id": provider_id,
        "supports_reasoning_effort": bool(efforts),
        "reasoning_efforts": efforts,
        "label": label,
        "options_fields": options or [],
        "credential_fields": credentials or [],
        "allow_no_auth": allow_no_auth,
        "default_base_url": default_base_url
        if default_base_url is not None
        else ("" if base_url_required else "provider-default"),
        "base_url_required": base_url_required,
        "base_url_label": base_url_label,
    }


PROVIDERS: dict[str, ProviderSpec] = {
    item["id"]: item
    for item in (
        _provider(
            "deepseek",
            "DeepSeek",
            credentials=_API_KEY,
            default_base_url="https://api.deepseek.com",
        ),
        _provider(
            "openai_chat",
            "OpenAI Chat Completions",
            credentials=_API_KEY,
            default_base_url="https://api.openai.com/v1",
        ),
        _provider(
            "openai_responses",
            "OpenAI Responses",
            credentials=_API_KEY,
            default_base_url="https://api.openai.com/v1",
        ),
        _provider(
            "openai_compatible",
            "OpenAI 兼容接口",
            credentials=_API_KEY,
            allow_no_auth=True,
            base_url_required=True,
        ),
        _provider(
            "azure_openai_chat",
            "Azure OpenAI Chat Completions",
            options=[_field("api_version", "Azure API 版本")],
            credentials=_API_KEY,
            base_url_required=True,
            base_url_label="Azure 资源地址",
        ),
        _provider(
            "azure_openai_responses",
            "Azure OpenAI Responses",
            options=[_field("api_version", "Azure API 版本")],
            credentials=_API_KEY,
            base_url_required=True,
            base_url_label="Azure 资源地址",
        ),
        _provider(
            "foundry",
            "Microsoft Foundry",
            credentials=_AZURE_IDENTITY,
            base_url_required=True,
            base_url_label="Foundry 项目地址",
        ),
        _provider(
            "foundry_local",
            "Foundry Local（已有 HTTP 服务）",
            allow_no_auth=True,
            base_url_required=True,
            base_url_label="本地服务 OpenAI 兼容地址",
        ),
        _provider(
            "anthropic",
            "Anthropic",
            credentials=_API_KEY,
            default_base_url="https://api.anthropic.com",
        ),
        _provider(
            "anthropic_foundry",
            "Anthropic on Foundry",
            credentials=_API_KEY,
            base_url_required=True,
            base_url_label="Anthropic Foundry 地址",
        ),
        _provider(
            "anthropic_bedrock",
            "Anthropic on Bedrock",
            options=[_field("region", "AWS 区域")],
            credentials=_AWS,
        ),
        _provider(
            "anthropic_vertex",
            "Anthropic on Vertex AI",
            options=[
                _field("project", "Google Cloud 项目"),
                _field("location", "Google Cloud 区域"),
            ],
            credentials=_GOOGLE_IDENTITY,
        ),
        _provider(
            "ollama",
            "Ollama",
            allow_no_auth=True,
            default_base_url="http://localhost:11434",
            base_url_required=True,
            base_url_label="Ollama 服务地址",
        ),
        _provider(
            "bedrock",
            "Amazon Bedrock",
            options=[_field("region", "AWS 区域")],
            credentials=_AWS,
        ),
        _provider("gemini", "Google Gemini", credentials=_API_KEY),
        _provider(
            "gemini_vertex",
            "Gemini on Vertex AI",
            options=[
                _field("project", "Google Cloud 项目"),
                _field("location", "Google Cloud 区域"),
            ],
            credentials=_GOOGLE_IDENTITY,
        ),
        _provider(
            "mistral", "Mistral AI", credentials=_API_KEY, default_base_url="https://api.mistral.ai"
        ),
    )
}


def reasoning_efforts(provider: str, model: str) -> list[str]:
    """Include native DeepSeek choices for compatible provider connections."""
    spec = PROVIDERS.get(provider)
    if spec is None or not spec["supports_reasoning_effort"]:
        return []
    if "deepseek" in model.lower():
        return list(PROVIDERS["deepseek"]["reasoning_efforts"])
    return list(spec["reasoning_efforts"])
