"""One ordinary Runner command and its source fence against real PostgreSQL."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from bbx_blackboard.auth import Principal
from bbx_blackboard.board_runner_dispatch import board_dispatch
from bbx_blackboard.board_runner_publication import BlackboardRunnerPublication
from bbx_blackboard.board_runner_scope import EXPLORATION_CONTRACT, require_board_run
from bbx_blackboard.board_runner_tools import invoke
from bbx_blackboard.gateway_auth import GatewayIdentity, RuntimeBusinessContext
from bbx_blackboard.runner_auth import RunnerIdentity
from bbx_blackboard.runner_control import RunnerControl, fence
from bbx_blackboard.store import schema as s
from bbx_contracts.runner import RunnerFence, runner_json_text
from bbx_contracts.runner_bootstrap import management_document_digest
from bbx_contracts.runner_control import BoardDispatchProof, RunnerOwnerRegistration
from bbx_runtime.clients import BlackboardClient
from fastapi import HTTPException
from sqlalchemy import func, select
from test_pi_model_ledger_integration import pi_app as pi_app
from test_pi_model_ledger_integration import pi_provider_origin as pi_provider_origin
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url

pytestmark = pytest.mark.integration


async def install_board(pi_app):
    app, http, tid, model, agents, _, started, user = pi_app
    engine = app.state.engine
    control = RunnerControl(engine)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await control.repo.lock(conn, tid)
        await conn.execute(
            s.task_runner_resources.insert().values(
                task_id=tid,
                operation_id=str(uuid4()),
                request_id=str(uuid4()),
                request_digest="a" * 64,
                request={},
                config={"execution_contract": EXPLORATION_CONTRACT},
                controller_generation=1,
                revision=1,
                namespace="fixture-only",
                state="ready",
                desired="active",
                hard_deadline=now + timedelta(minutes=5),
                sandbox_create_claimed=True,
                tenant_key_hash=hashlib.sha256(str(tid).encode()).hexdigest(),
                tenant_key_ciphertext="fixture-only",
                observation={},
                data={"execution_contract": EXPLORATION_CONTRACT, "mcp_transport": "direct-v2"},
                next_check_at=now,
                created_at=now,
            )
        )
        owner = RunnerOwnerRegistration(
            request_id=uuid4(), runner_id="fixture-runner", pod_uid="fixture-pod", expected_epoch=0
        )
        await control._register_owner(
            conn, tid, runner_json_text(owner.model_dump(mode="json")).encode()
        )
        proof = BoardDispatchProof(
            agent_id=agents[0],
            run_number=1,
            started_at=started,
            instructions="Inert fixture",
            input="Summarize these local notes",
        )
        first = await board_dispatch(conn, tid, control, proof)
    return app, http, tid, model, agents, started, user, control, now, proof, first


async def test_original_ordinary_dispatch_and_closing_fence(pi_app):
    app, http, tid, model, agents, started, user, control, now, proof, first = await install_board(
        pi_app
    )
    engine = app.state.engine
    async with engine.begin() as conn:
        await control.repo.lock(conn, tid)
        same_instant = started.astimezone(timezone(timedelta(hours=8)))
        assert (
            await board_dispatch(
                conn,
                tid,
                control,
                proof.model_copy(
                    update={"started_at": same_instant, "input": "Newer coordinator view"}
                ),
            )
            == first
        )
        commands = (
            await conn.execute(
                select(s.runner_commands.c.kind, s.runner_commands.c.message_proof)
                .where(s.runner_commands.c.task_id == tid)
                .order_by(s.runner_commands.c.command_seq)
            )
        ).all()
        assert commands == [("prepare", None), ("input", {"board_run_start": True})]
        assert not await conn.scalar(
            select(func.count()).select_from(s.ctf_members).where(s.ctf_members.c.task_id == tid)
        )

        await conn.execute(s.tasks.update().where(s.tasks.c.id == tid).values(status="closing"))
        await require_board_run(conn, tid, proof.agent_id, proof, action="control_binding")
        with pytest.raises(HTTPException, match="closing"):
            await require_board_run(conn, tid, proof.agent_id, proof, action="model_send")
        await conn.execute(
            s.agent_runs.update()
            .where(s.agent_runs.c.task_id == tid, s.agent_runs.c.id == proof.agent_id)
            .values(status="concluding")
        )
        await require_board_run(conn, tid, proof.agent_id, proof, action="active_binding")
        with pytest.raises(HTTPException, match="closing"):
            await require_board_run(conn, tid, proof.agent_id, proof, action="model_send")
        with pytest.raises(HTTPException, match="not current"):
            await require_board_run(
                conn,
                tid,
                proof.agent_id,
                proof.model_copy(update={"run_number": 2}),
                action="active_binding",
            )
    url = f"/api/tasks/{tid}/runtime/runner-bootstrap"
    assert (await http.get(url)).status_code == 401
    response = await http.get(
        url,
        headers={"Authorization": "Bearer " + app.state.settings.service_token.get_secret_value()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["execution_contract"] == EXPLORATION_CONTRACT

    # Commit a final Close through the fenced domain tool without a second model turn.
    service = app.state.board_service
    aid = await service.register_agent(tid, "close", close_mode="final")

    class ReportObjects:
        def __init__(self):
            self.content = {}

        async def put(self, uri, content, *, content_type):
            assert content_type == "text/markdown"
            self.content[uri] = content

        async def exists(self, uri):
            return uri in self.content

        async def stream(self, uri):
            yield self.content[uri]

    reports = ReportObjects()
    service.objects = reports
    async with engine.begin() as conn:
        await control.repo.lock(conn, tid)
        agent = (
            (
                await conn.execute(
                    select(s.agent_runs).where(
                        s.agent_runs.c.task_id == tid, s.agent_runs.c.id == aid
                    )
                )
            )
            .mappings()
            .one()
        )
        owner = await control._current_owner(conn, tid)
        proof = BoardDispatchProof(
            agent_id=aid,
            run_number=1,
            started_at=agent["started_at"],
            instructions="Close the local note task",
            input="Submit the checked report",
        )
        final = await board_dispatch(conn, tid, control, proof)
        binding_id = final["binding_id"]
        cursor = {
            "offset": 1,
            "seq": 1,
            "hash": "a" * 64,
            "leaf_id": "fixture-leaf",
            "anchor_offset": 0,
            "anchor_size": 1,
            "anchor_hash": "a" * 64,
        }
        await conn.execute(
            s.runner_bindings.update()
            .where(s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == binding_id)
            .values(
                status="prepared",
                sdk_session_id="fixture-sdk",
                cursor=cursor,
                permit_expires_at=now + timedelta(seconds=60),
            )
        )
        await conn.execute(
            s.runner_commands.update()
            .where(
                s.runner_commands.c.task_id == tid,
                s.runner_commands.c.request_id == final["input_request_id"],
            )
            .values(status="applied")
        )
        inv = "d" * 64
        await conn.execute(
            s.runner_calls.insert().values(
                task_id=tid,
                id="tool:" + inv,
                kind="tool",
                binding_id=binding_id,
                owner_epoch=owner["epoch"],
                state="dispatched",
                concurrency_active=True,
                provider_key="business",
                data={"tool_name": "submit_close", "sdk_session_id": "fixture-sdk"},
                created_at=now,
            )
        )
    identity = GatewayIdentity(
        RunnerFence.model_validate(fence(owner)),
        binding_id,
        owner["token_version"],
        "business-mcp",
        int(now.timestamp()) + 60,
        "close",
    )
    principal = Principal(
        "agent",
        aid,
        task_id=tid,
        agent_id=aid,
        runtime_context=RuntimeBusinessContext(identity, "fixture-sdk", cursor, inv),
    )
    payload = {
        "report": "Inert local report",
        "verdicts": [
            {
                "id": "A1",
                "verdict": "unmet",
                "reason": "Fixture",
                "missing": "Nothing further is run",
            }
        ],
    }
    writes = await invoke(app, tid, principal, "submit_close", payload)
    assert isinstance(writes, list)
    result_drain = await control.poll(
        tid, RunnerIdentity(identity.fence, identity.token_version), wait_seconds=0
    )
    assert result_drain["lease_renewals"] == []
    assert all(
        json.loads(cmd["payload_json"])["kind"] == "close"
        and json.loads(cmd["payload_json"])["binding_id"] != binding_id
        for cmd in result_drain["commands"]
    )
    await app.state.runner_gateway.save_business_result(tid, inv, {"isError": False, "content": []})
    assert {event["type"] for event in writes} >= {
        "acceptance.judged",
        "task.finished",
        "agent.finished",
    }
    assert reports.content == {f"reports/{tid}.md": b"Inert local report"}
    state = await service.state(tid)
    assert state["task"]["status"] == state["agents"][aid]["status"] == "finished"
    stopped = await app.state.native_runner_bootstrap.view(tid)
    assert stopped["desired"] == "stopped" and stopped["state"] == "stopping"
    with pytest.raises(HTTPException):
        await invoke(app, tid, principal, "submit_close", payload)

    # Exercise the public mode/start boundaries using a synthetic publication object.
    app.state.board_runner_publication = BlackboardRunnerPublication.model_validate(
        {
            "revision": 1,
            "runtime_resource_config": {
                "board_network": {"mode": "default"},
                "traffic_sandbox_image": "fixture-sandbox@sha256:" + "d" * 64,
            },
            "acceptance": {"file": "fixture.json", "sha256": "a" * 64},
        }
    )
    assert (await http.get("/api/task-creation-capabilities", headers=user)).json()["blackboard"][
        "available"
    ]
    created = await http.post(
        "/api/tasks",
        headers=user,
        json={
            "mode": "blackboard",
            "goal": "A second inert note",
            "acceptance": [{"id": "A1", "desc": "Review local notes"}],
            "budget": {"currency": "USD", "max_cost": "1", "max_minutes": 5},
            "agent_profile": "default",
            "pi_model": {"model_id": model["id"], "model_revision": 1},
        },
    )
    assert created.status_code == 200, created.text
    new_id = created.json()["id"]
    shared = app.state.native_runner_bootstrap
    assert new_id not in await shared.pending()
    start = await http.post(f"/api/tasks/{new_id}/start", headers=user)
    assert start.status_code == 200 and start.json()["status"] == "provisioning", start.text
    assert new_id in await shared.pending()
    increase = f"/api/tasks/{new_id}/budget"
    assert (
        await http.post(increase, headers=user, json={"additional_cost": "1", "currency": "USD"})
    ).status_code == 422
    request_a, request_b = str(uuid4()), str(uuid4())
    for key, amount in ((request_a, "1"), (request_b, "1"), (request_a, "1.00")):
        response = await http.post(
            increase,
            headers=user,
            json={"request_id": key, "additional_cost": amount, "currency": "USD"},
        )
        assert response.status_code == 200, response.text
    assert response.json()["budget"]["max_cost"] == "3"

    # Preserve the previous execution on explicit resume; this is not a PVC restore fixture.
    archive = f"workspace/{tid}.tar.zst"
    reports.content[archive] = b"fixture archive bytes"
    archive_sha = hashlib.sha256(reports.content[archive]).hexdigest()
    pvc = uuid4()
    table = s.task_runner_resources
    async with engine.begin() as conn:
        await control.repo.lock(conn, tid)
        record = (await conn.execute(select(table).where(table.c.task_id == tid))).mappings().one()
        await conn.execute(
            s.runner_owners.update()
            .where(s.runner_owners.c.task_id == tid)
            .values(current=False, revoked=True)
        )
        await conn.execute(
            table.update()
            .where(table.c.task_id == tid)
            .values(
                state="exited",
                request={"publication": app.state.board_runner_publication.frozen()},
                data={**record["data"], "owner_epoch": owner["epoch"], "exit_confirmed": True},
                observation={"workspace_pvc": {"uid": str(pvc)}},
            )
        )
        task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
        ended = await shared._get(conn, tid)
        await shared._finalize_board_exit(conn, tid, ended, task)
        await conn.execute(table.update().where(table.c.task_id == tid).values(state="stopped"))
    service_headers = {
        "Authorization": "Bearer " + app.state.settings.service_token.get_secret_value()
    }
    body = {
        "uri": archive,
        "size": len(reports.content[archive]),
        "fallback": "none",
        "runner_archive": {
            "operation_id": record["operation_id"],
            "resource_generation": 1,
            "workspace_pvc_uid": str(uuid4()),
            "run_number": 1,
            "sha256": archive_sha,
        },
    }
    archive_route = f"/api/tasks/{tid}/archive"
    assert (await http.post(archive_route, headers=service_headers, json=body)).status_code == 409
    body["runner_archive"]["workspace_pvc_uid"] = str(pvc)
    pending = await http.post(archive_route, headers=service_headers, json=body)
    assert pending.status_code == 409 and "projection" in pending.text
    raw = b"\n"
    source_uri = f"traces/{tid}/{aid}/{binding_id}/{cursor['hash']}.jsonl"
    reports.content[source_uri] = raw
    projection = {
        "format": "pi-journal-v1",
        "binding_id": binding_id,
        "sdk_session_id": "fixture-sdk",
        "cursor": cursor,
        "run_number": 1,
        "transcript_uri": source_uri,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }
    saved = await http.put(
        f"/api/tasks/{tid}/agents/{aid}/session", headers=service_headers, json=projection
    )
    assert saved.status_code == 200, saved.text
    stale = await http.post(archive_route, headers=service_headers, json=body)
    assert stale.status_code == 409 and "inventory" in stale.text
    body["runner_archive"]["journals_sha256"] = management_document_digest(
        {"journals": [{"agent_id": aid, "projection": projection}]}
    )
    archived = await http.post(archive_route, headers=service_headers, json=body)
    assert archived.status_code == 200, archived.text
    cleanup = await http.post(f"/api/tasks/{tid}/cleanup-ready", headers=service_headers)
    assert cleanup.status_code == 200, cleanup.text
    resume = await http.post(
        f"/api/tasks/{tid}/resume",
        headers=user,
        json={
            "request_id": str(uuid4()),
            "additional_cost": "1",
            "currency": "USD",
            "additional_minutes": 1,
        },
    )
    assert resume.status_code == 200, resume.text
    async with engine.connect() as conn:
        new = await shared._get(conn, tid)
        old = await conn.scalar(
            select(s.task_runs.c.workspace_uri).where(
                s.task_runs.c.task_id == tid, s.task_runs.c.run_number == 1
            )
        )
    assert new["operation_id"] != record["operation_id"] and new["data"]["resource_generation"] == 2
    assert new["data"]["board_workspace_source"]["sha256"] == archive_sha and old == archive
    assert not new["data"].get("board_workspace_restored")


async def test_full_file_evidence_uses_original_host_stream_and_not_model_hash(pi_app):
    app, http, tid, _, agents, started, user, control, now, run, first = await install_board(pi_app)
    aid, bid = agents[0], first["binding_id"]

    class Files:
        def __init__(self):
            self.content = {}

        async def put(self, uri, body, *, content_type, length=None):
            raw = body if isinstance(body, bytes) else body.read()
            assert length in (None, len(raw))
            self.content[uri] = raw

        async def exists(self, uri):
            return uri in self.content

        async def stream(self, uri):
            yield self.content[uri]

    files = Files()
    app.state.objects = app.state.board_service.objects = files
    report_key = "fixture-only-report-key"
    read_id, fact_id = "a" * 64, "b" * 64
    path = "/workspace/agents/local-note.txt"
    journal = b'{"type":"session","id":"file-sdk"}\n'
    cursor = {
        "offset": len(journal),
        "seq": 1,
        "hash": "a" * 64,
        "leaf_id": "leaf",
        "anchor_offset": 0,
        "anchor_size": 1,
        "anchor_hash": "a" * 64,
    }
    async with app.state.engine.begin() as conn:
        await control.repo.lock(conn, tid)
        owner = await control._current_owner(conn, tid)
        resource = await conn.scalar(
            select(s.task_runner_resources.c.data).where(s.task_runner_resources.c.task_id == tid)
        )
        await conn.execute(
            s.task_runner_resources.update()
            .where(s.task_runner_resources.c.task_id == tid)
            .values(
                data={**resource, "owner_epoch": owner["epoch"]},
                sandbox_host={"report_key_hash": hashlib.sha256(report_key.encode()).hexdigest()},
            )
        )
        await conn.execute(
            s.runner_bindings.update()
            .where(s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == bid)
            .values(
                status="prepared",
                sdk_session_id="file-sdk",
                cursor=cursor,
                permit_expires_at=now + timedelta(seconds=60),
            )
        )
        await conn.execute(
            s.runner_calls.insert(),
            [
                {
                    "task_id": tid,
                    "id": "tool:" + iid,
                    "kind": "tool",
                    "binding_id": bid,
                    "owner_epoch": owner["epoch"],
                    "state": "dispatched",
                    "concurrency_active": True,
                    "provider_key": provider,
                    "data": {
                        "tool_name": name,
                        "sdk_admitted": True,
                        "sandbox_binding": {"role": "explore", "agent_id": aid},
                        "arguments_ciphertext": app.state.runner_gateway.cipher.encrypt(
                            runner_json_text({"path": path}).encode()
                        ).decode(),
                    },
                    "created_at": now,
                }
                for iid, provider, name in (
                    (read_id, "sandbox", "read_file"),
                    (fact_id, "business", "post_fact"),
                )
            ],
        )
    data = b"local evidence\n" * 5000
    assert len(data) > 65536
    route = f"/api/tasks/{tid}/sandbox-host/calls/{read_id}"
    headers = {"X-BBX-Sandbox-Report": report_key, "Content-Type": "application/octet-stream"}
    denied = await http.put(
        route + "/evidence", headers={**headers, "X-BBX-Sandbox-Report": "wrong"}, content=data
    )
    assert denied.status_code == 401
    upload = await http.put(route + "/evidence", headers=headers, content=data)
    assert upload.status_code == 200, upload.text
    assert upload.json()["sha256"] == hashlib.sha256(data).hexdigest()
    assert (await http.get(route + "/evidence", headers=headers)).json() == upload.json()
    result = {
        "isError": False,
        "content": [],
        "structuredContent": {
            "result": {
                "path": path,
                "truncated": True,
                "offset": 0,
                "content": data[:65536].decode(),
                "data_base64": base64.b64encode(data[:65536]).decode(),
                "retained_bytes": 65536,
            }
        },
    }
    saved = await http.post(
        route + "/result", headers={"X-BBX-Sandbox-Report": report_key}, json={"result": result}
    )
    assert saved.status_code == 200, saved.text
    identity = GatewayIdentity(
        RunnerFence.model_validate(fence(owner)),
        bid,
        owner["token_version"],
        "business-mcp",
        int(now.timestamp()) + 60,
        "explore",
    )
    principal = Principal(
        "agent",
        aid,
        task_id=tid,
        agent_id=aid,
        runtime_context=RuntimeBusinessContext(identity, "file-sdk", cursor, fact_id),
    )
    fact = {
        "kind": "observation",
        "statement": "Local note contents verified",
        "evidence": [{"type": "file", "path": path, "summary": "Original local bytes"}],
    }
    written = await invoke(app, tid, principal, "post_fact", fact)
    assert isinstance(written, dict)
    state = await app.state.board_service.state(tid)
    evidence = state["facts"][written["id"]]["evidence"][0]
    assert evidence["size"] == len(data) and files.content[evidence["uri"]] == data

    journal_uri = f"traces/{tid}/{aid}/{bid}/{cursor['hash']}.jsonl"
    files.content[journal_uri] = journal
    proof = {
        "format": "pi-journal-v1",
        "binding_id": bid,
        "sdk_session_id": "file-sdk",
        "cursor": cursor,
        "run_number": 1,
        "transcript_uri": journal_uri,
        "sha256": hashlib.sha256(journal).hexdigest(),
        "size": len(journal),
    }
    journal_url = f"/api/tasks/{tid}/agents/{aid}/session"
    service_headers = {
        "Authorization": "Bearer " + app.state.settings.service_token.get_secret_value()
    }
    assert (await http.put(journal_url, headers=user, json=proof)).status_code == 403
    assert (
        await http.put(journal_url, headers=service_headers, json={**proof, "sha256": "0" * 64})
    ).status_code == 409
    original = await http.put(journal_url, headers=service_headers, json=proof)
    assert original.status_code == 200, original.text
    assert (
        await http.put(journal_url, headers=service_headers, json=proof)
    ).json() == original.json()
    native = await http.get(journal_url, headers=user, params={"journal": True, "run_number": 1})
    assert native.status_code == 200 and native.json()["runner_session"] == proof
    source = await http.get(
        f"/api/tasks/{tid}/runtime/runner-bootstrap",
        headers=service_headers,
        params={"journals": True},
    )
    assert source.status_code == 200 and source.json()["board_journals"][0]["cursor"] == cursor
    downloaded = await http.get(native.json()["download"], headers=user)
    assert downloaded.status_code == 200 and downloaded.content == journal
    sender = BlackboardClient(
        str(http.base_url), app.state.settings.service_token.get_secret_value(), http_client=http
    )
    assert await sender.get_board_review_journal(str(tid), aid, 1) == journal
    async with app.state.engine.begin() as conn:
        await control.repo.lock(conn, tid)
        await conn.execute(s.tasks.update().where(s.tasks.c.id == tid).values(status="finished"))
        await conn.execute(
            s.agent_runs.update().where(s.agent_runs.c.task_id == tid).values(status="finished")
        )
    archived = await app.state.conversations.export_archive(tid)
    assert archived["board_journals"] == [{"agent_id": aid, "projection": proof}]
    async with app.state.engine.begin() as conn:
        await control.repo.lock(conn, tid)
        await conn.execute(s.tasks.update().where(s.tasks.c.id == tid).values(run_number=2))
    later_archive = await app.state.conversations.export_archive(tid)
    assert later_archive["run_number"] == 2 and later_archive["board_journals"] == []
    assert await sender.get_board_review_journal(str(tid), aid, 1) == journal
