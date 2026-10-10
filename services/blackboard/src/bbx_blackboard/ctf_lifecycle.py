"""CTF terminal archives and explicit, serialized task resumption."""

import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import insert, select, update

from bbx_blackboard.domain.rules import event
from bbx_blackboard.profiles import ProfileStore, ctf_profile
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import patch


def check(condition, detail):
    if not condition:
        raise HTTPException(409, detail)


class CtfLifecycleMixin:
    repo: Any
    objects: Any
    _task: Any
    _actor: Any
    _message: Any

    async def _terminal(self, conn, tid):
        task = await self._task(conn, tid)
        check(task["status"] in {"finished", "failed", "stopped"}, "Task is not terminal")
        active = await conn.execute(
            select(s.ctf_turns.c.id).where(
                s.ctf_turns.c.task_id == tid, s.ctf_turns.c.status == "running"
            )
        )
        check(active.first() is None, "Task has an active execution or review turn")
        return task

    async def export_archive(self, tid):
        async with self.repo.engine.begin() as conn:
            task = await self._terminal(conn, tid)
            result = {
                "format": "bbx.task-archive.v1",
                "task_id": str(tid),
                "run_number": task["run_number"],
            }
            state = {"task": task}
            for name, table in [
                ("members", s.ctf_members),
                ("turns", s.ctf_turns),
                ("messages", s.ctf_messages),
                ("challenges", s.ctf_challenges),
                ("records", s.ctf_records),
                ("sessions", s.agent_sessions),
                ("events", s.events),
                ("task_runs", s.task_runs),
            ]:
                values = [
                    dict(x)
                    for x in (
                        await conn.execute(select(table).where(table.c.task_id == tid))
                    ).mappings()
                ]
                if name in {"challenges", "records"}:
                    values = [v["data"] for v in values]
                state[name] = values
            state["artifacts"] = [
                r["artifact"] for r in state["records"] if r.get("kind") == "artifact_registration"
            ]
            conclusion = task.get("ctf_conclusion") or {}
            report = "# CTF task conclusion\n\n" + "\n\n".join(
                f"## {key}\n\n{value}" for key, value in conclusion.items()
            )
            report += "\n\n## Challenge work and external target observations\n\n"
            report += json.dumps(
                jsonable_encoder(
                    [
                        {
                            key: challenge.get(key)
                            for key in ("id", "title", "work_status", "verification", "target")
                        }
                        for challenge in state["challenges"]
                    ]
                ),
                ensure_ascii=False,
                indent=2,
            )
            report += (
                "\n\nExternal target state is the last recorded observation; "
                "local container cleanup does not prove external target shutdown.\n"
            )
            report_uri = (
                f"reports/{tid}.md"
                if task["run_number"] == 1
                else f"reports/{tid}/run-{task['run_number']}.md"
            )
            check(self.objects is not None, "Object store required")
            await self.objects.put(report_uri, report.encode(), content_type="text/markdown")
            await self.repo.append(conn, tid, [event("task.report", "system", {"uri": report_uri})])
            state["task"] = {**task, "report_uri": report_uri}
            result.update(
                state=state,
                sessions=state["sessions"],
                messages=state["messages"],
                events=state["events"],
                task_runs=state["task_runs"],
            )
            return jsonable_encoder(result)

    async def record_archive(self, tid, uri, size, fallback=False):
        async with self.repo.engine.begin() as conn:
            task = await self._terminal(conn, tid)
            expected = (
                f"workspace/{tid}.tar.zst"
                if task["run_number"] == 1
                else f"workspace/{tid}/run-{task['run_number']}.tar.zst"
            )
            check(uri == expected and size >= 0, "Invalid archive reference")
            check(self.objects is not None and await self.objects.exists(uri), "Archive missing")
            written = await self.repo.append(
                conn,
                tid,
                [
                    event(
                        "task.archived", "system", {"uri": uri, "size": size, "fallback": fallback}
                    )
                ],
            )
            return written

    async def record_cleanup(self, tid):
        async with self.repo.engine.begin() as conn:
            task = await self._terminal(conn, tid)
            check(bool(task["workspace_uri"]), "Archive must be registered before cleanup")
            check(await self.objects.exists(task["workspace_uri"]), "Archive missing")
            control = dict(task["ctf_control"])
            control["cleanup"] = {"phase": "complete", "error": None}
            await patch(conn, s.tasks, tid, {"ctf_control": control})
            return await self.repo.append(conn, tid, [event("task.cleanup_ready", "system", {})])

    async def cleanup_status(self, tid, *, phase, error=None):
        check(phase in {"archiving", "destroying", "failed", "complete"}, "Invalid cleanup phase")
        async with self.repo.engine.begin() as conn:
            task = await self._terminal(conn, tid)
            control = dict(task["ctf_control"])
            control["cleanup"] = {"phase": phase, "error": str(error)[:2000] if error else None}
            await patch(conn, s.tasks, tid, {"ctf_control": control})
            return control["cleanup"]

    async def resume(
        self,
        tid,
        request_id,
        additional_cost,
        additional_minutes,
        refresh_tools=False,
        actor="user",
    ):
        request_id = UUID(str(request_id))
        additional_cost = Decimal(str(additional_cost))
        check(
            additional_cost.is_finite() and additional_cost >= 0 and additional_minutes >= 0,
            "Invalid additional budget",
        )
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            previous = (
                (
                    await conn.execute(
                        select(s.resume_requests).where(
                            s.resume_requests.c.task_id == tid,
                            s.resume_requests.c.request_id == request_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                check(
                    Decimal(previous["additional_cost"]) == additional_cost
                    and previous["additional_minutes"] == additional_minutes
                    and previous["refresh_tools"] == refresh_tools,
                    "Resume request conflict",
                )
                return {"run_number": previous["run_number"]}
            task = await self._terminal(conn, tid)
            check(
                task["cleanup_ready"] and bool(task["workspace_uri"]), "Archive or cleanup pending"
            )
            check(await self.objects.exists(task["workspace_uri"]), "Archive missing")
            budget = dict(task["budget"])
            budget["max_cost"] = str(Decimal(str(budget["max_cost"])) + additional_cost)
            budget["max_minutes"] += additional_minutes
            check(
                Decimal(budget["max_cost"]) > Decimal(str(task["usage"].get("cost", 0)))
                and budget["max_minutes"] * 60 > task["active_seconds"],
                "Budget exhausted",
            )
            version = task["agent_profile_version"]
            if refresh_tools:
                current = (
                    (
                        await conn.execute(
                            select(s.agent_profiles).where(
                                s.agent_profiles.c.name == task["agent_profile"],
                                s.agent_profiles.c.version == version,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                latest = (
                    (
                        await conn.execute(
                            select(s.agent_profiles)
                            .where(s.agent_profiles.c.name == "ctf")
                            .order_by(s.agent_profiles.c.version.desc())
                            .limit(1)
                        )
                    )
                    .mappings()
                    .one()
                )
                profile = ctf_profile(dict(current)).model_copy(
                    update={
                        "worker_tools": ctf_profile(dict(latest)).worker_tools,
                        "platform_tools": ctf_profile(dict(latest)).platform_tools,
                    }
                )
                stored = await ProfileStore(self.repo.engine).create_in_connection(
                    conn, task["agent_profile"], profile, actor
                )
                version = stored["version"]
            run = task["run_number"] + 1
            await conn.execute(
                insert(s.resume_requests).values(
                    task_id=tid,
                    request_id=request_id,
                    additional_cost=str(additional_cost),
                    additional_minutes=additional_minutes,
                    refresh_tools=refresh_tools,
                    run_number=run,
                )
            )
            await self.repo.append(
                conn,
                tid,
                [
                    event(
                        "task.resumed",
                        "user",
                        {
                            "request_id": str(request_id),
                            "budget": budget,
                            "run_number": run,
                            "workspace_uri": task["workspace_uri"],
                            "agent_profile_version": version,
                            "additional_cost": str(additional_cost),
                            "additional_minutes": additional_minutes,
                        },
                    )
                ],
            )
            control = dict(task["ctf_control"])
            control["conclusion_history"] = [
                *control.get("conclusion_history", []),
                {"run_number": task["run_number"], "conclusion": task["ctf_conclusion"]},
            ]
            for key in ("closing_request_id", "replacement", "cleanup"):
                control.pop(key, None)
            control.update(phase="provisioning", epoch=control.get("epoch", 0) + 1)
            await patch(conn, s.tasks, tid, {"ctf_control": control, "ctf_conclusion": None})
            await conn.execute(
                update(s.ctf_members)
                .where(s.ctf_members.c.task_id == tid)
                .values(execution=None, generation=s.ctf_members.c.generation + 1)
            )
            await conn.execute(
                update(s.ctf_messages)
                .where(
                    s.ctf_messages.c.task_id == tid,
                    s.ctf_messages.c.status.in_(["queued", "leased"]),
                )
                .values(
                    status="queued",
                    deferred=True,
                    claim_token=None,
                    claim_turn_id=None,
                    claim_generation=None,
                    lease_until=None,
                )
            )
            await self._message(
                conn,
                tid,
                "system",
                "lead",
                "Task explicitly resumed. First inspect existing targets and connections, "
                "shared records and unresolved work. Do not replay unknown tool calls. "
                "Previously queued messages are deferred: confirm_messages only after checking "
                "their continuing relevance. Stopped and removed members remain unchanged.",
                str(uuid5(request_id, "resume-lead")),
                kind="instruction",
            )
            return {"run_number": run}

    async def confirm_messages(self, tid, *, actor, message_ids, generation, turn_id):
        async with self.repo.engine.begin() as conn:
            task = await self._task(conn, tid)
            check(task["ctf_control"]["phase"] == "running", "Task is not running")
            await self._actor(conn, tid, actor, generation, turn_id)
            check(actor == "lead", "Only Lead can confirm deferred execution")
            identifiers = [str(UUID(str(value))) for value in message_ids]
            check(0 < len(identifiers) <= 100, "Invalid message count")
            messages = (
                (
                    await conn.execute(
                        select(s.ctf_messages).where(
                            s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id.in_(identifiers)
                        )
                    )
                )
                .mappings()
                .all()
            )
            check(len(messages) == len(set(identifiers)), "Unknown message")
            check(
                all(m["status"] == "queued" for m in messages),
                "Only queued messages can be confirmed",
            )
            await conn.execute(
                update(s.ctf_messages)
                .where(s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id.in_(identifiers))
                .values(deferred=False)
            )
            return {"message_ids": identifiers}
