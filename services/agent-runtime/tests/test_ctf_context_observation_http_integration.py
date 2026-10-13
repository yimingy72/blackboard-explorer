"""Context blockage diagnostics cross real HTTP validation and PostgreSQL registration."""

import json
from uuid import UUID

import pytest
from agent_framework import Content, Message
from bbx_blackboard.store import schema as s
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
from pydantic import SecretStr
from sqlalchemy import select
from test_ctf_cooperation_integration import cooperation_board as cooperation_board
from test_ctf_cooperation_integration import create_task
from test_ctf_vertical_integration import MemoryObjects
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

pytestmark = pytest.mark.integration


async def test_blocked_runner_diagnostic_is_registered_and_readable_over_real_http(
    cooperation_board, monkeypatch
):
    board, ctf, _, configured, _, engine = cooperation_board

    async def stream(self, uri):
        yield self.files[uri]

    monkeypatch.setattr(MemoryObjects, "stream", stream, raising=False)
    tid = await create_task(board, ctf, configured, "10")
    turn = await ctf.runtime(tid, "claim_turn", agent_id="lead", runtime_instance="blocked-http")
    checkpoint = await load_checkpoint(ctf, tid, "lead", turn, "Synthetic saved instructions")
    checkpoint.session.state["in_memory"] = {
        "messages": [
            Message(
                role="user",
                message_id="synthetic-large-input",
                contents=[Content.from_text("!" * 70000)],
            )
        ]
    }
    await checkpoint.save()
    client = ScriptedChatClient([ScriptStep(text="must not dispatch")])
    settings = Settings.model_construct(deepseek_api_key=SecretStr("unused-scripted"))
    runner = CtfRunner(ctf, settings, client_factory=lambda *_: client)
    await runner.run(tid, {"id": "lead", "display_name": "Lead", "role": "lead"}, turn)
    assert not client.received_messages

    async with engine.connect() as conn:
        traces = (
            (
                await conn.execute(
                    select(s.events.c.payload).where(
                        s.events.c.task_id == UUID(tid),
                        s.events.c.type == "agent.trace.recorded",
                        s.events.c.payload["kind"].astext == "turn_context",
                    )
                )
            )
            .scalars()
            .all()
        )
    decoded = [json.loads(await board.read_evidence(trace["uri"])) for trace in traces]
    boundaries = [body for body in decoded if body.get("boundary") == "context_blocked"]
    assert len(boundaries) == 1
    diagnostic = boundaries[0]["diagnostics"]
    assert diagnostic["branch"] == "unshrinkable_loaded_history"
    assert diagnostic["estimated_input_tokens"] > diagnostic["limit_tokens"]
    assert isinstance(diagnostic["eligible"], int)
    assert None not in diagnostic["input_message_ids"]
    saved = await ctf.get_agent_session(tid, "lead")
    assert saved["session"]["state"]["ctf_context_blocked"] == diagnostic
    state = await ctf.state(tid)
    finished = next(item for item in state["turns"] if item["id"] == turn["id"])
    assert finished["end_reason"] == "context_blocked"
    assert diagnostic["branch"] in finished["final_answer"]
    assert "splitting tool pairs" not in finished["final_answer"]
    assert not runner.pending_checkpoints
