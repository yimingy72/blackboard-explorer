"""A live chat answer must not prematurely end the same Agent's task run."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from agent_framework import AgentSession, Content, Message
from bbx_contracts.models import Params
from bbx_contracts.profile import load_profile
from bbx_objects import ObjectStore
from bbx_runtime.clients import BlackboardClient, RemoteError
from bbx_runtime.runner import AgentRunner
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall

PROFILE = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")[0]
RECEIPT = '{"accepted":true,"data":{"posted":[],"excluded":[]}}'


class Service:
    def __init__(self, *, posted_message: str | None) -> None:
        self.task_id = str(uuid4())
        self.agent_id = "agent-1"
        self.message_id = str(uuid4())
        self.posted_message = posted_message
        self.queued: list[dict[str, str]] = []
        self.delivered: list[str] = []
        self.session = None
        self.revision = 0
        self.search_calls = 0
        self.finished = None
        self.task = {
            "id": self.task_id,
            "goal": "Investigate a sample issue",
            "acceptance": [],
            "agent_profile": "default",
            "agent_profile_version": 1,
            "params": Params().model_dump(mode="json"),
            "budget": {"max_minutes": 1},
            "status": "running",
        }
        self.run = {
            "id": self.agent_id,
            "task_type": "derive",
            "status": "running",
            "steps": 0,
            "last_seen_version": 0,
        }

    async def state(self, _task_id):
        return {
            "task": self.task,
            "agents": {self.agent_id: self.run},
            "facts": {},
            "intents": {},
        }

    async def get_profile(self, _name, _version):
        return PROFILE.model_dump(mode="json")

    def with_token(self, _token):
        return self

    async def get_agent_session(self, _task_id, _agent_id):
        if self.session is None:
            raise RemoteError(404, "missing")
        return {
            "session": self.session,
            "opening_instructions": self.opening,
            "origin": "native",
            "revision": self.revision,
        }

    async def put_agent_session(
        self,
        _task_id,
        _agent_id,
        *,
        session,
        opening_instructions,
        origin,
        expected_revision,
        deliveries,
        review_claim,
    ):
        assert expected_revision == self.revision and origin == "native"
        assert review_claim is None
        self.session = session
        self.opening = opening_instructions
        self.revision += 1
        for delivery in deliveries:
            self.delivered.append(delivery["id"])
        return {"revision": self.revision}

    async def agent_messages(self, _task_id, _agent_id, *, status):
        assert status == "queued"
        return {"messages": list(self.queued)}

    async def claim_agent_message(self, _task_id, _agent_id, message_id, mode):
        assert mode == "active"
        item = next(item for item in self.queued if item["id"] == message_id)
        self.queued.remove(item)
        return {"message": item, "claim_token": str(uuid4())}

    async def search(self, _task_id, _q, _k, _type):
        self.search_calls += 1
        if self.posted_message is not None:
            self.queued.append({"id": self.message_id, "content": self.posted_message})
            self.posted_message = None
        return []

    async def record_tool_call(self, *_args):
        return []

    async def heartbeat(self, _task_id, _agent_id, **kwargs):
        self.run["steps"] += kwargs["steps"]
        self.run["last_seen_version"] = kwargs["last_seen_version"]
        self.run["context_tokens"] = kwargs["context_tokens"]
        return {}

    async def finish_agent(self, _task_id, _agent_id, receipt, end_reason):
        self.finished = (receipt, end_reason)
        self.run["status"] = "finished"
        return []


class Objects:
    async def put(self, *_args, **_kwargs):
        return None


@pytest.mark.parametrize("user_message", ["现在进度如何？", "请暂停并结束。"])
async def test_live_user_answer_continues_same_session_to_receipt(user_message: str):
    service = Service(posted_message=user_message)
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("search", {"q": "sample"}),)),
            ScriptStep(text="我已经查到初步线索。", expect_contains=user_message),
            ScriptStep(text=RECEIPT, expect_contains="不是任务回执"),
        ]
    )
    runner = AgentRunner(
        Settings.model_construct(),
        cast(BlackboardClient, service),
        cast(ObjectStore, Objects()),
        Mock(),
    )
    result = await runner.run_agent(
        service.task_id, service.agent_id, "derive", agent_token="test", client=client
    )
    assert result.receipt == {"accepted": True, "data": {"posted": [], "excluded": []}}
    assert service.finished is not None and service.finished[1] == "normal"
    assert service.delivered == [service.message_id]
    assert len(client.received_messages) == 3
    assert (
        sum(message.message_id == service.message_id for message in client.received_messages[-1])
        == 1
    )
    assert any("我已经查到初步线索" in message.text for message in client.received_messages[-1])
    assert service.session is not None
    history = AgentSession.from_dict(service.session).state["in_memory"]["messages"]
    assert any(message.message_id == service.message_id for message in history)
    assert service.search_calls == 1


async def test_invalid_receipt_without_new_user_message_is_not_retried():
    service = Service(posted_message=None)
    prior = AgentSession()
    prior.state["in_memory"] = {
        "messages": [
            Message(
                role="user", message_id="old-user-message", contents=[Content.from_text("进度如何")]
            )
        ]
    }
    service.session = prior.to_dict()
    service.opening = "previous instructions"
    service.revision = 1
    client = ScriptedChatClient([ScriptStep(text="没有完成任务。")])
    runner = AgentRunner(
        Settings.model_construct(),
        cast(BlackboardClient, service),
        cast(ObjectStore, Objects()),
        Mock(),
    )
    result = await runner.run_agent(
        service.task_id, service.agent_id, "derive", agent_token="test", client=client
    )
    assert result.receipt["raw_text"] == "没有完成任务。"
    assert len(client.received_messages) == 1
    assert any(message.message_id == "old-user-message" for message in client.received_messages[0])


async def test_two_new_message_batches_can_continue_without_replaying_old_delivery():
    class TwoMessages(Service):
        async def search(self, _task_id, _q, _k, _type):
            self.search_calls += 1
            self.queued.append({"id": str(uuid4()), "content": f"第{self.search_calls}次进度问题"})
            return []

    service = TwoMessages(posted_message=None)
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("search", {"q": "first"}),)),
            ScriptStep(text="已找到初步线索。", expect_contains="第1次进度问题"),
            ScriptStep(calls=(ScriptToolCall("search", {"q": "second"}),)),
            ScriptStep(text="正在复核依据。", expect_contains="第2次进度问题"),
            ScriptStep(text=RECEIPT, expect_contains="不是任务回执"),
        ]
    )
    runner = AgentRunner(
        Settings.model_construct(),
        cast(BlackboardClient, service),
        cast(ObjectStore, Objects()),
        Mock(),
    )
    result = await runner.run_agent(
        service.task_id, service.agent_id, "derive", agent_token="test", client=client
    )
    assert "raw_text" not in result.receipt
    assert len(set(service.delivered)) == len(service.delivered) == 2
    assert len(client.received_messages) == 5
    assert service.session is not None
    history = AgentSession.from_dict(service.session).state["in_memory"]["messages"]
    controls = [m for m in history if (m.message_id or "").startswith("bbx-control-")]
    assert len(controls) == 2
