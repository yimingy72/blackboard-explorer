"""Archived conversations keep real history and expose read-only tools only."""

import asyncio
import json
import struct
import zlib
from pathlib import Path
from typing import cast
from uuid import uuid4

from agent_framework import AgentSession, Content, Message
from bbx_contracts.profile import load_profile
from bbx_runtime.chatworker import ChatWorker, _completed_text
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import (
    ScriptedChatClient,
    ScriptStep,
    ScriptToolCall,
    ScriptUsage,
)
from pydantic import SecretStr

PROFILE_DIR = Path(__file__).resolve().parents[3] / "profiles/default"


class ReviewService:
    def __init__(self, *, native: bool):
        self.task_id = str(uuid4())
        self.agent_id = "agent-1"
        self.message_id = str(uuid4())
        self.revision = 0
        self.session = AgentSession().to_dict() if native else None
        self.opening = "旧的 conclude 指令和 JSON 回执" if native else ""
        self.origin = "native" if native else None
        self.completed = None
        self.get_calls = []
        self.profile = load_profile(PROFILE_DIR)[0].model_dump(mode="json")
        self.evidence: dict[str, bytes] = {}

    async def read_evidence(self, uri: str) -> bytes:
        return self.evidence[uri]

    async def claim_agent_message(self, _task_id, _agent_id, _message_id, mode):
        assert mode == "review"
        return {
            "message": {"id": self.message_id, "content": "What was found?"},
            "claim_token": str(uuid4()),
        }

    async def state(self, _task_id):
        return {
            "task": {
                "goal": "Analyze finding",
                "status": "finished",
                "agent_profile": "default",
                "agent_profile_version": 1,
                "acceptance": [],
            },
            "agents": {
                self.agent_id: {
                    "task_type": "explore",
                    "status": "finished",
                    "receipt": {"accepted": True, "data": {"note": "found it"}},
                }
            },
            "facts": {"F1": {"id": "F1", "statement": "Found issue", "status": "valid"}},
            "intents": {},
        }

    async def get_profile(self, _name, _version):
        return self.profile

    async def get_model_credentials(self, name, version):
        assert (name, version) == ("deepseek-default", 1)
        return {"credential_source": "stored", "secret": None, "credentials": {"api_key": "test"}}

    async def get_agent_session(self, _task_id, _agent_id):
        if self.session is None:
            raise RemoteError(404, "missing")
        return {
            "session": json.loads(json.dumps(self.session)),
            "opening_instructions": self.opening,
            "origin": self.origin,
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
        assert expected_revision == self.revision and not deliveries
        assert review_claim is not None and review_claim["id"] == self.message_id
        self.revision += 1
        self.session = json.loads(json.dumps(session))
        self.opening = opening_instructions
        self.origin = origin
        return {"revision": self.revision}

    async def get_object(self, _task_id, object_id, depth):
        self.get_calls.append((object_id, depth))
        return {"id": object_id, "statement": "Found issue"}

    async def complete_agent_message(self, _task_id, _agent_id, _message_id, body):
        assert body["expected_revision"] == self.revision
        self.session = body["session"]
        self.revision += 1
        self.completed = body
        return {"id": str(uuid4()), "role": "assistant", "content": body["content"]}

    async def fail_agent_message(self, *_args):
        raise AssertionError("review must not fail")


async def test_read_only_review_resumes_native_session(monkeypatch):
    class AuditedService(ReviewService):
        async def put_agent_session(self, *args, **kwargs):
            saved = AgentSession.from_dict(kwargs["session"]).state
            raw = saved.get("bbx_review_usage", {}).get(self.message_id)
            if raw:
                # Rates and cost must be durable in the same snapshot as the response.
                billed = saved["bbx_review_billed"][self.message_id]
                assert billed["usage"]["output_tokens"] == raw["output_token_count"]
                assert all(call["pricing"]["requested_at"] for call in billed["calls"])
            return await super().put_agent_session(*args, **kwargs)

    service = AuditedService(native=True)
    script = ScriptedChatClient(
        [
            ScriptStep(
                calls=(ScriptToolCall("get", {"object_id": "F1"}),), usage=ScriptUsage(2, 3, 4)
            ),
            ScriptStep(
                text="The finding was F1.",
                expect_contains="Found issue",
                usage=ScriptUsage(5, 6, 7),
            ),
        ]
    )
    monkeypatch.setattr("bbx_runtime.chatworker.make_client", lambda *_args, **_kwargs: script)
    worker = ChatWorker(
        Settings.model_construct(deepseek_api_key=SecretStr("test")),
        cast(BlackboardClient, service),
    )
    await worker.process(service.task_id, service.agent_id, service.message_id)
    assert service.completed is not None
    assert service.completed["content"] == "The finding was F1."
    assert service.get_calls == [("F1", 0)]
    assert service.completed["origin"] == "native"
    assert service.completed["usage"]["cache_hit_tokens"] == 7
    assert service.completed["usage"]["cache_miss_tokens"] == 9
    assert service.completed["usage"]["output_tokens"] == 11
    assert script.received_streams == [True, True]
    tools = script.received_options[0]["tools"]
    assert {item.name for item in tools} == {"get", "search", "read_evidence", "view_image"}
    assert "JSON 回执要求已结束" in script.received_options[0]["instructions"]


async def test_review_view_image_uses_saved_task_evidence_only(monkeypatch):
    service = ReviewService(native=True)
    uri = f"evidence/{service.task_id}/agent-1/proof.png"

    def chunk(name: bytes, payload: bytes) -> bytes:
        body = name + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    service.evidence[uri] = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )
    script = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("view_image", {"path_or_uri": uri}),)),
            ScriptStep(text="I see the image."),
        ]
    )
    monkeypatch.setattr("bbx_runtime.chatworker.make_client", lambda *_args, **_kwargs: script)
    worker = ChatWorker(
        Settings.model_construct(deepseek_api_key=SecretStr("test")),
        cast(BlackboardClient, service),
    )
    await worker.process(service.task_id, service.agent_id, service.message_id)
    assert service.completed is not None
    assert any(
        content.type == "data" and content.media_type == "image/png"
        for message in script.received_messages[1]
        for content in message.contents
    )
    assert service.session is not None and "data:image" not in str(service.session)


async def test_legacy_review_is_marked_and_keeps_its_new_session(monkeypatch):
    service = ReviewService(native=False)
    script = ScriptedChatClient([ScriptStep(text="Only the archived board is available.")])
    monkeypatch.setattr("bbx_runtime.chatworker.make_client", lambda *_args, **_kwargs: script)
    worker = ChatWorker(
        Settings.model_construct(deepseek_api_key=SecretStr("test")),
        cast(BlackboardClient, service),
    )
    await worker.process(service.task_id, service.agent_id, service.message_id)
    assert service.completed is not None
    assert service.completed["origin"] == "legacy"
    assert "原始模型会话未保存" in service.opening
    assert service.session is not None


async def test_saved_answer_completes_after_retry_without_model(monkeypatch):
    service = ReviewService(native=True)
    assert service.session is not None
    native = AgentSession.from_dict(service.session)
    native.state["bbx_review_response"] = {
        "message_id": service.message_id,
        "content": "Saved answer",
        "usage": {},
    }
    service.session = native.to_dict()
    monkeypatch.setattr("bbx_runtime.chatworker.make_client", lambda *_args, **_kwargs: 1 / 0)
    worker = ChatWorker(
        Settings.model_construct(deepseek_api_key=SecretStr("test")),
        cast(BlackboardClient, service),
    )
    await worker.process(service.task_id, service.agent_id, service.message_id)
    assert service.completed is not None
    assert service.completed["content"] == "Saved answer"
    assert service.completed["usage"] == {"unavailable": True}


def test_completed_text_does_not_take_a_later_users_answer():
    session = AgentSession()
    session.state["in_memory"] = {
        "messages": [
            Message(role="user", message_id="first", contents=[Content.from_text("one")]),
            Message(role="assistant", contents=[Content.from_text("answer one")]),
            Message(role="user", message_id="second", contents=[Content.from_text("two")]),
            Message(role="assistant", contents=[Content.from_text("answer two")]),
        ]
    }
    assert _completed_text(session, "first") == "answer one"
    assert _completed_text(session, "second") == "answer two"


async def test_review_worker_bounds_parallelism_and_serializes_each_agent():
    class Pending:
        async def pending_conversations(self):
            return [
                {"task_id": "task", "agent_id": str(index), "id": f"message-{index}"}
                for index in range(6)
            ] + [{"task_id": "task", "agent_id": "0", "id": "second-message"}]

    worker = ChatWorker(Settings.model_construct(), cast(BlackboardClient, Pending()))
    started: list[tuple[str, str]] = []
    ready = asyncio.Event()

    async def process(task_id: str, agent_id: str, message_id: str) -> None:
        started.append((agent_id, message_id))
        if len(started) == 4:
            ready.set()
        await asyncio.Event().wait()

    worker.process = process
    work = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert len(started) == 4
        assert len({agent_id for agent_id, _ in started}) == 4
        assert ("0", "second-message") not in started
    finally:
        await worker.stop()
        await work
    assert worker.inflight == {}
