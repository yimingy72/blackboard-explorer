"""Compact checkpoint replies retain exact write and delivery proofs."""

import copy
from typing import Any, cast

import httpx
import pytest
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.session import load_checkpoint
from test_ctf_runtime import TURN, MemoryCtf


class CompactStore(MemoryCtf):
    reads = 0
    writes = 0
    corruption: dict[str, Any] | None = None

    async def get_agent_session(self, *args):
        self.reads += 1
        return await super().get_agent_session(*args)

    async def runtime(self, task_id, operation, **payload):
        saved = await super().runtime(task_id, operation, **payload)
        if operation != "checkpoint":
            return saved
        assert isinstance(saved, dict)
        self.writes += 1
        receipt = {
            "revision": saved["revision"],
            "checkpoint_id": payload["session"]["state"]["bbx_checkpoint_id"],
            "task_id": task_id,
            "agent_id": payload["agent_id"],
            "turn_id": payload["turn_id"],
            "generation": payload["generation"],
            "purpose": "execution",
            "delivered_message_ids": [item["message_id"] for item in payload["deliveries"]],
        }
        return {**receipt, **(self.corruption or {})}


async def test_compact_success_does_not_read_back_complete_history():
    service = CompactStore()
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    checkpoint.session.state["synthetic_large_state"] = "a" * 1_000_000
    reads = service.reads
    await checkpoint.save()
    assert checkpoint.revision == 1
    assert service.reads == reads
    assert service.writes == 1
    assert service.saved is not None
    assert service.saved["session"]["state"]["synthetic_large_state"] == "a" * 1_000_000


@pytest.mark.parametrize(
    "corruption",
    [
        {"checkpoint_id": "different"},
        {"turn_id": "other-turn"},
        {"agent_id": "other-member"},
        {"generation": 4},
        {"purpose": "review"},
        {"task_id": "other-task"},
        {"revision": True},
        {"revision": 123},
        {"delivered_message_ids": ["unconfirmed-input"]},
        {"delivered_message_ids": [{}]},
    ],
)
async def test_bad_compact_receipt_requires_matching_full_readback(corruption):
    service = CompactStore()
    service.corruption = corruption
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    reads = service.reads
    await checkpoint.save()
    assert checkpoint.revision == 1
    assert service.reads == reads + 1
    assert service.writes == 1


async def test_compact_lost_reply_only_accepts_exact_saved_snapshot():
    service = CompactStore()
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    service.lose_response = True
    await checkpoint.save()
    assert checkpoint.revision == 1
    assert service.reads == 2


async def test_invalid_receipt_cannot_confirm_another_saved_revision(monkeypatch):
    service = CompactStore()
    service.corruption = {"checkpoint_id": "other"}
    original = service.get_agent_session

    async def changed(*args):
        result = copy.deepcopy(await original(*args))
        result["revision"] += 1
        return result

    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    monkeypatch.setattr(service, "get_agent_session", changed)
    with pytest.raises(httpx.RemoteProtocolError):
        await checkpoint.save()
    assert checkpoint.revision == 0
    assert checkpoint.failed
    assert service.writes == 1


@pytest.mark.parametrize("omitted", ["checkpoint_id", "session", "origin"])
async def test_incomplete_reply_without_full_legacy_proof_reads_back(omitted):
    class Store(CompactStore):
        async def runtime(self, task_id, operation, **payload):
            result = await super().runtime(task_id, operation, **payload)
            if operation == "checkpoint":
                assert isinstance(result, dict)
                if omitted == "checkpoint_id":
                    result.pop("checkpoint_id")
                else:
                    assert self.saved is not None
                    result = {key: value for key, value in self.saved.items() if key != omitted}
            return result

    service = Store()
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    reads = service.reads
    await checkpoint.save()
    assert checkpoint.revision == 1
    assert service.reads == reads + 1
    assert service.writes == 1
