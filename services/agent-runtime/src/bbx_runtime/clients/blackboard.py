"""Typed-enough HTTP boundary for one blackboard identity."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel


def _body(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


class RemoteError(RuntimeError):
    def __init__(self, status: int, message: str, code: str | None = None) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


class BlackboardClient:
    def __init__(
        self, base_url: str, token: str, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owned = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=30, trust_env=False)

    async def close(self) -> None:
        if self._owned:
            await self._http.aclose()

    async def __aenter__(self) -> BlackboardClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def with_token(self, token: str) -> BlackboardClient:
        return BlackboardClient(self.base_url, token, self._http)

    async def _response(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._http.request(
            method,
            f"{self.base_url}/api{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            **kwargs,
        )
        return await self._check(response)

    @staticmethod
    async def _check(response: httpx.Response) -> httpx.Response:
        if response.is_error:
            await response.aread()
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                detail = payload.get("message", payload.get("detail", payload.get("error")))
                message = detail if isinstance(detail, str) else response.text
                code = payload.get("code")
            else:
                message, code = response.text, None
            raise RemoteError(
                response.status_code, message, code if isinstance(code, str) else None
            )
        return response

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return (await self._response(method, path, **kwargs)).json()

    @staticmethod
    def _task(task_id: UUID | str) -> str:
        return f"/tasks/{quote(str(task_id), safe='')}"

    async def list_tasks(self) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        while True:
            page = await self._json("GET", "/tasks", params={"limit": 500, "offset": len(tasks)})
            tasks.extend(page)
            if len(page) < 500:
                return tasks

    async def get_task(self, task_id: UUID | str) -> dict[str, Any]:
        return await self._json("GET", self._task(task_id))

    async def create_task(self, request: BaseModel | dict[str, Any]) -> dict[str, Any]:
        return await self._json("POST", "/tasks", json=_body(request))

    async def start_task(self, task_id: UUID | str) -> dict[str, Any]:
        return await self._json("POST", f"{self._task(task_id)}/start")

    async def stop_task(self, task_id: UUID | str) -> dict[str, Any]:
        return await self._json("POST", f"{self._task(task_id)}/stop")

    async def state(self, task_id: UUID | str) -> dict[str, Any]:
        return await self._json("GET", f"{self._task(task_id)}/state")

    async def archive_data(self, task_id: UUID | str) -> dict[str, Any]:
        return await self._json("GET", f"{self._task(task_id)}/archive-data")

    async def events(
        self, task_id: UUID | str, since: int = 0, for_agent: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {"since": since}
        if for_agent is not None:
            params["for"] = for_agent
        return await self._json("GET", f"{self._task(task_id)}/events", params=params)

    async def stream(self, task_id: UUID | str, since: int = 0) -> AsyncIterator[dict[str, Any]]:
        timeout = httpx.Timeout(connect=10, read=None, write=30, pool=10)
        async with self._http.stream(
            "GET",
            f"{self.base_url}/api{self._task(task_id)}/stream",
            headers={"Authorization": f"Bearer {self.token}"},
            params={"since": since},
            timeout=timeout,
        ) as response:
            await self._check(response)
            fields: dict[str, str] = {}
            data: list[str] = []
            async for line in response.aiter_lines():
                if not line:
                    if data:
                        yield self._sse_event(fields, data)
                    fields, data = {}, []
                    continue
                if line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if not separator:
                    continue
                value = value.removeprefix(" ")
                if field == "data":
                    data.append(value)
                elif field in {"id", "event"}:
                    fields[field] = value
            if data:
                yield self._sse_event(fields, data)

    @staticmethod
    def _sse_event(fields: dict[str, str], data: list[str]) -> dict[str, Any]:
        if not fields.get("id", "").isdecimal() or not fields.get("event"):
            raise ValueError("SSE frame has no event id or type")
        payload = json.loads("\n".join(data))
        if not isinstance(payload, dict):
            raise ValueError("SSE event data must be an object")
        if payload.get("version") != int(fields["id"]) or payload.get("type") != fields["event"]:
            raise ValueError("SSE frame disagrees with event data")
        return payload

    async def snapshot(self, task_id: UUID | str, max_lines: int | None = None) -> str:
        params = {"max_lines": max_lines} if max_lines is not None else None
        return (await self._response("GET", f"{self._task(task_id)}/snapshot", params=params)).text

    async def get_object(
        self, task_id: UUID | str, object_id: str, depth: int = 1
    ) -> dict[str, Any]:
        return await self._json(
            "GET",
            f"{self._task(task_id)}/objects/{quote(object_id, safe='')}",
            params={"depth": depth},
        )

    async def read_evidence(self, uri: str) -> bytes:
        return (await self._response("GET", "/evidence", params={"uri": uri})).content

    async def search(
        self,
        task_id: UUID | str,
        q: str | None = None,
        k: int = 3,
        type: Literal["fact", "intent", "all"] = "all",
    ) -> list[dict[str, Any]]:
        params = {"k": k, "type": type}
        if q is not None:
            params["q"] = q
        return await self._json("GET", f"{self._task(task_id)}/search", params=params)

    async def post_fact(
        self, task_id: UUID | str, request: BaseModel | dict[str, Any], *, dry_run: bool = False
    ) -> dict[str, Any]:
        return await self._json(
            "POST", f"{self._task(task_id)}/facts", params={"dry_run": dry_run}, json=_body(request)
        )

    async def post_intent(
        self, task_id: UUID | str, request: BaseModel | dict[str, Any], *, dry_run: bool = False
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"{self._task(task_id)}/intents",
            params={"dry_run": dry_run},
            json=_body(request),
        )

    async def claim(self, task_id: UUID | str, intent_id: str) -> list[dict[str, Any]]:
        return await self._json(
            "POST", f"{self._task(task_id)}/intents/{quote(intent_id, safe='')}/claim"
        )

    async def release(self, task_id: UUID | str, intent_id: str, note: str) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"{self._task(task_id)}/intents/{quote(intent_id, safe='')}/release",
            json={"note": note},
        )

    async def submit_close(
        self,
        task_id: UUID | str,
        request: BaseModel | dict[str, Any],
        *,
        report_uri: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"{self._task(task_id)}/close",
            json={**_body(request), "report_uri": report_uri},
        )

    async def get_profile(self, name: str, version: int) -> dict[str, Any]:
        return await self._json("GET", f"/profiles/{quote(name, safe='')}/versions/{version}")

    async def get_worker_prompt(self, role: str, since: int = 0) -> dict[str, Any]:
        return await self._json(
            "GET", f"/settings/prompts/{quote(role, safe='')}", params={"since": since}
        )

    async def import_environment_key(self, api_key: str) -> dict[str, int]:
        return await self._json(
            "POST", "/platform/import-environment-key", json={"api_key": api_key}
        )

    async def get_model_credentials(self, name: str, version: int) -> dict[str, Any]:
        return await self._json(
            "GET", f"/platform/models/{quote(name, safe='')}/versions/{version}/credentials"
        )

    async def get_mcp_server(self, name: str, version: int) -> dict[str, Any]:
        return await self._json(
            "GET", f"/platform/mcp-servers/{quote(name, safe='')}/versions/{version}"
        )

    async def get_mcp_credentials(self, name: str, version: int) -> dict[str, Any]:
        return await self._json(
            "GET", f"/platform/mcp-servers/{quote(name, safe='')}/versions/{version}/credentials"
        )

    def _agent(self, task_id: UUID | str, agent_id: str) -> str:
        return f"{self._task(task_id)}/agents/{quote(agent_id, safe='')}"

    async def get_agent_session(self, task_id: UUID | str, agent_id: str) -> dict[str, Any]:
        return await self._json("GET", f"{self._agent(task_id, agent_id)}/session")

    async def put_agent_session(
        self,
        task_id: UUID | str,
        agent_id: str,
        *,
        session: dict[str, Any],
        opening_instructions: str,
        origin: Literal["native", "legacy"],
        expected_revision: int,
        deliveries: list[dict[str, str]] | None = None,
        review_claim: dict[str, str] | None = None,
        expected_derive_round: int | None = None,
    ) -> dict[str, Any]:
        return await self._json(
            "PUT",
            f"{self._agent(task_id, agent_id)}/session",
            json={
                "session": session,
                "opening_instructions": opening_instructions,
                "origin": origin,
                "expected_revision": expected_revision,
                "deliveries": deliveries or [],
                **({"review_claim": review_claim} if review_claim is not None else {}),
                **(
                    {"expected_derive_round": expected_derive_round}
                    if expected_derive_round is not None
                    else {}
                ),
            },
        )

    async def agent_messages(
        self, task_id: UUID | str, agent_id: str, *, status: str | None = None
    ) -> dict[str, Any]:
        params = {"status": status} if status is not None else None
        return await self._json("GET", f"{self._agent(task_id, agent_id)}/messages", params=params)

    async def claim_agent_message(
        self, task_id: UUID | str, agent_id: str, message_id: str, mode: Literal["active", "review"]
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"{self._agent(task_id, agent_id)}/messages/{quote(message_id, safe='')}/claim",
            json={"mode": mode},
        )

    async def complete_agent_message(
        self, task_id: UUID | str, agent_id: str, message_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        path = f"{self._agent(task_id, agent_id)}/messages/{quote(message_id, safe='')}/complete"
        try:
            return await self._json("POST", path, json=body)
        except httpx.TransportError:
            # The first request may have committed. The endpoint returns the same reply
            # for a repeated completion with the same claim token.
            return await self._json("POST", path, json=body)

    async def fail_agent_message(
        self, task_id: UUID | str, agent_id: str, message_id: str, claim_token: str, error: str
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"{self._agent(task_id, agent_id)}/messages/{quote(message_id, safe='')}/fail",
            json={"claim_token": claim_token, "error": error},
        )

    async def pending_conversations(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/conversations/pending")

    async def recover_conversations(self) -> dict[str, Any]:
        return await self._json("POST", "/conversations/recover")

    async def pending_deletions(self) -> list[str]:
        return await self._json("GET", "/tasks/deletions")

    async def purge_task(self, task_id: UUID | str) -> dict[str, Any]:
        return await self._json("POST", f"{self._task(task_id)}/purge")

    async def register_agent(
        self,
        task_id: UUID | str,
        task_type: Literal["explore", "derive", "close"],
        *,
        is_seed: bool = False,
        close_mode: Literal["judge", "final"] | None = None,
        derive_parallel: bool | None = None,
        derive_review: bool | None = None,
    ) -> dict[str, Any]:
        body = {"task_type": task_type, "is_seed": is_seed, "close_mode": close_mode}
        if derive_parallel is not None:
            body["derive_parallel"] = derive_parallel
        if derive_review is not None:
            body["derive_review"] = derive_review
        return await self._json(
            "POST",
            f"{self._task(task_id)}/agents",
            json=body,
        )

    async def heartbeat(
        self,
        task_id: UUID | str,
        agent_id: str,
        *,
        steps: int,
        context_tokens: int,
        usage: BaseModel | dict[str, Any],
        last_seen_version: int,
        requested_at: datetime | None = None,
        expected_derive_round: int | None = None,
    ) -> dict[str, Any]:
        return await self._json(
            "PATCH",
            self._agent(task_id, agent_id),
            json={
                "steps": steps,
                "context_tokens": context_tokens,
                "usage": _body(usage),
                "last_seen_version": last_seen_version,
                **({"requested_at": requested_at.isoformat()} if requested_at else {}),
                **(
                    {"expected_derive_round": expected_derive_round}
                    if expected_derive_round is not None
                    else {}
                ),
            },
        )

    async def conclude(
        self,
        task_id: UUID | str,
        agent_id: str,
        reason: str,
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"{self._agent(task_id, agent_id)}/conclude",
            json={
                "reason": reason,
                **(
                    {"expected_derive_round": expected_derive_round}
                    if expected_derive_round is not None
                    else {}
                ),
            },
        )

    async def take_grace(
        self,
        task_id: UUID | str,
        agent_id: str,
        *,
        expected_derive_round: int | None = None,
    ) -> int:
        result = await self._json(
            "POST",
            f"{self._agent(task_id, agent_id)}/grace",
            **(
                {"json": {"expected_derive_round": expected_derive_round}}
                if expected_derive_round is not None
                else {}
            ),
        )
        return int(result["remaining"])

    async def finish_agent(
        self,
        task_id: UUID | str,
        agent_id: str,
        receipt: dict[str, Any],
        end_reason: str,
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"{self._agent(task_id, agent_id)}/finish",
            json={
                "receipt": receipt,
                "end_reason": end_reason,
                **(
                    {"expected_derive_round": expected_derive_round}
                    if expected_derive_round is not None
                    else {}
                ),
            },
        )

    async def transition(
        self, task_id: UUID | str, status: str, reason: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST", f"{self._task(task_id)}/status", json={"status": status, "reason": reason}
        )

    async def record_archive(
        self, task_id: UUID | str, uri: str, size: int, fallback: str
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"{self._task(task_id)}/archive",
            json={"uri": uri, "size": size, "fallback": fallback},
        )

    async def record_cleanup(self, task_id: UUID | str) -> list[dict[str, Any]]:
        return await self._json("POST", f"{self._task(task_id)}/cleanup-ready")

    async def claim_for(
        self, task_id: UUID | str, intent_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"{self._task(task_id)}/claim_for",
            json={"intent_id": intent_id, "agent_id": agent_id},
        )

    async def system_close(self, task_id: UUID | str, intent_id: str) -> list[dict[str, Any]]:
        return await self._json(
            "POST", f"{self._task(task_id)}/intents/{quote(intent_id, safe='')}/system_close"
        )

    async def record_tool_call(
        self,
        task_id: UUID | str,
        call: dict[str, Any],
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"{self._task(task_id)}/tool_calls",
            json={
                **call,
                **(
                    {"expected_derive_round": expected_derive_round}
                    if expected_derive_round is not None
                    else {}
                ),
            },
        )

    async def record_agent_trace(
        self,
        task_id: UUID | str,
        agent_id: str,
        trace: dict[str, Any],
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._json(
            "POST",
            f"{self._agent(task_id, agent_id)}/traces",
            json={
                **trace,
                **(
                    {"expected_derive_round": expected_derive_round}
                    if expected_derive_round is not None
                    else {}
                ),
            },
        )
