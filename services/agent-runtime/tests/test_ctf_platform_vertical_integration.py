"""Real backend and runtime adapter with simulated model/platform, never a real target."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from agent_framework import Agent
from bbx_blackboard.api import create_app
from bbx_blackboard.platform import McpInput
from bbx_blackboard.settings import Settings
from bbx_contracts.ctf import CtfAgentProfile, load_ctf_profile
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.platform import PlatformAdapter
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from test_ctf_vertical_integration import MemoryObjects
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


async def test_fake_platform_rejection_correction_acceptance_and_explicit_finish(
    ctf_vertical_database,
):
    settings = Settings(
        postgres_password=SecretStr("isolated-test"),
        minio_root_password=SecretStr("unused-test"),
        service_token=SecretStr("ctf-platform-test-service"),
        agent_token_secret=SecretStr("ctf-platform-isolated-test-signing-key"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    engine = create_async_engine(ctf_vertical_database)
    objects = MemoryObjects()
    app = create_app(settings, engine=engine, objects=objects)
    await app.state.profile_store.ensure_bundled(settings.profiles_dir, app.state.platform_store)
    server = await app.state.platform_store.save(
        "mcp-servers",
        "fake-platform",
        McpInput(label="Simulated platform only", url="http://fake.invalid/mcp"),
        "test",
    )
    data = load_ctf_profile(ROOT / "profiles/ctf").model_dump()
    data["platform_tools"] = [
        {
            "server_name": "fake-platform",
            "server_version": server["version"],
            "tool_name": "submit",
            "purpose": "submit",
            "result_adapter": "fake_ctf_v1",
        }
    ]
    for role in ("lead", "teammate"):
        data["worker_tools"][role]["mcp_servers"] = [
            {
                "name": "fake-platform",
                "version": server["version"],
                "allowed_tools": ["submit"],
            }
        ]
    profile = CtfAgentProfile.model_validate(data)
    configured = await app.state.profile_store.create("ctf-platform-vertical", profile, "test")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://board", trust_env=False
        ) as http:
            board = BlackboardClient("http://board", "ctf-platform-test-service", http)
            client = CtfClient(board)
            tid = (
                await board.create_task(
                    {
                        "mode": "ctf",
                        "goal": "Complete the explicitly simulated challenge",
                        "budget": {"max_cost": "100", "max_minutes": 20},
                        "agent_profile": configured["name"],
                        "profile_version": configured["version"],
                    }
                )
            )["id"]
            await board.start_task(tid)
            await client.runtime(tid, "start")
            lead = await client.runtime(tid, "claim_turn", agent_id="lead", runtime_instance="test")

            async def tool(turn, name, **body):
                return await client.tool(tid, turn["token"], name, **body)

            await tool(lead, "create_teammate", display_name="Owner", request_id=str(uuid4()))
            challenge = await tool(
                lead, "create_challenge", request_id=str(uuid4()), title="Fake checksum"
            )
            cid = challenge["id"]
            await tool(
                lead,
                "update_challenge",
                challenge_id=cid,
                request_id=str(uuid4()),
                expected_revision=challenge["revision"],
                action="assign",
                owner_id="member-1",
            )
            await tool(
                lead,
                "send_message",
                recipient_id="member-1",
                message_id=str(uuid4()),
                kind="instruction",
                body=f"Solve simulated challenge {cid}; use the fake adapter only",
            )
            owner = await client.runtime(
                tid, "claim_turn", agent_id="member-1", runtime_instance="test"
            )

            async def current():
                return await tool(lead, "get_challenge", challenge_id=cid)

            await tool(
                lead,
                "set_verification_required",
                challenge_id=cid,
                request_id=str(uuid4()),
                expected_revision=(await current())["revision"],
                required=True,
                basis="This isolated test explicitly requires the fake adapter acceptance",
            )
            await tool(
                owner,
                "request_help",
                challenge_id=cid,
                request_id=str(uuid4()),
                expected_revision=(await current())["revision"],
                body="Need help understanding the checksum",
                attempted_routes="Inspected the sample manually",
                observations_and_basis="The sample checksum differs",
                failure_conditions="Header was omitted",
                current_blocker="Unclear checksum input",
                help_needed="Check whether to include the header",
                no_artifacts_reason="Manual inspection only; no script created",
            )
            assert (await current())["work_status"] == "blocked"
            await tool(
                lead,
                "append_record",
                challenge_id=cid,
                request_id=str(uuid4()),
                body="Include the header; record each simulated submission result",
            )
            with pytest.raises(RemoteError) as forged:
                await tool(
                    owner,
                    "record_candidate",
                    challenge_id=cid,
                    request_id=str(uuid4()),
                    expected_revision=(await current())["revision"],
                    status="accepted",
                    source="platform",
                    summary="Pretend success",
                )
            assert forged.value.status in {403, 409, 422}

            checkpoint = await load_checkpoint(
                client, tid, "member-1", owner, "Simulated platform test"
            )
            calls = []

            async def fake_transport(pinned, headers, name, arguments):
                assert pinned["version"] == server["version"] and not headers
                if name is None:
                    return {"tools": [{"name": "submit", "inputSchema": {"type": "object"}}]}
                calls.append(arguments)
                return {
                    "structuredContent": {
                        "status": "accepted"
                        if arguments["answer"] == "include-header"
                        else "rejected",
                        "target_id": "simulated-target",
                        "submission_id": f"fake-{len(calls)}",
                    }
                }

            adapter = PlatformAdapter(client, lambda: objects, fake_transport)
            platform_tools = await adapter.build(
                profile, "teammate", tid, "member-1", owner, lambda: checkpoint
            )

            async def submit(answer):
                scripted = ScriptedChatClient(
                    [
                        ScriptStep(
                            calls=(
                                ScriptToolCall(
                                    "platform_fake-platform_submit",
                                    {
                                        "challenge_id": cid,
                                        "expected_revision": (await current())["revision"],
                                        "arguments": {"answer": answer},
                                    },
                                ),
                            )
                        ),
                        ScriptStep(text="Recorded simulated submission"),
                    ]
                )
                async with Agent(
                    client=scripted,
                    instructions="Call only the fake platform tool",
                    tools=platform_tools,
                ) as agent:
                    result = await agent.run(
                        "Submit the scripted candidate", session=checkpoint.session
                    )
                assert result.text == "Recorded simulated submission"
                await checkpoint.save()

            await submit("omit-header")
            rejected = await current()
            assert rejected["verification"]["status"] == "rejected"
            assert rejected["verification"]["source"] == "platform"
            assert rejected["verification"]["test_only"] is True
            await tool(
                owner,
                "update_challenge",
                challenge_id=cid,
                request_id=str(uuid4()),
                expected_revision=rejected["revision"],
                action="set_status",
                work_status="completed",
            )
            with pytest.raises(RemoteError) as unverified:
                await tool(
                    lead,
                    "finish_task",
                    request_id=str(uuid4()),
                    conclusion={
                        "end_reason": "goal_claimed",
                        "summary": "Premature",
                        "lead_claim": True,
                    },
                )
            assert unverified.value.status == 409
            await tool(
                owner,
                "update_challenge",
                challenge_id=cid,
                request_id=str(uuid4()),
                expected_revision=(await current())["revision"],
                action="reopen",
            )
            await tool(
                owner,
                "append_record",
                challenge_id=cid,
                request_id=str(uuid4()),
                body="Fake rejection confirms omitted header; retry with the corrected input",
            )
            await submit("include-header")
            accepted = await current()
            assert accepted["verification"]["status"] == "accepted"
            assert accepted["verification"]["test_only"] is True
            assert len(calls) == 2
            state = await client.state(tid)
            assert state["task"]["status"] == "running"
            assert state["task"]["ctf_control"]["phase"] == "running"
            records = (await tool(lead, "list_records", challenge_id=cid))["records"]
            platform_records = [r for r in records if r.get("platform_call")]
            assert len(platform_records) == 2
            for record in platform_records:
                evidence = record["platform_call"]
                saved = json.loads(objects.files[evidence["response_uri"]])
                assert saved["structuredContent"]["target_id"] == "simulated-target"
            await tool(
                owner,
                "update_challenge",
                challenge_id=cid,
                request_id=str(uuid4()),
                expected_revision=accepted["revision"],
                action="set_status",
                work_status="completed",
            )
            closing = await tool(
                lead,
                "finish_task",
                request_id=str(uuid4()),
                conclusion={
                    "end_reason": "goal_claimed",
                    "summary": "Simulated acceptance only; no real platform was validated",
                    "lead_claim": True,
                },
            )
            assert closing["phase"] == "closing"
            for mid, turn in (("member-1", owner), ("lead", lead)):
                await client.runtime(
                    tid,
                    "finish_turn",
                    agent_id=mid,
                    turn_id=turn["id"],
                    generation=turn["generation"],
                    end_reason="completed",
                    answer="Simulated work complete",
                )
            assert (await client.runtime(tid, "finalize_close", drained=True))[
                "status"
            ] == "finished"
    finally:
        await app.state.workspace_cache.close()
        await engine.dispose()
