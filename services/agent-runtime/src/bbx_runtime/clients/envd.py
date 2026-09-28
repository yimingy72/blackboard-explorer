"""Internal envd HTTP client; MCP tool traffic uses the official SDK separately."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .blackboard import RemoteError


class EnvdClient:
    def __init__(
        self, base_url: str, token: str, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owned = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=30, trust_env=False)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def close(self) -> None:
        if self._owned:
            await self._http.aclose()

    async def __aenter__(self) -> EnvdClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @staticmethod
    async def _check(response: httpx.Response) -> httpx.Response:
        if response.is_error:
            await response.aread()
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                detail = payload.get("error", payload.get("detail"))
                message = detail if isinstance(detail, str) else response.text
            else:
                message = response.text
            raise RemoteError(response.status_code, message)
        return response

    async def _response(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._http.request(
            method, f"{self.base_url}{path}", headers=self.headers, **kwargs
        )
        return await self._check(response)

    async def health(self) -> dict[str, str]:
        return (await self._response("GET", "/health")).json()

    async def create_user(self, agent_id: str) -> dict[str, str]:
        return (await self._response("POST", "/users", json={"agent_id": agent_id})).json()

    async def restore_status(self) -> dict[str, Any]:
        return (await self._response("GET", "/restore/status")).json()

    async def restore(self, content: AsyncIterator[bytes]) -> dict[str, Any]:
        timeout = httpx.Timeout(connect=10, read=None, write=None, pool=10)
        return (await self._response("POST", "/restore", content=content, timeout=timeout)).json()

    async def stat(self, path: str) -> dict[str, Any]:
        return (await self._response("GET", "/stat", params={"path": path})).json()

    async def read_file(self, path: str) -> bytes:
        return (await self._response("GET", "/files", params={"path": path})).content

    @asynccontextmanager
    async def file_stream(self, path: str) -> AsyncIterator[httpx.Response]:
        async with self._http.stream(
            "GET", f"{self.base_url}/files", headers=self.headers, params={"path": path}
        ) as response:
            yield await self._check(response)

    @asynccontextmanager
    async def archive_stream(self) -> AsyncIterator[httpx.Response]:
        timeout = httpx.Timeout(connect=10, read=None, write=30, pool=10)
        async with self._http.stream(
            "POST", f"{self.base_url}/archive", headers=self.headers, timeout=timeout
        ) as response:
            yield await self._check(response)
