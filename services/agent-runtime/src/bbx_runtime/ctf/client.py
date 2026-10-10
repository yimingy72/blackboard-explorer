"""CTF HTTP operations with service and per-turn identities kept separate."""

from typing import Any

from bbx_runtime.clients.blackboard import BlackboardClient


class CtfClient:
    def __init__(self, client: BlackboardClient) -> None:
        self.client = client

    async def state(self, task_id: str) -> dict[str, Any]:
        return await self.client._json("GET", f"{self.client._task(task_id)}/ctf/state")

    async def runtime(self, task_id: str, operation: str, **payload: Any) -> Any:
        return await self.client._json(
            "POST", f"{self.client._task(task_id)}/ctf/runtime/{operation}", json=payload
        )

    async def tool(self, task_id: str, token: str, operation: str, **payload: Any) -> Any:
        client = self.client.with_token(token)
        return await client._json(
            "POST", f"{client._task(task_id)}/ctf/tools/{operation}", json=payload
        )

    async def get_agent_session(self, task_id: str, agent_id: str) -> dict[str, Any]:
        return await self.client.get_agent_session(task_id, agent_id)

    async def read_evidence(self, uri: str) -> bytes:
        return await self.client.read_evidence(uri)
