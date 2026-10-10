"""Real HTTP/DB access boundaries; all tasks and objects are disposable fixtures."""

import asyncio
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_blackboard.settings import Settings
from bbx_blackboard.sse import SSEDispatcher, stream_events
from bbx_blackboard.store import schema as s
from fastapi.responses import StreamingResponse
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from test_ctf_vertical_integration import ROOT, MemoryObjects
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

pytestmark = pytest.mark.integration


class AccessObjects(MemoryObjects):
    async def stream(self, uri):
        yield self.files[uri]


class LocalDispatcher:
    @asynccontextmanager
    async def subscribe(self, tid):
        yield asyncio.Queue()


async def test_ctf_http_access_matrix_and_real_sse_backfill(ctf_vertical_database, monkeypatch):
    settings = Settings(
        postgres_password=SecretStr("isolated-test"),
        minio_root_password=SecretStr("unused-test"),
        service_token=SecretStr("ctf-access-service"),
        agent_token_secret=SecretStr("ctf-access-isolated-signing-key-32"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    engine = create_async_engine(ctf_vertical_database)
    objects = AccessObjects()
    app = create_app(settings, engine=engine, objects=objects)
    await app.state.profile_store.ensure_bundled(settings.profiles_dir, app.state.platform_store)
    ctf = app.state.ctf_service

    # Bound the actual SSE generator to its initial DB backlog, not an unbounded socket.
    def finite_stream(service, dispatcher, tid, since=0, for_agent=None):
        async def content():
            rows = await service.events(tid, since, for_agent)
            if not rows:
                return
            source = stream_events(
                service, cast(SSEDispatcher, LocalDispatcher()), tid, since, for_agent
            )
            try:
                for _ in rows:
                    item = await anext(source)
                    yield f"id: {item['id']}\nevent: {item['event']}\ndata: {item['data']}\n\n"
            finally:
                await source.aclose()

        return StreamingResponse(content(), media_type="text/event-stream")

    monkeypatch.setattr("bbx_blackboard.sse.stream_response", finite_stream)
    try:
        tids = []
        for _ in range(2):
            tid = await ctf.create_task(
                {
                    "mode": "ctf",
                    "goal": "Isolated access fixture",
                    "budget": {"max_cost": "10", "max_minutes": 60},
                }
            )
            await ctf.provision(tid)
            await ctf.start(tid)
            tids.append(tid)
        tid, other = tids
        members = [
            await ctf.create_member(tid, "user", name, str(uuid4())) for name in ("Alice", "Bob")
        ]
        for member in members:
            await ctf.post_message(
                tid, "user", member["id"], "Fixture assignment", str(uuid4()), "instruction"
            )
        turns = {
            aid: await ctf.claim_turn(tid, aid, "access-runtime")
            for aid in ("lead", "member-1", "member-2")
        }
        for aid, turn in turns.items():
            await ctf.checkpoint(
                tid,
                aid,
                turn["id"],
                turn["generation"],
                {
                    "state": {
                        "in_memory": {
                            "messages": [
                                {
                                    "role": "assistant",
                                    "message_id": str(uuid4()),
                                    "contents": [
                                        {"type": "text", "text": f"PRIVATE_SESSION_{aid}"}
                                    ],
                                }
                            ]
                        }
                    }
                },
                "Fixture instructions",
                0,
                [],
            )
        private = await ctf.post_message(
            tid,
            "member-1",
            "member-2",
            "PRIVATE_TEAMMATE_MESSAGE",
            str(uuid4()),
            generation=turns["member-1"]["generation"],
            turn_id=turns["member-1"]["id"],
        )
        own = await ctf.record_observation(
            tid,
            "lead",
            turns["lead"]["id"],
            turns["lead"]["generation"],
            "tool_result",
            {"tool": "fixture", "result": "OWN_TOOL_RESULT"},
            "own",
        )
        hidden = await ctf.record_observation(
            tid,
            "member-1",
            turns["member-1"]["id"],
            turns["member-1"]["generation"],
            "tool_result",
            {"tool": "fixture", "result": "PRIVATE_TOOL_RESULT"},
            "hidden",
        )
        user = {"Authorization": f"Bearer {issue_user_token(settings, 'admin')}"}
        service = {"Authorization": "Bearer ctf-access-service"}
        turn = turns["lead"]
        token = issue_agent_token(
            settings, tid, "lead", generation=turn["generation"], turn_id=UUID(turn["id"])
        )
        lead = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://board"
        ) as http:
            for path in ("state", "ctf/state"):
                response = await http.get(f"/api/tasks/{tid}/{path}", headers=lead)
                assert response.status_code == 200, response.text
                assert "ctf_control" not in response.text and "PRIVATE_" not in response.text
                assert "claim_token" not in response.text
            for suffix in ("events", "stream", "stream?since=1"):
                response = await http.get(f"/api/tasks/{tid}/{suffix}", headers=lead)
                assert response.status_code == 200, response.text
                assert "PRIVATE_TEAMMATE_MESSAGE" not in response.text
                assert hidden["uri"] not in response.text
                assert "claim_token" not in response.text
                response = await http.get(f"/api/tasks/{tid}/{suffix}", headers=user)
                assert response.status_code == 200 and "PRIVATE_TEAMMATE_MESSAGE" in response.text
            for path in ("ctf/state", "events", "stream", "agents/lead/session"):
                assert (
                    await http.get(f"/api/tasks/{other}/{path}", headers=lead)
                ).status_code == 403
            for aid in ("lead", "member-1"):
                response = await http.get(f"/api/tasks/{tid}/agents/{aid}/session", headers=user)
                assert response.status_code == 200 and f"PRIVATE_SESSION_{aid}" in response.text
                response = await http.get(f"/api/tasks/{tid}/agents/{aid}/session", headers=lead)
                assert response.status_code == (200 if aid == "lead" else 403)
            assert (
                await http.get(f"/api/tasks/{tid}/agents/member-1/messages", headers=lead)
            ).status_code == 403
            visible = await http.get(f"/api/tasks/{tid}/agents/member-1/messages", headers=user)
            assert visible.status_code == 200 and private["id"] in visible.text
            for uri, allowed in ((own["uri"], True), (hidden["uri"], False)):
                response = await http.get("/api/evidence", params={"uri": uri}, headers=lead)
                assert response.status_code == (200 if allowed else 403), response.text
                assert (
                    await http.get("/api/evidence", params={"uri": uri}, headers=user)
                ).status_code == 200
            for suffix in (
                "archive-data",
                "workspace",
                "workspace/tree",
                "workspace/file?path=ctf.json",
            ):
                response = await http.get(f"/api/tasks/{tid}/{suffix}", headers=lead)
                assert response.status_code in {403, 409}, response.text
            assert (
                await http.get(f"/api/tasks/{tid}/archive-data", headers=user)
            ).status_code == 403
            archive = await http.get(f"/api/tasks/{tid}/archive-data", headers=service)
            assert archive.status_code == 409, archive.text
            for endpoint in ("resume", "purge", "ctf/runtime/recover"):
                assert (
                    await http.post(f"/api/tasks/{tid}/{endpoint}", json={}, headers=lead)
                ).status_code in {403, 409, 422}

            # Replay must preserve actual session and runtime authority, not reconstruct them.
            async with engine.connect() as conn:
                before = {
                    table.name: [
                        dict(row)
                        for row in (
                            await conn.execute(select(table).where(table.c.task_id == tid))
                        ).mappings()
                    ]
                    for table in (s.ctf_messages, s.agent_sessions, s.ctf_turns)
                }
            control = (await ctf.state(tid))["task"]["ctf_control"]
            await ctf.repo.replay(tid)
            async with engine.connect() as conn:
                after = {
                    table.name: [
                        dict(row)
                        for row in (
                            await conn.execute(select(table).where(table.c.task_id == tid))
                        ).mappings()
                    ]
                    for table in (s.ctf_messages, s.agent_sessions, s.ctf_turns)
                }
            assert before == after
            assert (await ctf.state(tid))["task"]["ctf_control"] == control
            await ctf.recover(tid)
            for suffix in ("state", "events", "stream", "agents/lead/session"):
                assert (await http.get(f"/api/tasks/{tid}/{suffix}", headers=lead)).status_code in {
                    403,
                    409,
                }
            await ctf.request_finish(
                tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Fixture done"}
            )
            await ctf.finalize_close(tid, True)
            archive = await http.get(f"/api/tasks/{tid}/archive-data", headers=service)
            assert archive.status_code == 200, archive.text
            assert (
                "PRIVATE_SESSION_member-1" in archive.text
                and "PRIVATE_TEAMMATE_MESSAGE" in archive.text
            )
    finally:
        await app.state.workspace_cache.close()
        await engine.dispose()
