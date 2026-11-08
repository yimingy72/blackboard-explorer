"""One supervised background child through Pi, the fixed gateway and original Sandbox."""

import argparse
import asyncio
import json
import runpy
from datetime import UTC, datetime
from pathlib import Path

from common import ROOT, STATE, kubectl

shared = runpy.run_path(str(ROOT / "deploy/kubernetes/p4g/probe.py"))
tool_results = shared["tool_results"]


class BackgroundProbe(shared["HelperProbe"]):
    prefix = "p4h"
    state = STATE
    launch_options = {"helper_background": True}
    resource_options = {"journal_writer_lock": True}

    def __init__(self):
        super().__init__(scoped=True)
        self.gateway_name = "bbx-p4h-gateway-scoped"
        self.gateway_owner = "p4h-scoped"
        self.result_directory = "joint"
        self.task_minutes = 15
        self.first_model, self.first_release = asyncio.Event(), asyncio.Event()
        self.second_model, self.second_release = asyncio.Event(), asyncio.Event()

    async def tool(self, body, names):
        assert not {"bash", "read", "write", "edit", "codemode", "subagent", "bg_wait"} & names
        if "board_dispatch_challenge" in names:
            assert "delegate_helper_background" not in names
            return await super().tool(body, names)
        if "delegate_helper_background" in names:
            self.models["worker"] += 1
            assert "delegate_helper" not in names
            prior = tool_results(body["messages"])
            if "delegate_helper_background" not in prior:
                return "delegate_helper_background", {
                    "goal": "Print the synthetic background helper marker.",
                    "input_refs": [],
                    "expected_output": "One marker.",
                }
            assert "accepted" in str(prior["delegate_helper_background"]).lower()
            notices = [
                message
                for message in body["messages"]
                if "An original background helper ended" in str(message.get("content"))
            ]
            if notices:
                assert len(notices) == 1 and "Synthetic Sandbox receipt complete." in str(
                    notices[0]
                )
                self.parent_received = True
            return None
        assert names and names <= {"exec_command", "read_file"}
        self.models["helper"] += 1
        if "exec_command" not in tool_results(body["messages"]):
            self.first_model.set()
            await self.first_release.wait()
            return "exec_command", {
                "command": "printf 'p4h-background-helper\\n'; id -u",
                "cwd": "/workspace",
                "timeout_seconds": 5,
            }
        self.second_model.set()
        await self.second_release.wait()
        return None

    async def verify(self, tid, app, engine, wait, evidence):
        timeline = asyncio.create_task(self.trace(tid, app, engine))
        try:
            await self.verify_background(tid, app, engine, wait, evidence)
        except Exception as error:
            (STATE / self.result_directory / "awaiting-diagnosis.json").write_text(
                json.dumps(
                    {
                        "task_id": str(tid),
                        "stage": evidence.get("background_stage"),
                        "failure_type": type(error).__name__,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                + "\n"
            )
            await asyncio.Event().wait()
        finally:
            self.first_release.set()
            self.second_release.set()
            timeline.cancel()
            await asyncio.gather(timeline, return_exceptions=True)

    async def verify_background(self, tid, app, engine, wait, evidence):
        from bbx_blackboard.store import schema as tables
        from sqlalchemy import select

        async def snapshot():
            record = await app.state.native_runner_bootstrap.view(tid)
            async with engine.connect() as conn:
                calls = (
                    (
                        await conn.execute(
                            select(tables.runner_calls).where(tables.runner_calls.c.task_id == tid)
                        )
                    )
                    .mappings()
                    .all()
                )
                bind = (
                    (
                        await conn.execute(
                            select(tables.runner_bindings).where(
                                tables.runner_bindings.c.task_id == tid
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                receipts = (
                    (
                        await conn.execute(
                            select(tables.runner_receipts).where(
                                tables.runner_receipts.c.task_id == tid
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                commands = (
                    (
                        await conn.execute(
                            select(tables.runner_commands).where(
                                tables.runner_commands.c.task_id == tid
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                sandbox = (
                    (
                        await conn.execute(
                            select(tables.sandbox_command_records).where(
                                tables.sandbox_command_records.c.task_id == tid
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
                            ).where(tables.native_task_budgets.c.task_id == tid)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                task = (
                    await conn.execute(
                        select(tables.tasks.c.ctf_control).where(tables.tasks.c.id == tid)
                    )
                ).scalar_one()
            helper = next((item for item in calls if item["provider_key"] == "helper"), None)
            if helper is None:
                return None
            by_id = {item["id"]: item for item in bind}
            parent = by_id[helper["binding_id"]]
            child = by_id[helper["data"]["binding_id"]]
            models = [
                item
                for item in calls
                if item["binding_id"] == child["id"] and item["kind"] == "model"
            ]
            acquired = next(
                (
                    item
                    for item in receipts
                    if item["kind"] == "acquire" and item["binding_id"] == child["id"]
                ),
                None,
            )
            return record, helper, parent, child, models, acquired, commands, sandbox, budget, task

        async def entered(event):
            return event.is_set()

        evidence["background_stage"] = "wait for original Worker preparation and admitted child"
        await wait(lambda: entered(self.first_model), 660)
        original = await snapshot()
        assert original is not None
        record, helper, parent, child, models, acquired, *_ = original
        assert helper["state"] == "completed" and helper["data"]["background"]["accepted"] is True
        assert len(models) == 1 and acquired and acquired["response"]["lease_revision"] == 1
        initial_expiry = acquired["response"]["lease_expires_at"]
        first_limit = datetime.fromisoformat(models[0]["data"]["authorized_until"]).timestamp()
        evidence["background_initial"] = {
            "task_deadline": str(record["hard_deadline"]),
            "original_child_expiry": initial_expiry,
            "first_model_deadline": first_limit,
            "parent_binding_id": parent["id"],
            "child_binding_id": child["id"],
        }
        node_check = (
            "const fs=require('fs'),p=process.argv[1];const b=fs.readFileSync(p);"
            "const lines=b.toString().trim().split('\\n').map(JSON.parse);"
            "console.log(JSON.stringify({size:b.length,ended:lines.some(e=>e.type==='message'&&"
            "e.message?.role==='assistant'&&JSON.stringify(e.message.content)"
            ".includes('Synthetic Sandbox receipt complete.'))}));"
        )

        async def original_parent_ended_and_child_renewed():
            state = await snapshot()
            latest, _, live_parent, live_child, *_ = state
            if (
                self.models["worker"] != 2
                or not live_parent["cursor"]
                or live_child["permit_revision"] <= 1
            ):
                return None
            pod_uid = await asyncio.to_thread(
                kubectl,
                "-n",
                latest["namespace"],
                "get",
                "pod",
                "bbx-runner",
                "-o",
                "jsonpath={.metadata.uid}",
            )
            assert pod_uid == record["observation"]["runner"]["pod_uid"]
            check = json.loads(
                await asyncio.to_thread(
                    kubectl,
                    "-n",
                    latest["namespace"],
                    "exec",
                    "bbx-runner",
                    "-c",
                    "runner",
                    "--",
                    "node",
                    "-e",
                    node_check,
                    f"/journal/{parent['id']}/session.jsonl",
                )
            )
            if check["ended"] and check["size"] == live_parent["cursor"]["offset"]:
                return live_child["permit_revision"]
            return None

        evidence["background_stage"] = (
            "parent normal turn finishes while the original child remains gated"
        )
        revision = await wait(
            original_parent_ended_and_child_renewed,
            max(1, first_limit - datetime.now(UTC).timestamp() - 3),
        )
        evidence["background_initial"]["renewed_revision_while_first_model_gated"] = revision
        self.first_release.set()
        await wait(lambda: entered(self.second_model), 28)
        state = await snapshot()
        assert len(state[4]) == 2
        second = max(state[4], key=lambda item: item["data"]["dispatched_at"])
        second_limit = datetime.fromisoformat(second["data"]["authorized_until"]).timestamp()
        past_initial = initial_expiry + 1 - datetime.now(UTC).timestamp()
        if past_initial > 0:
            assert datetime.now(UTC).timestamp() + past_initial < second_limit - 3
            await asyncio.sleep(past_initial)
        assert datetime.now(UTC).timestamp() >= initial_expiry
        evidence["background_initial"]["second_model_deadline"] = second_limit
        evidence["background_initial"]["child_crossed_original_permit_at"] = datetime.now(
            UTC
        ).isoformat()
        self.second_release.set()

        async def delivered():
            state = await snapshot()
            _, call, _, _, models, _, commands, _, _, task = state
            bg = call["data"].get("background", {})
            if not bg.get("outcome_digest") or not bg.get("notice_id") or not self.parent_received:
                return None
            notices = [item for item in commands if item["input_id"] == bg["notice_id"]]
            if (
                len(notices) == 1
                and notices[0]["status"] == "applied"
                and notices[0]["native_proof"]
                and not task.get("native_helpers_active")
                and len(models) == 2
                and all(item["data"].get("fee_state") == "known" for item in models)
            ):
                return state
            return None

        evidence["background_stage"] = "exact completion after child positive close"
        _, call, parent, child, models, _, _, sandbox, budget, _ = await wait(delivered, 60)
        proof = call["data"]["terminal"]
        assert (
            proof["disposed"] and proof["journal_confirmed"] and proof["cursor"] == child["cursor"]
        )
        assert child["sdk_session_id"] != parent["sdk_session_id"]
        assert len(sandbox) == 1
        command = sandbox[0]
        assert command["binding_id"] == child["id"] and command["state"] == "completed"
        assert command["data"]["stdout"].splitlines() == ["p4h-background-helper", "10000"]
        assert command["data"]["scope_state"] == "sealed" and command["exit_code"] == 0
        assert command["data"]["binding"]["role"] == "helper"
        assert command["data"]["binding"]["parent_agent_id"] == parent["binding"]["agent_id"]
        assert all(
            all(
                item["data"][key] == budget[key]
                for key in ("native_resource_id", "accounting_source_id")
            )
            and item["data"]["transport"]["phase"] == "response_ended"
            for item in models
        )
        assert self.models["worker"] == 3 and self.models["helper"] == 2
        evidence["checks"].update(
            {
                "parent_ended_with_original_child_running": True,
                "child_crossed_initial_permit_with_real_renewal": True,
                "same_budget_and_actual_nonroot_helper_command": True,
                "positive_child_journal_scope_and_exact_parent_follow_up": True,
            }
        )
        evidence["background"] = {
            "notice_id": call["data"]["background"]["notice_id"],
            "child_sdk_session_id": child["sdk_session_id"],
            "plugin_run_id": proof["plugin_run_id"],
            "sandbox_execution_id": command["execution_id"],
            "model_counts": self.models,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--stop-only", action="store_true")
    args = parser.parse_args()
    harness = runpy.run_path(str(ROOT / "deploy/kubernetes/p3h/probe.py"))
    asyncio.run(
        harness["main"](
            args.candidate.resolve(), args.stop_only, args.stop_only, workers=BackgroundProbe()
        )
    )
