"""Bounded native gateway HTTP I/O; publication and accounting stay with the caller."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException

from bbx_blackboard.settings import Settings

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 5
KEYS_PATH = "/api/config/resources/llm.apiKey"
ROUTES_PATH = "/api/config/resources/traffic.route"
MODELS_PATH = "/api/config/resources/llm.model"
CATALOG_PATH = "/api/config/resources/modelCatalog"


def unavailable() -> HTTPException:
    return HTTPException(503, "Native gateway management is unavailable")


class NativeAdmin:
    def __init__(self, settings: Settings, *, http: httpx.AsyncClient | None = None):
        self.admin_url = (settings.native_gateway_admin_url or "").rstrip("/")
        self.data_url = (settings.native_gateway_data_url or "").rstrip("/")
        self.admin_token = (
            settings.native_gateway_admin_token.get_secret_value()
            if settings.native_gateway_admin_token
            else ""
        )
        self.http = http or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=httpx.AsyncHTTPTransport(retries=0),
        )
        self.owned = http is None

    async def close(self) -> None:
        if self.owned:
            await self.http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
        task_key: str | None = None,
        missing_ok: bool = False,
    ) -> Any:
        base = self.data_url if task_key is not None else self.admin_url
        try:
            parsed = urlsplit(base)
        except ValueError as error:
            raise unavailable() from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise unavailable()
        token = task_key if task_key is not None else self.admin_token
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with (
                asyncio.timeout(REQUEST_TIMEOUT_SECONDS),
                self.http.stream(
                    method,
                    base + path,
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    follow_redirects=False,
                ) as response,
            ):
                if missing_ok and response.status_code == 404:
                    return None
                if not response.is_success:
                    raise unavailable()
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise unavailable()
                    payload.extend(chunk)
                if response.status_code == 204:
                    return None
                return json.loads(payload)
        except (httpx.HTTPError, ValueError, UnicodeError, TimeoutError) as error:
            raise unavailable() from error

    async def resources(self, path=KEYS_PATH) -> list[dict]:
        value = await self.request("GET", path)
        if not isinstance(value, dict) or not isinstance(value.get("resources"), list):
            raise unavailable()
        if not all(
            isinstance(resource, dict)
            and isinstance(resource.get("id"), str)
            and isinstance(resource.get("value"), dict)
            for resource in value["resources"]
        ):
            raise unavailable()
        return value["resources"]

    async def put_route(self, value: dict) -> None:
        await self.request("PUT", ROUTES_PATH, body={"resources": [{"value": value}]})

    async def put_model_config(
        self, path: str, value: dict, *, resource_id: str | None = None
    ) -> dict:
        if path not in {MODELS_PATH, CATALOG_PATH}:
            raise unavailable()
        if resource_id is None:
            result = await self.request("PUT", path, body={"resources": [{"value": value}]})
        else:
            result = await self.request(
                "PUT", path + "/" + quote(resource_id, safe=""), body={"value": value}
            )
        resources = result.get("resources") if isinstance(result, dict) else None
        if not isinstance(resources, list) or len(resources) != 1:
            raise unavailable()
        written = resources[0]
        if not isinstance(written, dict) or type(written.get("revision")) is not int:
            raise unavailable()
        return written

    async def delete_model_config(self, resource_id: str) -> None:
        if not resource_id or len(resource_id) > 128:
            raise unavailable()
        await self.request("DELETE", MODELS_PATH + "/" + quote(resource_id, safe=""))

    async def delete_route(self, resource_id: str) -> None:
        if not resource_id or len(resource_id) > 512:
            raise unavailable()
        await self.request(
            "DELETE", ROUTES_PATH + "/" + quote(resource_id, safe=""), missing_ok=True
        )

    async def create_key(self, value: dict) -> dict:
        # This collection PUT creates a fresh native UUID. The caller owns uncertain-ACK readback.
        result = await self.request("PUT", KEYS_PATH, body={"resources": [{"value": value}]})
        if not isinstance(result, dict):
            raise unavailable()
        return result

    async def update_key(self, resource_id: str, value: dict) -> None:
        try:
            if str(UUID(resource_id)) != resource_id:
                raise ValueError
        except (ValueError, AttributeError, TypeError) as error:
            raise unavailable() from error
        # Only the existing resource path updates a key; the caller verifies desired and live state.
        await self.request("PUT", KEYS_PATH + "/" + resource_id, body={"value": value})

    async def delete_key(self, resource_id: str) -> None:
        if not resource_id or len(resource_id) > 512:
            raise unavailable()
        await self.request("DELETE", KEYS_PATH + "/" + quote(resource_id, safe=""), missing_ok=True)

    async def models(self, task_key: str) -> list[dict]:
        if not task_key:
            raise unavailable()
        result = await self.request("GET", "/v1/models", task_key=task_key)
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise unavailable()
        if not all(
            isinstance(model, dict) and isinstance(model.get("id"), str) for model in result["data"]
        ):
            raise unavailable()
        return result["data"]

    async def budget(self, key_name: str) -> dict:
        result = await self.request("GET", "/api/budgets/status", params={"apiKeyName": key_name})
        if not isinstance(result, dict):
            raise unavailable()
        return result

    async def logs(self, attributes: dict, cursor: str | None = None, limit: int = 100) -> dict:
        if not 1 <= limit <= 500:
            raise unavailable()
        body = {"limit": limit, "includeAttributes": True, "filters": {"attributes": attributes}}
        if cursor is not None:
            body["cursor"] = cursor
        result = await self.request("POST", "/api/logs/search", body=body)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("logs"), list)
            or result.get("nextCursor") is not None
            and not isinstance(result["nextCursor"], str)
            or not all(isinstance(log, dict) for log in result["logs"])
        ):
            raise unavailable()
        return result

    async def get_log(self, request_id: str) -> dict:
        result = await self.request(
            "POST", "/api/logs/get", body={"id": request_id, "includePayload": False}
        )
        if not isinstance(result, dict):
            raise unavailable()
        return result
