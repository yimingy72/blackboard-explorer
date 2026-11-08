"""One original Worker uses actual Pi to run a foreground Sandbox helper."""

import argparse
import asyncio
import json
import runpy
from datetime import UTC, datetime
from pathlib import Path

from common import FINAL_ATTEMPT, ROOT, STATE, kubectl


def tool_results(messages):
    calls, results = {}, {}
    for message in messages:
        for call in message.get("tool_calls") or ():
            calls[call["id"]] = call["function"]["name"]
        if message.get("role") != "tool":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
        try:
            content = json.JSONDecoder().raw_decode(content.strip())[0]
        except (ValueError, AttributeError):
            pass
        results[calls.get(message.get("tool_call_id"))] = content
    return results


class HelperProbe:
    prefix = "p4g"
    state = STATE
    launch_options = {"helper_foreground": True}
    resource_options = {"journal_writer_lock": True}

    def __init__(self, filtered=False, scoped=False):
        self.models = {"lead": 0, "worker": 0, "helper": 0}
        self.parent_received = False
        self.child_model_entered = asyncio.Event()
        self.child_gate = asyncio.Event()
        self.filtered = filtered or scoped
        if scoped:
            self.gateway_name = "bbx-p4g-gateway-scoped"
            self.gateway_owner = "p4g-scoped"
            self.result_directory = "joint-final" if FINAL_ATTEMPT else "joint-scoped"
            if FINAL_ATTEMPT:
                self.task_minutes = 15
        elif filtered:
            self.gateway_name = "bbx-p4g-gateway-filtered"
            self.result_directory = "joint-filtered"

    async def tool(self, body, names):
        assert not {"bash", "read", "write", "edit", "codemode", "subagent", "bg_wait"} & names
        prior = tool_results(body["messages"])
        if "board_dispatch_challenge" in names:
            assert "delegate_helper" not in names
            self.models["lead"] += 1
            if "board_create_challenge" not in prior:
                return "board_create_challenge", {
                    "title": "P4g local helper evidence",
                    "description": "Solve a synthetic marker.",
                }
            if "board_dispatch_challenge" not in prior:
                challenge = prior["board_create_challenge"]
                return "board_dispatch_challenge", {
                    "challenge_id": challenge["id"],
                    "expected_revision": challenge["revision"],
                    "action": "ready",
                    "readiness": "no_external_materials",
                    "priority": 0,
                }
            return None
        if "delegate_helper" in names:
            self.models["worker"] += 1
            if "delegate_helper" not in prior:
                return "delegate_helper", {
                    "goal": "Print the synthetic marker in your approved Sandbox.",
                    "input_refs": [],
                    "expected_output": "One marker.",
                }
            assert "Synthetic Sandbox receipt complete." in str(prior["delegate_helper"])
            self.parent_received = True
            return None
        assert names and names <= {"exec_command", "read_file"}
        self.models["helper"] += 1
        if "exec_command" not in prior:
            if self.filtered:
                self.child_model_entered.set()
                await asyncio.wait_for(self.child_gate.wait(), timeout=15)
            return "exec_command", {
                "command": "printf 'p4g-foreground-helper\\n'; id -u",
                "cwd": "/workspace",
                "timeout_seconds": 5,
            }
        return None

    async def trace(self, tid, app, engine):
        from bbx_blackboard.store import schema as tables
        from sqlalchemy import select

        destination = STATE / self.result_directory / "readiness-timeline.jsonl"
        last = None
        while True:
            record = await app.state.native_runner_bootstrap.view(tid)
            async with engine.connect() as conn:
                bindings = tables.runner_bindings
                rows = (
                    (
                        await conn.execute(
                            select(
                                bindings.c.id,
                                bindings.c.binding,
                                bindings.c.status,
                                bindings.c.permit_revision,
                                bindings.c.sdk_session_id,
                                bindings.c.cursor,
                            ).where(bindings.c.task_id == tid)
                        )
                    )
                    .mappings()
                    .all()
                )
                calls = tables.runner_calls
                trace_calls = (
                    await conn.execute(
                        select(calls.c.kind, calls.c.provider_key, calls.c.state).where(
                            calls.c.task_id == tid
                        )
                    )
                ).all()
                commands = tables.runner_commands
                trace_commands = (
                    await conn.execute(
                        select(commands.c.kind, commands.c.binding_id, commands.c.status).where(
                            commands.c.task_id == tid
                        )
                    )
                ).all()
                receipts = tables.runner_receipts
                trace_receipts = (
                    await conn.execute(
                        select(
                            receipts.c.kind,
                            receipts.c.binding_id,
                            receipts.c.response["status"].astext,
                        ).where(receipts.c.task_id == tid)
                    )
                ).all()
            expected_pod = record["observation"].get("runner", {}).get("pod_uid")
            mounted = None
            if expected_pod:
                try:
                    pod = json.loads(
                        await asyncio.to_thread(
                            kubectl,
                            "-n",
                            record["namespace"],
                            "get",
                            "pod",
                            "bbx-runner",
                            "-o",
                            "json",
                        )
                    )
                    if pod["metadata"]["uid"] == expected_pod:
                        mounted = json.loads(
                            await asyncio.to_thread(
                                kubectl,
                                "-n",
                                record["namespace"],
                                "exec",
                                "bbx-runner",
                                "-c",
                                "runner",
                                "--",
                                "node",
                                "-e",
                                "const f=require('fs'),p='/var/run/bbx/credentials.json';"
                                "console.log(JSON.stringify(f.existsSync(p)?Object.keys(JSON.parse(f.readFileSync(p))).sort():[]))",
                            )
                        )
                except (RuntimeError, ValueError):
                    pass
            snapshot = {
                "task_id": str(tid),
                "state": record["state"],
                "last_error": record["last_error"],
                "hard_deadline": str(record["hard_deadline"]),
                "runner_uid": expected_pod,
                "host_ready": record["data"].get("sandbox_host_ready"),
                "mounted_credential_refs": mounted,
                "bindings": [
                    {
                        "binding_id": b["id"],
                        "agent": b["binding"]["agent_id"],
                        "status": b["status"],
                        "permit_revision": b["permit_revision"],
                        "sdk_confirmed": bool(b["sdk_session_id"]),
                        "journal_confirmed": bool(b["cursor"]),
                    }
                    for b in rows
                ],
                "command_status": [list(r) for r in trace_commands],
                "receipt_status": [list(r) for r in trace_receipts],
                "call_status": [list(r) for r in trace_calls],
            }
            if snapshot != last:
                with destination.open("a") as output:
                    output.write(
                        json.dumps({"at": datetime.now(UTC).isoformat(), **snapshot}) + "\n"
                    )
                last = snapshot
            await asyncio.sleep(5)

    async def verify(self, tid, app, engine, wait, evidence):
        trace = asyncio.create_task(self.trace(tid, app, engine)) if FINAL_ATTEMPT else None
        try:
            await self.verify_main(tid, app, engine, wait, evidence)
        except TimeoutError:
            if not FINAL_ATTEMPT:
                raise
            (STATE / self.result_directory / "awaiting-diagnosis.json").write_text(
                json.dumps(
                    {
                        "task_id": str(tid),
                        "at": datetime.now(UTC).isoformat(),
                        "state": "observer_timeout_original_environment_retained",
                    }
                )
                + "\n"
            )
            await asyncio.Event().wait()
        finally:
            if trace:
                trace.cancel()
                await asyncio.gather(trace, return_exceptions=True)

    async def verify_main(self, tid, app, engine, wait, evidence):
        import httpx
        from bbx_blackboard.gateway_auth import issue_gateway_credentials
        from bbx_blackboard.store import schema as tables
        from sqlalchemy import select

        async def ready():
            record = await app.state.native_runner_bootstrap.view(tid)
            evidence["last_helper_task"] = {
                "state": record["state"],
                "last_error": record["last_error"],
            }
            async with engine.connect() as conn:
                task = (
                    await conn.execute(
                        select(tables.tasks.c.ctf_control).where(tables.tasks.c.id == tid)
                    )
                ).scalar_one()
                calls = tables.runner_calls
                call = (
                    (
                        await conn.execute(
                            select(calls).where(
                                calls.c.task_id == tid,
                                calls.c.provider_key == "helper",
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    call is None
                    or call["state"] != "completed"
                    or task.get("native_helpers_active")
                    or not self.parent_received
                ):
                    return None
                bindings = tables.runner_bindings
                helper = (
                    (
                        await conn.execute(
                            select(bindings).where(
                                bindings.c.task_id == tid,
                                bindings.c.id == call["data"]["binding_id"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                commands = tables.sandbox_command_records
                command = (
                    (
                        await conn.execute(
                            select(commands).where(
                                commands.c.task_id == tid,
                                commands.c.binding_id == helper["id"],
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                models = (
                    (
                        await conn.execute(
                            select(calls).where(
                                calls.c.task_id == tid,
                                calls.c.kind == "model",
                                calls.c.binding_id == helper["id"],
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                budget = (
                    (
                        await conn.execute(
                            select(
                                tables.native_task_budgets.c.native_resource_id,
                                tables.native_task_budgets.c.accounting_source_id,
                            ).where(
                                tables.native_task_budgets.c.task_id == tid,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
            if (
                not command
                or len(models) != 2
                or not all(item["data"].get("fee_state") == "known" for item in models)
            ):
                return None
            assert all(
                all(
                    item["data"][key] == budget[key]
                    for key in ("native_resource_id", "accounting_source_id")
                )
                and item["data"]["transport"]["phase"] == "response_ended"
                for item in models
            )
            return task, call, helper, command, budget

        if self.filtered:

            async def child_entered():
                return self.child_model_entered.is_set()

            await wait(child_entered, 660 if FINAL_ATTEMPT else 400)
            try:
                async with engine.connect() as conn:
                    owner = (
                        (
                            await conn.execute(
                                select(tables.runner_owners).where(
                                    tables.runner_owners.c.task_id == tid,
                                    tables.runner_owners.c.current.is_(True),
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    calls = tables.runner_calls
                    call = (
                        (
                            await conn.execute(
                                select(calls).where(
                                    calls.c.task_id == tid,
                                    calls.c.provider_key == "helper",
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    binding = (
                        (
                            await conn.execute(
                                select(tables.runner_bindings).where(
                                    tables.runner_bindings.c.task_id == tid,
                                    tables.runner_bindings.c.id == call["data"]["binding_id"],
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                deadline = call["data"]["authorized_wait_until_ms"]
                assert binding["status"] == "prepared" and binding["sdk_session_id"]
                assert call["state"] == "dispatched" and deadline > int(
                    datetime.now(UTC).timestamp() * 1000
                )
                capability = issue_gateway_credentials(
                    app.state.settings, owner, binding["id"], "helper", deadline_ms=deadline
                )["mcp"]["token"]
                backend = app.state.settings.runner_mcp_backend_token.get_secret_value()
                headers = {
                    "Authorization": "Bearer " + capability,
                    "x-bbx-gateway-backend": backend,
                    "x-bbx-sdk-session-id": binding["sdk_session_id"],
                }
                refused = {}
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://local"
                ) as client:
                    for method in ("initialize", "tools/list", "tools/call"):
                        response = await client.post(
                            f"/api/tasks/{tid}/business-mcp/",
                            headers=headers,
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": method,
                                "params": {"name": "board_create_challenge", "arguments": {}},
                            },
                        )
                        assert response.status_code == 403
                        assert (
                            response.json()["detail"]
                            == "Helper capability is scoped to the Sandbox target"
                        )
                        refused[method] = response.status_code
                evidence["helper_business_while_live"] = refused
                evidence["checks"]["live_helper_business_target_denied"] = True
            finally:
                self.child_gate.set()
        task, call, helper, command, budget = await wait(ready, 380)
        child, proof = helper["business"], call["data"]["terminal"]
        assert (
            proof["sdk_session_id"] == helper["sdk_session_id"]
            and proof["cursor"] == helper["cursor"]
        )
        assert proof["disposed"] and proof["journal_confirmed"]
        assert command["state"] == "completed" and command["data"]["scope_state"] == "sealed"
        assert command["data"]["stdout"].splitlines() == ["p4g-foreground-helper", "10000"]
        assert command["exit_code"] == 0
        assert command["data"]["binding"]["role"] == "helper"
        assert command["data"]["binding"]["parent_agent_id"] == child["parent_agent_id"]
        assert (
            task["native_worker_active"][child["parent_agent_id"]]["binding_id"]
            == call["binding_id"]
        )
        assert budget and self.models["helper"] == 2
        evidence["checks"].update(
            {
                "actual_parent_model_tool_and_foreground_plugin_child": True,
                "scoped_helper_sandbox_command_and_parent_attribution": True,
                "own_confirmed_child_journal_positive_dispose_and_scope_release": True,
                "one_task_budget_with_confirmed_helper_cost": True,
            }
        )
        evidence["helper"] = {
            "binding_id": helper["id"],
            "parent_binding_id": call["binding_id"],
            "sdk_session_id": helper["sdk_session_id"],
            "plugin_run_id": proof["plugin_run_id"],
            "sandbox_execution_id": command["execution_id"],
            "role": command["data"]["binding"]["role"],
            "parent_agent_id": child["parent_agent_id"],
            "models": self.models,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--stop-only", action="store_true")
    parser.add_argument("--resume-existing", action="store_true")
    attempts = parser.add_mutually_exclusive_group()
    attempts.add_argument("--after-first-refusal", action="store_true")
    attempts.add_argument("--scoped-attempt", action="store_true")
    args = parser.parse_args()
    harness = runpy.run_path(
        str(ROOT / "deploy/kubernetes/p3h/probe.py"), run_name="p4g_reused_native_harness"
    )
    asyncio.run(
        harness["main"](
            args.candidate.resolve(),
            args.stop_only or args.resume_existing,
            args.stop_only,
            workers=HelperProbe(args.after_first_refusal, args.scoped_attempt),
        )
    )
