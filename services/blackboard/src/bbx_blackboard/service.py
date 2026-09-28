"""Transaction boundary and public blackboard operations."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from bbx_contracts.billing import effective_price
from bbx_contracts.models import (
    AgentProfile,
    ModelConfig,
    Params,
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
from bbx_blackboard.profiles import ProfileStore
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
                "completion_basis": "inferred",
                "completion_reason": None,
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
            "failure_window_kind": None,
            "failure_window_started_at": None,
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
        self,
        tid: UUID,
        command: str,
        actor: str,
        data: dict[str, Any],
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            state = await self.repo.load(conn, tid)
            if state.task["deleting"]:
                raise RuleViolation("task_deleting", "任务正在删除。")
            target = str(data["agent_id"]) if command in {"conclude", "take_grace"} else actor
            self._check_derive_round(state.agents.get(target), expected_derive_round)
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

    @staticmethod
    def _check_derive_round(
        agent: dict[str, Any] | None, expected_derive_round: int | None
    ) -> None:
        if (
            agent is not None
            and agent["task_type"] == "derive"
            and (int(agent.get("derive_round") or 1) != (expected_derive_round or 1))
        ):
            raise RuleViolation("stale_agent_round", "推导轮次已变化，旧请求不能写入本轮。")

    async def transition(
        self, tid: UUID, status: str, *, actor: str = "scheduler", reason: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._write(tid, "transition", actor, {"status": status, "reason": reason})

    async def record_archive(
        self, tid: UUID, uri: str, size: int, fallback: str
    ) -> list[dict[str, Any]]:
        if uri != f"workspace/{tid}.tar.zst" and not re.fullmatch(
            rf"workspace/{tid}/run-(?:[2-9]|[1-9][0-9]+)\.tar\.zst", uri
        ):
            raise RuleViolation("archive_invalid_uri", "工作区归档 key 与任务不匹配。")
        if not await self.objects.exists(uri):
            raise RuleViolation("archive_missing", "工作区归档对象不存在，请先上传。")
        return await self._write(
            tid,
            "record_archive",
            "scheduler",
            {"uri": uri, "size": size, "fallback": fallback},
        )

    async def record_cleanup(self, tid: UUID) -> list[dict[str, Any]]:
        return await self._write(tid, "record_cleanup", "scheduler", {})

    async def resume(
        self,
        tid: UUID,
        request_id: UUID,
        additional_cost: Decimal,
        additional_minutes: int,
        refresh_tools: bool = False,
        actor: str = "user",
    ) -> None:
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
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
            if previous is not None:
                if (
                    Decimal(previous["additional_cost"]) != additional_cost
                    or previous["additional_minutes"] != additional_minutes
                    or previous["refresh_tools"] != refresh_tools
                ):
                    raise RuleViolation(
                        "resume_request_conflict", "此请求编号已用于不同的追加额度。"
                    )
                return
            state = await self.repo.load(conn, tid)
            task = state.task
            if task["deleting"]:
                raise RuleViolation("task_deleting", "任务正在删除。")
            if task["status"] not in {"finished", "failed", "stopped"}:
                raise RuleViolation("resume_not_terminal", "任务尚未结束。")
            source_uri = task.get("workspace_uri") or task.get("resume_workspace_uri")
            if not task.get("cleanup_ready") or not source_uri:
                raise RuleViolation(
                    "resume_archive_pending", "工作区归档或清理尚未完成，请稍后重试。"
                )
            if any(a["status"] in {"running", "concluding"} for a in state.agents.values()):
                raise RuleViolation("resume_agents_active", "仍有 Agent 在运行。")
            processing = await conn.execute(
                select(s.agent_messages.c.id)
                .where(
                    s.agent_messages.c.task_id == tid,
                    s.agent_messages.c.status == "processing",
                )
                .limit(1)
            )
            if processing.first():
                raise RuleViolation("resume_review_active", "复盘消息仍在处理，请稍后重试。")
            if not await self.objects.exists(source_uri):
                raise RuleViolation("resume_archive_missing", "工作区归档对象不存在。")
            budget = dict(task["budget"])
            budget["max_cost"] = str(Decimal(str(budget["max_cost"])) + additional_cost)
            budget["max_minutes"] = int(budget["max_minutes"]) + additional_minutes
            if Decimal(budget["max_cost"]) <= Decimal(str(task["usage"].get("cost", 0))) or budget[
                "max_minutes"
            ] * 60 <= int(task.get("active_seconds") or 0):
                raise RuleViolation("resume_budget_exhausted", "追加后仍需留有金额和运行时间。")
            reserve = Params.model_validate(task["params"]).close_reserve_ratio
            if Decimal(budget["max_cost"]) * (1 - reserve) <= Decimal(
                str(task["usage"].get("cost", 0))
            ):
                raise RuleViolation("resume_budget_exhausted", "追加后仍需留有可探索金额。")
            run = int(task.get("run_number") or 1) + 1
            profile_version = task["agent_profile_version"]
            if refresh_tools:
                profiles = ProfileStore(self.repo.engine)
                current = (
                    (
                        await conn.execute(
                            select(s.agent_profiles).where(
                                s.agent_profiles.c.name == task["agent_profile"],
                                s.agent_profiles.c.version == profile_version,
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
                            .where(s.agent_profiles.c.name == "default")
                            .order_by(s.agent_profiles.c.version.desc())
                            .limit(1)
                        )
                    )
                    .mappings()
                    .one()
                )
                content = {key: current[key] for key in AgentProfile.model_fields}
                content["worker_tools"] = latest["worker_tools"]
                profile = AgentProfile.model_validate(content)
                profile_row = await profiles.create_in_connection(
                    conn, task["agent_profile"], profile, actor
                )
                profile_version = profile_row["version"]
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
            written = await self.repo.append(
                conn,
                tid,
                [
                    event(
                        "task.resumed",
                        "user",
                        {
                            "request_id": str(request_id),
                            "additional_cost": str(additional_cost),
                            "additional_minutes": additional_minutes,
                            "budget": budget,
                            "run_number": run,
                            "workspace_uri": source_uri,
                            "agent_profile_version": profile_version,
                        },
                    )
                ],
            )
        await self.repo.notify(tid, written[-1]["version"])

    async def post_fact(
        self,
        tid: UUID,
        agent_id: str,
        request: PostFactRequest | dict[str, Any],
        *,
        dry_run: bool = False,
        expected_derive_round: int | None = None,
    ) -> dict[str, Any]:
        data = PostFactRequest.model_validate(request).model_dump(mode="json")
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            state = await self.repo.load(conn, tid)
            if state.task["deleting"]:
                raise RuleViolation("task_deleting", "任务正在删除。")
            self._check_derive_round(state.agents.get(agent_id), expected_derive_round)
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
        expected_derive_round: int | None = None,
    ) -> dict[str, Any]:
        data = PostIntentRequest.model_validate(request).model_dump(mode="json")
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            state = await self.repo.load(conn, tid)
            if state.task["deleting"]:
                raise RuleViolation("task_deleting", "任务正在删除。")
            self._check_derive_round(state.agents.get(agent_id), expected_derive_round)
            planned = decide(state, "post_intent", agent_id, data)
            if dry_run:
                return {"valid": True}
            written = await self.repo.append(conn, tid, planned)
        await self.repo.notify(tid, written[-1]["version"])
        return {"id": written[0]["object_id"], "events": written}

    async def claim(
        self,
        tid: UUID,
        agent_id: str,
        intent_id: str,
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._write(
            tid,
            "claim",
            agent_id,
            {"intent_id": intent_id},
            expected_derive_round=expected_derive_round,
        )

    async def claim_for(self, tid: UUID, intent_id: str, agent_id: str) -> list[dict[str, Any]]:
        return await self._write(
            tid, "claim_for", "scheduler", {"intent_id": intent_id, "agent_id": agent_id}
        )

    async def release(
        self,
        tid: UUID,
        agent_id: str,
        intent_id: str,
        note: str,
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._write(
            tid,
            "release",
            agent_id,
            {"intent_id": intent_id, "note": note},
            expected_derive_round=expected_derive_round,
        )

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
        derive_review: bool = False,
    ) -> str:
        data = {
            "task_type": task_type,
            "is_seed": is_seed,
            "close_mode": close_mode,
            "derive_review": derive_review,
        }
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
        model: ModelConfig | None = None,
        requested_at: datetime | None = None,
        billing_mode: str | None = None,
        expected_derive_round: int | None = None,
    ) -> dict[str, Any]:
        delta = Usage.model_validate(usage)
        selected = Price.model_validate(price or {})
        pricing: dict[str, Any] = {}
        if model is not None:
            selected, pricing = effective_price(
                model, requested_at or datetime.now(UTC), mode_override=billing_mode
            )
            pricing["timestamp_basis"] = "request_start" if requested_at else "heartbeat_received"
        amount, warning = calculate_cost(delta, selected)
        values = delta.model_dump(mode="json")
        values["cost"] = str(amount)
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
                "pricing": pricing,
            },
            expected_derive_round=expected_derive_round,
        )
        return {"events": events, "cost": amount, "warning": warning}

    async def conclude(
        self,
        tid: UUID,
        agent_id: str,
        reason: str,
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._write(
            tid,
            "conclude",
            "scheduler",
            {"agent_id": agent_id, "reason": reason},
            expected_derive_round=expected_derive_round,
        )

    async def take_grace(
        self, tid: UUID, agent_id: str, *, expected_derive_round: int | None = None
    ) -> int:
        events = await self._write(
            tid,
            "take_grace",
            agent_id,
            {"agent_id": agent_id},
            expected_derive_round=expected_derive_round,
        )
        return events[0]["payload"]["grace_left"]

    async def finish_agent(
        self,
        tid: UUID,
        agent_id: str,
        receipt: dict[str, Any],
        end_reason: str,
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._write(
            tid,
            "finish_agent",
            agent_id,
            {"agent_id": agent_id, "receipt": receipt, "end_reason": end_reason},
            expected_derive_round=expected_derive_round,
        )

    async def submit_close(
        self,
        tid: UUID,
        agent_id: str,
        request: SubmitCloseRequest | dict[str, Any],
        *,
        report_uri: str | None = None,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        data = SubmitCloseRequest.model_validate(request).model_dump(mode="json")
        data["report_uri"] = report_uri
        if report_uri and not await self.objects.exists(report_uri):
            raise RuleViolation("report_missing", "报告 uri 不存在，请先持久化终结报告。")
        return await self._write(
            tid,
            "submit_close",
            agent_id,
            data,
            expected_derive_round=expected_derive_round,
        )

    async def record_tool_call(
        self,
        tid: UUID,
        agent_id: str,
        call: dict[str, Any],
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        data = {**call, "agent_id": agent_id}
        if data.get("result_head"):
            data["result_head"] = data["result_head"][:4096]
        return await self._write(
            tid,
            "record_tool_call",
            agent_id,
            data,
            expected_derive_round=expected_derive_round,
        )

    async def record_agent_trace(
        self,
        tid: UUID,
        agent_id: str,
        trace: dict[str, Any],
        *,
        expected_derive_round: int | None = None,
    ) -> list[dict[str, Any]]:
        kind = trace.get("kind")
        step = trace.get("step")
        uri = trace.get("uri")
        summary = trace.get("summary")
        if (
            kind not in {"initial_context", "board_update", "model_output", "model_error"}
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
            self._check_derive_round(agent, expected_derive_round)
            if agent is None or agent["status"] not in {"running", "concluding"}:
                raise RuleViolation("agent_inactive", "Trace 必须属于当前任务的活动 Agent。")
            duplicates = [s.events.c.payload["uri"].astext == uri]
            if kind == "initial_context":
                same_agent_and_kind = (s.events.c.payload["agent_id"].astext == agent_id) & (
                    s.events.c.payload["kind"].astext == kind
                )
                if agent["task_type"] == "derive":
                    derive_round = expected_derive_round or 1
                    same_agent_and_kind &= func.coalesce(
                        s.events.c.payload["derive_round"].astext, "1"
                    ) == str(derive_round)
                duplicates.append(same_agent_and_kind)
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
            if agent["task_type"] == "derive":
                payload["derive_round"] = expected_derive_round or 1
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
