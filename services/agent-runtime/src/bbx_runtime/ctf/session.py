"""Reuse shared snapshot CAS and response-loss reconciliation for CTF mailboxes."""

from typing import Any, cast

from agent_framework import AgentSession

from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.session import SessionCheckpoint, repair_unpaired_tool_calls


class CtfCheckpoint(SessionCheckpoint):
    """Keep an unresolved final write visible to the persistent close barrier."""

    failed = False

    async def save(self) -> None:
        try:
            await super().save()
            self.failed = False
        except Exception:
            self.failed = True
            adapter = cast(TurnSessionClient, self.service)
            await adapter.service.runtime(
                self.task_id,
                "checkpoint_failed",
                agent_id=self.agent_id,
                turn_id=adapter.turn["id"],
                generation=adapter.turn["generation"],
            )
            raise


class TurnSessionClient:
    def __init__(self, service: CtfClient, turn: dict[str, Any]) -> None:
        self.service, self.turn = service, turn

    async def get_agent_session(self, task_id: str, agent_id: str) -> dict[str, Any]:
        return await self.service.get_agent_session(task_id, agent_id)

    async def read_evidence(self, uri: str) -> bytes:
        return await self.service.read_evidence(uri)

    async def put_agent_session(self, task_id: str, agent_id: str, **payload: Any) -> Any:
        deliveries = payload.pop("deliveries")
        payload.pop("review_claim", None)
        payload.pop("origin", None)
        return await self.service.runtime(
            task_id,
            "checkpoint",
            agent_id=agent_id,
            turn_id=self.turn["id"],
            generation=self.turn["generation"],
            deliveries=[
                {"message_id": item["id"], "claim_token": item["claim_token"]}
                for item in deliveries
            ],
            **payload,
        )


async def load_checkpoint(
    service: CtfClient, task_id: str, member_id: str, turn: dict[str, Any], instructions: str
) -> SessionCheckpoint:
    adapter = cast(BlackboardClient, TurnSessionClient(service, turn))
    try:
        saved = await service.get_agent_session(task_id, member_id)
    except RemoteError as error:
        if error.status != 404:
            raise
        saved = None
    checkpoint = CtfCheckpoint(
        adapter,
        task_id,
        member_id,
        AgentSession.from_dict(saved["session"]) if saved else AgentSession(),
        opening_instructions=saved["opening_instructions"] if saved else instructions,
        revision=int(saved["revision"]) if saved else 0,
    )
    repair_unpaired_tool_calls(checkpoint.session, include_interrupted=True)
    return checkpoint
