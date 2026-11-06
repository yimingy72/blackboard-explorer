"""Persistent call authority with synthetic upstreams, never real model services."""

from uuid import uuid4

import pytest
from bbx_blackboard.business_tools import BusinessTools
from bbx_blackboard.gateway_auth import require_gateway_identity
from bbx_blackboard.store import schema as s
from bbx_blackboard.task_board import TaskBoardService
from bbx_contracts.runner import PROTOCOL, ToolInvocation, runner_json_text
from fastapi import HTTPException
from runner_gateway_support import (
    cap,
    digest,
    invocation,
    prepare,
    private,
    worker_scope,
)
from sqlalchemy import select, update
from starlette.requests import Request
from test_board_flags_integration import identity as agent_identity
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_runner_control_integration import SERVICE, connect, mgmt, register_owner

pytestmark = pytest.mark.integration


def gateway_identity(app, scope, audience="mcp"):
    request = Request(
        {
            "type": "http",
            "app": app,
            "headers": [(b"authorization", cap(scope, audience)["Authorization"].encode())],
        }
    )
    return require_gateway_identity(request, scope["tid"], audience)


async def test_business_host_fence_saved_result_and_cross_owner_no_redispatch(ctf):
    async with connect(ctf) as (app, http):
        scope = await prepare(http, ctf, tools=["board_create_challenge", "board_finish_task"])
        body = invocation(scope, "board_create_challenge", {"title": "must-not-be-created"})
        path = (
            f"/api/tasks/{scope['tid']}/runner-control/bindings/"
            f"{scope['binding']['binding_id']}/tools"
        )
        admitted = await http.post(path + "/admit", headers=cap(scope, "mcp"), json=body)
        assert admitted.status_code == 200, admitted.text
        identity = gateway_identity(app, scope)
        claimed, _ = await app.state.runner_gateway.claim_business_tool(
            scope["tid"],
            identity,
            ToolInvocation.model_validate(body),
            body["tool_name"],
            {"title": "must-not-be-created"},
        )
        principal, data, arguments = claimed
        successor = await register_owner(http, scope["tid"], 1)
        with pytest.raises(HTTPException):
            await BusinessTools(
                ctf, app.state.settings.agent_token_secret.get_secret_value()
            ).invoke(
                scope["tid"],
                principal,
                body["tool_name"],
                {**arguments, "request_id": data["mutation_request_id"]},
            )
        await app.state.runner_gateway.save_business_result(
            scope["tid"],
            body["invocation_id"],
            {"content": [{"type": "text", "text": "original result 1.0 / 你好"}], "isError": True},
        )
        attached = await http.post(
            mgmt(scope["tid"]) + f"/bindings/{scope['binding']['binding_id']}/attach",
            headers=SERVICE,
            json={"expected_epoch": 2},
        )
        assert attached.status_code == 200
        command = scope["command"]
        from test_runner_control_integration import acquire

        await acquire(http, scope["tid"], successor, scope["binding"], command)
        response = await http.post(
            private(scope) + f"/bindings/{scope['binding']['binding_id']}/credentials",
            headers=SERVICE,
            json={"expected_epoch": 2},
        )
        current = {**scope, "owner": successor, "credentials": response.json()}
        updated = {**body, "fence": successor["fence"]}
        replayed = await http.post(path + "/admit", headers=cap(current, "mcp"), json=updated)
        assert replayed.status_code == 200 and replayed.json()["disposition"] == "completed"
        confirmation = {"protocol": PROTOCOL, "invocation": updated}
        forged = await http.post(
            path + f"/{body['invocation_id']}/result",
            headers=cap(current, "mcp"),
            json={**confirmation, "result_json": "forged"},
        )
        assert forged.status_code == 422
        result = await http.post(
            path + f"/{body['invocation_id']}/result",
            headers=cap(current, "mcp"),
            json=confirmation,
        )
        assert (
            result.status_code == 200
            and digest(result.json()["result_json"]) == result.json()["result_digest"]
        )
        with pytest.raises(HTTPException):
            await app.state.runner_gateway.claim_business_tool(
                scope["tid"],
                gateway_identity(app, current),
                ToolInvocation.model_validate(
                    {
                        **updated,
                        "arguments_json": runner_json_text({"title": "other"}),
                        "arguments_digest": digest(runner_json_text({"title": "other"})),
                    }
                ),
                body["tool_name"],
                {"title": "other"},
            )
        async with ctf.repo.engine.connect() as conn:
            assert not (
                await conn.execute(
                    select(s.ctf_challenges.c.id).where(s.ctf_challenges.c.task_id == scope["tid"])
                )
            ).all()
        worker = await worker_scope(
            http, ctf, current, tools=["board_announcements", "board_challenges"]
        )
        async with ctf.repo.engine.connect() as conn:
            lead = (
                (
                    await conn.execute(
                        select(s.ctf_members).where(
                            s.ctf_members.c.task_id == scope["tid"], s.ctf_members.c.id == "lead"
                        )
                    )
                )
                .mappings()
                .one()
            )
        history = await TaskBoardService(ctf).create_challenge(
            scope["tid"],
            agent_identity(scope["tid"], "lead", lead["generation"], lead["current_turn_id"]),
            {"request_id": str(uuid4()), "title": "Old Worker scope"},
        )
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.ctf_challenges)
                .where(
                    s.ctf_challenges.c.task_id == scope["tid"],
                    s.ctf_challenges.c.id == history["id"],
                )
                .values(
                    data={
                        **history,
                        "owner_id": worker["binding"]["agent_id"],
                        "work_status": "completed",
                    }
                )
            )

        async def execute(actor, name, args, suffix):
            inv = invocation(actor, name, args, suffix)
            tool_path = (
                f"/api/tasks/{scope['tid']}/runner-control/bindings/"
                f"{actor['binding']['binding_id']}/tools"
            )
            admitted = await http.post(tool_path + "/admit", headers=cap(actor, "mcp"), json=inv)
            assert admitted.status_code == 200, admitted.text
            claimed, _ = await app.state.runner_gateway.claim_business_tool(
                scope["tid"],
                gateway_identity(app, actor),
                ToolInvocation.model_validate(inv),
                name,
                args,
            )
            principal, data, original = claimed
            arguments = dict(original)
            if name == "board_finish_task":
                arguments["request_id"] = data["mutation_request_id"]
            value = await BusinessTools(
                ctf, app.state.settings.agent_token_secret.get_secret_value()
            ).invoke(scope["tid"], principal, name, arguments)
            await app.state.runner_gateway.save_business_result(
                scope["tid"],
                inv["invocation_id"],
                {"content": [{"type": "text", "text": runner_json_text(value)}], "isError": False},
            )
            confirmation = await http.post(
                tool_path + f"/{inv['invocation_id']}/result",
                headers=cap(actor, "mcp"),
                json={"protocol": PROTOCOL, "invocation": inv},
            )
            assert confirmation.status_code == 200, confirmation.text
            return value

        assert await execute(worker, "board_announcements", {}, "announcements") == []
        challenges = await execute(worker, "board_challenges", {}, "challenges")
        assert [item["id"] for item in challenges] == [worker["challenge"]["id"]]
        # The actual finish transaction closes admission before its saved result is confirmed.
        finished = await execute(
            current,
            "board_finish_task",
            {"conclusion": {"end_reason": "user_stop", "summary": "Stop fixture"}},
            "finish",
        )
        assert finished["phase"] == "closing"
        await app.state.runner_gateway.native.admin.close()
