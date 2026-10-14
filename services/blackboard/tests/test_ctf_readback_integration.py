"""Readback against the existing disposable PostgreSQL team fixture."""

import copy
import json
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from test_ctf_integration import board_team, history, team
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def test_readback_saved_tool_pages_are_private_and_ambiguous_ids_are_rejected(ctf):
    tid, owner, helper = await board_team(ctf)
    state = {
        "state": {
            "in_memory": {
                "messages": [
                    {
                        "role": "tool",
                        "contents": [
                            {
                                "type": "function_result",
                                "call_id": "selected",
                                "result": "头" * 17001,
                            },
                            {
                                "type": "function_result",
                                "call_id": "other",
                                "result": "Private unrelated output",
                            },
                        ],
                    }
                ]
            },
            "bbx_tool_results": {
                "backup": {
                    "type": "function_result",
                    "call_id": "backup",
                    "result": {"items": [1, "二"]},
                }
            },
        }
    }
    async with ctf.repo.engine.begin() as conn:
        await ctf._task(conn, tid)
        await ctf.sessions._save_session(
            conn, tid, owner["actor"], state, "Private opening", "native", 0
        )
    first = await ctf.read_saved_tool_result(tid, owner["actor"], "selected", limit=16000)
    second = await ctf.read_saved_tool_result(
        tid, owner["actor"], "selected", offset=first["next_offset"]
    )
    assert first["result"] + second["result"] == "头" * 17001
    assert second["next_offset"] is None and second["total_characters"] == 17001
    backup = await ctf.read_saved_tool_result(tid, owner["actor"], "backup")
    assert json.loads(backup["result"]) == {"items": [1, "二"]}
    for actor in ("lead", helper["actor"]):
        with pytest.raises(HTTPException, match="not found"):
            await ctf.read_saved_tool_result(tid, actor, "selected")
    saved = await ctf.sessions.get_session(tid, owner["actor"])
    assert saved["session"] == state
    state["state"]["bbx_tool_results"]["selected"] = {
        "type": "function_result",
        "call_id": "selected",
        "result": "Different occurrence",
    }
    async with ctf.repo.engine.begin() as conn:
        await ctf._task(conn, tid)
        await ctf.sessions._save_session(
            conn, tid, owner["actor"], state, "Private opening", "native", 1
        )
    with pytest.raises(HTTPException, match="conflicting"):
        await ctf.read_saved_tool_result(tid, owner["actor"], "selected")


async def test_saved_tool_projection_excludes_large_history_and_keeps_every_occurrence(
    ctf, monkeypatch
):
    tid, owner, helper = await board_team(ctf)
    call_id = 'selected"].result'
    value = {"a": 1, "b": ["二", 2]}
    state = {
        "state": {
            "in_memory": {
                "messages": [
                    {
                        "contents": [
                            {
                                "type": "function_result",
                                "call_id": f"other-{i}",
                                "result": "x" * 6500,
                            }
                        ]
                    }
                    for i in range(1000)
                ]
                + [
                    {
                        "contents": [
                            {"type": "function_result", "call_id": call_id, "result": value}
                        ]
                    },
                    {
                        "contents": [
                            {
                                "type": "function_result",
                                "call_id": call_id,
                                "result": {"b": ["二", 2], "a": 1},
                            }
                        ]
                    },
                ]
            },
            "bbx_tool_results": {
                call_id: {"type": "function_result", "call_id": call_id, "result": value}
            },
        }
    }
    async with ctf.repo.engine.begin() as conn:
        await ctf.sessions._save_session(conn, tid, owner["actor"], state, "role", "native", 0)
    projections = []
    execute = AsyncConnection.execute

    async def capture(conn, statement, *args, **kwargs):
        result = await execute(conn, statement, *args, **kwargs)
        if getattr(statement, "is_select", False) and "contents" in statement.selected_columns:
            frozen = result.freeze()
            projections.extend(copy.deepcopy(dict(row)) for row in frozen().mappings())
            return frozen()
        return result

    monkeypatch.setattr(AsyncConnection, "execute", capture)
    page = await ctf.read_saved_tool_result(tid, owner["actor"], call_id)
    assert json.loads(page["result"]) == value
    assert len(projections) == 1 and len(projections[0]["contents"]) == 2
    assert len(json.dumps(projections).encode()) < 1000
    assert len(json.dumps(state).encode()) > 6_500_000
    with pytest.raises(HTTPException, match="not found"):
        await ctf.read_saved_tool_result(tid, helper["actor"], call_id)
    with pytest.raises(HTTPException, match="not found"):
        await ctf.read_saved_tool_result(tid, owner["actor"], "missing")
    state["state"]["in_memory"]["messages"][-1]["contents"][0]["result"] = "conflict"
    async with ctf.repo.engine.begin() as conn:
        await ctf.sessions._save_session(conn, tid, owner["actor"], state, "role", "native", 1)
    with pytest.raises(HTTPException, match="conflicting"):
        await ctf.read_saved_tool_result(tid, owner["actor"], call_id)


async def test_readback_settled_assignment_output_keeps_turn_and_task_permissions(ctf):
    tid, owner, helper = await board_team(ctf)
    turn_id, generation = owner["turn_id"], owner["generation"]
    mailbox = await ctf.claim_messages(tid, owner["actor"], turn_id, generation)
    await ctf.checkpoint(
        tid,
        owner["actor"],
        turn_id,
        generation,
        history(mailbox),
        "role",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in mailbox],
    )
    await ctf.finish_turn(tid, owner["actor"], turn_id, generation, "completed", "Result " * 5000)
    first = await ctf.read_turn_result(tid, "lead", turn_id, limit=16000)
    second = await ctf.read_turn_result(
        tid, "lead", turn_id, offset=first["next_offset"], limit=16000
    )
    final = await ctf.read_turn_result(
        tid, "lead", turn_id, offset=second["next_offset"], limit=16000
    )
    assert (
        first["final_answer"] + second["final_answer"] + final["final_answer"] == "Result " * 5000
    )
    assert final["next_offset"] is None
    with pytest.raises(HTTPException, match="Private turn"):
        await ctf.read_turn_result(tid, helper["actor"], turn_id)
    with pytest.raises(HTTPException, match="not settled"):
        await ctf.read_turn_result(tid, helper["actor"], helper["turn_id"])
    other = await team(ctf)
    with pytest.raises(HTTPException):
        await ctf.read_turn_result(other, "lead", turn_id)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_turns)
            .where(
                s.ctf_turns.c.task_id == tid,
                s.ctf_turns.c.id == turn_id,
            )
            .values(assignment_message_ids=[])
        )
    with pytest.raises(HTTPException, match="Private turn"):
        await ctf.read_turn_result(tid, "lead", turn_id)


async def test_readback_deferred_pagination_filters_private_previews_without_releasing_inputs(ctf):
    tid, owner, helper = await board_team(ctf)
    rows = []
    async with ctf.repo.engine.begin() as conn:
        await ctf._task(conn, tid)
        for index in range(105):
            sender, recipient = [
                (owner["actor"], "lead"),
                ("lead", owner["actor"]),
                (owner["actor"], helper["actor"]),
                ("user", helper["actor"]),
            ][index % 4]
            rows.append(
                await ctf._message(
                    conn,
                    tid,
                    sender,
                    recipient,
                    f"Synthetic private message {index}:" + "汉" * 300,
                    str(uuid4()),
                    deferred=True,
                )
            )
    expected = sorted(rows, key=lambda m: (m["recipient_id"], m["recipient_sequence"], m["id"]))
    values = []
    offset = 0
    while True:
        page = await ctf.list_deferred_messages(tid, "lead", offset=offset)
        values.extend(page["messages"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert [m["id"] for m in values] == [m["id"] for m in expected]
    for value in values:
        assert "body" not in value and "claim_token" not in value
        if value["sender_id"] == "lead" or value["recipient_id"] == "lead":
            assert len(value["body_preview"]) == 240
        else:
            assert "body_preview" not in value
    async with ctf.repo.engine.connect() as conn:
        persisted = (
            await conn.execute(
                select(
                    s.ctf_messages.c.id, s.ctf_messages.c.status, s.ctf_messages.c.deferred
                ).where(
                    s.ctf_messages.c.task_id == tid,
                    s.ctf_messages.c.id.in_([m["id"] for m in rows]),
                )
            )
        ).all()
    assert len(persisted) == 105
    assert all(m.status == "queued" and m.deferred for m in persisted)
    for actor in (owner["actor"], helper["actor"], "user", "service"):
        with pytest.raises(HTTPException, match="Only Lead"):
            await ctf.list_deferred_messages(tid, actor)
