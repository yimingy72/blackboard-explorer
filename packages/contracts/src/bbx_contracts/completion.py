"""Shared completion predicates for scheduling and transactional guards."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def all_met(task: Mapping[str, Any]) -> bool:
    acceptance = task.get("acceptance_state") or {}
    return bool(acceptance) and all(item.get("status") == "met" for item in acceptance.values())


def explicit_completion(task: Mapping[str, Any]) -> bool:
    return (
        all_met(task)
        and int(task.get("last_judgment_version") or 0) >= int(task.get("last_change_version") or 0)
        and all(
            item.get("completion_basis") == "explicit"
            and isinstance(item.get("completion_reason"), str)
            and bool(item["completion_reason"].strip())
            and bool(item.get("evidence_facts"))
            for item in task["acceptance_state"].values()
        )
    )


def current_review(task: Mapping[str, Any], agents: Mapping[str, Any]) -> dict[str, Any] | None:
    candidates = []
    for agent in agents.values():
        receipt = agent.get("receipt") or {}
        data = receipt.get("data") or {}
        excluded = data.get("excluded")
        if (
            agent.get("task_type") == "derive"
            and agent.get("derive_review")
            and agent.get("status") == "finished"
            and agent.get("end_reason") == "normal"
            and agent.get("derive_from_version") == int(task.get("last_change_version") or 0)
            and int(agent.get("finished_version") or 0) > 0
            and receipt.get("accepted") is True
            and "raw_text" not in receipt
            and data.get("posted") == []
            and isinstance(excluded, list)
            and any(isinstance(item, str) and item.strip() for item in excluded)
        ):
            candidates.append(agent)
    return max(candidates, key=lambda item: item["finished_version"]) if candidates else None


def quiescent(agents: Mapping[str, Any], intents: Mapping[str, Any]) -> bool:
    return not any(
        item["task_type"] in {"explore", "derive"} and item["status"] in {"running", "concluding"}
        for item in agents.values()
    ) and not any(item["status"] in {"open", "claimed"} for item in intents.values())


def can_accept(
    task: Mapping[str, Any], agents: Mapping[str, Any], intents: Mapping[str, Any]
) -> bool:
    if explicit_completion(task):
        return True
    review = current_review(task, agents)
    return bool(
        all_met(task)
        and quiescent(agents, intents)
        and review
        and int(task.get("last_judgment_version") or 0) >= int(review["finished_version"])
    )
