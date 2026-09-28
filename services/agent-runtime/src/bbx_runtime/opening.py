"""Pinned profile templates and the first context for one Agent run."""

import json
from typing import Any

from agent_framework import ContextProvider, SessionContext
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.context import RunContext
from bbx_runtime.session import SessionCheckpoint
from bbx_runtime.trace import record_trace


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _acceptance(task: dict[str, Any]) -> str:
    state = task.get("acceptance_state", {})
    lines = []
    for item in task["acceptance"]:
        result = state.get(item["id"], {})
        lines.append(
            f"- {item['id']} {item['desc']}｜状态：{result.get('status', 'unmet')}"
            f"｜最近裁定理由：{result.get('reason') or '无'}"
            f"｜缺口：{result.get('missing') or '无'}"
            f"｜完成依据：{result.get('completion_basis') or 'inferred'} "
            f"{result.get('completion_reason') or ''}"
            f"｜支撑事实：{', '.join(result.get('evidence_facts') or []) or '无'}"
        )
    return "\n".join(lines)


def _fact_summary(fact: dict[str, Any]) -> dict[str, object]:
    return {
        "id": fact["id"],
        "kind": fact["kind"],
        "status": fact["status"],
        "statement": fact["statement"],
        "provenance": fact.get("provenance"),
        "relied_by": fact.get("relied_by", 0),
        "satisfies": fact.get("satisfies", []),
        "evidence": [
            {"type": item.get("type"), "summary": item.get("summary"), "uri": item.get("uri")}
            for item in fact.get("evidence", [])
        ],
    }


def _intent_summary(intent: dict[str, Any]) -> dict[str, object]:
    return {
        "id": intent["id"],
        "statement": intent["statement"],
        "status": intent["status"],
        "result": intent.get("result"),
        "based_on": intent.get("based_on", []),
        "expected": intent.get("expected"),
        "method": intent.get("method"),
        "relates_to": intent.get("relates_to", []),
        "retry_of": intent.get("retry_of"),
        "notes": intent.get("notes", []),
    }


def _previous_excluded(agents: dict[str, dict[str, Any]]) -> str:
    previous = [
        agent
        for agent in agents.values()
        if agent.get("task_type") == "derive" and agent.get("finished_at")
    ]
    if not previous:
        return "无（这是首次推导）"
    latest = max(previous, key=lambda agent: str(agent["finished_at"]))
    receipt = latest.get("receipt") or {}
    detail = receipt.get("data") if isinstance(receipt, dict) else None
    excluded = detail.get("excluded", []) if isinstance(detail, dict) else []
    return _json(excluded) if excluded else "上次未列出排除理由"


class OpeningContextProvider(ContextProvider):
    def __init__(self, run: RunContext, checkpoint: SessionCheckpoint | None = None) -> None:
        super().__init__(source_id=f"opening:{run.agent_id}")
        self.run = run
        self.environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        self.trace_recorded = False
        self.checkpoint = checkpoint
        self.prompt_revision = checkpoint.prompt_revision if checkpoint else 0
        self.current_instructions = checkpoint.opening_instructions if checkpoint else ""

    async def render(self, source: str | None = None, board: dict[str, Any] | None = None) -> str:
        run = self.run
        board = board or run.state
        task = board["task"]
        facts = board.get("facts", {})
        intents = board.get("intents", {})
        data: dict[str, object] = {
            "goal": task["goal"],
            "domain_context": task.get("domain_context") or "无",
            "acceptance_status": _acceptance(task),
            # Published profile versions may still reference this placeholder.
            "budget_left": "由系统管理",
            "agent_id": run.agent_id,
            "seed_max_steps": run.params.seed_max_steps,
            "conclude_grace_calls": run.params.conclude_grace_calls,
            "current_intent": None,
            "yaml_snapshot": "",
            "facts_text": "",
            "intents_text": "",
            "previous_excluded": "",
            "satisfies_facts": "",
            "judgment_history": "",
            "mode": run.mode or "",
        }
        if run.task_type == "explore" and run.intent_id:
            intent = intents.get(run.intent_id)
            if intent is None:
                raise ValueError(f"current intent {run.intent_id} missing from board state")
            based_on = [_fact_summary(facts[fid]) for fid in intent["based_on"] if fid in facts]
            data["current_intent"] = _json(
                {"intent": _intent_summary(intent), "based_on_facts": based_on}
            )
            data["yaml_snapshot"] = await run.board.snapshot(
                run.task_id, max_lines=run.params.snapshot_max_lines
            )
        elif run.task_type == "derive":
            data["facts_text"] = _json([_fact_summary(fact) for fact in facts.values()])
            data["intents_text"] = _json([_intent_summary(intent) for intent in intents.values()])
            data["previous_excluded"] = _previous_excluded(board.get("agents", {}))
        elif run.task_type == "close":
            claimed = {
                item["id"]: [
                    _fact_summary(fact)
                    for fact in facts.values()
                    if item["id"] in fact.get("satisfies", [])
                ]
                for item in task["acceptance"]
            }
            data["satisfies_facts"] = _json(claimed)
            data["yaml_snapshot"] = await run.board.snapshot(
                run.task_id, max_lines=run.params.snapshot_max_lines
            )
            events = await run.board.events(run.task_id, since=0)
            data["judgment_history"] = _json(
                [
                    {"version": event["version"], "verdicts": event["payload"]["verdicts"]}
                    for event in events
                    if event["type"] == "acceptance.judged"
                ]
            )

        if source is None:
            source = getattr(run.profile.prompt_templates, run.task_type)
        assert isinstance(source, str)
        rendered = self.environment.from_string(source).render(**data)
        if run.task_type == "derive" and board.get("agents", {}).get(run.agent_id, {}).get(
            "derive_review"
        ):
            rendered += (
                "\n\n# 本次运行：完成前必要复核\n"
                "即使现有验收已标 met，仍检查目标范围、完成证据和未验证事项。"
                "找到有事实依据且值得继续的缺口，就用 post_intent 提交；"
                "不要机械重复或扩展无关范围。"
                "没有必要方向时，回执 posted 留空，excluded 至少说明一项具体复核与排除理由。"
                "只有实际提交到黑板的意图才算产出；没有新方向不自动证明目标已经完成。"
            )
        if run.task_type == "close":
            reviews = [
                agent for agent in board.get("agents", {}).values() if agent.get("derive_review")
            ]
            latest = max(
                reviews, key=lambda agent: int(agent.get("finished_version") or 0), default=None
            )
            summary = (
                {
                    key: latest.get(key)
                    for key in (
                        "id",
                        "status",
                        "end_reason",
                        "derive_from_version",
                        "finished_version",
                        "receipt",
                    )
                }
                if latest
                else "尚未进行完成前必要复核"
            )
            rendered += (
                "\n\n# 完成依据协议与最近复核\n"
                "只有范围明确、直接证据覆盖验收、没有影响完成结论的未验证事项时，"
                "可将该项 met 的 completion_basis 标为 explicit，"
                "并写 completion_reason 和支撑事实。"
                "其他情况用 inferred；不能用未发现更多推断已完整完成。"
                "若完整性是验收要求但范围未确认，保持 unmet；inferred 不降低 met 的证据要求。"
                "请结合下面的复核记录重新裁定，复核空产本身不是完成证明。\n" + _json(summary)
            )
        return rendered

    async def refresh(self, board: dict[str, Any] | None = None, *, step: int = 0) -> str:
        """Read the live template and keep exactly one rendered system instruction."""
        getter = getattr(self.run.service, "get_worker_prompt", None)
        if getter is not None:
            try:
                latest = await getter(self.run.task_type, since=self.prompt_revision)
            except RemoteError as error:
                if error.status != 404:
                    raise
            else:
                revision = int(latest["revision"])
                if revision < self.prompt_revision:
                    raise ValueError("Worker prompt revision moved backwards")
                source = latest.get("prompt")
                if source is not None:
                    if not isinstance(source, str):
                        raise ValueError("Worker prompt must be text")
                    previous = self.run.profile.prompt_templates
                    pinned = getattr(previous, self.run.task_type)
                    current_source = (
                        self.checkpoint.session.state.get("bbx_prompt_source", pinned)
                        if self.checkpoint
                        else getattr(self, "_prompt_source", pinned)
                    )
                    if source != current_source:
                        self.current_instructions = await self.render(source, board)
                        if self.checkpoint:
                            self.checkpoint.opening_instructions = self.current_instructions
                            self.checkpoint.session.state["bbx_prompt_source"] = source
                            self.checkpoint.prompt_revision = revision
                            await self.checkpoint.save()
                        else:
                            self._prompt_source = source
                        if step:
                            await record_trace(
                                self.run,
                                "board_update",
                                step,
                                "[Worker 系统提示词更新，版本 "
                                f"{revision}]\n{self.current_instructions}",
                            )
                    elif self.checkpoint and revision != self.prompt_revision:
                        self.checkpoint.prompt_revision = revision
                        if self.current_instructions:
                            await self.checkpoint.save()
                self.prompt_revision = revision
        if not self.current_instructions:
            self.current_instructions = await self.render(board=board)
            if self.checkpoint:
                self.checkpoint.opening_instructions = self.current_instructions
                await self.checkpoint.save()
        return self.current_instructions

    async def before_run(
        self,
        *,
        agent: Any,
        session: Any,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        rendered = (
            await self.refresh() if not self.current_instructions else self.current_instructions
        )
        context.extend_instructions(self.source_id, rendered)
        if not self.trace_recorded:
            await record_trace(self.run, "initial_context", 0, f"开始。\n\n{rendered}")
            self.trace_recorded = True
