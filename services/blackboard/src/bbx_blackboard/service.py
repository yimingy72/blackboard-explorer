"""Transaction boundary and public blackboard operations."""

from __future__ import annotations

import re
from typing import Any, Protocol
from uuid import UUID, uuid4

from bbx_contracts.models import (
    PostFactRequest,
    PostIntentRequest,
    Price,
    SubmitCloseRequest,
    TaskSpec,
    Usage,
)
from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bbx_blackboard.domain import (
    RuleViolation,
    calculate_cost,
    decide,
    dispute_fields,
    pending_claims,
)
from bbx_blackboard.domain.rules import event
from bbx_blackboard.store import Repository
from bbx_blackboard.store import schema as s


class ObjectStore(Protocol):
    async def exists(self, uri: str) -> bool: ...


class BoardService:
    def __init__(self, engine: AsyncEngine, objects: ObjectStore) -> None:
        self.repo = Repository(engine)
        self.objects = objects

    async def create_task(
        self, spec: TaskSpec | dict[str, Any], *, profile_version: int = 1
    ) -> UUID:
        spec = TaskSpec.model_validate(spec)
        if len({x.id for x in spec.acceptance}) != len(spec.acceptance):
            raise RuleViolation("duplicate_acceptance", "验收项编号重复，请为每项使用唯一 id。")
        tid = uuid4()
        acceptance = {
            x.id: {
                "status": "unmet",
                "reason": None,
                "missing": None,
                "evidence_facts": [],
                "judged_version": None,
            }
            for x in spec.acceptance
        }
        fields = spec.model_dump(mode="json")
        payload = {
            "goal": fields["goal"],
            "domain_context": fields["domain_context"],
            "egress_allowlist": fields["egress_allowlist"],
            "acceptance": fields["acceptance"],
            "acceptance_state": acceptance,
            "budget": fields["budget"],
            "params": fields["params"],
            "agent_profile": fields["agent_profile"],
            "agent_profile_version": profile_version,
            "status": "created",
            "usage": {},
            "last_change_version": 0,
            "last_judgment_version": 0,
            "derive_empty_streak": 0,
            "failure_streak": 0,
            "seed_empty_count": 0,
        }
        async with self.repo.engine.begin() as conn:
            await conn.execute(insert(s.tasks).values(id=tid, **payload))
            written = await self.repo.append(
                conn,
                tid,
                [
                    {
                        "type": "task.created",
                        "actor": "user",
                        "object_id": None,
                        "payload": payload,
                        "addressed_to": None,
                    }
                ],
            )
        await self.repo.notify(tid, written[-1]["version"])
        return tid

    async def _write(
        self, tid: UUID, command: str, actor: str, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            state = await self.repo.load(conn, tid)
            if state.task["deleting"]:
                raise RuleViolation("task_deleting", "任务正在删除。")
            events = decide(state, command, actor, data)
            written = await self.repo.append(conn, tid, events)
            if command == "finish_agent":
                await conn.execute(
                    update(s.agent_messages)
                    .where(
                        s.agent_messages.c.task_id == tid,
                        s.agent_messages.c.agent_id == data["agent_id"],
                        s.agent_messages.c.role == "user",
                        s.agent_messages.c.status == "processing",
                    )
                    .values(
                        status="queued", claim_token=None, lease_until=None, updated_at=func.now()
                    )
                )
        if written:
            await self.repo.notify(tid, written[-1]["version"])
        return written

    async def transition(
        self, tid: UUID, status: str, *, actor: str = "scheduler", reason: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._write(tid, "transition", actor, {"status": status, "reason": reason})

    async def record_archive(
        self, tid: UUID, uri: str, size: int, fallback: str
    ) -> list[dict[str, Any]]:
        if uri != f"workspace/{tid}.tar.zst":
            raise RuleViolation("archive_invalid_uri", "工作区归档 key 与任务不匹配。")
        if not await self.objects.exists(uri):
            raise RuleViolation("archive_missing", "工作区归档对象不存在，请先上传。")
        return await self._write(
            tid,
            "record_archive",
            "scheduler",
            {"uri": uri, "size": size, "fallback": fallback},
        )

    async def post_fact(
        self,
        tid: UUID,
        agent_id: str,
        request: PostFactRequest | dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        data = PostFactRequest.model_validate(request).model_dump(mode="json")
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            state = await self.repo.load(conn, tid)
            if state.task["deleting"]:
                raise RuleViolation("task_deleting", "任务正在删除。")
            for evidence in data["evidence"]:
                uri = evidence.get("uri")
                if not uri or not await self.objects.exists(uri):
                    raise RuleViolation(
                        "evidence_missing", f"证据 {uri or '(无 uri)'} 未持久化，请先上传再提交。"
                    )
            planned = decide(state, "post_fact", agent_id, data)
            if dry_run:
                return {"valid": True}
            written = await self.repo.append(conn, tid, planned)
        await self.repo.notify(tid, written[-1]["version"])
        return {"id": written[0]["object_id"], "events": written}

    async def post_intent(
        self,
        tid: UUID,
        agent_id: str,
        request: PostIntentRequest | dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        data = PostIntentRequest.model_validate(request).model_dump(mode="json")
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            state = await self.repo.load(conn, tid)
            if state.task["deleting"]:
                raise RuleViolation("task_deleting", "任务正在删除。")
            planned = decide(state, "post_intent", agent_id, data)
            if dry_run:
                return {"valid": True}
            written = await self.repo.append(conn, tid, planned)
        await self.repo.notify(tid, written[-1]["version"])
        return {"id": written[0]["object_id"], "events": written}

    async def claim(self, tid: UUID, agent_id: str, intent_id: str) -> list[dict[str, Any]]:
        return await self._write(tid, "claim", agent_id, {"intent_id": intent_id})

    async def claim_for(self, tid: UUID, intent_id: str, agent_id: str) -> list[dict[str, Any]]:
        return await self._write(
            tid, "claim_for", "scheduler", {"intent_id": intent_id, "agent_id": agent_id}
        )

    async def release(
        self, tid: UUID, agent_id: str, intent_id: str, note: str
    ) -> list[dict[str, Any]]:
        return await self._write(tid, "release", agent_id, {"intent_id": intent_id, "note": note})

    async def system_close(self, tid: UUID, intent_id: str) -> list[dict[str, Any]]:
        return await self._write(tid, "system_close", "system", {"intent_id": intent_id})

    async def register_agent(
        self,
        tid: UUID,
        task_type: str,
        *,
        is_seed: bool = False,
        close_mode: str | None = None,
        derive_parallel: bool | None = None,
    ) -> str:
        data = {"task_type": task_type, "is_seed": is_seed, "close_mode": close_mode}
        if task_type == "derive" and derive_parallel is not None:
            data["derive_parallel"] = derive_parallel
        events = await self._write(
            tid,
            "register_agent",
            "scheduler",
            data,
        )
        return events[0]["object_id"]

    async def heartbeat(
        self,
        tid: UUID,
        agent_id: str,
        *,
        steps: int,
        context_tokens: int,
        usage: Usage | dict[str, Any],
        last_seen_version: int,
        price: Price | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        delta = Usage.model_validate(usage)
        amount, warning = calculate_cost(delta, Price.model_validate(price or {}))
        values = delta.model_dump(mode="json")
        values["cost"] = float(amount)
        events = await self._write(
            tid,
            "heartbeat",
            agent_id,
            {
                "agent_id": agent_id,
                "steps": steps,
                "context_tokens": context_tokens,
                "usage": values,
                "last_seen_version": last_seen_version,
            },
        )
        return {"events": events, "cost": amount, "warning": warning}

    async def conclude(self, tid: UUID, agent_id: str, reason: str) -> list[dict[str, Any]]:
        return await self._write(
            tid, "conclude", "scheduler", {"agent_id": agent_id, "reason": reason}
        )

    async def take_grace(self, tid: UUID, agent_id: str) -> int:
        events = await self._write(tid, "take_grace", agent_id, {"agent_id": agent_id})
        return events[0]["payload"]["grace_left"]

    async def finish_agent(
        self, tid: UUID, agent_id: str, receipt: dict[str, Any], end_reason: str
    ) -> list[dict[str, Any]]:
        return await self._write(
            tid,
            "finish_agent",
            agent_id,
            {"agent_id": agent_id, "receipt": receipt, "end_reason": end_reason},
        )

    async def submit_close(
        self,
        tid: UUID,
        agent_id: str,
        request: SubmitCloseRequest | dict[str, Any],
        *,
        report_uri: str | None = None,
    ) -> list[dict[str, Any]]:
        data = SubmitCloseRequest.model_validate(request).model_dump(mode="json")
        data["report_uri"] = report_uri
        if report_uri and not await self.objects.exists(report_uri):
            raise RuleViolation("report_missing", "报告 uri 不存在，请先持久化终结报告。")
        return await self._write(tid, "submit_close", agent_id, data)

    async def record_tool_call(
        self, tid: UUID, agent_id: str, call: dict[str, Any]
    ) -> list[dict[str, Any]]:
        data = {**call, "agent_id": agent_id}
        if data.get("result_head"):
            data["result_head"] = data["result_head"][:4096]
        return await self._write(tid, "record_tool_call", agent_id, data)

    async def record_agent_trace(
        self, tid: UUID, agent_id: str, trace: dict[str, Any]
    ) -> list[dict[str, Any]]:
        kind = trace.get("kind")
        step = trace.get("step")
        uri = trace.get("uri")
        summary = trace.get("summary")
        if (
            kind not in {"initial_context", "board_update", "model_output"}
            or not isinstance(step, int)
            or isinstance(step, bool)
            or step < 0
            or (kind == "initial_context") != (step == 0)
            or not isinstance(summary, str)
            or len(summary) > 240
            or not isinstance(uri, str)
            or not re.fullmatch(
                rf"traces/{re.escape(str(tid))}/{re.escape(agent_id)}/[^/.][^/]*\.json", uri
            )
            or any(part in {".", ".."} for part in uri.split("/"))
        ):
            raise RuleViolation("invalid_trace", "Trace 元数据或对象 key 不合法。")
        if not await self.objects.exists(uri):
            raise RuleViolation("trace_missing", "Trace 对象不存在，请先持久化正文。")
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            state = await self.repo.load(conn, tid)
            if state.task["deleting"]:
                raise RuleViolation("task_deleting", "任务正在删除。")
            agent = state.agents.get(agent_id)
            if agent is None or agent["status"] not in {"running", "concluding"}:
                raise RuleViolation("agent_inactive", "Trace 必须属于当前任务的活动 Agent。")
            duplicates = [s.events.c.payload["uri"].astext == uri]
            if kind == "initial_context":
                duplicates.append(
                    (s.events.c.payload["agent_id"].astext == agent_id)
                    & (s.events.c.payload["kind"].astext == kind)
                )
            previous = await conn.execute(
                select(s.events.c.version)
                .where(
                    s.events.c.task_id == tid,
                    s.events.c.type == "agent.trace.recorded",
                    or_(*duplicates),
                )
                .limit(1)
            )
            if previous.first() is not None:
                raise RuleViolation("duplicate_trace", "Trace 或初始上下文已登记。")
            payload = {
                "agent_id": agent_id,
                "kind": kind,
                "step": step,
                "uri": uri,
                "summary": summary,
            }
            written = await self.repo.append(
                conn, tid, [event("agent.trace.recorded", agent_id, payload)]
            )
        await self.repo.notify(tid, written[-1]["version"])
        return written

    async def state(self, tid: UUID) -> dict[str, Any]:
        async with self.repo.engine.connect() as conn:
            state = await self.repo.load(conn, tid)
        fields = dispute_fields(state.facts)
        facts = {fid: {**fact, **fields[fid]} for fid, fact in state.facts.items()}
        return {
            "task": state.task,
            "facts": facts,
            "intents": state.intents,
            "agents": state.agents,
            "counters": state.counters,
            "pending_claims": pending_claims(state),
            "board_empty": not (state.facts or state.intents),
            "last_change_version": state.task["last_change_version"],
            "last_judgment_version": state.task["last_judgment_version"],
        }

    async def events(
        self, tid: UUID, since: int = 0, for_agent: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.repo.engine.connect() as conn:
            stmt = select(s.events).where(s.events.c.task_id == tid, s.events.c.version > since)
            if for_agent is not None:
                stmt = stmt.where(
                    or_(s.events.c.addressed_to.is_(None), s.events.c.addressed_to.any(for_agent))
                )
            return [
                dict(x) for x in (await conn.execute(stmt.order_by(s.events.c.version))).mappings()
            ]

    async def get_object(self, tid: UUID, oid: str, depth: int = 1) -> dict[str, Any]:
        board = await self.state(tid)
        objects = {**board["facts"], **board["intents"]}
        if oid not in objects:
            raise KeyError(oid)
        obj = objects[oid]
        if depth < 1:
            return {"object": obj, "related": {}}
        keys = set(obj.get("derived_from", []) + obj.get("disputes", []) + obj.get("based_on", []))
        keys.update(
            x["id"]
            for x in objects.values()
            if oid in x.get("derived_from", []) + x.get("disputes", []) + x.get("based_on", [])
            or oid in (x.get("resolves"), x.get("retry_of"))
        )
        for key in ("resolves", "retry_of"):
            if obj.get(key):
                keys.add(obj[key])
        return {"object": obj, "related": {key: objects[key] for key in keys if key in objects}}

    async def replay(self, tid: UUID) -> None:
        await self.repo.replay(tid)
