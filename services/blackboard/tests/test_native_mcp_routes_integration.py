"""Native route publication follows real credentials, stop and PostgreSQL recovery."""

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_blackboard.conversations import Conversations
from bbx_blackboard.native_admin import ROUTES_PATH, NativeAdmin
from bbx_blackboard.native_mcp_routes import NativeMCPRoutes
from bbx_blackboard.runner_gateway import RunnerGateway
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from mcp_gateway_support import native_business_gateway
from runner_control_support import live_http
from runner_gateway_support import cap, prepare, private
from sqlalchemy import select, update
from test_board_flags_integration import Objects
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_runner_control_integration import SERVICE, connect

pytestmark = pytest.mark.integration


@asynccontextmanager
async def route_app(ctf, directory):
    ctf.objects = Objects()
    async with connect(ctf, native_mcp=True) as (app, http):
        async with app.state.business_mcp.run(), live_http(app, "/unused") as (base, _):

            async def configure(data, admin):
                config = app.state.settings.model_copy(
                    update={
                        "native_gateway_accounting_source_id": "synthetic-persistent-route-db",
                        "native_gateway_admin_url": admin,
                        "native_mcp_gateway_url": data,
                        "native_mcp_backend_url": base.replace("127.0.0.1", "host.docker.internal"),
                        "native_mcp_jwks_file": "/probe/jwks.json",
                        "native_mcp_backend_token_file": "/probe/backend-token",
                    }
                )
                await app.state.runner_gateway.native.admin.close()
                app.state.settings = config
                app.state.runner_gateway = RunnerGateway(ctf.repo.engine, config)

            async with native_business_gateway(
                directory, app, base, [], dynamic=True, on_ready=configure
            ) as data:
                try:
                    yield app, http, data
                finally:
                    await app.state.runner_gateway.native.admin.close()


class Writes:
    def __init__(self, admin):
        self.puts, self.deletes = [], []
        self.raw_put, self.raw_delete = admin.put_route, admin.delete_route
        self.lost_ack = self.unsent = self.paused = False
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.pending_tid = None
        admin.put_route, admin.delete_route = self.put, self.delete

    async def put(self, value):
        self.puts.append(value["name"])
        if self.unsent:
            self.unsent = False
            raise HTTPException(503, "Synthetic uncertain publication")
        if self.paused:
            self.paused = False
            self.pending_tid = UUID(value["matches"][0]["path"]["exact"].split("/")[3])
            self.entered.set()
            await self.release.wait()
        await self.raw_put(value)
        if self.lost_ack:
            self.lost_ack = False
            raise HTTPException(503, "Synthetic lost publication ACK")

    async def delete(self, resource_id):
        self.deletes.append(resource_id)
        await self.raw_delete(resource_id)


async def credentials(http, scope):
    return await http.post(
        private(scope) + f"/bindings/{scope['binding']['binding_id']}/credentials",
        headers=SERVICE,
        json={"expected_epoch": scope["owner"]["fence"]["epoch"]},
    )


async def state(http, scope):
    response = await http.get(private(scope) + "/mcp-route", headers=SERVICE)
    assert response.status_code == 200, response.text
    return response.json()


async def wait_state(http, scope, expected):
    async with asyncio.timeout(6):
        while True:
            value = await state(http, scope)
            if value["publication_state"] == expected:
                return value
            await asyncio.sleep(0.03)


async def catalog(scope):
    async with httpx.AsyncClient(trust_env=False) as client:
        headers = {
            **cap(scope, "mcp"),
            "accept": "application/json, text/event-stream",
            "x-bbx-sdk-session-id": scope["sdk"],
        }
        url = scope["credentials"]["mcp_gateway_url"]
        response = await client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "route-lifecycle", "version": "1"},
                },
            },
        )
        assert response.status_code == 200, response.text
        headers["mcp-session-id"] = response.headers["mcp-session-id"]
        response = await client.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200, response.text
        value = (
            json.loads(
                next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
            )
            if response.headers.get("content-type", "").startswith("text/event-stream")
            else response.json()
        )
        return {item["name"] for item in value["result"]["tools"]}


async def stop(http, scope):
    response = await http.post(f"/api/tasks/{scope['tid']}/stop", headers=SERVICE)
    assert response.status_code == 200, response.text


async def cancel(worker):
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker


async def test_credentials_publish_active_routes_and_restart_reconciles_lost_ack(
    ctf, tmp_path, record_property
):
    async with route_app(ctf, tmp_path / "gateway") as (app, http, data):
        gateway = app.state.runner_gateway
        writes = Writes(gateway.native.admin)
        first = await prepare(http, ctf, tools=["board_challenges"])
        other = await prepare(http, ctf, tools=["board_announcements"])
        assert first["credentials"]["mcp_gateway_url"] == data + f"/api/tasks/{first['tid']}/mcp"
        assert await catalog(first) == {"business_board_challenges"}
        assert await catalog(other) == {"business_board_announcements"}
        original = await state(http, first)
        concurrent = await asyncio.gather(*(credentials(http, first) for _ in range(5)))
        assert all(response.status_code == 200 for response in concurrent)
        assert (await state(http, first))["revision"] == original["revision"]
        assert len(writes.puts) == 2
        # Confirmed publication can recover the same identity after an external deletion.
        await writes.raw_delete(original["resource_id"])
        response = await credentials(http, first)
        assert response.status_code == 200, response.text
        assert (await state(http, first))["resource_id"] == original["resource_id"]
        assert writes.puts.count(original["resource_id"]) == 2
        writes.lost_ack = True
        lost = await prepare(http, ctf, tools=["board_challenges"], credentials_status=503)
        assert (await state(http, lost))["publication_state"] == "unknown"
        lost_id = (await state(http, lost))["resource_id"]
        # A new HTTP owner and manager recover from persisted intent through the real run loop.
        await gateway.native.admin.close()
        gateway.native.admin = NativeAdmin(app.state.settings)
        restarted_writes = Writes(gateway.native.admin)
        gateway.routes = NativeMCPRoutes(gateway)
        worker = asyncio.create_task(gateway.routes.run())
        try:
            recovered = await wait_state(http, lost, "ready")
            assert recovered["resource_id"] == lost_id
            lost["credentials"] = (await credentials(http, lost)).json()
            assert await catalog(lost) == {"business_board_challenges"}
            assert writes.puts.count(lost_id) == 1
            assert not restarted_writes.puts
            await stop(http, first)
            retired = await wait_state(http, first, "retired")
            assert retired["desired"] == "retired"
            assert await catalog(other) == {"business_board_announcements"}
            # Legacy resume must not reopen a V1 Task before or after retirement is observed.
            async with ctf.repo.engine.connect() as conn:
                before = dict(
                    (await conn.execute(select(s.tasks).where(s.tasks.c.id == first["tid"])))
                    .mappings()
                    .one()
                )
            with pytest.raises(HTTPException) as denied:
                await ctf.resume(first["tid"], uuid4(), "1", 1)
            assert denied.value.status_code == 409
            async with ctf.repo.engine.connect() as conn:
                after = dict(
                    (await conn.execute(select(s.tasks).where(s.tasks.c.id == first["tid"])))
                    .mappings()
                    .one()
                )
            assert (after["run_number"], after["ctf_control"], after["status"]) == (
                before["run_number"],
                before["ctf_control"],
                before["status"],
            )
            await cancel(worker)
            # Confirmed retirement no longer needs the old deployment to remain configured.
            gateway.routes.settings = app.state.settings.model_copy(
                update={
                    "native_gateway_accounting_source_id": "different-deployment",
                    "native_gateway_admin_url": "",
                }
            )
            await gateway.routes.retire(first["tid"])
            assert (await state(http, first))["publication_state"] == "retired"
            # This fixture owns no remote process; model the trusted cleaner acknowledgement.
            async with ctf.repo.engine.begin() as conn:
                await gateway.repo.lock(conn, first["tid"])
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == first["tid"])
                    .values(status="stopped", deleting=True, cleanup_ready=False)
                )
            with pytest.raises(HTTPException, match="Runner cleanup"):
                await Conversations(ctf.repo.engine).purge(first["tid"], ctf.objects)
            async with ctf.repo.engine.begin() as conn:
                await gateway.repo.lock(conn, first["tid"])
                await conn.execute(
                    update(s.tasks).where(s.tasks.c.id == first["tid"]).values(cleanup_ready=True)
                )
            assert await Conversations(ctf.repo.engine).purge(first["tid"], ctf.objects) == {
                "purged": True
            }
            async with ctf.repo.engine.connect() as conn:
                assert not (
                    await conn.execute(
                        select(s.native_task_mcp_routes).where(
                            s.native_task_mcp_routes.c.task_id == first["tid"]
                        )
                    )
                ).first()
            record_property(
                "route_lifecycle",
                json.dumps(
                    {
                        "shared_tasks": 3,
                        "concurrent_credentials": 5,
                        "same_route_recovered": True,
                        "lost_ack_restart": True,
                        "retired_active_absent": True,
                        "other_task_still_usable": True,
                        "v1_resume_denied": True,
                        "retirement_alone_does_not_confirm_cleanup": True,
                    }
                ),
            )
        finally:
            if not worker.done():
                await cancel(worker)


async def test_unknown_and_drift_fail_closed_while_late_publish_is_retired(
    ctf, tmp_path, record_property
):
    async with route_app(ctf, tmp_path / "gateway") as (app, http, _):
        gateway = app.state.runner_gateway
        publisher, admin = gateway.routes, gateway.native.admin
        writes = Writes(admin)
        writes.unsent = True
        unknown = await prepare(http, ctf, native=False, credentials_status=503)
        unknown_id = (await state(http, unknown))["resource_id"]
        retried = await credentials(http, unknown)
        assert retried.status_code == 503 and writes.puts.count(unknown_id) == 1
        await stop(http, unknown)
        await publisher.reconcile(unknown["tid"])
        held = await state(http, unknown)
        assert held["desired"] == "retired" and held["publication_state"] == "unknown"
        assert held["last_error"] == "mcp_original_publication_unconfirmed"
        assert not writes.deletes
        async with ctf.repo.engine.begin() as conn:
            await gateway.repo.lock(conn, unknown["tid"])
            await conn.execute(
                update(s.tasks).where(s.tasks.c.id == unknown["tid"]).values(deleting=True)
            )
        with pytest.raises(HTTPException, match="MCP route retirement"):
            await Conversations(ctf.repo.engine).purge(unknown["tid"], ctf.objects)
        before_http = []
        raw_request = admin.request

        async def counted(*args, **kwargs):
            before_http.append(args[:2])
            return await raw_request(*args, **kwargs)

        admin.request = counted
        publisher.settings = app.state.settings.model_copy(
            update={"native_gateway_accounting_source_id": "foreign-source"}
        )
        with pytest.raises(HTTPException) as denied:
            await publisher.retire(unknown["tid"])
        assert denied.value.status_code == 409 and not before_http
        publisher.settings = app.state.settings
        other = await prepare(http, ctf, tools=["board_challenges"])
        resources = await admin.resources(ROUTES_PATH)
        other_state = await state(http, other)
        owned = next(
            item["value"] for item in resources if item["id"] == other_state["resource_id"]
        )
        foreign = deepcopy(owned)
        foreign["name"] = "foreign-controller-" + uuid4().hex
        foreign["matches"] = [{"path": {"pathPrefix": "/api/tasks/"}}]
        await writes.raw_put(foreign)
        before_deletes = len(writes.deletes)
        with pytest.raises(HTTPException) as denied:
            await publisher.reconcile(other["tid"])
        assert denied.value.status_code == 409 and len(writes.deletes) == before_deletes
        assert any(item["id"] == foreign["name"] for item in await admin.resources(ROUTES_PATH))
        await writes.raw_delete(foreign["name"])
        # A differently named P3d file-owned shadow must never be adopted or deleted.
        path = tmp_path / "gateway/config.json"
        original_file = path.read_text()
        base = json.loads(original_file)
        shadow = deepcopy(owned)
        shadow["name"] = "business-legacy-shadow-" + uuid4().hex
        base["routes"] = [shadow]
        replacement = path.with_suffix(".next")
        replacement.write_text(json.dumps(base))
        replacement.replace(path)
        try:
            async with asyncio.timeout(3):
                while True:
                    try:
                        observed_file = await raw_request("GET", "/api/config")
                        if any(
                            item["name"] == shadow["name"]
                            for item in observed_file.get("routes", [])
                        ):
                            break
                    except HTTPException:
                        pass
                    await asyncio.sleep(0.03)
            with pytest.raises(HTTPException) as denied:
                await publisher.reconcile(other["tid"])
            assert denied.value.status_code == 409 and len(writes.deletes) == before_deletes
            assert shadow["name"] not in writes.puts
        finally:
            replacement.write_text(original_file)
            replacement.replace(path)
        # Wait for native file reload by observing it, without treating a sleep as an ACK.
        async with asyncio.timeout(5):
            while True:
                try:
                    await publisher.reconcile(other["tid"])
                    break
                except HTTPException:
                    await asyncio.sleep(0.05)
        writes.paused = True
        pending = asyncio.create_task(prepare(http, ctf, native=False, credentials_status=409))
        await asyncio.wait_for(writes.entered.wait(), 5)
        tid = writes.pending_tid
        assert tid is not None
        stopped = await http.post(f"/api/tasks/{tid}/stop", headers=SERVICE)
        assert stopped.status_code == 200, stopped.text
        worker = asyncio.create_task(publisher.run())
        try:
            async with asyncio.timeout(5):
                while True:
                    value = await publisher.view(tid)
                    if value["desired"] == "retired" and value["publication_state"] == "unknown":
                        break
                    await asyncio.sleep(0.03)
            assert value["publication_state"] == "unknown"
            writes.release.set()
            late = await pending
            assert "mcp_gateway_url" not in late["credentials"]
            retired = await wait_state(http, late, "retired")
            assert retired["desired"] == "retired"
            assert writes.puts.count(retired["resource_id"]) == 1
            assert writes.deletes.count(retired["resource_id"]) == 1
            assert (await state(http, unknown))["publication_state"] == "unknown"
            assert await catalog(other) == {"business_board_challenges"}
            private_read = await http.get(private(unknown) + "/mcp-route")
            assert private_read.status_code in {401, 403}
            record_property(
                "route_fail_closed",
                json.dumps(
                    {
                        "unknown_absent_blocks_purge": True,
                        "source_drift_http_requests": 0,
                        "foreign_resource_deleted": False,
                        "file_shadow_adopted": False,
                        "late_publication_retired": True,
                        "late_credentials_issued": False,
                    }
                ),
            )
        finally:
            writes.release.set()
            if not pending.done():
                await pending
            await cancel(worker)
