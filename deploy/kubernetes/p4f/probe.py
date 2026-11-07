"""Exercise one real native Task through frozen recovery on the private P4f cluster."""

import argparse
import asyncio
import json
import runpy
from pathlib import Path
from uuid import uuid4

from common import ROOT, STATE, kubectl


class RecoveryProbe:
    prefix = "p4f"
    state = STATE
    resource_options = {"journal_writer_lock": True}

    def __init__(self):
        self.generation = 1
        self.model_calls = {1: 0, 2: 0}

    async def tool(self, body, names):
        self.model_calls[self.generation] += 1
        tools = [item for item in body["messages"] if item.get("role") == "tool"]
        if tools:
            return None
        if self.model_calls[self.generation] != 1 or "exec_command" not in names:
            raise RuntimeError("Fresh Lead conversation or Sandbox tool contract changed")
        command = (
            "printf 'retained-workspace\\n' > /workspace/p4f-marker; printf 'old-epoch\\n'"
            if self.generation == 1
            else "cat /workspace/p4f-marker; printf 'new-epoch\\n'"
        )
        return "exec_command", {"command": command, "cwd": "/workspace", "timeout_seconds": 10}

    async def verify(self, tid, app, engine, wait, evidence):
        import httpx
        from bbx_blackboard.auth import issue_user_token
        from bbx_blackboard.store import schema as tables
        from sqlalchemy import select

        bootstrap = app.state.native_runner_bootstrap
        user = {"Authorization": "Bearer " + issue_user_token(app.state.settings, "alice")}
        url = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap/recovery"

        async def ready(generation):
            value = await bootstrap.view(tid)
            recovery = value["data"].get("recovery", {})
            evidence["last_recovery"] = {
                "state": value["state"],
                "resource_generation": value["data"].get("resource_generation", 1),
                "last_error": value.get("last_error"),
                "blocker": recovery.get("blocker"),
            }
            if (
                not value["data"].get("activated")
                or value["data"].get("resource_generation", 1) != generation
                or self.model_calls[generation] < 2
            ):
                return None
            epoch = value["data"]["owner_epoch"]
            async with engine.connect() as conn:
                command = (
                    (
                        await conn.execute(
                            select(tables.sandbox_command_records).where(
                                tables.sandbox_command_records.c.task_id == tid,
                                tables.sandbox_command_records.c.epoch == epoch,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                binding = (
                    (
                        await conn.execute(
                            select(tables.runner_bindings).where(
                                tables.runner_bindings.c.task_id == tid,
                                tables.runner_bindings.c.id == value["data"]["binding_id"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                calls = (
                    (
                        await conn.execute(
                            select(tables.runner_calls.c.state, tables.runner_calls.c.data).where(
                                tables.runner_calls.c.task_id == tid,
                                tables.runner_calls.c.owner_epoch == epoch,
                                tables.runner_calls.c.kind == "model",
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
                                tables.native_task_budgets.c.key_fingerprint,
                                tables.native_task_budgets.c.config,
                            ).where(tables.native_task_budgets.c.task_id == tid)
                        )
                    )
                    .mappings()
                    .one()
                )
            if (
                not command
                or command["state"] != "completed"
                or command["exit_code"] != 0
                or not binding["cursor"]
                or len(calls) != 2
                or any(
                    call["state"] != "completed" or call["data"].get("fee_state") != "known"
                    for call in calls
                )
            ):
                return None
            fees = await app.state.runner_gateway.native.view(tid)
            return value, command, binding, budget, fees

        original, old_cmd, old_binding, old_budget, old_fees = await wait(lambda: ready(1), 300)
        namespace = original["namespace"]
        evidence["recovery_original"] = {
            "runner": original["observation"]["runner"]["pod_uid"],
            "sandbox": original["observation"]["sandbox"]["pod_uid"],
            "epoch": original["data"]["owner_epoch"],
            "sdk": old_binding["sdk_session_id"],
            "scope_before_freeze": old_cmd["data"].get("scope_state"),
        }
        await asyncio.to_thread(
            kubectl,
            "-n",
            namespace,
            "exec",
            "bbx-runner",
            "-c",
            "runner",
            "--",
            "sh",
            "-c",
            'flock -n /run/bbx-journal-lock true; test "$?" -eq 1',
        )
        evidence["checks"]["original_live_writer_rejects_competing_lock"] = True
        await asyncio.to_thread(
            kubectl,
            "-n",
            namespace,
            "exec",
            "bbx-runner",
            "-c",
            "runner",
            "--",
            "node",
            "-e",
            "process.kill(1, 'SIGTERM')",
        )

        async def frozen():
            value = await bootstrap.view(tid)
            return value if value["state"] == "frozen" else None

        freeze = await wait(frozen, 75)
        self.generation = 2
        incident = freeze["data"]["recovery"]["incident_id"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            response = await client.post(
                url,
                headers=user,
                json={
                    "request_id": str(uuid4()),
                    "incident_id": incident,
                    "instruction": "Verify the same workspace from the new session.",
                },
            )
            if response.status_code != 200:
                raise RuntimeError(f"User recovery was refused ({response.status_code})")
            new, new_cmd, new_binding, new_budget, new_fees = await wait(lambda: ready(2), 300)
            status = (await client.get(url, headers=user)).raise_for_status().json()
        proof = (
            status["replacement_proof"]
            if "replacement_proof" in status
            else new["data"]["recovery_history"][-1]["replacement_proof"]
        )
        assert status["state"] == "resumed"
        assert (
            new["operation_id"] == original["operation_id"]
            and new["hard_deadline"] == original["hard_deadline"]
        )
        assert new["data"]["owner_epoch"] == original["data"]["owner_epoch"] + 1
        assert (
            new_binding["id"] != old_binding["id"]
            and new_binding["sdk_session_id"] != old_binding["sdk_session_id"]
        )
        assert (
            new["observation"]["runner"]["pod_uid"] != original["observation"]["runner"]["pod_uid"]
        )
        assert (
            new["observation"]["sandbox"]["pod_uid"]
            != original["observation"]["sandbox"]["pod_uid"]
        )
        assert new_cmd["execution_id"] != old_cmd["execution_id"]
        assert new_cmd["data"]["stdout"] == "retained-workspace\nnew-epoch\n"
        for name in ("journal_pvc", "workspace_pvc", "journal_lock_id", "journal_storage"):
            assert new["observation"][name] == original["observation"][name]
        assert new["observation"]["journal_subdir"] == "epoch-2"
        assert proof["writer_released"] is True
        assert proof["work_copy"]["bindings"][0]["cursor"]["hash"] == old_binding["cursor"]["hash"]
        assert new_budget["native_resource_id"] == old_budget["native_resource_id"]
        assert new_budget["key_fingerprint"] == old_budget["key_fingerprint"]
        assert new_budget["config"]["threshold_usd"] == old_budget["config"]["threshold_usd"]
        from decimal import Decimal

        assert Decimal(new_fees["confirmed_usd"]) > Decimal(old_fees["confirmed_usd"]) > 0
        assert self.model_calls == {1: 2, 2: 2}
        current_pod = json.loads(
            await asyncio.to_thread(
                kubectl, "-n", namespace, "get", "pod", "bbx-runner", "-o", "json"
            )
        )
        runner = next(
            item for item in current_pod["spec"]["containers"] if item["name"] == "runner"
        )
        assert [
            mount["subPath"] for mount in runner["volumeMounts"] if mount["name"] == "journal"
        ] == [
            "epoch-2",
            ".bbx-writer.lock",
        ]
        assert runner["volumeMounts"][-1]["readOnly"] is True
        await asyncio.to_thread(
            kubectl,
            "-n",
            namespace,
            "exec",
            "bbx-runner",
            "-c",
            "runner",
            "--",
            "test",
            "-s",
            f"/journal/recovery-evidence/{old_binding['id']}.jsonl",
        )
        evidence["checks"].update(
            {
                "same_task_fresh_runner_sandbox_epoch_sdk_and_original_journal_copy": True,
                "native_budget_identity_threshold_and_accumulation_retained": True,
                "fresh_real_sandbox_command_read_original_workspace_no_old_replay": True,
            }
        )
        evidence["recovery_replacement"] = {
            "epoch": new["data"]["owner_epoch"],
            "runner": new["observation"]["runner"]["pod_uid"],
            "sandbox": new["observation"]["sandbox"]["pod_uid"],
            "sdk": new_binding["sdk_session_id"],
            "verifier_pod_uid": proof["verifier_pod_uid"],
            "old_confirmed_usd": old_fees["confirmed_usd"],
            "new_confirmed_usd": new_fees["confirmed_usd"],
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--stop-only", action="store_true")
    args = parser.parse_args()
    harness = runpy.run_path(
        str(ROOT / "deploy/kubernetes/p3h/probe.py"), run_name="p4f_reused_native_harness"
    )
    asyncio.run(
        harness["main"](
            args.candidate.resolve(),
            args.stop_only,
            args.stop_only,
            workers=RecoveryProbe(),
        )
    )
