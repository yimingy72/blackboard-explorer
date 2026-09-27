"""A scripted MAF agent writing through the real blackboard API and database."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from agent_framework import AgentSession
from bbx_blackboard.api import create_app
from bbx_blackboard.settings import Settings as BoardSettings
from bbx_objects import ObjectStore
from bbx_runtime.chatworker import ChatWorker
from bbx_runtime.clients import BlackboardClient, EnvdClient
from bbx_runtime.execenv import ExecEnvHandle, ExecEnvManager
from bbx_runtime.runner import AgentRunner
from bbx_runtime.scheduler.supervisor import TaskSupervisor
from bbx_runtime.settings import Settings
from bbx_runtime.testing.fake_envd import CommandOutput, FakeEnvd
from bbx_runtime.testing.scripted_client import (
    ScriptedChatClient,
    ScriptStep,
    ScriptToolCall,
    ScriptUsage,
)
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


class LoseCompleteResponse(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.dropped = False

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if request.url.path.endswith("/complete") and not self.dropped:
            self.dropped = True
            await response.aclose()
            raise httpx.ReadError("completion response lost", request=request)
        return response

    async def aclose(self):
        await self.inner.aclose()


async def test_scripted_seed_records_board_tools_usage_and_receipt(
    runtime_infrastructure, tmp_path, monkeypatch
):
    database_url, endpoint = runtime_infrastructure
    parsed = make_url(database_url)
    settings = BoardSettings(
        postgres_host=parsed.host or "127.0.0.1",
        postgres_port=parsed.port or 5432,
        postgres_user=parsed.username or "test",
        postgres_password=SecretStr(parsed.password or "test"),
        postgres_db=parsed.database or "test",
        minio_root_user="bbxm2buser",
        minio_root_password=SecretStr("bbxm2b-test-password"),
        minio_endpoint=f"http://{endpoint}",
        minio_bucket="bbxm2btest",
        service_token=SecretStr("m2b-service"),
        agent_token_secret=SecretStr("m2b-agent-signing-integration-secret"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    objects = ObjectStore(endpoint, "bbxm2buser", "bbxm2b-test-password", settings.minio_bucket)
    await objects.ensure_bucket()
    engine = create_async_engine(database_url)
    app = create_app(settings, engine=engine, objects=objects)
    runtime_settings = Settings.model_construct(deepseek_api_key=SecretStr("test-model"))
    fake = FakeEnvd(
        tmp_path,
        command_outputs={
            "inspect": CommandOutput(
                stdout="observed",
                files={"/workspace/agents/agent-1/evidence.txt": b"repeatable observation"},
            )
        },
    )
    try:
        async with app.router.lifespan_context(app), fake:
            async with (
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as board_http,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=fake.app), base_url="http://fake"
                ) as envd_http,
            ):
                board = BlackboardClient("http://board", "m2b-service", board_http)
                tid = (
                    await board.create_task(
                        {
                            "goal": "Describe a repeatable observation",
                            "acceptance": [{"id": "A1", "desc": "Evidence exists"}],
                            "budget": {"max_cost": "1", "max_minutes": 5},
                            "agent_profile": "default",
                        }
                    )
                )["id"]
                await board.start_task(tid)
                await board.transition(tid, "running")
                registration = await board.register_agent(tid, "explore", is_seed=True)
                aid = registration["agent_id"]
                await EnvdClient("http://fake", fake.token, envd_http).create_user(aid)
                usage = ScriptUsage(10, 20, 5, 2)
                model = ScriptedChatClient(
                    [
                        ScriptStep(
                            calls=(ScriptToolCall("execute_command", {"command": "inspect"}),),
                            usage=usage,
                        ),
                        ScriptStep(
                            calls=(
                                ScriptToolCall(
                                    "post_fact",
                                    {
                                        "kind": "observation",
                                        "statement": "Inspection returned a repeatable observation",
                                        "evidence": [
                                            {
                                                "type": "text",
                                                "path": f"/workspace/agents/{aid}/evidence.txt",
                                                "summary": "Inspection result",
                                            }
                                        ],
                                    },
                                ),
                            ),
                            usage=usage,
                        ),
                        ScriptStep(
                            calls=(
                                ScriptToolCall(
                                    "post_intent",
                                    {
                                        "statement": "Check the observation under concurrent load",
                                        "based_on": ["F1"],
                                        "expected": "The result remains stable",
                                        "method": "Repeat with parallel callers",
                                        "relates_to": ["A1"],
                                        "claim": True,
                                    },
                                ),
                            ),
                            usage=usage,
                        ),
                        ScriptStep(
                            calls=(
                                ScriptToolCall(
                                    "release",
                                    {"intent_id": "I1", "note": "Prepared baseline evidence"},
                                ),
                            ),
                            usage=usage,
                        ),
                        ScriptStep(
                            text='{"accepted":true,"data":{"intent_result":"none","posted":["F1","I1"],"note":"Ready"}}',
                            usage=usage,
                        ),
                    ]
                )
                result = await AgentRunner(runtime_settings, board, objects, None).run_agent(  # type: ignore[arg-type]
                    tid,
                    aid,
                    "explore",
                    agent_token=registration["token"],
                    client=model,
                    handle=ExecEnvHandle(
                        UUID(tid), "fake-container", "fake", "http://fake", fake.token
                    ),
                    envd_http_client=envd_http,
                )
                assert result.end_reason == "normal", result.receipt
                state = await board.state(tid)
                assert set(state["facts"]) == {"F1"}
                assert state["intents"]["I1"]["holder"] is None
                assert state["intents"]["I1"]["notes"][-1]["text"] == "Prepared baseline evidence"
                run = state["agents"][aid]
                assert run["status"] == "finished" and run["receipt"] == result.receipt
                assert run["steps"] == 5
                assert run["context_tokens"] == 30
                assert run["usage"]["cache_hit_tokens"] == 50
                assert run["usage"]["cache_miss_tokens"] == 100
                assert run["usage"]["output_tokens"] == 25
                records = [e for e in await board.events(tid) if e["type"] == "tool_call.recorded"]
                assert len(records) == 4
                for record in records:
                    assert await objects.exists(record["payload"]["result_uri"])
                traces = [e for e in await board.events(tid) if e["type"] == "agent.trace.recorded"]
                assert traces[0]["payload"]["kind"] == "initial_context"
                outputs = [e for e in traces if e["payload"]["kind"] == "model_output"]
                assert [e["payload"]["step"] for e in outputs] == [1, 2, 3, 4, 5]
                for trace in traces:
                    uri = trace["payload"]["uri"]
                    assert await objects.exists(uri)
                    response = await board_http.get(
                        "http://board/api/evidence",
                        params={"uri": uri},
                        headers={"Authorization": "Bearer m2b-service"},
                    )
                    assert response.status_code == 200
                    assert isinstance(response.json()["text"], str)
                assert len(fake.commands) == 1
                saved = await board.get_agent_session(tid, aid)
                assert saved["origin"] == "native"
                history = AgentSession.from_dict(saved["session"]).state["in_memory"]["messages"]
                assert any(
                    content.type == "function_result" and "observed" in str(content.result)
                    for message in history
                    for content in message.contents
                )

                login = await board_http.post(
                    "http://board/api/login",
                    json={"username": "admin", "password": "test"},
                )
                assert login.status_code == 200
                mid = str(uuid4())
                posted = await board_http.post(
                    f"http://board/api/tasks/{tid}/agents/{aid}/messages",
                    json={"id": mid, "content": "What did you find?"},
                )
                assert posted.status_code == 200
                review = ScriptedChatClient(
                    [ScriptStep(text="I found repeatable evidence.", expect_contains="observed")]
                )
                monkeypatch.setattr(
                    "bbx_runtime.chatworker.make_client", lambda *_args, **_kwargs: review
                )
                await ChatWorker(runtime_settings, board).process(tid, aid, mid)
                conversation = await board.agent_messages(tid, aid)
                assert [item["role"] for item in conversation["messages"]] == ["user", "assistant"]
                assert conversation["messages"][0]["status"] == "completed"
                assert conversation["messages"][1]["content"] == "I found repeatable evidence."
                assert (await board.state(tid))["facts"] == state["facts"]

                second_id = str(uuid4())
                second_post = await board_http.post(
                    f"http://board/api/tasks/{tid}/agents/{aid}/messages",
                    json={"id": second_id, "content": "What did you find?"},
                )
                assert second_post.status_code == 200
                second_review = ScriptedChatClient(
                    [
                        ScriptStep(
                            text="The evidence was saved under F1.",
                            expect_contains="I found repeatable evidence.",
                        )
                    ]
                )
                monkeypatch.setattr(
                    "bbx_runtime.chatworker.make_client", lambda *_args, **_kwargs: second_review
                )
                uncertain_transport = LoseCompleteResponse(app)
                async with httpx.AsyncClient(transport=uncertain_transport) as uncertain_http:
                    uncertain_board = BlackboardClient(
                        "http://board", "m2b-service", uncertain_http
                    )
                    await ChatWorker(runtime_settings, uncertain_board).process(tid, aid, second_id)
                assert uncertain_transport.dropped
                conversation = await board.agent_messages(tid, aid)
                assert [item["role"] for item in conversation["messages"]] == [
                    "user",
                    "assistant",
                    "user",
                    "assistant",
                ]
                saved = await board.get_agent_session(tid, aid)
                history = AgentSession.from_dict(saved["session"]).state["in_memory"]["messages"]
                assert sum(message.text == "What did you find?" for message in history) == 2
                assert (await board.state(tid))["facts"] == state["facts"]

                stopped = await board_http.post(f"http://board/api/tasks/{tid}/stop")
                assert stopped.status_code == 200
                await board.transition(tid, "stopped")
                deletion = await board_http.delete(f"http://board/api/tasks/{tid}")
                assert deletion.status_code == 202
                manager = SimpleNamespace(destroy=AsyncMock())
                supervisor = TaskSupervisor(
                    runtime_settings,
                    board,
                    cast(ExecEnvManager, manager),
                    cast(AgentRunner, None),
                )
                await supervisor._purge_deleted(tid)
                manager.destroy.assert_awaited_once_with(tid)
                gone = await board_http.get(f"http://board/api/tasks/{tid}")
                assert gone.status_code == 404
    finally:
        await engine.dispose()
