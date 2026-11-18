"""Automatic admission holds; only confirmed stop and persistence release them."""

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from bbx_contracts.ctf import CtfFailureDiagnostic
from sqlalchemy import select

from bbx_blackboard.ctf_board import ensure
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import patch, row


class CtfFailureMixin:
    repo: Any
    _task: Any
    _emit: Any
    _message: Any

    async def pause_for_failure(self, tid, failure, agent_id=None, turn_id=None, generation=None):
        diagnostic = CtfFailureDiagnostic.model_validate(failure).model_dump(mode="json")
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            ensure(
                task.get("ctf_options", {}).get("dispatch_mode") == "automatic",
                "Failure holds require automatic mode",
            )
            if task["ctf_control"]["phase"] != "running":
                return {"held": False, "reason": "task_stopping"}
            control = dict(task["ctf_control"])
            failures = dict(control.get("member_failures", {}))
            if agent_id is not None:
                ensure(agent_id != "lead", "Lead failure requires a task-wide hold")
                member = await row(conn, s.ctf_members, tid, agent_id)
                if member["generation"] != generation or member["current_turn_id"] != str(turn_id):
                    control["failure_hold"] = {"failure": diagnostic, "task_wide": True}
                    await patch(conn, s.tasks, tid, {"ctf_control": control})
                    return {"held": True, "reason": "writer_changed"}
                previous = failures.get(agent_id)
                if previous and previous["turn_id"] == str(turn_id):
                    return {"held": bool(control.get("failure_hold"))}
                operation = member.get("pending_operation")
                if not operation:
                    execution = member.get("execution") or {}
                    operation = {
                        "request_id": str(
                            uuid5(NAMESPACE_URL, f"ctf-failure-stop:{tid}:{turn_id}")
                        ),
                        "kind": "stop",
                        "target_generation": execution.get("generation", generation),
                        "boot_id": execution.get("boot_id"),
                        "phase": "stopping",
                        "failure_stop": True,
                    }
                    await patch(
                        conn,
                        s.ctf_members,
                        tid,
                        {"pending_operation": operation, "run_state": "stopping"},
                        agent_id,
                    )
                failures[agent_id] = {
                    "turn_id": str(turn_id),
                    "generation": generation,
                    "stop_request_id": operation["request_id"],
                    "failure": diagnostic,
                    "resolved": False,
                }
                control["member_failures"] = failures
            control["failure_hold"] = {
                "failure": diagnostic,
                "task_wide": agent_id is None
                or bool(control.get("failure_hold", {}).get("task_wide")),
            }
            await patch(conn, s.tasks, tid, {"ctf_control": control})
            await self._emit(
                conn,
                tid,
                "member.state_changed",
                "system",
                {"id": agent_id or "lead", "admission_paused": True, "failure": diagnostic},
            )
            return {"held": True}

    async def reconcile_member_failures(self, tid):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            control = dict(task["ctf_control"])
            hold = control.get("failure_hold")
            if not hold:
                return {"held": False}
            if hold.get("task_wide") or control["phase"] != "running":
                return {"held": True}
            failures = dict(control.get("member_failures", {}))
            for agent_id, entry in failures.items():
                if entry.get("resolved"):
                    continue
                member = await row(conn, s.ctf_members, tid, agent_id)
                turn = await row(conn, s.ctf_turns, tid, entry["turn_id"])
                execution = member.get("execution") or {}
                if (
                    member.get("pending_operation")
                    or member.get("current_turn_id")
                    or member["lifecycle"] not in {"stopped", "removed"}
                    or turn["status"] != "finished"
                    or turn["generation"] != entry["generation"]
                    or not execution.get("drained")
                    or (
                        execution.get("generation") != entry["generation"]
                        and not execution.get("container_destroyed")
                    )
                    or entry["turn_id"] in control.get("unresolved_checkpoints", [])
                    or not any(
                        item.get("request_id") == entry["stop_request_id"]
                        and item.get("settled_turn_id") == entry["turn_id"]
                        and item.get("settled_turn_generation") == entry["generation"]
                        for item in member.get("operation_history", [])
                    )
                ):
                    continue
                saved = (
                    await conn.execute(
                        select(
                            s.agent_sessions.c.session["state"]["ctf_usage_outbox"],
                            s.agent_sessions.c.session["state"]["ctf_platform_calls"],
                        ).where(
                            s.agent_sessions.c.task_id == tid,
                            s.agent_sessions.c.agent_id == agent_id,
                        )
                    )
                ).first()
                if saved and (
                    saved[0]
                    or any(
                        value.get("state") in {"dispatched", "unknown"} or "record" not in value
                        for value in (saved[1] or {}).values()
                    )
                ):
                    continue
                failures[agent_id] = {**entry, "resolved": True}
                await self._message(
                    conn,
                    tid,
                    "system",
                    "lead",
                    json.dumps(
                        {
                            "member_id": agent_id,
                            "turn_id": entry["turn_id"],
                            "summary": (
                                "Failed member stopped safely. Review evidence before reassigning."
                            ),
                            "failure": entry["failure"],
                        },
                        ensure_ascii=False,
                    ),
                    str(uuid5(NAMESPACE_URL, f"ctf-failure-result:{tid}:{entry['turn_id']}")),
                )
            control["member_failures"] = failures
            if failures and all(item.get("resolved") for item in failures.values()):
                control.pop("failure_hold", None)
            await patch(conn, s.tasks, tid, {"ctf_control": control})
            return {"held": bool(control.get("failure_hold"))}
