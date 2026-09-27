"""Pure command decisions. All persistent changes are described as events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn

from bbx_contracts.models import Params, Price, Usage


class RuleViolation(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class BoardState:
    task: dict[str, Any]
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    intents: dict[str, dict[str, Any]] = field(default_factory=dict)
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)


def fail(code: str, message: str) -> NoReturn:
    raise RuleViolation(code, message)


def event(
    kind: str,
    actor: str,
    payload: dict[str, Any],
    object_id: str | None = None,
    addressed_to: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": kind,
        "actor": actor,
        "payload": payload,
        "object_id": object_id,
        "addressed_to": addressed_to,
    }


def _require_refs(ids: list[str], objects: dict[str, Any], label: str) -> None:
    for oid in ids:
        if oid not in objects:
            fail("invalid_reference", f"{label} {oid} 不存在，请改用已有编号。")


def dispute_fields(facts: dict[str, dict[str, Any]]) -> dict[str, dict[str, int | str]]:
    """Resolve the acyclic dispute graph newest first, including counter-disputes."""
    result: dict[str, dict[str, int | str]] = {}
    disputers: dict[str, list[str]] = {fid: [] for fid in facts}
    for fid, fact in facts.items():
        for target in fact["disputes"]:
            if target in disputers:
                disputers[target].append(fid)
    depths: dict[str, int] = {}
    for fid, fact in facts.items():
        depths[fid] = 1 + max((depths[x] for x in fact["disputes"]), default=-1)
    for fid in reversed(list(facts)):
        fact = facts[fid]
        result[fid] = {
            "status": "disputed"
            if any(result[x]["status"] == "proposed" for x in disputers[fid])
            else "proposed",
            "depth": depths[fid],
            "relied_by": sum(
                x["author"] != fact["author"] and fid in x["derived_from"] for x in facts.values()
            ),
        }
    return result


def pending_claims(state: BoardState) -> bool:
    unmet = {
        aid for aid, item in state.task["acceptance_state"].items() if item["status"] == "unmet"
    }
    judged = state.task["last_judgment_version"]
    return any(
        f["version"] > judged and unmet.intersection(f["satisfies"]) for f in state.facts.values()
    )


def calculate_cost(usage: Usage, price: Price) -> tuple[Decimal, str | None]:
    if any(x is None for x in (price.cache_hit_per_m, price.cache_miss_per_m, price.output_per_m)):
        return Decimal(0), "价格未配置"
    assert price.cache_hit_per_m is not None
    assert price.cache_miss_per_m is not None
    assert price.output_per_m is not None
    cost = (
        Decimal(usage.cache_hit_tokens) * price.cache_hit_per_m
        + Decimal(usage.cache_miss_tokens) * price.cache_miss_per_m
        + Decimal(usage.output_tokens) * price.output_per_m
    ) / Decimal(1_000_000)
    return cost, None


def _id(state: BoardState, kind: str) -> str:
    value = state.counters.get(kind, 0) + 1
    return f"agent-{value}" if kind == "agent" else f"{kind}{value}"


def _agent(state: BoardState, aid: str) -> dict[str, Any]:
    agent = state.agents.get(aid)
    if agent is None:
        fail("agent_missing", f"Agent {aid} 不存在，请使用已登记的 Agent。")
    return agent


def _active_agent(state: BoardState, aid: str) -> dict[str, Any]:
    agent = _agent(state, aid)
    if agent["status"] not in {"running", "concluding"}:
        fail("agent_inactive", f"Agent {aid} 已结束，不能继续写入黑板。")
    return agent


def _claim(state: BoardState, iid: str, aid: str, actor: str) -> list[dict[str, Any]]:
    intent = state.intents.get(iid)
    if intent is None:
        fail("invalid_reference", f"意图 {iid} 不存在，请使用已有编号。")
    _agent(state, aid)
    if intent["status"] != "open":
        fail("intent_unavailable", f"意图 {iid} 已被认领或关闭，请选择 open 意图。")
    if state.agents[aid]["status"] not in {"running", "concluding"}:
        fail("agent_inactive", f"Agent {aid} 已结束，不能认领意图。")
    if any(item["holder"] == aid for item in state.intents.values()):
        fail("already_holding", f"Agent {aid} 已持有一条意图，请先关闭或释放它。")
    return [event("intent.claimed", actor, {"intent_id": iid, "holder": aid}, iid)]


def _release(
    state: BoardState, iid: str, aid: str, note: str, counted: bool
) -> list[dict[str, Any]]:
    intent = state.intents.get(iid)
    if not intent or intent["holder"] != aid:
        fail("not_holder", f"{aid} 未持有意图 {iid}，请先确认认领状态。")
    if not note.strip():
        fail("note_required", "释放意图必须写交接说明，请补充 note。")
    events = [
        event(
            "intent.released",
            aid,
            {"intent_id": iid, "holder": aid, "note": note, "counted": counted},
            iid,
        )
    ]
    if counted and intent["attempts"] + 1 >= int(
        state.task["params"].get("intent_max_attempts", 3)
    ):
        events.append(
            event(
                "intent.closed",
                "system",
                {
                    "intent_id": iid,
                    "result": "inconclusive",
                    "by": "system",
                    "reason": "多次尝试未完成",
                },
                iid,
            )
        )
    return events


def decide(
    state: BoardState, command: str, actor: str, data: dict[str, Any]
) -> list[dict[str, Any]]:
    """Validate a command and return ordered events without modifying state."""
    task = state.task
    if command == "record_archive":
        if task["status"] not in {"finished", "failed", "stopped"}:
            fail("archive_not_terminal", "任务尚未结束，不能登记工作区归档。")
        if any(agent["status"] in {"running", "concluding"} for agent in state.agents.values()):
            fail("archive_agents_active", "仍有运行中的 Agent，不能登记工作区归档。")
        if data["uri"] != f"workspace/{state.task['id']}.tar.zst":
            fail("archive_invalid_uri", "工作区归档 key 与任务不匹配。")
        if data["size"] < 0 or data["fallback"] not in {"none", "agents-only"}:
            fail("archive_invalid_metadata", "工作区归档大小或退化标记无效。")
        if task.get("workspace_uri") == data["uri"]:
            return []
        if task.get("workspace_uri"):
            fail("archive_conflict", "任务已经登记了另一份工作区归档。")
        return [event("task.archived", actor, data)]
    if command == "transition":
        status = data["status"]
        old = task["status"]
        if old in {"finished", "failed", "stopped"}:
            if status == old:
                return []
            fail("invalid_transition", f"任务已处于终态 {old}，不能改为 {status}。")
        next_statuses = {
            "created": {"provisioning"},
            "provisioning": {"running"},
            "running": {"closing"},
            "closing": {"finished"},
        }
        if status not in next_statuses.get(old, set()) and status not in {"failed", "stopped"}:
            fail("invalid_transition", f"任务不能从 {old} 进入 {status}，请按生命周期迁移。")
        return [event(f"task.{status}", actor, {"status": status, "reason": data.get("reason")})]
    if command == "post_fact":
        _active_agent(state, actor)
        evidence = data.get("evidence", [])
        if not evidence:
            fail("evidence_required", "事实必须附证据，请添加已持久化的 evidence。")
        if data["kind"] == "inference" and not data.get("derived_from"):
            fail("derived_from_required", "推断事实必须引用依据事实，请填写 derived_from。")
        for key in ("derived_from", "disputes"):
            _require_refs(data.get(key, []), state.facts, "事实")
        acceptance = {x["id"]: x for x in task["acceptance"]}
        _require_refs(data.get("satisfies", []), acceptance, "验收项")
        iid = data.get("resolves")
        if bool(iid) != bool(data.get("result")):
            fail("result_required", "resolves 与 result 必须同时填写，请补齐或都移除。")
        if iid and (iid not in state.intents or state.intents[iid]["holder"] != actor):
            fail("not_holder", f"只有意图 {iid} 的持有者能用事实关闭它，请先认领。")
        fid = _id(state, "F")
        payload = {
            **data,
            "id": fid,
            "author": actor,
            "provenance": "tool_backed"
            if any(
                e.get("call_id") in state.tool_calls
                and state.tool_calls[e["call_id"]]["agent_id"] == actor
                for e in evidence
                if e.get("call_id")
            )
            else "self_reported",
        }
        before = dispute_fields(state.facts)
        after = dispute_fields({**state.facts, fid: payload})
        events = [event("fact.posted", actor, payload, fid)]
        if iid:
            events.append(
                event(
                    "intent.closed",
                    actor,
                    {"intent_id": iid, "result": data["result"], "by": fid, "reason": None},
                    iid,
                )
            )
        reverted: set[str] = set()
        for target in state.facts:
            if before[target]["status"] != after[target]["status"]:
                depth = int(after[fid]["depth"])
                notified = (
                    [state.facts[target]["author"]]
                    if depth <= int(task["params"].get("dispute_notify_depth", 2))
                    else None
                )
                kind = (
                    "fact.disputed" if after[target]["status"] == "disputed" else "fact.undisputed"
                )
                events.append(
                    event(
                        kind,
                        "system",
                        {"fact_id": target, "by": fid, "depth": depth},
                        target,
                        notified,
                    )
                )
                if kind == "fact.disputed":
                    for aid, item in task["acceptance_state"].items():
                        if (
                            aid not in reverted
                            and item["status"] == "met"
                            and target in item["evidence_facts"]
                        ):
                            reverted.add(aid)
                            events.append(
                                event(
                                    "acceptance.reverted",
                                    "system",
                                    {"id": aid, "fact_id": target},
                                    aid,
                                )
                            )
        return events
    if command == "post_intent":
        _active_agent(state, actor)
        if data.get("claim") and any(item["holder"] == actor for item in state.intents.values()):
            fail("already_holding", f"Agent {actor} 已持有一条意图，请先关闭或释放它。")
        for key in ("statement", "expected", "method"):
            if not str(data.get(key, "")).strip():
                fail("intent_incomplete", f"意图缺少 {key}，请补全调查方向、预期与方法。")
        if not data.get("based_on") or not data.get("relates_to"):
            fail(
                "intent_incomplete", "意图必须有 based_on 和 relates_to，请补全事实依据与验收关联。"
            )
        _require_refs(data["based_on"], state.facts, "事实")
        _require_refs(data["relates_to"], {x["id"]: x for x in task["acceptance"]}, "验收项")
        retry = data.get("retry_of")
        if retry:
            previous = state.intents.get(retry)
            if (
                not previous
                or previous["result"] != "inconclusive"
                or previous["method"] == data["method"]
            ):
                fail(
                    "invalid_retry",
                    "retry_of 必须指向已关闭为 inconclusive 且方法不同的意图，请更换方法。",
                )
        iid = _id(state, "I")
        payload = {**data, "id": iid, "author": actor, "claim": bool(data.get("claim"))}
        return [event("intent.posted", actor, payload, iid)]
    if command in {"claim", "claim_for"}:
        return _claim(state, data["intent_id"], data.get("agent_id", actor), actor)
    if command == "release":
        agent = _agent(state, actor)
        counted = not (
            agent["status"] == "concluding" and agent.get("conclude_reason") == "closing"
        )
        return _release(state, data["intent_id"], actor, data["note"], counted)
    if command == "system_close":
        iid = data["intent_id"]
        intent = state.intents.get(iid)
        if (
            not intent
            or intent["status"] == "closed"
            or intent["attempts"] < int(task["params"].get("intent_max_attempts", 3))
        ):
            fail("close_not_allowed", f"意图 {iid} 未达到尝试上限或已关闭，请检查 attempts。")
        return [
            event(
                "intent.closed",
                "system",
                {
                    "intent_id": iid,
                    "result": "inconclusive",
                    "by": "system",
                    "reason": "多次尝试未完成",
                },
                iid,
            )
        ]
    if command == "register_agent":
        if task["status"] in {"finished", "failed", "stopped"}:
            fail("task_terminal", "任务已结束，不能再登记 Agent。")
        aid = _id(state, "agent")
        kind = data["task_type"]
        if kind not in {"explore", "derive", "close"}:
            fail("invalid_agent_type", "Agent 类型必须是 explore、derive 或 close。")
        if kind == "close" and any(
            agent["task_type"] == "close" and agent["status"] in {"running", "concluding"}
            for agent in state.agents.values()
        ):
            fail("close_already_running", "已有收尾 Agent 在运行，不能重复登记。")
        if kind == "close" and data.get("close_mode") not in {"judge", "final"}:
            fail("close_mode_required", "close Agent 必须指定 judge 或 final 模式。")
        derive_fields = {}
        if kind == "derive":
            expected = data.get("derive_parallel")
            active = [
                agent
                for agent in state.agents.values()
                if agent["status"] in {"running", "concluding"}
            ]
            workers = [agent for agent in active if agent["task_type"] in {"explore", "derive"}]
            explore_active = any(agent["task_type"] == "explore" for agent in workers)
            derive_active = any(agent["task_type"] == "derive" for agent in workers)
            latest_fact = max((fact["version"] for fact in state.facts.values()), default=0)
            if expected is not None:
                params = Params.model_validate(task["params"])
                budget = task["budget"]
                spent = Decimal(str(task["usage"].get("cost", 0) or 0))
                limit = Decimal(str(budget["max_cost"])) * (1 - params.close_reserve_ratio)
                started = task.get("started_at")
                budget_exhausted = spent >= limit or (
                    started is not None
                    and datetime.now(UTC) >= started + timedelta(minutes=int(budget["max_minutes"]))
                )
                closing_due = (
                    task["status"] != "running"
                    or budget_exhausted
                    or task["derive_empty_streak"] >= params.derive_empty_limit
                    or all(item["status"] == "met" for item in task["acceptance_state"].values())
                )
                open_intents = any(intent["status"] == "open" for intent in state.intents.values())
                claimed_intents = any(
                    intent["status"] == "claimed" for intent in state.intents.values()
                )
                judging = any(agent["task_type"] == "close" for agent in active)
                if expected:
                    latest_derive = max(
                        (
                            agent["derive_from_version"]
                            for agent in state.agents.values()
                            if agent["task_type"] == "derive"
                            and agent.get("derive_from_version") is not None
                        ),
                        default=0,
                    )
                    valid_phase = (
                        explore_active
                        and not derive_active
                        and not open_intents
                        and not judging
                        and len(workers) < int(budget["max_concurrent_agents"])
                        and latest_fact > latest_derive
                    )
                else:
                    valid_phase = (
                        not workers
                        and not open_intents
                        and not claimed_intents
                        and not judging
                        and task["last_judgment_version"] >= task["last_change_version"]
                    )
                if not params.derive_enabled or closing_due or not valid_phase:
                    fail("stale_derive", "推导派发条件已变化，请重新调度。")
            derive_fields = {
                "derive_from_version": latest_fact,
                "derive_parallel": explore_active if expected is None else expected,
            }
        return [
            event(
                "agent.spawned",
                actor,
                {
                    "id": aid,
                    **data,
                    **derive_fields,
                    "judge_from_version": task.get("version", 0) if kind == "close" else None,
                },
                aid,
            )
        ]
    if command == "heartbeat":
        agent = _agent(state, data["agent_id"])
        if agent["status"] not in {"running", "concluding"}:
            fail("agent_inactive", "Agent 已结束，不能提交心跳。")
        if any(data[key] < 0 for key in ("steps", "context_tokens", "last_seen_version")):
            fail("invalid_progress", "心跳计数不能为负，请提交非负步数、上下文长度与版本。")
        delta = data["usage"]
        previous = task["usage"]
        usage = {
            key: previous.get(key, 0) + delta.get(key, 0) for key in previous.keys() | delta.keys()
        }
        exhausted = Decimal(str(usage.get("cost", 0))) >= Decimal(str(task["budget"]["max_cost"]))
        return [
            event("agent.progress", actor, data, data["agent_id"]),
            event("budget.updated", "system", {"usage": usage, "exhausted": exhausted}),
        ]
    if command == "conclude":
        agent = _agent(state, data["agent_id"])
        if agent["status"] != "running":
            fail("agent_inactive", "Agent 不在 running 状态，不能重复 conclude。")
        return [
            event("agent.conclude_requested", actor, data, data["agent_id"], [data["agent_id"]])
        ]
    if command == "take_grace":
        agent = _agent(state, data["agent_id"])
        if agent["status"] != "concluding" or not agent["grace_calls_left"]:
            fail("grace_exhausted", "交接宽限已用尽，不能继续调用工具。")
        return [
            event(
                "agent.progress",
                actor,
                {"agent_id": data["agent_id"], "grace_left": agent["grace_calls_left"] - 1},
                data["agent_id"],
            )
        ]
    if command == "submit_close":
        agent = _agent(state, actor)
        if agent["task_type"] != "close" or agent["status"] != "running":
            fail("not_close_agent", "只有运行中的 close Agent 可以提交裁定。")
        verdicts = data["verdicts"]
        expected = {x["id"] for x in task["acceptance"]}
        if {x["id"] for x in verdicts} != expected or len(verdicts) != len(expected):
            fail("verdicts_incomplete", "裁定必须恰好覆盖所有验收项，请补齐 verdicts。")
        status = dispute_fields(state.facts)
        for item in verdicts:
            _require_refs(item.get("evidence_facts", []), state.facts, "事实")
            if item["verdict"] == "met" and (
                not item.get("evidence_facts")
                or any(status[x]["status"] != "proposed" for x in item["evidence_facts"])
            ):
                fail(
                    "invalid_evidence_fact",
                    "met 必须引用至少一条未被争议的支撑事实，请修改 evidence_facts。",
                )
            if item["verdict"] == "unmet" and not item.get("missing"):
                fail("missing_required", "unmet 裁定必须说明缺什么，请填写 missing。")
        result = [
            event(
                "acceptance.judged",
                actor,
                {
                    "verdicts": verdicts,
                    "mode": agent["close_mode"],
                    "judge_from_version": agent["judge_from_version"],
                },
            )
        ]
        if agent["close_mode"] == "final":
            if task["status"] != "closing":
                fail("invalid_transition", "终结报告只能在 closing 状态提交，请先进入收尾。")
            if not data.get("report_uri"):
                fail("report_required", "终结模式必须提供已持久化的报告 uri。")
            result += [
                event("task.report", actor, {"uri": data["report_uri"]}),
                event("task.finished", actor, {"status": "finished", "reason": None}),
            ]
        return result
    if command == "finish_agent":
        aid = data["agent_id"]
        agent = _agent(state, aid)
        if agent["status"] not in {"running", "concluding"}:
            fail("agent_inactive", "Agent 已结束，不能重复 finish。")
        reason = data["end_reason"]
        concluding_for_limit = (
            agent["status"] == "concluding" and agent.get("conclude_reason") == "limit"
        )
        concluding_for_closing = (
            agent["status"] == "concluding" and agent.get("conclude_reason") == "closing"
        )
        counted = reason != "runtime_restart" and (
            reason in {"heartbeat", "runtime_error"}
            or (
                not concluding_for_closing
                and (
                    reason in {"refused", "grace_timeout"}
                    or (reason == "limit" and agent["status"] == "concluding")
                    or concluding_for_limit
                )
            )
        )
        result: list[dict[str, Any]] = []
        for iid, intent in state.intents.items():
            if intent["holder"] == aid:
                result += _release(state, iid, aid, "由系统强制释放，持有者未完成交接", counted)
        if agent["task_type"] == "derive" and reason == "normal":
            receipt = data.get("receipt") or {}
            detail = receipt.get("data")
            if receipt.get("accepted") is True and isinstance(detail, dict) and "posted" in detail:
                result.append(
                    event(
                        "derive.result",
                        aid,
                        {
                            "posted": detail["posted"],
                            "excluded": detail.get("excluded", []),
                            "derive_parallel": agent.get("derive_parallel", False),
                        },
                        aid,
                    )
                )
        result.append(event("agent.finished", aid, data, aid))
        if (
            task["status"] not in {"finished", "failed", "stopped"}
            and reason == "runtime_error"
            and task["failure_streak"] + 1 >= int(task["params"].get("max_consecutive_failures", 3))
        ):
            result.append(
                event("task.failed", "system", {"status": "failed", "reason": "Agent 连续运行失败"})
            )
        elif (
            task["status"] not in {"finished", "failed", "stopped"}
            and reason != "runtime_restart"
            and agent.get("is_seed", False)
            and not (state.facts or state.intents)
            and task["seed_empty_count"] + 1 >= 2
        ):
            result.append(
                event("task.failed", "system", {"status": "failed", "reason": "种子无法起步"})
            )
        return result
    if command == "record_tool_call":
        _active_agent(state, data["agent_id"])
        if data["id"] in state.tool_calls:
            fail("duplicate_tool_call", "工具调用编号已存在，请使用新的 call_id。")
        return [event("tool_call.recorded", actor, data, data["id"])]
    fail("unknown_command", f"未知命令 {command}，请使用黑板公开方法。")
