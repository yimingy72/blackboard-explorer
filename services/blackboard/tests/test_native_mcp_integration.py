"""Real Pi, native Virtual MCP and PostgreSQL, with only synthetic model responses."""

import asyncio
import json

import httpx
import pytest
from bbx_blackboard.store import schema as s
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.runner_proxy import RunnerModelProxy
from bbx_runtime.settings import Settings as RuntimeSettings
from mcp_gateway_support import native_business_gateway
from native_gateway_support import native_gateway
from runner_control_support import live_http, node_runner
from runner_gateway_support import cap, invocation, prepare, worker_scope
from sqlalchemy import select
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_native_fees_integration import provision_native
from test_runner_control_integration import connect, control, node_data, post_input, wait_record

pytestmark = pytest.mark.integration


class ObserveBusinessMCP:
    def __init__(self, app):
        self.app = app
        self.requests = []
        self.dropped = False

    async def __call__(self, scope, receive, send):
        business = "/business-mcp" in scope.get("path", "")
        message = None
        if business and scope["method"] == "POST":
            raw = bytearray()
            while True:
                event = await receive()
                raw.extend(event.get("body", b""))
                if not event.get("more_body"):
                    break
            message = json.loads(raw)
            headers = [v.decode() for k, v in scope["headers"] if k == b"x-bbx-gateway-backend"]
            self.requests.append((scope["path"], message.get("method"), headers))

            async def replay():
                return {"type": "http.request", "body": bytes(raw)}

            receive = replay
        lose = business and message and message.get("method") == "tools/call" and not self.dropped

        async def intercept(event):
            if lose:
                if event["type"] == "http.response.start":
                    self.dropped = True
                    await send({"type": "http.response.start", "status": 503, "headers": []})
                    await send(
                        {"type": "http.response.body", "body": b"Synthetic saved reply loss"}
                    )
                return
            await send(event)

        await self.app(scope, receive, intercept)


async def test_shared_native_business_routes_saved_reply_and_scope(ctf, tmp_path, record_property):
    async with (
        native_gateway(
            tmp_path / "models", tool_arguments={"title": "Original Pi challenge"}
        ) as native,
        connect(ctf) as (app, http),
    ):
        lead = await prepare(http, ctf, native=False, tools=["board_create_challenge"])
        other = await prepare(http, ctf, tools=["board_challenges"])
        await provision_native(app, http, lead, native, expected_usd="0.142857142")
        observed = ObserveBusinessMCP(app)
        async with app.state.business_mcp.run(), live_http(observed, "/unused") as (base, _):
            async with native_business_gateway(
                tmp_path / "business", app, base, [lead["tid"], other["tid"]]
            ) as gateway:
                model_proxy = RunnerModelProxy(
                    RuntimeSettings.model_construct(
                        runner_gateway_url=native["data"], runner_proxy_max_inflight=4
                    ),
                    BlackboardClient(base, "service-test"),
                )
                try:
                    async with live_http(model_proxy.app, "/unused") as (proxy_url, _):
                        data = await node_data(ctf, lead["tid"], lead["owner"], base)
                        data["model_proxy_url"] = proxy_url
                        data["mcp_gateway_url"] = gateway + f"/api/tasks/{lead['tid']}/mcp"
                        data["gateway_credentials"] = {
                            lead["binding"]["binding_id"]: lead["credentials"]
                        }
                        async with node_runner(data, tmp_path) as node:
                            try:
                                await wait_record(
                                    ctf,
                                    s.runner_bindings,
                                    lead["tid"],
                                    lead["binding"]["binding_id"],
                                    status="prepared",
                                )
                            except TimeoutError:
                                raise AssertionError(
                                    {
                                        "runner": await node.command("status"),
                                        "backend_methods": [
                                            (p, m) for p, m, _ in observed.requests
                                        ],
                                    }
                                ) from None
                            message = await post_input(
                                http,
                                ctf,
                                lead["tid"],
                                lead["owner"],
                                lead["binding"],
                                "Create the synthetic challenge",
                            )
                            await wait_record(
                                ctf,
                                s.runner_commands,
                                lead["tid"],
                                "1:" + message["request_id"],
                                status="applied",
                            )
                            async with asyncio.timeout(15):
                                while True:
                                    async with ctf.repo.engine.connect() as conn:
                                        calls = (
                                            (
                                                await conn.execute(
                                                    select(s.runner_calls).where(
                                                        s.runner_calls.c.task_id == lead["tid"]
                                                    )
                                                )
                                            )
                                            .mappings()
                                            .all()
                                        )
                                    if len([v for v in calls if v["kind"] == "model"]) == 2 and any(
                                        v["state"] == "completed"
                                        for v in calls
                                        if v["kind"] == "tool"
                                    ):
                                        break
                                    await asyncio.sleep(0.03)
                            assert (await node.command("status"))["exit_code"] is None
                    tools = [v for v in calls if v["kind"] == "tool"]
                    assert len(tools) == 1 and tools[0]["state"] == "completed"
                    assert tools[0]["data"]["tool_name"] == "board_create_challenge"
                    assert observed.dropped
                    assert sum(m == "tools/call" for _, m, _ in observed.requests) == 1
                    async with ctf.repo.engine.connect() as conn:
                        challenges = (
                            (
                                await conn.execute(
                                    select(s.ctf_challenges).where(
                                        s.ctf_challenges.c.task_id == lead["tid"]
                                    )
                                )
                            )
                            .mappings()
                            .all()
                        )
                    assert (
                        len(challenges) == 1
                        and challenges[0]["data"]["title"] == "Original Pi challenge"
                    )
                    worker = await worker_scope(
                        http, ctf, lead, tools=["board_read", "board_challenges"]
                    )
                    # A wrong-Task initialize must stop before any backend request.
                    async with httpx.AsyncClient(trust_env=False) as client:

                        async def rpc(actor, tid, method="initialize", params=None, **headers):
                            payload = (
                                {
                                    "protocolVersion": "2025-11-25",
                                    "capabilities": {},
                                    "clientInfo": {"name": "scope-probe", "version": "1"},
                                }
                                if method == "initialize"
                                else params
                            )
                            return await client.post(
                                gateway + f"/api/tasks/{tid}/mcp",
                                headers={
                                    **cap(actor, "mcp"),
                                    "accept": "application/json, text/event-stream",
                                    "x-bbx-sdk-session-id": actor["sdk"],
                                    **headers,
                                },
                                json={
                                    "jsonrpc": "2.0",
                                    "id": 1,
                                    "method": method,
                                    "params": payload,
                                },
                            )

                        before = len(observed.requests)
                        denied = await rpc(other, lead["tid"])
                        assert denied.status_code == 403 and len(observed.requests) == before
                        initialized = await rpc(
                            other, other["tid"], **{"x-bbx-gateway-backend": "forged-client-value"}
                        )
                        assert initialized.status_code == 200, initialized.text
                        assert observed.requests[-1][2] == [
                            app.state.settings.runner_mcp_backend_token.get_secret_value()
                        ]
                        direct = await client.post(
                            base + f"/api/tasks/{other['tid']}/business-mcp/",
                            headers={
                                **cap(other, "mcp"),
                                "x-bbx-sdk-session-id": other["sdk"],
                                "x-bbx-gateway-backend": "forged-client-value",
                            },
                            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                        )
                        assert direct.status_code == 401
                        before = len(observed.requests)
                        denied = await rpc(
                            other,
                            other["tid"],
                            "tools/call",
                            {"name": "foreign_board_challenges", "arguments": {}},
                            **{"mcp-session-id": initialized.headers["mcp-session-id"]},
                        )
                        assert (denied.status_code != 200 or "error" in denied.json()) and len(
                            observed.requests
                        ) == before
                        worker_init = await rpc(worker, lead["tid"])
                        assert worker_init.status_code == 200, worker_init.text
                        worker_headers = {"mcp-session-id": worker_init.headers["mcp-session-id"]}
                        listed = await rpc(worker, lead["tid"], "tools/list", {}, **worker_headers)
                        listed_result = (
                            json.loads(
                                next(
                                    line[6:]
                                    for line in listed.text.splitlines()
                                    if line.startswith("data: ")
                                )
                            )
                            if listed.headers.get("content-type", "").startswith(
                                "text/event-stream"
                            )
                            else listed.json()
                        )
                        assert {t["name"] for t in listed_result["result"]["tools"]} == {
                            "business_board_read",
                            "business_board_challenges",
                        }
                        before = len(observed.requests)
                        hidden = await rpc(
                            worker,
                            lead["tid"],
                            "tools/call",
                            {
                                "name": "business_board_create_challenge",
                                "arguments": {"title": "Denied"},
                            },
                            **worker_headers,
                        )
                        assert (hidden.status_code != 200 or "error" in hidden.json()) and len(
                            observed.requests
                        ) == before
                    wrong = invocation(
                        worker,
                        "board_read",
                        {"challenge_id": str(challenges[0]["id"])},
                        "cross-challenge",
                    )
                    path = (
                        control(lead["tid"]) + f"/bindings/{worker['binding']['binding_id']}/tools"
                    )
                    denied = await http.post(
                        path + "/admit", headers=cap(worker, "mcp"), json=wrong
                    )
                    assert denied.status_code == 403
                    record_property(
                        "native_mcp_evidence",
                        json.dumps(
                            {
                                "shared_gateway_tasks": 2,
                                "saved_reply_lost": observed.dropped,
                                "domain_effects": 1,
                                "business_dispatches": 1,
                                "cross_task_backend_requests": 0,
                                "foreign_target_backend_requests": 0,
                                "client_backend_header_overwritten": True,
                                "worker_cross_challenge_status": denied.status_code,
                            }
                        ),
                    )
                finally:
                    await model_proxy.close()
                    await model_proxy.service.close()
                    await app.state.runner_gateway.native.admin.close()
