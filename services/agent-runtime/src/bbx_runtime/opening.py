"""Pinned profile templates and the first context for one Agent run."""

import json
from typing import Any

from agent_framework import ContextProvider, SessionContext
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from bbx_runtime.context import RunContext


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
    def __init__(self, run: RunContext) -> None:
        super().__init__(source_id=f"opening:{run.agent_id}")
        self.run = run
        self.environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

    async def render(self) -> str:
        run = self.run
        board = run.state
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

        source = getattr(run.profile.prompt_templates, run.task_type)
        return self.environment.from_string(source).render(**data)

    async def before_run(
        self,
        *,
        agent: Any,
        session: Any,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        context.extend_instructions(self.source_id, await self.render())
