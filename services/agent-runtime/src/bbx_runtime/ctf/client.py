"""CTF HTTP operations with service and per-turn identities kept separate."""

from typing import Any

import httpx

from bbx_runtime.clients.blackboard import BlackboardClient


class CtfClient:
    def __init__(self, client: BlackboardClient) -> None:
        self.client = client

    async def state(self, task_id: str) -> dict[str, Any]:
        return await self.client._json("GET", f"{self.client._task(task_id)}/ctf/state")

    async def runtime_state(self, task_id: str) -> dict[str, Any]:
        return await self.client._json("GET", f"{self.client._task(task_id)}/ctf/runtime-state")

    async def runtime(self, task_id: str, operation: str, **payload: Any) -> Any:
        path = f"{self.client._task(task_id)}/ctf/runtime/{operation}"
        try:
            return await self.client._json("POST", path, json=payload)
        except httpx.TransportError:
            if operation != "claim_turn":
                raise
            state = await self.runtime_state(task_id)
            owned = any(
                turn["agent_id"] == payload.get("agent_id")
                and turn.get("runtime_instance") == payload.get("runtime_instance")
                and turn["status"] == "running"
                for turn in state.get("turns", [])
            )
            if not owned or state["task"]["ctf_control"]["phase"] != "running":
                raise
            # The same-instance claim returns the existing turn and a fresh fenced token.
            return await self.client._json("POST", path, json=payload)

    async def tool(self, task_id: str, token: str, operation: str, **payload: Any) -> Any:
        client = self.client.with_token(token)
        return await client._json(
            "POST", f"{client._task(task_id)}/ctf/tools/{operation}", json=payload
        )

    async def get_agent_session(self, task_id: str, agent_id: str) -> dict[str, Any]:
        return await self.client.get_agent_session(task_id, agent_id)

    async def read_evidence(self, uri: str) -> bytes:
        return await self.client.read_evidence(uri)
