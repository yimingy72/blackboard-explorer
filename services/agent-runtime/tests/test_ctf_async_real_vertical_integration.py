"""Scripted MAF -> real PG mailbox/session -> disposable envd, without a target or LLM."""

import asyncio
import json
import shlex
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.auth import issue_user_token
from bbx_runtime.clients import EnvdClient
from bbx_runtime.ctf.execution import CtfExecutionAdapter
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_cooperation_integration import cooperation_board as cooperation_board
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

from services.envd.tests.test_ctf_container_integration import ENVD_PYTHON, TOKEN, running_container

pytestmark = pytest.mark.integration


async def test_async_command_receives_coordination_in_same_turn_before_real_completion(
    cooperation_board,
):
    board, ctf, profile, configured, _, _ = cooperation_board
    transport = board._http._transport
    assert isinstance(transport, httpx.ASGITransport)
    app = cast(Any, transport.app)
    async_profile = profile.model_copy(deep=True)
    async_profile.worker_tools["lead"].builtin += [
        "start_command",
        "cancel_command",
        "read_command_result",
    ]
    published = await app.state.profile_store.create(
        f"ctf-async-vertical-{uuid4().hex[:8]}", async_profile, "test"
    )
    tid = (
        await board.create_task(
            {
                "mode": "ctf",
                "goal": "Run a harmless async command while receiving a coordination message",
                "budget": {"max_cost": "10", "max_minutes": 5},
                "agent_profile": published["name"],
                "profile_version": published["version"],
                "model_id": configured["model_id"],
                "model_version": configured["model_version"],
            }
        )
    )["id"]
    await board.start_task(tid)
    await ctf.runtime(tid, "start")
    turn = await ctf.runtime(tid, "claim_turn", agent_id="lead", runtime_instance="async-vertical")
    assert turn is not None
    coordination = "Coordination message: keep the original command and report its result"
    waiting, coordinated = asyncio.Event(), asyncio.Event()
    counter = "/workspace/shared/async-vertical-once.txt"
    release = "/workspace/shared/async-vertical-release"
    script = (
        "from pathlib import Path\nimport time\n"
        f"with Path({counter!r}).open('a') as stream: stream.write('once\\n')\n"
        "deadline = time.monotonic() + 30\n"
        f"while not Path({release!r}).exists():\n"
        "    if time.monotonic() >= deadline: raise SystemExit(7)\n"
        "    time.sleep(0.05)\n"
        "print('async-result')\n"
    )

    class CoordinatedClient(ScriptedChatClient):
        command_id: str | None = None

        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            if self._index == 1:
                pending = [
                    json.loads(content.result)
                    for message in messages
                    for content in message.contents
                    if content.type == "function_result" and isinstance(content.result, str)
                ]
                assert len(pending) == 1 and pending[0]["state"] == "running"
                self.command_id = pending[0]["command_id"]
                waiting.set()
            elif self._index == 2:
                coordinated.set()
            elif self._index == 3:
                assert self.command_id is not None
                self.steps[3] = ScriptStep(
                    calls=(ScriptToolCall("read_command_result", {"command_id": self.command_id}),),
                    expect_contains="command_result",
                )
            return super()._inner_get_response(
                messages=messages, stream=stream, options=options, **kwargs
            )

    client = CoordinatedClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "start_command",
                        {"command": f"python3 -c {shlex.quote(script)}", "timeout_sec": 45},
                    ),
                )
            ),
            ScriptStep(text="Waiting for command completion", expect_contains='"state": "running"'),
            ScriptStep(
                text="Coordination received while the command runs", expect_contains=coordination
            ),
            ScriptStep(text="Replaced by exact result read"),
            ScriptStep(text="Final after persisted async-result", expect_contains="async-result"),
        ]
    )
    factories = []

    def factory(member, _profile):
        factories.append(member["id"])
        return client

    with running_container(tid) as (container, url, _):
        envd = EnvdClient(url, TOKEN)
        adapter = CtfExecutionAdapter(ctf, envd)
        runner = CtfRunner(
            ctf,
            cast(Any, SimpleNamespace()),
            client_factory=factory,
            envd=adapter,
            objects=app.state.objects,
        )
        running = asyncio.create_task(
            runner.run(tid, {"id": "lead", "role": "lead", "display_name": "Lead"}, turn)
        )
        try:
            await asyncio.wait_for(waiting.wait(), 15)
            before = await ctf.get_agent_session(tid, "lead")
            session_id = before["session"]["session_id"]
            entry = next(iter(before["session"]["state"]["ctf_commands"].values()))
            command_id = entry["command_id"]
            assert entry["state"] == "running"
            message_id = str(uuid4())
            posted = await board._http.post(
                f"/api/tasks/{tid}/agents/lead/messages",
                json={"id": message_id, "content": coordination},
                headers={
                    "Authorization": f"Bearer {issue_user_token(app.state.settings, 'admin')}"
                },
            )
            assert posted.status_code == 200, posted.text
            await asyncio.wait_for(coordinated.wait(), 10)
            during = await ctf.state(tid)
            assert len(during["turns"]) == 1 and during["turns"][0]["id"] == turn["id"]
            assert during["turns"][0]["status"] == "running"
            status = await envd.ctf_status(**entry["identity"], command_id=command_id)
            assert status["state"] == "running"
            assert (await ctf.get_agent_session(tid, "lead"))["session"]["session_id"] == session_id
            # The test controller releases the harmless process only after coordination
            # reached the real provider boundary, so scheduling speed cannot hide a race.
            released = await asyncio.to_thread(
                container.get_wrapped_container().exec_run,
                [
                    ENVD_PYTHON,
                    "-c",
                    f"from pathlib import Path; Path({release!r}).write_text('ready')",
                ],
            )
            assert released.exit_code == 0, released.output
            await asyncio.wait_for(running, 20)

            final = await ctf.state(tid)
            assert len(final["turns"]) == 1
            saved_turn = final["turns"][0]
            assert saved_turn["id"] == turn["id"] and saved_turn["generation"] == turn["generation"]
            assert saved_turn["status"] == "finished" and saved_turn["end_reason"] == "completed"
            assert saved_turn["final_answer"] == "Final after persisted async-result"
            notices = [
                m for m in final["messages"] if m["notification_purpose"] == "command_result"
            ]
            assert len(notices) == 1 and notices[0]["status"] == "delivered"
            assert notices[0]["delivered_turn_id"] == turn["id"]
            input_message = next(m for m in final["messages"] if m["id"] == message_id)
            assert (
                input_message["status"] == "delivered"
                and input_message["delivered_turn_id"] == turn["id"]
            )
            saved = (await ctf.get_agent_session(tid, "lead"))["session"]
            assert saved["session_id"] == session_id
            final_entry = next(iter(saved["state"]["ctf_commands"].values()))
            assert final_entry["command_id"] == command_id and final_entry["state"] == "finished"
            assert (
                final_entry["completion_notified"] and final_entry["terminal_status"] == "completed"
            )
            assert json.loads(final_entry["result"])["structuredContent"]["exit_code"] == 0
            assert await envd.read_file(counter) == b"once\n"
            assert factories == ["lead"] and len(client.received_messages) == 5
            calls = {
                content.call_id: content.name
                for message in client.received_messages[-1]
                for content in message.contents
                if content.type == "function_call"
            }
            result_reads = [
                json.loads(content.result)
                for message in client.received_messages[-1]
                for content in message.contents
                if content.type == "function_result"
                and calls.get(content.call_id) == "read_command_result"
            ]
            assert len(result_reads) == 1 and result_reads[0]["command_id"] == command_id
            output = json.loads(result_reads[0]["result"])["structuredContent"]
            assert output["exit_code"] == 0
            assert output["stdout"] == "<command_output>async-result\n</command_output>"
            assert saved["state"]["ctf_steps"][str(turn["id"])] == 5
            assert runner.command_checkpoints == {}
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            await adapter.close()
