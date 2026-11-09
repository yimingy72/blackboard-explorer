"""Read safe model references from the original AgentGateway deployment."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from bbx_contracts.gateway_models import GatewayModelSnapshot
from bbx_contracts.runner_bootstrap import management_document_digest
from fastapi import HTTPException
from pydantic import ValidationError

from bbx_blackboard.native_admin import CATALOG_PATH, MODELS_PATH, NativeAdmin, unavailable
from bbx_blackboard.settings import Settings

CATALOG_READER_CLAIM = "bbx_catalog_reader"
CATALOG_READER_RULE = {
    "require": "!has(apiKey.bbx_catalog_reader) || (apiKey.bbx_catalog_reader == true && "
    'request.method == "GET" && request.path == "/v1/models")'
}
PINNED_REVISION = "26f5aebe99a36a037fe54f8080a608aca5547e15"
PROVIDERS_PATH = "/api/config/resources/llm.provider"


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _array(value: Any) -> list:
    return value if isinstance(value, list) else []


def _catalog_model(resource: dict, provider: str, model: str) -> dict:
    root = _object(resource.get("value"))
    merged = {}
    for layer in ("base", "custom"):
        providers = _object(_object(root.get(layer)).get("providers"))
        entries = _object(_object(providers.get(provider)).get("models"))
        item = _object(entries.get(model))
        if item:
            merged = {
                **merged,
                **item,
                "rates": {
                    **_object(merged.get("rates")),
                    **{k: v for k, v in _object(item.get("rates")).items() if v is not None},
                },
            }
    return merged


def _live_route(dump: dict, alias: str) -> tuple[str, str, dict] | None:
    entries = [_object(item) for item in _array(dump.get("backends"))]
    routers = []
    for item in entries:
        backend = _object(item.get("backend"))
        router = _object(backend.get("llmRouter"))
        routers.extend(
            model
            for raw in _array(_object(router.get("target")).get("models"))
            if (model := _object(raw)).get("name") == alias
        )
    if len(routers) != 1:
        return None
    router = routers[0]
    discovery = _object(router.get("discovery"))
    provider, upstream = discovery.get("provider"), discovery.get("model")
    ref = _object(router.get("backend")).get("backend")
    if (
        not isinstance(provider, str)
        or not provider
        or not isinstance(upstream, str)
        or not upstream
        or not isinstance(ref, str)
        or not ref
    ):
        return None
    matches = []
    for item in entries:
        ai = _object(_object(item.get("backend")).get("ai"))
        if "/" + str(ai.get("name", "")) != ref:
            continue
        for raw in _array(_object(ai.get("target")).get("providers")):
            active = _object(_object(raw).get("active"))
            endpoint = _object(_object(active.get(alias)).get("endpoint"))
            if endpoint:
                # Do not put redacted gateway secret structures into a platform view/digest.
                matches.append(
                    {
                        "provider_type": sorted(_object(endpoint.get("provider"))),
                        "host": endpoint.get("hostOverride"),
                        "path_prefix": endpoint.get("pathPrefix"),
                        "path_override": endpoint.get("pathOverride"),
                        "tls": any(
                            "backendTLS" in _object(policy)
                            for policy in _array(item.get("inlinePolicies"))
                        ),
                    }
                )
    if len(matches) != 1:
        return None
    return provider, upstream, matches[0]


def _selection(metadata: dict, native: dict, alias: str) -> dict:
    rates = _object(native.get("rates"))
    if (
        rates.get("input") is None
        or rates.get("output") is None
        or native.get("tiers")
        or any(
            value is not None and key not in {"input", "output", "cacheRead", "cacheWrite"}
            for key, value in rates.items()
        )
    ):
        raise ValueError("AgentGateway model lacks supported flat token rates")
    architecture = _object(metadata.get("architecture"))
    capabilities = _object(metadata.get("capabilities"))
    return {
        "label": metadata.get("name") or alias,
        "context_window": metadata.get("context_window"),
        "max_output_tokens": metadata.get("max_tokens"),
        "max_input_tokens": metadata.get("max_input_tokens"),
        "input_modalities": [
            item
            for item in _array(architecture.get("input_modalities"))
            if item in {"text", "image"}
        ],
        "tool_call": capabilities.get("tools") is True,
        "reasoning": capabilities.get("reasoning"),
        "usd_rates": {
            "input": rates["input"],
            "output": rates["output"],
            # Native 1.7 folds cache tokens into input if no separate rate is configured.
            "cache_read": rates.get("cacheRead", rates["input"]),
            "cache_write": rates.get("cacheWrite", rates["input"]),
        },
    }


class GatewayModels:
    def __init__(self, settings: Settings, *, admin: NativeAdmin | None = None):
        self.settings = settings
        self.admin = admin or NativeAdmin(settings)
        self.owned = admin is None

    async def close(self) -> None:
        if self.owned:
            await self.admin.close()

    async def _deployment(self) -> tuple[dict, str]:
        key = self.settings.gateway_catalog_read_key.get_secret_value()
        if not key or not self.settings.native_gateway_accounting_source_id:
            raise unavailable()
        config, runtime = await asyncio.gather(
            self.admin.request("GET", "/api/config"), self.admin.request("GET", "/api/runtime")
        )
        config, runtime = _object(config), _object(runtime)
        if (
            _object(runtime.get("build")).get("gitRevision") != PINNED_REVISION
            or _object(runtime.get("ui")).get("configStoreMode") != "hybrid"
            or _object(runtime.get("configReload")).get("synchronized") is not True
            # The pinned gateway applies file sources after DB entries. Require
            # managed rates to have no extra file source that can override them.
            or _object(config.get("config")).get("modelCatalog")
        ):
            raise unavailable()
        policy = _object(_object(config.get("llm")).get("policies"))
        api_key = _object(policy.get("apiKey"))
        fingerprint = "sha256:" + hashlib.sha256(key.encode()).hexdigest()
        if (
            api_key.get("mode") != "strict"
            or CATALOG_READER_RULE not in _array(_object(policy.get("authorization")).get("rules"))
            or not any(
                _object(value).get("keyHash") == fingerprint
                and _object(_object(value).get("metadata")).get(CATALOG_READER_CLAIM) is True
                for value in _array(api_key.get("keys"))
            )
        ):
            raise unavailable()
        return config, key

    async def _managed_catalog(self) -> dict:
        resources = await self.admin.resources(CATALOG_PATH)
        if not resources:
            return {}
        if (
            len(resources) != 1
            or resources[0]["id"] != "default"
            or type(resources[0].get("revision")) is not int
        ):
            raise unavailable()
        return resources[0]

    async def list_active(self) -> list[GatewayModelSnapshot]:
        _, key = await self._deployment()
        routes, providers, catalog, sdk, live, dump = await asyncio.gather(
            self.admin.resources(MODELS_PATH),
            self.admin.resources(PROVIDERS_PATH),
            self._managed_catalog(),
            self.admin.models(key),
            self.admin.request("GET", "/api/costs/models"),
            self.admin.request("GET", "/config_dump"),
        )
        if _object(live).get("loaded") is not True or not isinstance(dump, dict):
            raise unavailable()
        applied = {
            (item["provider"], name)
            for item in _array(live.get("providers"))
            if isinstance(_object(item).get("provider"), str)
            for name in _array(item.get("models"))
            if isinstance(name, str)
        }
        model_rows = {item["id"]: item for item in sdk}
        if len(model_rows) != len(sdk):
            raise unavailable()
        source = self.settings.native_gateway_accounting_source_id
        provider_revisions = {
            item["id"]: {"revision": item["revision"], "created_at": item["createdAt"]}
            for item in providers
            if type(item.get("revision")) is int and isinstance(item.get("createdAt"), str)
        }
        snapshots = []
        for route in routes:
            value = _object(route.get("value"))
            alias, revision, created = (
                value.get("name"),
                route.get("revision"),
                route.get("createdAt"),
            )
            if (
                not isinstance(alias, str)
                or type(revision) is not int
                or revision < 1
                or not isinstance(created, str)
                or not created
            ):
                continue
            observed = _live_route(dump, alias)
            metadata = model_rows.get(alias)
            if observed is None or metadata is None:
                continue
            provider, upstream, endpoint = observed
            if metadata.get("owned_by") != provider or (provider, upstream) not in applied:
                continue
            desired_model = _object(value.get("params")).get("model")
            if desired_model is not None and desired_model != upstream:
                continue
            native = _catalog_model(catalog, provider, upstream)
            provider_ref = value.get("provider")
            try:
                selected = _selection(metadata, native, alias)
                route_digest = management_document_digest(
                    {
                        "resource_id": route["id"],
                        "revision": revision,
                        "created_at": created,
                        "provider": provider,
                        "upstream": upstream,
                        "endpoint": endpoint,
                        "provider_revision": provider_revisions.get(provider_ref)
                        if isinstance(provider_ref, str)
                        else None,
                    }
                )
                entry_digest = management_document_digest(
                    {"provider": provider, "upstream": upstream, "native": native, "sdk": selected}
                )
                snapshots.append(
                    GatewayModelSnapshot(
                        alias=alias,
                        catalog_revision=management_document_digest(
                            {"source": source, "route": route_digest, "catalog": entry_digest}
                        ),
                        accounting_source_id=source,
                        route_revision=revision,
                        route_digest=route_digest,
                        model_catalog_digest=entry_digest,
                        **selected,
                    )
                )
            except (ValidationError, ValueError, TypeError):
                continue
        return sorted(snapshots, key=lambda model: model.alias)

    async def require_active(
        self, alias: str, catalog_revision: str | None = None
    ) -> GatewayModelSnapshot:
        for model in await self.list_active():
            if model.alias == alias and (
                catalog_revision is None or model.catalog_revision == catalog_revision
            ):
                return model
        raise HTTPException(409, "The selected AgentGateway model is unavailable or changed")
