"""Lead verification uses its own saved Runtime evidence without a Worker Assignment."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from bbx_blackboard.challenge_board import ChallengeBoardService
from bbx_blackboard.runner_control import row
from bbx_blackboard.runner_gateway import RunnerGateway
from bbx_blackboard.store import schema as s
from bbx_contracts.runner import runner_json_text
from fastapi import HTTPException
from runner_gateway_support import invocation
from sqlalchemy import insert, update
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_worker_pool_integration import pool_task, publish, synthetic_principal

pytestmark = pytest.mark.integration


async def test_lead_can_post_current_command_evidence_but_not_an_old_epoch(ctf):
    async with pool_task(ctf, 1) as (tid, pool):
        challenge = await publish(ctf, tid)
        lead = await synthetic_principal(ctf, pool, tid, "lead")
        assert lead.runtime_context is not None
        bid = lead.runtime_context.identity.binding_id
        epoch = lead.runtime_context.identity.fence.epoch
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                insert(s.sandbox_command_records).values(
                    task_id=tid,
                    id="lead-proof",
                    binding_id=bid,
                    agent_id="lead",
                    role="lead",
                    challenge_id=None,
                    assignment_id="lead-assignment",
                    generation=lead.generation,
                    epoch=epoch,
                    execution_id="exec-lead",
                    state="completed",
                    exit_code=0,
                    created_at=datetime.now(UTC),
                    data={"receipt": "foreground_terminal"},
                )
            )
            await conn.execute(
                insert(s.runner_calls).values(
                    task_id=tid,
                    id="tool:lead-proof",
                    kind="tool",
                    binding_id=bid,
                    owner_epoch=epoch,
                    state="completed",
                    concurrency_active=False,
                    provider_key="sandbox",
                    created_at=datetime.now(UTC),
                    data={"tool_name": "exec_command", "binding": {"turn_id": str(lead.turn_id)}},
                )
            )
        body = {
            "request_id": str(uuid4()),
            "expected_revision": challenge["revision"],
            "kind": "observation",
            "statement": "Lead observed the saved local result",
            "evidence": [{"type": "command_output", "call_id": "lead-proof", "summary": "4"}],
        }
        service = ChallengeBoardService(ctf)
        result = await service.write(tid, UUID(challenge["id"]), lead, body, kind="fact")
        assert result["object"]["author"] == "lead"
        assert result["object"]["provenance"] == "tool_backed"
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.sandbox_command_records)
                .where(
                    s.sandbox_command_records.c.task_id == tid,
                )
                .values(epoch=epoch - 1)
            )
        with pytest.raises(HTTPException, match="unconfirmed"):
            await service.write(
                tid, UUID(challenge["id"]), lead, {**body, "request_id": str(uuid4())}, kind="fact"
            )


async def test_lead_file_fact_parameter_returns_a_saved_error_without_freezing(ctf):
    async with pool_task(ctf, 1) as (tid, pool):
        lead = await synthetic_principal(ctf, pool, tid, "lead")
        assert lead.runtime_context is not None
        context = lead.runtime_context
        identity = replace(context.identity, audience="sandbox-mcp")
        async with ctf.repo.engine.begin() as conn:
            binding = await row(conn, s.runner_bindings, tid, context.identity.binding_id)
            payload = json.loads(binding["payload_json"])
            payload["spec"]["allowed_tools"] = ["read_file"]
            payload["spec"]["mcp_tool_routes"] = {
                "read_file": {"target": "sandbox", "name": "read_file"}
            }
            await conn.execute(
                update(s.runner_bindings)
                .where(
                    s.runner_bindings.c.task_id == tid,
                    s.runner_bindings.c.id == binding["id"],
                )
                .values(payload_json=runner_json_text(payload))
            )
        scope = {
            "tid": tid,
            "binding": binding["binding"],
            "sdk": context.sdk_session_id,
            "journal": context.journal,
            "owner": {"fence": context.identity.fence.model_dump(mode="json")},
        }
        gateway = RunnerGateway(ctf.repo.engine, settings())
        gateway.sandbox.binding = AsyncMock(return_value={})
        value = invocation(
            scope,
            "read_file",
            {
                "path": "/workspace/result.txt",
                "fact": {"kind": "observation", "statement": "result", "expected_revision": 1},
            },
        )
        permit = await gateway.admit_tool(tid, identity, runner_json_text(value).encode())
        assert permit["disposition"] == "completed"
        result = json.loads(permit["result_json"])
        assert result["isError"] and "Nothing was executed" in result["content"][0]["text"]
        gateway.sandbox.binding.assert_not_awaited()
        retry = invocation(scope, "read_file", {"path": "/workspace/result.txt"}, "corrected")
        assert (await gateway.admit_tool(tid, identity, runner_json_text(retry).encode()))[
            "disposition"
        ] == "admitted"
