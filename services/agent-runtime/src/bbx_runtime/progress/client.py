"""Keep service job admission separate from the restricted progress identity."""

import asyncio
from typing import Any
from urllib.parse import quote

from bbx_runtime.clients.blackboard import BlackboardClient, is_control_transport_error


class ProgressClient:
    def __init__(self, client: BlackboardClient, job: dict[str, Any] | None = None) -> None:
        self.client = client
        self.job = job
        self.tools = client.with_token(job["token"]) if job is not None else client

    async def claim(self) -> dict[str, Any] | None:
        return await self.client._json("POST", "/progress/jobs/claim", json={})

    def bound(self, job: dict[str, Any]) -> "ProgressClient":
        return ProgressClient(self.client, job)

    @property
    def path(self) -> str:
        assert self.job is not None
        return f"/tasks/{quote(str(self.job['task_id']), safe='')}/progress"

    async def operation(self, name: str, **payload: Any) -> Any:
        assert self.job is not None
        body = {
            **payload,
            "claim_token": self.job["claim_token"],
            "run_number": self.job["run_number"],
        }
        for attempt in range(3):
            try:
                return await self.client._json(
                    "POST",
                    f"{self.path}/jobs/{quote(str(self.job['job_id']), safe='')}/{name}",
                    json=body,
                )
            except Exception as error:
                if (
                    name not in {"reserve", "settle", "complete", "candidate"}
                    or attempt == 2
                    or not is_control_transport_error(error)
                ):
                    raise
                # These requests have persistent IDs or a durable completion result.
                await asyncio.sleep(0.1 * 2**attempt)

    async def session(self) -> dict[str, Any]:
        assert self.job is not None
        return await self.client._json(
            "GET",
            f"{self.path}/session",
            params={
                "job_id": self.job["job_id"],
                "claim_token": self.job["claim_token"],
                "run_number": self.job["run_number"],
            },
        )

    async def sources(self, after: int, limit: int = 30) -> dict[str, Any]:
        assert self.job is not None
        return await self.tools._json(
            "GET",
            f"{self.path}/sources",
            params={
                "after_version": after,
                "through_version": self.job["through_version"],
                "limit": limit,
            },
        )

    async def object(self, object_id: str) -> Any:
        return await self.tools._json("GET", f"{self.path}/objects/{quote(object_id, safe='')}")

    async def search(self, q: str, after_version: int = 0, limit: int = 20) -> Any:
        return await self.tools._json(
            "GET",
            f"{self.path}/search",
            params={"q": q, "after_version": after_version, "limit": limit},
        )

    async def evidence(self, uri: str, version: int, sha256: str, offset: int, limit: int) -> Any:
        return await self.tools._json(
            "GET",
            f"{self.path}/evidence",
            params={
                "uri": uri,
                "version": version,
                "sha256": sha256,
                "offset": offset,
                "limit": limit,
            },
        )
