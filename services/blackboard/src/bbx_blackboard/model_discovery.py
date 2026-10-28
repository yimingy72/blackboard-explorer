"""Bounded, metadata-only model discovery using provider model catalogue APIs."""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from bbx_contracts.model_urls import in_container, resolve_model_url
from bbx_contracts.models import Price
from bbx_contracts.providers import PROVIDERS
from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError

MAX_MODELS = 500
MAX_PAGES = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SUPPORTED_PROVIDERS = {
    "openai_chat",
    "openai_responses",
    "openai_compatible",
    "deepseek",
    "mistral",
    "anthropic",
    "gemini",
    "ollama",
}


class DiscoveredModel(BaseModel):
    id: str
    name: str | None = None
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_vision: bool | None = None
    price: Price | None = None
    metadata_source: str = "provider_api"


class ModelDiscoveryResult(BaseModel):
    models: list[DiscoveredModel] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    supported: bool = True


class CatalogProvider(BaseModel):
    id: str
    name: str


class ModelCatalogResult(BaseModel):
    providers: list[CatalogProvider]
    models: list[DiscoveredModel] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provider: str | None = None


_catalog_cache: tuple[float, dict[str, Any]] | None = None
_catalog_lock = asyncio.Lock()


def _cost(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


async def model_catalog(provider: str | None) -> ModelCatalogResult:
    """Public reference metadata stays separate from connection-specific discovery."""
    global _catalog_cache
    try:
        async with _catalog_lock:
            if _catalog_cache is None or time.monotonic() - _catalog_cache[0] >= 600:
                async with (
                    asyncio.timeout(20),
                    httpx.AsyncClient(
                        timeout=15, trust_env=False, follow_redirects=False
                    ) as client,
                ):
                    payload = await _json(
                        client, "GET", "https://models.dev/api.json", max_bytes=32 * 1024 * 1024
                    )
                _catalog_cache = (time.monotonic(), payload)
            catalog = _catalog_cache[1]
        providers = [
            CatalogProvider(id=key, name=_name(value.get("name")) or key)
            for key, value in catalog.items()
            if _name(key) and isinstance(value, dict) and isinstance(value.get("models"), dict)
        ]
        result = ModelCatalogResult(
            providers=sorted(providers, key=lambda item: item.name),
            provider=provider,
            warnings=[
                "models.dev 是第三方参考目录；参数与美元价格不代表当前连接或网关的实际配置和收费。"
            ],
        )
        if provider is None:
            return result
        selected = catalog.get(provider)
        if not isinstance(selected, dict) or not isinstance(selected.get("models"), dict):
            raise HTTPException(404, "参考目录中不存在此 Provider")
        for identifier, item in selected["models"].items():
            if not _name(identifier) or not isinstance(item, dict):
                continue
            limits = item.get("limit")
            limits = limits if isinstance(limits, dict) else {}
            modalities = item.get("modalities")
            inputs = modalities.get("input") if isinstance(modalities, dict) else None
            cost = item.get("cost")
            price = None
            if isinstance(cost, dict):
                price = Price(
                    currency="USD",
                    cache_hit_per_m=_cost(cost.get("cache_read")),
                    cache_miss_per_m=_cost(cost.get("input")),
                    output_per_m=_cost(cost.get("output")),
                )
                if all(
                    getattr(price, key) is None
                    for key in ("cache_hit_per_m", "cache_miss_per_m", "output_per_m")
                ):
                    price = None
            result.models.append(
                DiscoveredModel(
                    id=identifier,
                    name=_name(item.get("name")),
                    metadata_source="models.dev",
                    context_window=_positive(limits.get("context")),
                    max_input_tokens=_positive(limits.get("input")),
                    max_output_tokens=_positive(limits.get("output")),
                    price=price,
                    supports_vision=("image" in inputs)
                    if isinstance(inputs, list) and all(isinstance(x, str) for x in inputs)
                    else None,
                )
            )
            if len(result.models) >= MAX_MODELS:
                result.warnings.append("参考目录已达到 500 项上限。")
                break
        result.models.sort(key=lambda item: item.id)
        return result
    except (httpx.HTTPError, ValueError, TypeError, TimeoutError):
        raise HTTPException(502, "参考目录暂时不可用，请手工填写或稍后重试") from None


def _positive(value: Any) -> int | None:
    return value if type(value) is int and 0 < value < 2**63 else None


def _name(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 200 else None


def _normalize(provider: str, item: dict[str, Any]) -> DiscoveredModel:
    identifier = _name(item.get("name") if provider in {"gemini", "ollama"} else item.get("id"))
    if identifier is None:
        raise ValueError("Invalid model identifier")
    if provider == "gemini":
        identifier = identifier.removeprefix("models/")
        if not identifier:
            raise ValueError("Invalid model identifier")
    model = DiscoveredModel(
        id=identifier,
        name=_name(item.get("displayName") if provider == "gemini" else item.get("display_name"))
        or (_name(item.get("name")) if provider not in {"gemini", "ollama"} else None),
        context_window=_positive(item.get("context_window")),
        max_input_tokens=_positive(item.get("max_input_tokens")),
        max_output_tokens=_positive(item.get("max_output_tokens")),
    )
    vision = item.get("supports_vision")
    if type(vision) is bool:
        model.supports_vision = vision
    modalities = item.get("input_modalities")
    if isinstance(modalities, list) and all(isinstance(value, str) for value in modalities):
        model.supports_vision = "image" in modalities
    capabilities = item.get("capabilities")
    if provider == "anthropic":
        model.max_input_tokens = _positive(item.get("max_input_tokens"))
        model.max_output_tokens = _positive(item.get("max_tokens"))
        if isinstance(capabilities, dict):
            image = capabilities.get("image_input")
            if isinstance(image, dict) and type(image.get("supported")) is bool:
                model.supports_vision = image["supported"]
    elif provider == "gemini":
        model.max_input_tokens = _positive(item.get("inputTokenLimit"))
        model.max_output_tokens = _positive(item.get("outputTokenLimit"))
    elif provider == "mistral":
        model.context_window = _positive(item.get("max_context_length"))
        if isinstance(capabilities, dict) and type(capabilities.get("vision")) is bool:
            model.supports_vision = capabilities["vision"]
    # Accept an explicit platform-shaped price only; provider-specific units must not be guessed.
    if isinstance(item.get("price"), dict):
        try:
            model.price = Price.model_validate(item["price"])
        except ValidationError:
            pass
    return model


async def _json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    **kwargs: Any,
) -> dict[str, Any]:
    async with client.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError("Model catalogue too large")
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise ValueError("Invalid model catalogue")
    return payload


async def _ollama_details(
    client: httpx.AsyncClient, base_url: str, models: list[DiscoveredModel], warnings: list[str]
) -> None:
    semaphore = asyncio.Semaphore(4)

    async def detail(model: DiscoveredModel) -> bool:
        try:
            async with semaphore:
                data = await _json(client, "POST", f"{base_url}/api/show", json={"model": model.id})
            info = data.get("model_info", {})
            if isinstance(info, dict):
                architecture = info.get("general.architecture")
                model.context_window = _positive(info.get(f"{architecture}.context_length"))
            capabilities = data.get("capabilities")
            if isinstance(capabilities, list) and all(isinstance(x, str) for x in capabilities):
                model.supports_vision = "vision" in capabilities
            return True
        except (httpx.HTTPError, ValueError, TypeError):
            return False

    # Show reads installed metadata without loading or running a model; bound fan-out too.
    results = await asyncio.gather(*(detail(model) for model in models[:40]))
    if not all(results) or len(models) > 40:
        warnings.append("部分 Ollama 模型详情未获取；未知参数请手工核对。")


async def discover_models(
    provider: str, base_url: str, credentials: dict[str, str]
) -> ModelDiscoveryResult:
    if provider not in SUPPORTED_PROVIDERS:
        return ModelDiscoveryResult(
            supported=False, warnings=["此 Provider 暂不支持自动发现，请手工填写模型 ID 和参数。"]
        )
    spec = PROVIDERS[provider]
    if not spec["allow_no_auth"] and not credentials.get("api_key"):
        raise HTTPException(
            422, "请填写 API Key；复用已保存凭据须保持 Provider、地址和连接选项一致"
        )
    headers = {"Accept": "application/json"}
    key = credentials.get("api_key")
    if provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif provider == "gemini":
        if key:
            headers["x-goog-api-key"] = key
        base_url = "https://generativelanguage.googleapis.com/v1beta"
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    if provider in {"anthropic", "mistral"} and not base_url.endswith("/v1"):
        base_url += "/v1"
    base_url = resolve_model_url(base_url, containerized=in_container())
    url = f"{base_url}/api/tags" if provider == "ollama" else f"{base_url}/models"
    result = ModelDiscoveryResult()
    try:
        async with (
            asyncio.timeout(20),
            httpx.AsyncClient(
                headers=headers, timeout=10, trust_env=False, follow_redirects=False
            ) as client,
        ):
            params: dict[str, str | int] = {}
            seen_cursors: set[str] = set()
            models: dict[str, DiscoveredModel] = {}
            for _ in range(MAX_PAGES):
                payload = await _json(client, "GET", url, params=params)
                items = payload.get("models" if provider in {"gemini", "ollama"} else "data")
                if not isinstance(items, list):
                    raise ValueError("Missing models list")
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError("Invalid model entry")
                    if provider == "gemini" and "supportedGenerationMethods" in item:
                        methods = item["supportedGenerationMethods"]
                        if not isinstance(methods, list):
                            raise ValueError("Invalid generation methods")
                        if "generateContent" not in methods:
                            continue
                    model = _normalize(provider, item)
                    models[model.id] = model
                    if len(models) >= MAX_MODELS:
                        result.warnings.append("模型列表已达到 500 项上限，其他模型请手工填写。")
                        break
                if len(models) >= MAX_MODELS:
                    break
                cursor = (
                    payload.get("nextPageToken")
                    if provider == "gemini"
                    else (payload.get("last_id") if payload.get("has_more") is True else None)
                )
                if not cursor:
                    if payload.get("has_more") is True:
                        raise ValueError("Missing pagination cursor")
                    break
                if not isinstance(cursor, str) or len(cursor) > 1000 or cursor in seen_cursors:
                    raise ValueError("Invalid pagination cursor")
                seen_cursors.add(cursor)
                params = {
                    "pageToken"
                    if provider == "gemini"
                    else "after_id"
                    if provider == "anthropic"
                    else "after": cursor
                }
            else:
                result.warnings.append("模型列表已达到分页上限，其他模型请手工填写。")
            result.models = sorted(models.values(), key=lambda item: item.id)
            if provider == "ollama":
                await _ollama_details(client, base_url, result.models, result.warnings)
        # A gateway must not turn credential echoes into public model metadata.
        for model in result.models:
            strings = (model.id, model.name, model.price.currency if model.price else None)
            if any(
                secret and value and secret in value
                for secret in credentials.values()
                for value in strings
            ):
                raise ValueError("Credentials in model metadata")
    except (httpx.HTTPError, ValueError, TypeError, TimeoutError):
        raise HTTPException(
            502, "模型列表获取失败，请检查地址、认证及服务端模型目录支持情况"
        ) from None
    result.warnings.append(
        "仅展示服务端明确返回的参数；未返回的上限、能力和价格均为未知，请核对后填写。"
    )
    return result
