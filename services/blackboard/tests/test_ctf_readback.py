"""Readback privacy, pagination and SQL projection using synthetic SQLite data."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import JSONPATH, dialect
from sqlalchemy.ext.compiler import compiles
from test_ctf_communication_storage import message
from test_ctf_communication_storage import storage as storage_fixture

base_storage = storage_fixture


@compiles(JSONPATH, "sqlite")
def _sqlite_jsonpath(_type, _compiler, **_kwargs):
    return "TEXT"


@pytest.fixture
def storage(base_storage):
    """Emulate JSONB projection only; PostgreSQL integration verifies its actual path."""

    def matching_contents(raw, _path, raw_variables):
        session = json.loads(raw)
        call_id = json.loads(raw_variables)["call_id"]
        return json.dumps(
            [
                content
                for message in session.get("state", {}).get("in_memory", {}).get("messages", [])
                if isinstance(message, dict) and isinstance(message.get("contents"), list)
                for content in message["contents"]
                if isinstance(content, dict)
                and content.get("type") == "function_result"
                and content.get("call_id") == call_id
            ]
        )

    with base_storage.database.connect() as conn:
        raw = conn.connection.driver_connection
        raw.create_function("jsonb_build_object", 2, lambda key, value: json.dumps({key: value}))
        raw.create_function("jsonb_path_query_array", 3, matching_contents)
    return base_storage


def member(storage, aid="member-1", **changes):
    with storage.database.begin() as conn:
        conn.execute(
            insert(s.ctf_members).values(
                {
                    "task_id": storage.tid,
                    "id": aid,
                    "generation": 1,
                    "lifecycle": "active",
                    **changes,
                }
            )
        )


def saved(storage, actor, contents, *, backup=None):
    session = {
        "state": {
            "in_memory": {"messages": [{"role": "tool", "contents": contents}]},
            "bbx_tool_results": backup or {},
        }
    }
    with storage.database.begin() as conn:
        conn.execute(
            insert(s.agent_sessions).values(
                task_id=storage.tid,
                agent_id=actor,
                session=session,
                opening_instructions="Private opening must not escape",
                origin="native",
                revision=1,
            )
        )
    return session


def result(call_id, value):
    return {"type": "function_result", "call_id": call_id, "result": value}


async def test_saved_tool_pages_only_own_result_and_reads_backup_without_reexecution(storage):
    member(storage)
    original = saved(
        storage,
        "lead",
        [result("selected", "头" * 17001), result("other", "private")],
        backup={"backup": result("backup", {"items": [1, "二"]})},
    )
    saved(storage, "member-1", [result("foreign", "Teammate private result")])
    first = await storage.service.read_saved_tool_result(
        storage.tid, "lead", "selected", limit=16000
    )
    assert first == {
        "call_id": "selected",
        "result": "头" * 16000,
        "offset": 0,
        "next_offset": 16000,
        "total_characters": 17001,
    }
    second = await storage.service.read_saved_tool_result(
        storage.tid, "lead", "selected", offset=16000
    )
    assert first["result"] + second["result"] == "头" * 17001
    assert second["next_offset"] is None
    beyond = await storage.service.read_saved_tool_result(
        storage.tid, "lead", "selected", offset=99999
    )
    assert beyond["result"] == "" and beyond["next_offset"] is None
    backup = await storage.service.read_saved_tool_result(storage.tid, "lead", "backup")
    assert json.loads(backup["result"]) == {"items": [1, "二"]}
    assert set(backup) == {"call_id", "result", "offset", "next_offset", "total_characters"}
    with pytest.raises(HTTPException, match="not found"):
        await storage.service.read_saved_tool_result(storage.tid, "lead", "foreign")
    assert (await storage.service.sessions.get_session(storage.tid, "lead"))["session"] == original


@pytest.mark.parametrize("conflicting", [False, True])
async def test_tool_call_reuse_requires_identical_result_in_history_and_backup(
    storage, conflicting
):
    saved(
        storage,
        "lead",
        [result("same", {"a": 1, "b": [2]})],
        backup={"same": result("same", {"b": [3 if conflicting else 2], "a": 1})},
    )
    if conflicting:
        with pytest.raises(HTTPException, match="conflicting") as error:
            await storage.service.read_saved_tool_result(storage.tid, "lead", "same")
        assert error.value.status_code == 409
    else:
        value = await storage.service.read_saved_tool_result(storage.tid, "lead", "same")
        assert json.loads(value["result"]) == {"a": 1, "b": [2]}


async def test_turn_result_keeps_existing_owner_lead_assignment_and_settled_permissions(storage):
    member(storage)
    turn_id = str(uuid4())
    with storage.database.begin() as conn:
        conn.execute(
            insert(s.ctf_turns).values(
                task_id=storage.tid,
                id=turn_id,
                agent_id="member-1",
                generation=1,
                purpose="execution",
                status="finished",
                end_reason="completed",
                final_answer="Private output " * 2000,
                assignment_message_ids=[],
                explicit_reply_ids=[],
            )
        )
    with pytest.raises(HTTPException, match="Private turn"):
        await storage.service.read_turn_result(storage.tid, "lead", turn_id)
    owner = await storage.service.read_turn_result(storage.tid, "member-1", turn_id, limit=37)
    assert len(owner["final_answer"]) == 37 and owner["next_offset"] == 37
    assert owner["agent_id"] == "member-1" and owner["total_characters"] == 30000
    with storage.database.begin() as conn:
        conn.execute(
            update(s.ctf_turns)
            .where(s.ctf_turns.c.id == turn_id)
            .values(assignment_message_ids=[str(uuid4())])
        )
    lead = await storage.service.read_turn_result(
        storage.tid, "lead", turn_id, offset=37, limit=16000
    )
    assert len(lead["final_answer"]) == 16000 and lead["next_offset"] == 16037
    user = await storage.service.read_turn_result(storage.tid, "user", turn_id, offset=30000)
    assert user["final_answer"] == "" and user["next_offset"] is None
    with pytest.raises(HTTPException, match="not settled"):
        await storage.service.read_turn_result(storage.tid, "lead", storage.current_id)


async def test_removed_and_unknown_member_readers_are_rejected(storage):
    member(storage, lifecycle="removed")
    saved(storage, "member-1", [result("old", "Private old history")])
    with pytest.raises(HTTPException):
        await storage.service.read_saved_tool_result(storage.tid, "member-1", "old")
    with pytest.raises(HTTPException):
        await storage.service.read_saved_tool_result(storage.tid, "missing-member", "old")
    for actor in ("user", "service", "system"):
        with pytest.raises(HTTPException):
            await storage.service.read_saved_tool_result(storage.tid, actor, "old")


async def test_removed_member_keeps_only_existing_review_reader_authority(storage):
    review_id = str(uuid4())
    member(storage, lifecycle="removed", current_turn_id=review_id)
    saved(storage, "member-1", [result("old", "Saved private history")])
    with storage.database.begin() as conn:
        conn.execute(update(s.tasks).values(status="finished"))
        conn.execute(
            insert(s.ctf_turns).values(
                task_id=storage.tid,
                id=review_id,
                agent_id="member-1",
                generation=1,
                purpose="review",
                status="running",
                assignment_message_ids=[],
            )
        )
    value = await storage.service.read_saved_tool_result(storage.tid, "member-1", "old")
    assert value["result"] == "Saved private history"


async def test_deferred_metadata_pages_and_sql_preview_preserve_private_bodies_and_status(storage):
    rows = []
    for index in range(105):
        recipient = ["lead", "member-1", "member-2"][index % 3]
        sender = ["member-1", "lead", "user"][index % 3]
        rows.append(
            message(
                storage,
                recipient_id=recipient,
                recipient_sequence=index,
                sender_id=sender,
                sender_kind="user" if sender == "user" else "agent",
                status="queued",
                deferred=True,
                created_at=datetime.now(UTC),
                body=f"Private-{index}:" + "汉" * 400,
            )
        )
    rows.extend(
        message(storage, status=status, deferred=True, recipient_sequence=1000 + i)
        for i, status in enumerate(["cancelled", "failed", "delivered"])
    )
    rows.append(message(storage, status="queued", deferred=False, recipient_sequence=1004))
    rows.append(
        message(storage, status="queued", deferred=True, purpose="review", recipient_sequence=1005)
    )
    with storage.database.begin() as conn:
        for item in rows:
            conn.execute(insert(s.ctf_messages).values(**item))
    collected = []
    offset = 0
    while True:
        page = await storage.service.list_deferred_messages(
            storage.tid, "lead", offset=offset, limit=50
        )
        collected.extend(page["messages"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    expected = sorted(
        rows[:105], key=lambda item: (item["recipient_id"], item["recipient_sequence"], item["id"])
    )
    assert [item["id"] for item in collected] == [item["id"] for item in expected]
    for entry in collected:
        assert "body" not in entry and "claim_token" not in entry
        if entry["sender_id"] == "lead" or entry["recipient_id"] == "lead":
            assert len(entry["body_preview"]) == 240
        else:
            assert "body_preview" not in entry
    queries = [
        q
        for q in storage.queries
        if getattr(q, "is_select", False) and "body_preview" in q.selected_columns.keys()
    ]
    assert len(queries) == 3
    assert "body" not in queries[0].selected_columns.keys()
    sql = str(queries[0].compile(dialect=dialect()))
    assert "CASE WHEN" in sql and "substr(" in sql
    with storage.database.connect() as conn:
        actual = conn.execute(
            select(s.ctf_messages.c.id, s.ctf_messages.c.status, s.ctf_messages.c.deferred)
        ).all()
    assert set(actual) == {(item["id"], item["status"], item["deferred"]) for item in rows}
    for actor in ("member-1", "user", "service", "system"):
        with pytest.raises(HTTPException, match="Only Lead"):
            await storage.service.list_deferred_messages(storage.tid, actor)


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, 16001), (True, 1), (0, 1.5)])
async def test_result_page_bounds_are_enforced_inside_readers(storage, offset, limit):
    with pytest.raises(HTTPException) as error:
        await storage.service.read_saved_tool_result(storage.tid, "lead", "tool", offset, limit)
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        await storage.service.read_turn_result(storage.tid, "lead", storage.old_id, offset, limit)
    assert error.value.status_code == 422


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, 101), (True, 1), (0, 1.5)])
async def test_deferred_page_bounds_are_enforced_inside_reader(storage, offset, limit):
    with pytest.raises(HTTPException) as error:
        await storage.service.list_deferred_messages(storage.tid, "lead", offset, limit)
    assert error.value.status_code == 422
