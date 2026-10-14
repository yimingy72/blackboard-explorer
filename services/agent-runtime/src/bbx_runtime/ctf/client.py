"""CTF HTTP operations with service and per-turn identities kept separate."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from bbx_runtime.clients.blackboard import BlackboardClient, is_control_transport_error

logger = logging.getLogger(__name__)


class CtfControlTransportExhausted(RuntimeError):
    """Retain the local pending turn after bounded control recovery fails."""

    def __init__(self, task_id: str, member_id: str, turn_id: str, generation: int, stage: str):
        super().__init__("CTF control settlement transport retries exhausted")
        self.task_id, self.member_id, self.turn_id = task_id, member_id, turn_id
        self.generation, self.stage = generation, stage


class CtfClient:
    def __init__(self, client: BlackboardClient) -> None:
        self.client = client

    async def _read(self, operation: str, request: Callable[[], Awaitable[Any]]) -> Any:
        for attempt in range(3):
            try:
                return await request()
            except Exception as error:
                if not is_control_transport_error(error):
                    raise
                if attempt == 2:
                    raise
                logger.warning(
                    "CTF control read retry operation=%s attempt=%s error_type=%s",
                    operation,
                    attempt + 1,
                    type(error).__name__,
                )
                await asyncio.sleep(0.1 * 2**attempt)

    async def state(self, task_id: str) -> dict[str, Any]:
        return await self._read(
            "state", lambda: self.client._json("GET", f"{self.client._task(task_id)}/ctf/state")
        )

    async def runtime_state(self, task_id: str) -> dict[str, Any]:
        return await self._read(
            "runtime_state",
            lambda: self.client._json("GET", f"{self.client._task(task_id)}/ctf/runtime-state"),
        )

    async def runtime(self, task_id: str, operation: str, **payload: Any) -> Any:
        path = f"{self.client._task(task_id)}/ctf/runtime/{operation}"
        if operation == "recovery_input_proofs":
            return await self._read(
                operation, lambda: self.client._json("POST", path, json=payload)
            )
        try:
            result = await self.client._json("POST", path, json=payload)
            if operation == "finish_turn" and (
                not isinstance(result, dict) or result.get("status") != "finished"
            ):
                raise httpx.RemoteProtocolError("CTF finish response lacks settlement proof")
            return result
        except Exception as error:
            if not is_control_transport_error(error):
                raise
            if operation not in {"claim_turn", "claim_review_turn"}:
                raise
            try:
                state = await self.runtime_state(task_id)
            except Exception as read_error:
                if not is_control_transport_error(read_error):
                    raise
                raise error from read_error
            owned = next(
                (
                    turn
                    for turn in state.get("turns", [])
                    if turn["agent_id"] == payload.get("agent_id")
                    and turn.get("runtime_instance") == payload.get("runtime_instance")
                    and turn["status"] == "running"
                    and turn.get("purpose", "execution")
                    == ("review" if operation == "claim_review_turn" else "execution")
                ),
                None,
            )
            if operation == "claim_review_turn":
                member = next(
                    (
                        item
                        for item in state.get("members", [])
                        if item["id"] == payload.get("agent_id")
                    ),
                    None,
                )
                if (
                    owned
                    and member
                    and member.get("current_turn_id") == owned["id"]
                    and member.get("generation") == owned["generation"]
                    and state["task"]["status"] in {"finished", "failed", "stopped"}
                ):
                    # The fenced API re-signs the existing same-instance review identity.
                    claimed = await self.client._json(
                        "POST", path, json={**payload, "expected_turn_id": owned["id"]}
                    )
                    if claimed is None:
                        return None
                    if (
                        not isinstance(claimed, dict)
                        or claimed.get("id") != owned["id"]
                        or not claimed.get("token")
                    ):
                        raise error
                    return claimed
                raise
            if not owned or state["task"]["ctf_control"]["phase"] != "running":
                raise
            # Conditional re-signing cannot create a replacement after a concurrent settlement.
            claimed = await self.client._json(
                "POST", path, json={**payload, "expected_turn_id": owned["id"]}
            )
            if claimed is None:
                return None
            if (
                not isinstance(claimed, dict)
                or claimed.get("id") != owned["id"]
                or not claimed.get("token")
            ):
                raise error
            return claimed

    async def tool(self, task_id: str, token: str, operation: str, **payload: Any) -> Any:
        client = self.client.with_token(token)
        return await client._json(
            "POST", f"{client._task(task_id)}/ctf/tools/{operation}", json=payload
        )

    async def get_agent_session(self, task_id: str, agent_id: str) -> dict[str, Any]:
        return await self._read(
            "agent_session", lambda: self.client.get_agent_session(task_id, agent_id)
        )

    async def read_evidence(self, uri: str) -> bytes:
        return await self._read("evidence", lambda: self.client.read_evidence(uri))
