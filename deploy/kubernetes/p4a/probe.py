"""One real Lead → automatic Workers → original scope drain → replacement Task."""

import argparse
import asyncio
import json
import re
import runpy
from pathlib import Path

from common import ROOT, STATE

TITLES = ("P4a A local marker", "P4a B local marker", "P4a C local marker")


def _content(value):
    if isinstance(value, list):
        return "\n".join(part.get("text", "") for part in value if isinstance(part, dict))
    return value if isinstance(value, str) else ""


def _json(value):
    text = _content(value).strip()
    if text.startswith(("{", "[")):
        try:
            return json.JSONDecoder().raw_decode(text)[0]
        except ValueError:
            pass
    return None


def _replies(messages):
    names = {}
    results = []
    for message in messages:
        for call in message.get("tool_calls") or ():
            names[call["id"]] = call["function"]["name"]
        if message.get("role") != "tool":
            continue
        result = _json(message.get("content"))
        if (
            isinstance(result, dict)
            and isinstance(result.get("status"), int)
            and result["status"] >= 400
        ):
            raise RuntimeError(f"P4a synthetic tool refused ({result['status']})")
        name = names.get(message.get("tool_call_id")) or message.get("name")
        results.append((name, result))
    return results


class WorkerProbe:
    state = STATE

    def __init__(self):
        self.wakes = set()
        self.executing = set()
        self.overlap = asyncio.Event()

    async def tool(self, body, names):
        messages = body["messages"]
        results = _replies(messages)
        inputs = [_json(m.get("content")) for m in messages if m.get("role") == "user"]
        if "board_dispatch_challenge" in names:
            return self._lead(results, inputs)
        assignment = next(
            (
                v
                for v in reversed(inputs)
                if isinstance(v, dict)
                and {"assignment_id", "challenge_id", "challenge_revision", "title"} <= v.keys()
            ),
            None,
        )
        if (
            assignment is None
            or assignment["title"] not in TITLES
            or "board_post_fact" not in names
        ):
            raise RuntimeError("The original Worker input or role scope is missing")
        if "board_create_challenge" in names or "board_dispatch_challenge" in names:
            raise RuntimeError("A Worker received Lead capability")
        tool = self._worker(assignment, results)
        if tool and tool[0] == "exec_command" and assignment["title"] in TITLES[:2]:
            self.executing.add(assignment["title"])
            if self.executing == set(TITLES[:2]):
                self.overlap.set()
            await asyncio.wait_for(self.overlap.wait(), timeout=12)
        return tool

    def _lead(self, results, inputs):
        created = {
            result["title"]: result
            for name, result in results
            if name == "board_create_challenge"
            and isinstance(result, dict)
            and result.get("title") in TITLES
        }
        for title in TITLES:
            if title not in created:
                return "board_create_challenge", {
                    "title": title,
                    "description": "Verify local shell identity without an external target.",
                }
        dispatched = {
            result["challenge"]["title"]
            for name, result in results
            if name == "board_dispatch_challenge"
            and isinstance(result, dict)
            and result.get("dispatch_status") == "ready"
        }
        for priority, title in enumerate(TITLES):
            if title not in dispatched:
                return "board_dispatch_challenge", {
                    "challenge_id": created[title]["id"],
                    "expected_revision": created[title]["revision"],
                    "action": "ready",
                    "readiness": "no_external_materials",
                    "priority": priority,
                }
        facts = {
            v["challenge_id"]: v
            for v in inputs
            if isinstance(v, dict)
            and {"challenge_id", "challenge_revision", "assignment_id", "fact_id"} <= v.keys()
        }
        self.wakes.update(facts)
        first, second = (created[t]["id"] for t in TITLES[:2])
        withdrawn = any(
            name == "board_dispatch_challenge"
            and isinstance(result, dict)
            and result.get("dispatch_status") in {"draining", "cancelled"}
            for name, result in results
        )
        if first in facts and second in facts and not withdrawn:
            return "board_dispatch_challenge", {
                "challenge_id": first,
                "expected_revision": facts[first]["challenge_revision"],
                "action": "withdraw",
            }
        return None

    def _worker(self, assignment, results):
        read = next((value for name, value in results if name == "board_challenges"), None)
        if read is None:
            return "board_challenges", {}
        cid = assignment["challenge_id"]
        if (
            not isinstance(read, list)
            or len(read) != 1
            or read[0].get("id") != cid
            or not read[0].get("description")
        ):
            raise RuntimeError("A Worker could not read exactly its original challenge")
        command = next((value for name, value in results if name == "exec_command"), None)
        if command is None:
            duration = 4 if assignment["title"] in TITLES[:2] else 0
            return "exec_command", {
                "command": (
                    "printf 'p4a-start='; date +%s%N; "
                    f"sleep {duration}; printf 'p4a-end='; date +%s%N; "
                    "id -u; cat /proc/self/cgroup"
                ),
                "cwd": "/workspace",
                "timeout_seconds": 20,
            }
        if command.get("exit_code") != 0 or not command.get("command_id"):
            raise RuntimeError("The original Worker command has no terminal execution receipt")
        if assignment["title"] != TITLES[2] and not any(
            name == "board_post_fact" for name, _ in results
        ):
            return "board_post_fact", {
                "challenge_id": cid,
                "expected_revision": read[0]["revision"],
                "kind": "observation",
                "statement": "Observed original solver identity and Assignment cgroup.",
                "evidence": [
                    {
                        "type": "command_output",
                        "summary": "Original local shell receipt",
                        "call_id": command["command_id"],
                    }
                ],
            }
        return None

    async def verify(self, tid, app, engine, wait, evidence):
        from bbx_blackboard.store import schema as tables
        from bbx_sandbox_execution.execution import AssignmentBinding
        from sqlalchemy import select

        async def collected():
            async with engine.connect() as conn:
                task = (
                    await conn.execute(
                        select(tables.tasks.c.ctf_control).where(tables.tasks.c.id == tid)
                    )
                ).scalar_one()
                challenges = (
                    (
                        await conn.execute(
                            select(tables.ctf_challenges.c.data).where(
                                tables.ctf_challenges.c.task_id == tid
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                commands = (
                    (
                        await conn.execute(
                            select(tables.sandbox_command_records).where(
                                tables.sandbox_command_records.c.task_id == tid,
                                tables.sandbox_command_records.c.role == "worker",
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                events = (
                    (
                        await conn.execute(
                            select(
                                tables.events.c.type,
                                tables.events.c.version,
                                tables.events.c.payload,
                            )
                            .where(
                                tables.events.c.task_id == tid,
                                tables.events.c.type.in_(
                                    [
                                        "ctf.native_worker.assigned",
                                        "ctf.native_worker.released",
                                        "ctf.native_worker.cleanup_changed",
                                    ]
                                ),
                            )
                            .order_by(tables.events.c.version)
                        )
                    )
                    .mappings()
                    .all()
                )
                model_rows = (
                    (
                        await conn.execute(
                            select(
                                tables.runner_calls.c.binding_id, tables.runner_calls.c.state
                            ).where(
                                tables.runner_calls.c.task_id == tid,
                                tables.runner_calls.c.kind == "model",
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                bindings = (
                    (
                        await conn.execute(
                            select(
                                tables.runner_bindings.c.id,
                                tables.runner_bindings.c.status,
                                tables.runner_bindings.c.sdk_session_id,
                            ).where(tables.runner_bindings.c.task_id == tid)
                        )
                    )
                    .mappings()
                    .all()
                )
            by_title = {c["title"]: c["id"] for c in challenges if c["title"] in TITLES}
            rows = {
                str(c["challenge_id"]): c
                for c in commands
                if c["state"] == "completed" and c["execution_id"]
            }
            evidence["last_worker_counts"] = {
                "known_challenges": len(by_title),
                "completed_commands": len(rows),
                "active": len(task.get("native_worker_active", {})),
                "ready": len(task.get("native_worker_ready", {})),
            }
            if set(by_title) == set(TITLES) and all(v in rows for v in by_title.values()):
                replacement = rows[by_title[TITLES[2]]]
                phases = [
                    m["state"] for m in model_rows if m["binding_id"] == replacement["binding_id"]
                ]
                if len(phases) >= 3 and all(phase == "completed" for phase in phases):
                    return [rows[by_title[t]] for t in TITLES], events, bindings, task
            return None

        commands, events, bindings, control = await wait(collected, 360)
        first, second, replacement = commands
        bind = {b["id"]: b for b in bindings}
        samples = []
        for record in commands:
            raw = record["data"]["stdout"]
            times = re.search(r"p4a-start=(\d+)\s+p4a-end=(\d+)", raw)
            assert times and "\n10000\n" in raw
            scope, _ = AssignmentBinding(**record["data"]["binding"]).scope_request()
            assert "0::/" + scope in raw and record["exit_code"] == 0
            samples.append(
                {
                    "agent_id": record["agent_id"],
                    "binding_id": record["binding_id"],
                    "assignment_id": record["assignment_id"],
                    "execution_id": record["execution_id"],
                    "scope_id": scope,
                    "sdk_session_id": bind[record["binding_id"]]["sdk_session_id"],
                    "start_ns": int(times[1]),
                    "end_ns": int(times[2]),
                }
            )
        assert (
            len({v["scope_id"] for v in samples})
            == len({v["sdk_session_id"] for v in samples})
            == 3
        )
        a, b, c = samples
        assert max(a["start_ns"], b["start_ns"]) < min(a["end_ns"], b["end_ns"])
        assert a["agent_id"] != b["agent_id"] and c["agent_id"] == a["agent_id"]
        assert (
            c["assignment_id"] != a["assignment_id"]
            and replacement["generation"] > first["generation"]
        )
        assert (
            first["data"]["scope_state"] == "sealed"
            and first["data"]["scope_execution_id"] != first["execution_id"]
        )
        assert bind[first["binding_id"]]["status"] == "closed"
        active = control["native_worker_active"]
        assert active[second["agent_id"]]["assignment_ref"] == second["assignment_id"]
        released = next(
            e["version"]
            for e in events
            if e["type"] == "ctf.native_worker.released"
            and e["payload"]["assignment_ref"] == first["assignment_id"]
        )
        assigned = next(
            e["version"]
            for e in events
            if e["type"] == "ctf.native_worker.assigned"
            and e["payload"]["assignment_ref"] == replacement["assignment_id"]
        )
        assert released < assigned
        assert {str(first["challenge_id"]), str(second["challenge_id"])} <= self.wakes
        observed = await app.state.native_runner_bootstrap.view(tid)
        evidence["checks"].update(
            {
                "real_two_worker_pi_sdk_sessions_and_command_overlap": True,
                "lead_fact_wake_and_original_scope_close": True,
                "replacement_only_after_release_with_new_assignment": True,
            }
        )
        evidence["worker_commands"] = samples
        evidence["release_and_replacement_versions"] = [released, assigned]
        evidence["original_scope_execution_id"] = first["data"]["scope_execution_id"]
        evidence["original_runner_pod_uid"] = observed["observation"]["runner"]["pod_uid"]


async def main(candidate, resume, stop_only):
    harness = runpy.run_path(
        str(ROOT / "deploy/kubernetes/p3h/probe.py"), run_name="p4a_reused_native_harness"
    )
    await harness["main"](candidate, resume, stop_only, workers=WorkerProbe())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-only", action="store_true")
    args = parser.parse_args()
    if args.resume != args.stop_only:
        parser.error("The original P4a Task may only resume for positive stop")
    asyncio.run(main(args.candidate.resolve(), args.resume, args.stop_only))
