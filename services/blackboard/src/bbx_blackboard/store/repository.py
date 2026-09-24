"""All projection writes pass through apply, including replay."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from bbx_blackboard.domain import BoardState

from . import schema as s


def now() -> datetime:
    return datetime.now(UTC)


async def row(
    conn: AsyncConnection, table, task_id: UUID, oid: str | None = None
) -> dict[str, Any]:
    where = table.c.id == oid if oid is not None else table.c.id == task_id
    result = await conn.execute(
        select(table).where(where, *([table.c.task_id == task_id] if oid is not None else []))
    )
    return dict(result.mappings().one())


async def patch(
    conn: AsyncConnection, table, task_id: UUID, values: dict[str, Any], oid: str | None = None
) -> None:
    where = table.c.id == oid if oid is not None else table.c.id == task_id
    stmt = (
        update(table)
        .where(where, *([table.c.task_id == task_id] if oid is not None else []))
        .values(**values)
    )
    await conn.execute(stmt)


async def counter(conn: AsyncConnection, task_id: UUID, kind: str, value: int) -> None:
    await conn.execute(
        insert(s.task_counters)
        .values(task_id=task_id, kind=kind, value=value)
        .on_conflict_do_update(index_elements=["task_id", "kind"], set_={"value": value})
    )


async def apply(conn: AsyncConnection, evt: dict[str, Any]) -> None:
    """Project one durable event into folded tables."""
    tid, kind, p, version = evt["task_id"], evt["type"], evt["payload"], evt["version"]
    stamp = evt["created_at"]
    if kind == "task.created":
        await patch(conn, s.tasks, tid, p)
    elif kind.startswith("task.") and kind not in {"task.report"}:
        values: dict[str, Any] = {"status": kind.split(".")[1]}
        if kind == "task.running":
            values["started_at"] = stamp
        if kind in {"task.finished", "task.failed", "task.stopped"}:
            values["finished_at"] = stamp
        if kind == "task.failed":
            values["fail_reason"] = p.get("reason")
        await patch(conn, s.tasks, tid, values)
    elif kind == "task.report":
        await patch(conn, s.tasks, tid, {"report_uri": p["uri"]})
    elif kind == "fact.posted":
        fields = {
            k: p.get(k)
            for k in (
                "kind",
                "statement",
                "evidence",
                "derived_from",
                "disputes",
                "resolves",
                "result",
                "satisfies",
                "author",
                "provenance",
            )
        }
        await conn.execute(
            insert(s.facts).values(task_id=tid, id=p["id"], version=version, **fields)
        )
        await counter(conn, tid, "F", int(p["id"][1:]))
    elif kind == "intent.posted":
        fields = {
            k: p.get(k)
            for k in (
                "statement",
                "based_on",
                "expected",
                "method",
                "relates_to",
                "retry_of",
                "author",
            )
        }
        claim = p.get("claim", False)
        await conn.execute(
            insert(s.intents).values(
                task_id=tid,
                id=p["id"],
                version=version,
                status="claimed" if claim else "open",
                holder=p["author"] if claim else None,
                claimed_at=stamp if claim else None,
                result=None,
                closed_by=None,
                result_facts=[],
                notes=[],
                attempts=0,
                **fields,
            )
        )
        await counter(conn, tid, "I", int(p["id"][1:]))
        if claim:
            await patch(conn, s.agent_runs, tid, {"intent_id": p["id"]}, p["author"])
    elif kind == "intent.claimed":
        await patch(
            conn,
            s.intents,
            tid,
            {"status": "claimed", "holder": p["holder"], "claimed_at": stamp},
            p["intent_id"],
        )
        await patch(conn, s.agent_runs, tid, {"intent_id": p["intent_id"]}, p["holder"])
    elif kind == "intent.released":
        intent = await row(conn, s.intents, tid, p["intent_id"])
        notes = [*intent["notes"], {"by": p["holder"], "at": stamp.isoformat(), "text": p["note"]}]
        await patch(
            conn,
            s.intents,
            tid,
            {
                "status": "open",
                "holder": None,
                "claimed_at": None,
                "notes": notes,
                "attempts": intent["attempts"] + int(p["counted"]),
            },
            p["intent_id"],
        )
        await patch(conn, s.agent_runs, tid, {"intent_id": None}, p["holder"])
    elif kind == "intent.closed":
        intent = await row(conn, s.intents, tid, p["intent_id"])
        await patch(
            conn,
            s.intents,
            tid,
            {
                "status": "closed",
                "holder": None,
                "claimed_at": None,
                "result": p["result"],
                "closed_by": p["by"],
                "result_facts": [*intent["result_facts"], p["by"]]
                if p["by"] != "system"
                else intent["result_facts"],
            },
            p["intent_id"],
        )
        if intent["holder"]:
            await patch(conn, s.agent_runs, tid, {"intent_id": None}, intent["holder"])
    elif kind == "agent.spawned":
        await conn.execute(
            insert(s.agent_runs).values(
                task_id=tid,
                id=p["id"],
                task_type=p["task_type"],
                is_seed=p.get("is_seed", False),
                close_mode=p.get("close_mode"),
                judge_from_version=p.get("judge_from_version"),
                intent_id=None,
                status="running",
                end_reason=None,
                steps=0,
                context_tokens=0,
                usage={},
                conclude_reason=None,
                conclude_requested_at=None,
                conclude_injected=False,
                grace_calls_left=None,
                last_seen_version=0,
                last_heartbeat_at=stamp,
                receipt=None,
                started_at=stamp,
                finished_at=None,
            )
        )
        await counter(conn, tid, "agent", int(p["id"].split("-")[1]))
    elif kind == "agent.progress":
        agent = await row(conn, s.agent_runs, tid, p["agent_id"])
        values: dict[str, Any] = {"last_heartbeat_at": stamp}
        if "steps" in p:
            values["steps"] = agent["steps"] + p["steps"]
        if "context_tokens" in p:
            values["context_tokens"] = p["context_tokens"]
        if "last_seen_version" in p:
            values["last_seen_version"] = max(agent["last_seen_version"], p["last_seen_version"])
        if "usage" in p:
            delta = p["usage"]
            values["usage"] = {
                k: agent["usage"].get(k, 0) + delta.get(k, 0)
                for k in set(agent["usage"]) | set(delta)
            }
        if "grace_left" in p:
            values["grace_calls_left"] = p["grace_left"]
        await patch(conn, s.agent_runs, tid, values, p["agent_id"])
    elif kind == "agent.conclude_requested":
        task = await row(conn, s.tasks, tid)
        await patch(
            conn,
            s.agent_runs,
            tid,
            {
                "status": "concluding",
                "conclude_reason": p["reason"],
                "conclude_requested_at": stamp,
                "grace_calls_left": int(task["params"].get("conclude_grace_calls", 3)),
            },
            p["agent_id"],
        )
    elif kind == "agent.finished":
        agent = await row(conn, s.agent_runs, tid, p["agent_id"])
        reason = p["end_reason"]
        await patch(
            conn,
            s.agent_runs,
            tid,
            {
                "status": "failed" if reason == "runtime_error" else "finished",
                "end_reason": reason,
                "receipt": p.get("receipt"),
                "finished_at": stamp,
            },
            p["agent_id"],
        )
        task = await row(conn, s.tasks, tid)
        changes: dict[str, Any] = {
            "failure_streak": task["failure_streak"] + 1 if reason == "runtime_error" else 0
        }
        if agent["is_seed"] and not await board_has_content(conn, tid):
            changes["seed_empty_count"] = task["seed_empty_count"] + 1
        await patch(conn, s.tasks, tid, changes)
    elif kind == "derive.result":
        task = await row(conn, s.tasks, tid)
        await patch(
            conn,
            s.tasks,
            tid,
            {"derive_empty_streak": task["derive_empty_streak"] + 1 if not p["posted"] else 0},
        )
    elif kind == "budget.updated":
        await patch(conn, s.tasks, tid, {"usage": p["usage"]})
    elif kind == "acceptance.judged":
        task = await row(conn, s.tasks, tid)
        state = dict(task["acceptance_state"])
        for item in p["verdicts"]:
            state[item["id"]] = {
                "status": item["verdict"],
                "reason": item["reason"],
                "missing": item.get("missing"),
                "evidence_facts": item.get("evidence_facts", []),
                "judged_version": p["judge_from_version"],
            }
        await patch(
            conn,
            s.tasks,
            tid,
            {"acceptance_state": state, "last_judgment_version": p["judge_from_version"] or 0},
        )
    elif kind == "acceptance.reverted":
        task = await row(conn, s.tasks, tid)
        state = dict(task["acceptance_state"])
        item = dict(state[p["id"]])
        item.update(status="unmet", missing=f"支撑事实 {p['fact_id']} 被争议", evidence_facts=[])
        state[p["id"]] = item
        await patch(conn, s.tasks, tid, {"acceptance_state": state})
    elif kind == "tool_call.recorded":
        await conn.execute(
            insert(s.tool_calls).values(
                task_id=tid,
                id=p["id"],
                agent_id=p["agent_id"],
                tool=p["tool"],
                args=p["args"],
                result_head=p.get("result_head"),
                result_uri=p.get("result_uri"),
                created_at=stamp,
            )
        )
    elif kind in {"fact.disputed", "fact.undisputed"}:
        pass
    else:
        raise ValueError(f"unhandled event type: {kind}")
    if kind in {
        "fact.posted",
        "intent.posted",
        "intent.claimed",
        "intent.released",
        "intent.closed",
        "fact.disputed",
        "fact.undisputed",
    }:
        await patch(conn, s.tasks, tid, {"last_change_version": version, "derive_empty_streak": 0})


async def board_has_content(conn: AsyncConnection, tid: UUID) -> bool:
    for table in (s.facts, s.intents):
        if (await conn.execute(select(table.c.id).where(table.c.task_id == tid).limit(1))).first():
            return True
    return False


class Repository:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def lock(self, conn: AsyncConnection, tid: UUID) -> None:
        found = await conn.execute(
            select(s.tasks.c.id).where(s.tasks.c.id == tid).with_for_update()
        )
        if found.scalar_one_or_none() is None:
            raise KeyError(f"task {tid} does not exist")

    async def load(self, conn: AsyncConnection, tid: UUID) -> BoardState:
        task = await row(conn, s.tasks, tid)
        task["version"] = (
            await conn.execute(
                select(func.coalesce(func.max(s.events.c.version), 0)).where(
                    s.events.c.task_id == tid
                )
            )
        ).scalar_one()

        async def mapped(table):
            rows = (
                (
                    await conn.execute(
                        select(table)
                        .where(table.c.task_id == tid)
                        .order_by(table.c.version if "version" in table.c else table.c.id)
                    )
                )
                .mappings()
                .all()
            )
            return {x["id"]: dict(x) for x in rows}

        counts = (
            (await conn.execute(select(s.task_counters).where(s.task_counters.c.task_id == tid)))
            .mappings()
            .all()
        )
        return BoardState(
            task,
            await mapped(s.facts),
            await mapped(s.intents),
            await mapped(s.agent_runs),
            await mapped(s.tool_calls),
            {x["kind"]: x["value"] for x in counts},
        )

    async def append(
        self, conn: AsyncConnection, tid: UUID, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        written = []
        for item in items:
            result = await conn.execute(
                insert(s.events).values(task_id=tid, **item).returning(s.events)
            )
            evt = dict(result.mappings().one())
            await apply(conn, evt)
            written.append(evt)
        return written

    async def replay(self, tid: UUID) -> None:
        async with self.engine.begin() as conn:
            await self.lock(conn, tid)
            log = [
                dict(x)
                for x in (
                    await conn.execute(
                        select(s.events)
                        .where(s.events.c.task_id == tid)
                        .order_by(s.events.c.version)
                    )
                ).mappings()
            ]
            for table in (s.facts, s.intents, s.agent_runs, s.tool_calls, s.task_counters):
                await conn.execute(delete(table).where(table.c.task_id == tid))
            await patch(
                conn,
                s.tasks,
                tid,
                {
                    "last_change_version": 0,
                    "last_judgment_version": 0,
                    "derive_empty_streak": 0,
                    "failure_streak": 0,
                    "seed_empty_count": 0,
                    "usage": {},
                    "report_uri": None,
                    "started_at": None,
                    "finished_at": None,
                    "fail_reason": None,
                },
            )
            for evt in log:
                await apply(conn, evt)

    async def notify(self, tid: UUID, version: int) -> None:
        channel = f"bbx_task_{tid.hex}"
        async with self.engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_notify(:channel, :version)"),
                {"channel": channel, "version": str(version)},
            )
