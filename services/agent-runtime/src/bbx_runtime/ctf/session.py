"""Reuse shared snapshot CAS and response-loss reconciliation for CTF mailboxes."""

import asyncio
import logging
from typing import Any, cast

import httpx
from agent_framework import AgentSession, Message

from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.context import message_signature
from bbx_runtime.session import SessionCheckpoint, repair_unpaired_tool_calls

logger = logging.getLogger(__name__)


class CtfCheckpoint(SessionCheckpoint):
    """Keep an unresolved final write visible to the persistent close barrier."""

    failed = False
    pending_end_reason: str | None = None
    confirmed_history: tuple[str, ...] = ()
    confirmed_context_epoch = 0

    async def save(self) -> None:
        adapter = cast(TurnSessionClient, self.service)
        try:
            await super().save()
            self.confirmed_history = adapter.confirmed_history
            self.confirmed_context_epoch = adapter.confirmed_context_epoch
            self.failed = False
        except Exception as error:
            original = adapter.write_error or error
            adapter.write_error = None
            uncertain = isinstance(original, (httpx.TransportError, ConnectionError)) or (
                isinstance(original, RemoteError) and original.status >= 500
            )
            if uncertain:
                self.failed = True
                try:
                    await adapter.service.runtime(
                        self.task_id,
                        "checkpoint_failed",
                        agent_id=self.agent_id,
                        turn_id=adapter.turn["id"],
                        generation=adapter.turn["generation"],
                    )
                except Exception as marker_error:
                    logger.warning(
                        "CTF checkpoint failure marker rejected turn_id=%s generation=%s "
                        "error_type=%s status=%s",
                        adapter.turn["id"],
                        adapter.turn["generation"],
                        type(marker_error).__name__,
                        getattr(marker_error, "status", None),
                    )
            if original is not error:
                raise original from error
            raise


class TurnSessionClient:
    def __init__(self, service: CtfClient, turn: dict[str, Any]) -> None:
        self.service, self.turn = service, turn
        self.write_error: Exception | None = None
        self.confirmed_history: tuple[str, ...] = ()
        self.confirmed_context_epoch = 0

    async def get_agent_session(self, task_id: str, agent_id: str) -> dict[str, Any]:
        saved = await self.service.get_agent_session(task_id, agent_id)
        self.confirmed_history = confirmed_history(saved["session"])
        self.confirmed_context_epoch = (
            saved["session"].get("state", {}).get("ctf_history_view", {}).get("epoch", 0)
        )
        return saved

    async def read_evidence(self, uri: str) -> bytes:
        return await self.service.read_evidence(uri)

    async def put_agent_session(self, task_id: str, agent_id: str, **payload: Any) -> Any:
        self.write_error = None
        proof = confirmed_history(payload["session"])
        epoch = payload["session"].get("state", {}).get("ctf_history_view", {}).get("epoch", 0)
        deliveries = payload.pop("deliveries")
        payload.pop("review_claim", None)
        payload.pop("origin", None)
        for attempt in range(3):
            try:
                result = await self.service.runtime(
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
                self.write_error = None
                break
            except (httpx.TransportError, ConnectionError, RemoteError) as error:
                if isinstance(error, RemoteError) and error.status < 500:
                    self.write_error = None
                    raise
                self.write_error = error
                try:
                    saved = await self.service.get_agent_session(task_id, agent_id)
                except RemoteError as read_error:
                    if read_error.status != 404 or payload["expected_revision"] != 0:
                        raise error from read_error
                    saved = None
                except Exception as read_error:
                    raise error from read_error
                if saved and (
                    int(saved["revision"]) == payload["expected_revision"] + 1
                    and saved["session"] == payload["session"]
                    and saved["opening_instructions"] == payload["opening_instructions"]
                    and saved["origin"] == "native"
                ):
                    result = saved
                    self.write_error = None
                    break
                # Retry only this exact CAS write after proving it did not commit.
                if attempt == 2 or (
                    saved and int(saved["revision"]) != payload["expected_revision"]
                ):
                    raise
                await asyncio.sleep(0.1 * (attempt + 1))
        else:
            raise AssertionError("Checkpoint retry budget exhausted")
        self.confirmed_history = proof
        self.confirmed_context_epoch = epoch
        return result


def confirmed_history(session: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        message_signature(Message.from_dict(message))
        for message in session.get("state", {}).get("in_memory", {}).get("messages", [])
    )


async def load_checkpoint(
    service: CtfClient, task_id: str, member_id: str, turn: dict[str, Any], instructions: str
) -> CtfCheckpoint:
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
    if saved:
        checkpoint.confirmed_history = confirmed_history(saved["session"])
        cast(TurnSessionClient, adapter).confirmed_history = checkpoint.confirmed_history
        checkpoint.confirmed_context_epoch = (
            saved["session"].get("state", {}).get("ctf_history_view", {}).get("epoch", 0)
        )
        cast(
            TurnSessionClient, adapter
        ).confirmed_context_epoch = checkpoint.confirmed_context_epoch
    repair_unpaired_tool_calls(checkpoint.session, include_interrupted=True)
    return checkpoint
