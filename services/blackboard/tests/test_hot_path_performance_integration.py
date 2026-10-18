"""Measure database work on synthetic hot histories, without production data or models."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from bbx_blackboard.service import BoardService, ctf_agent_event
from bbx_blackboard.store import schema as s
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_ctf_integration import team
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url
from test_store_integration import fact, task

pytestmark = pytest.mark.integration


class Queries:
    def __init__(self, monkeypatch):
        self.count = 0
        self.bytes = 0
        self.tool_rows = 0
        self.sql = []
        execute = AsyncConnection.execute

        async def capture(conn, statement, *args, **kwargs):
            self.count += 1
            self.sql.append(str(statement))
            result = await execute(conn, statement, *args, **kwargs)
            if getattr(statement, "is_select", False):
                frozen = result.freeze()
                rows = [dict(row) for row in frozen().mappings()]
                self.bytes += len(json.dumps(rows, default=str).encode())
                if "FROM tool_calls" in str(statement):
                    self.tool_rows += len(rows)
                return frozen()
            return result

        monkeypatch.setattr(AsyncConnection, "execute", capture)

    def report(self, name):
        print(
            f"{name}: sql={self.count}, selected_json_bytes={self.bytes}, "
            f"tool_rows={self.tool_rows}"
        )


@pytest.mark.parametrize("operation", ["heartbeat", "tool", "trace", "fact", "intent"])
async def test_ordinary_hot_writes_do_not_fetch_unrelated_tool_history(
    board_service, monkeypatch, operation
):
    service = board_service
    tid = await task(service)
    aid = await service.register_agent(tid, "explore")
    started = (await service.state(tid))["agents"][aid]["started_at"]
    if operation == "intent":
        await service.post_fact(tid, aid, fact())
    async with service.repo.engine.begin() as conn:
        await conn.execute(
            insert(s.tool_calls),
            [
                {
                    "task_id": tid,
                    "id": f"history-{i}",
                    "agent_id": aid,
                    "tool": "inspect",
                    "args": {"command": "x" * 6000},
                    "result_head": "y" * 4096,
                    "created_at": datetime.now(UTC),
                }
                for i in range(1000)
            ],
        )
    queries = Queries(monkeypatch)
    if operation == "heartbeat":
        await service.heartbeat(
            tid,
            aid,
            steps=1,
            context_tokens=10,
            usage={},
            last_seen_version=0,
            expected_started_at=started,
        )
    elif operation == "tool":
        await service.record_tool_call(tid, aid, {"id": "new", "tool": "inspect", "args": {}})
    elif operation == "trace":
        await service.record_agent_trace(
            tid,
            aid,
            {
                "kind": "model_output",
                "step": 1,
                "summary": "Synthetic trace",
                "uri": f"traces/{tid}/{aid}/one.json",
            },
        )
    elif operation == "fact":
        posted = await service.post_fact(
            tid,
            aid,
            fact(
                evidence=[
                    {
                        "type": "text",
                        "uri": "evidence/one",
                        "summary": "Known call",
                        "call_id": "history-0",
                    },
                ]
            ),
        )
        assert posted["events"][0]["payload"]["provenance"] == "tool_backed"
    else:
        await service.post_intent(
            tid,
            aid,
            {
                "statement": "Inspect data",
                "based_on": ["F1"],
                "expected": "Evidence",
                "method": "Read",
                "relates_to": ["A1"],
            },
        )
    queries.report(operation)
    assert queries.tool_rows <= (1 if operation == "fact" else 0)
    assert queries.bytes < 20000
    if operation in {"heartbeat", "tool", "trace"}:
        assert not any(
            f"FROM {name}" in sql
            for sql in queries.sql
            for name in ("facts", "intents", "task_counters")
        )


@pytest.mark.parametrize("count", [1, 20, 80, 1200])
async def test_ctf_claim_updates_mailbox_in_bounded_batches(ctf, monkeypatch, count):
    tid = await team(ctf)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            insert(s.ctf_messages),
            [
                {
                    "task_id": tid,
                    "id": str(uuid4()),
                    "sender_kind": "user",
                    "sender_id": "user",
                    "recipient_id": "lead",
                    "recipient_sequence": 100 + i,
                    "kind": "instruction",
                    "body": "Synthetic input",
                    "purpose": "execution",
                    "status": "queued",
                    "deferred": False,
                    "created_at": datetime.now(UTC),
                }
                for i in range(count)
            ],
        )
    turn = await ctf.claim_turn(tid, "lead", "synthetic-runtime")
    queries = Queries(monkeypatch)
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    queries.report(f"claim-{count}")
    assert len(messages) >= count
    assert len({message["claim_token"] for message in messages}) == len(messages)
    batches = (len(messages) + 999) // 1000
    assert sum(sql.startswith("UPDATE ctf_messages") for sql in queries.sql) == batches
    assert queries.count == 5 + batches
    repeated = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    assert [(m["id"], m["claim_token"]) for m in repeated] == [
        (m["id"], m["claim_token"]) for m in messages
    ]


async def test_ctf_agent_event_read_projects_private_bodies_before_transfer(ctf, monkeypatch):
    tid = await team(ctf)
    reader = "lead"
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            insert(s.events),
            [
                {
                    "task_id": tid,
                    "type": "ctf.message.posted",
                    "actor": "someone",
                    "object_id": str(i),
                    "payload": {
                        "sender_id": "someone",
                        "recipient_id": "another",
                        "body": "x" * 6000,
                    },
                    "addressed_to": ["someone", "another"],
                }
                for i in range(1000)
            ],
        )
        samples = [
            (
                "ctf.message.posted",
                "lead",
                {
                    "sender_id": "lead",
                    "recipient_id": "another",
                    "body": "own",
                    "claim_token": "private",
                },
            ),
            (
                "ctf.message.posted",
                "another",
                {"sender_id": "another", "recipient_id": "lead", "body": "received"},
            ),
            ("ctf.message.posted", "another", {"body": "legacy missing ownership"}),
            ("ctf.turn.started", "system", {"agent_id": "lead", "runtime_instance": "private"}),
            ("ctf.turn.started", "system", {"agent_id": "another", "runtime_instance": "private"}),
            (
                "ctf.challenge.created",
                "another",
                {"result": {"id": "challenge"}, "private": "discard"},
            ),
            (
                "ctf.member.created",
                "another",
                {"id": "another", "role": "teammate", "private": "discard"},
            ),
            ("task.created", "user", {"goal": "Task input", "ctf_control": {"private": True}}),
        ]
        for kind in (
            "ctf.platform.dispatched",
            "ctf.message.delivered",
            "ctf.turn.finished",
            "agent.trace.recorded",
            "tool_call.recorded",
        ):
            for actor in ("lead", "another"):
                samples.append((kind, actor, {"private_result": "x" * 6000}))
        await conn.execute(
            insert(s.events),
            [
                {
                    "task_id": tid,
                    "type": kind,
                    "actor": actor,
                    "object_id": str(uuid4()),
                    "payload": payload,
                    "addressed_to": None,
                }
                for kind, actor, payload in samples
            ],
        )
        original = [
            dict(row)
            for row in (
                await conn.execute(
                    select(s.events).where(s.events.c.task_id == tid).order_by(s.events.c.version)
                )
            ).mappings()
        ]
    queries = Queries(monkeypatch)
    actual = await BoardService(ctf.repo.engine, objects=ctf.objects).events(tid, for_agent=reader)
    queries.report("ctf-private-events")
    assert actual == [ctf_agent_event(item, reader) for item in original]
    assert queries.bytes < 500000


async def test_search_reads_only_searchable_fields_and_preserves_unicode_casefold(
    board_service, monkeypatch
):
    import httpx
    from bbx_blackboard.api import create_app
    from bbx_contracts.models import PostFactRequest
    from test_ctf_api import settings
    from test_store_integration import intent

    service = board_service
    tid = await task(service)
    aid = await service.register_agent(tid, "explore")
    async with service.repo.engine.begin() as conn:
        await conn.execute(
            insert(s.facts),
            [
                {
                    "task_id": tid,
                    "id": f"F{i + 1}",
                    "author": aid,
                    "provenance": "self_reported",
                    "version": i + 100,
                    **PostFactRequest.model_validate(
                        fact(
                            statement=f"Straße {i}",
                            evidence=[
                                {"type": "text", "uri": "evidence/one", "summary": "x" * 400}
                                for _ in range(4)
                            ],
                        )
                    ).model_dump(mode="json"),
                }
                for i in range(500)
            ],
        )
    await service.post_intent(tid, aid, intent(statement="Read data", method="Inspect STRASSE"))
    with monkeypatch.context() as baseline:
        original = Queries(baseline)
        await service.state(tid)
        original.report("search-old-full-state")
    queries = Queries(monkeypatch)
    app = create_app(settings(), engine=service.repo.engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer service-test"},
    ) as client:
        response = await client.get(f"/api/tasks/{tid}/search", params={"q": "STRASSE", "k": 3})
        assert response.status_code == 200, response.text
        assert [hit["id"] for hit in response.json()] == ["F500", "F499", "F498"]
        queries.report("search-narrow-http")
        # HTTP also includes middleware/existence reads omitted by the old-state measure.
        assert queries.bytes < original.bytes // 5
        assert not any(
            "facts.evidence" in sql or "FROM agent_runs" in sql or "FROM events" in sql
            for sql in queries.sql
        )
        only_intents = await client.get(
            f"/api/tasks/{tid}/search", params={"q": "straße", "type": "intent"}
        )
        assert [hit["id"] for hit in only_intents.json()] == ["I1"]


async def test_state_cursor_and_latest_closing_reason_use_index_endpoints(
    board_service, monkeypatch
):
    from sqlalchemy import text

    service = board_service
    tid = await task(service)
    async with service.repo.engine.begin() as conn:
        await conn.execute(
            insert(s.events),
            [
                {
                    "task_id": tid,
                    "type": "task.closing",
                    "actor": "scheduler",
                    "payload": {"reason": "old"},
                },
                {
                    "task_id": tid,
                    "type": "task.stopped",
                    "actor": "user",
                    "payload": {"reason": "latest"},
                },
            ],
        )
        await conn.execute(
            insert(s.events),
            [
                {
                    "task_id": tid,
                    "type": "agent.trace.recorded",
                    "actor": "agent-1",
                    "payload": {"trace": "x" * 256},
                }
                for _ in range(10000)
            ],
        )
        await conn.execute(text("ANALYZE events"))
    execute = AsyncConnection.execute
    cursor_queries = []

    async def capture(conn, statement, *args, **kwargs):
        if getattr(statement, "is_select", False) and list(statement.selected_columns.keys())[
            :1
        ] == ["version"]:
            cursor_queries.append(statement)
        return await execute(conn, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", capture)
    board = await service.state(tid)
    assert len(cursor_queries) == 1
    async with service.repo.engine.connect() as conn:
        query = str(
            cursor_queries[0].compile(dialect=conn.dialect, compile_kwargs={"literal_binds": True})
        )
        explanation = (
            await conn.execute(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query))
        ).scalar_one()
    nodes = [explanation[0]["Plan"]]
    examined = 0
    while nodes:
        node = nodes.pop()
        if node.get("Relation Name") == "events":
            examined += (
                node["Actual Rows"]
                + node.get("Rows Removed by Filter", 0)
                + node.get("Rows Removed by Index Recheck", 0)
            ) * node["Actual Loops"]
        nodes.extend(node.get("Plans", []))
    print(
        f"state-cursor: examined_event_rows={examined}, "
        f"closing_reason={board['task'].get('closing_reason')}, "
        f"execution_ms={explanation[0]['Execution Time']}"
    )
    assert board["task"]["closing_reason"] == "latest"
    assert "ix_events_task_closing_version" in json.dumps(explanation)
    assert "events.type IN ('task.closing', 'task.stopped')" in str(cursor_queries[0])
    assert examined <= 4


@pytest.mark.parametrize("count", [80, 1200])
async def test_ctf_checkpoint_batches_delivery_validation_and_wakeup(ctf, monkeypatch, count):
    tid = await team(ctf)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            insert(s.ctf_messages),
            [
                {
                    "task_id": tid,
                    "id": str(uuid4()),
                    "sender_kind": "user",
                    "sender_id": "user",
                    "recipient_id": "lead",
                    "recipient_sequence": 100 + i,
                    "kind": "instruction",
                    "body": "Synthetic checkpoint input",
                    "purpose": "execution",
                    "status": "queued",
                    "deferred": False,
                    "created_at": datetime.now(UTC),
                }
                for i in range(count)
            ],
        )
    turn = await ctf.claim_turn(tid, "lead", "checkpoint-measure")
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    session = {
        "state": {
            "in_memory": {
                "messages": [
                    {"role": "user", "message_id": message["id"], "text": message["body"]}
                    for message in messages
                ]
            }
        }
    }
    deliveries = [
        {"message_id": message["id"], "claim_token": message["claim_token"]} for message in messages
    ]
    queries = Queries(monkeypatch)
    result = await ctf.checkpoint(
        tid, "lead", turn["id"], turn["generation"], session, "opening", 0, deliveries
    )
    queries.report(f"checkpoint-{len(messages)}")
    assert result["delivered_message_ids"] == [message["id"] for message in messages]
    assert queries.count <= len(messages) + 12
    assert sum("pg_notify" in sql for sql in queries.sql) == 1
