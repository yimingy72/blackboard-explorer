"""Render committed blackboard changes for either supported Pi execution host."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

DIGEST_TYPES = {"fact.posted", "intent.posted", "intent.closed", "fact.disputed", "fact.undisputed"}
VERDICT_TYPES = {"acceptance.judged", "acceptance.reverted"}


def _full_event(event: Mapping[str, Any]) -> str:
    return json.dumps(
        {key: event.get(key) for key in ("version", "type", "actor", "object_id", "payload")},
        ensure_ascii=False,
        default=str,
    )


def _digest(event: Mapping[str, Any]) -> str:
    payload = event.get("payload") or {}
    label = event.get("object_id") or payload.get("fact_id") or payload.get("intent_id") or "?"
    statement = payload.get("statement") or payload.get("result") or ""
    return f"[{label} {event.get('type')} by {event.get('actor')}] {str(statement)[:120]}"


def _digest_counts(events: Sequence[Mapping[str, Any]]) -> str:
    facts = sum(event.get("type") == "fact.posted" for event in events)
    intents = sum(event.get("type") == "intent.posted" for event in events)
    disputes = sum(event.get("type") in {"fact.disputed", "fact.undisputed"} for event in events)
    others = len(events) - facts - intents - disputes
    return (
        f"新增事实 {facts} / 意图 {intents} / 争议变化 {disputes} / "
        f"其他变化 {others}，用 get 或 search 查看"
    )


def render_events(events: Sequence[Mapping[str, Any]], aid: str, max_lines: int) -> list[str]:
    targeted: list[str] = []
    verdicts: list[str] = []
    digest: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("type") == "agent.message.posted":
            continue
        addressed = event.get("addressed_to")
        if addressed:
            if aid in addressed:
                targeted.append(f"[点名] {_full_event(event)}")
        elif event.get("type") in VERDICT_TYPES:
            verdicts.append(f"[裁定] {_full_event(event)}")
        elif event.get("type") in DIGEST_TYPES and event.get("actor") != aid:
            digest.append(event)
    lines = [*targeted, *verdicts]
    if len(digest) > max_lines:
        lines.append(_digest_counts(digest))
    else:
        lines.extend(_digest(event) for event in digest)
    return lines
