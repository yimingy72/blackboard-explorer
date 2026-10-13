"""Offline guard and native history checks."""

from datetime import timedelta
from typing import Any, cast

import pytest
from bbx_blackboard.ctf import check_budget, dispatch_message_condition, history_ids
from bbx_blackboard.store.repository import now
from fastapi import HTTPException
from sqlalchemy import column, create_engine, select, table, values


@pytest.mark.parametrize("blocked_ids", [["old", None], [None], ["old"]])
@pytest.mark.parametrize("delay_seconds", [None, -1, 2])
def test_context_dispatch_ignores_missing_model_ids_without_replaying_old_inputs(
    blocked_ids, delay_seconds
):
    instant = now()
    mailbox = table(
        "ctf_messages", column("id"), column("recipient_id"), column("kind"), column("sender_kind")
    )
    rows = (
        values(*mailbox.c)
        .data(
            [
                ("old", "member", "instruction", "agent"),
                ("new", "member", "instruction", "agent"),
                ("user", "member", "message", "user"),
                ("notice", "member", "message", "system"),
                ("retry", "member", "instruction", "system"),
            ]
        )
        .cte("ctf_messages")
    )
    control: dict[str, Any] = {
        "context_blocked_members": {"member": {"input_message_ids": blocked_ids}}
    }
    if delay_seconds is not None:
        control["turn_retry_delays"] = {
            "member": {
                "message_id": "retry",
                "ready_at": (instant + timedelta(seconds=delay_seconds)).isoformat(),
            }
        }
    condition = dispatch_message_condition({"ctf_control": control}, instant=instant)
    # Execute SQL three-valued logic rather than checking rendered query text.
    from sqlalchemy.sql.util import ClauseAdapter

    condition = ClauseAdapter(rows, adapt_on_names=True).traverse(condition)
    with create_engine("sqlite://").connect() as connection:
        ready = set(connection.execute(select(rows.c.id).where(condition)).scalars())
    assert ready == {
        "new",
        "user",
        *([] if "old" in blocked_ids else ["old"]),
        *(["retry"] if delay_seconds is None or delay_seconds < 0 else []),
    }


def test_delivery_requires_real_history_not_pending_or_metadata():
    session = {
        "state": {
            "in_memory": {
                "messages": [
                    {"role": "user", "message_id": "actual"},
                    {"role": "assistant", "message_id": "answer"},
                ]
            },
            "pending_messages": [{"role": "user", "message_id": "pending"}],
        },
        "untrusted": {"role": "user", "message_id": "forged"},
    }
    assert history_ids(session) == {"actual"}
    session["state"]["in_memory"]["messages"].append({"role": "user", "message_id": "actual"})
    with pytest.raises(HTTPException, match="Duplicate"):
        history_ids(session)


@pytest.mark.parametrize("change", ["cost", "time", "closing"])
def test_budget_and_closing_guard_blocks_new_requests(change):
    task = {
        "ctf_control": {"phase": "running"},
        "active_seconds": 0,
        "active_since": None,
        "usage": {"cost": "0.1"},
        "budget": {"max_cost": "1.0", "max_minutes": 1},
    }
    check_budget(task)
    if change == "cost":
        task["usage"]["cost"] = "1.0"
    elif change == "time":
        task["active_since"] = now() - timedelta(seconds=61)
    else:
        task["ctf_control"]["phase"] = "closing"
    with pytest.raises(HTTPException):
        check_budget(task)


def test_budget_guard_uses_persistent_reservations_not_a_magic_threshold():
    task = {
        "ctf_control": {"phase": "running"},
        "active_seconds": 0,
        "active_since": None,
        "usage": {"cost": "0.79"},
        "budget": {"max_cost": "1.0", "max_minutes": 1},
    }
    check_budget(task)
    task["usage"]["cost"] = "0.80"
    check_budget(task)
    task["ctf_control"]["budget_reservations"] = {"call-1": {"cost": "0.20"}}
    with pytest.raises(HTTPException) as caught:
        check_budget(task)
    assert isinstance(caught.value.detail, dict)
    assert cast(dict[str, Any], caught.value.detail)["code"] == "ctf_budget_wait"


def test_initial_input_preserves_long_goal_workflow_and_attachment_references():
    import json

    from bbx_blackboard.ctf import initial_input_parts

    fields = {
        "goal": "Analyze the supplied artifact. " * 1600,
        "domain_context": "Use the custom offline workflow; no platform submission.",
        "ctf_control": {"completion_requirements": "Deliver a reproducible analysis report."},
        "initial_attachments": [{"name": "sample.txt", "uri": "inputs/task/sample.txt"}],
    }
    parts = initial_input_parts(fields)
    assert len(parts) > 1
    assert all(len(part) <= 20000 for part in parts)
    restored = json.loads("".join(part.split("\n", 1)[1] for part in parts))
    assert restored["goal"] == fields["goal"]
    assert restored["domain_context"] == fields["domain_context"]
    assert restored["completion_requirements"] == fields["ctf_control"]["completion_requirements"]
    assert restored["initial_attachments"] == fields["initial_attachments"]
