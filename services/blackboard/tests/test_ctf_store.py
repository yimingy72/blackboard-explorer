"""Offline guard and native history checks."""

from datetime import timedelta

import pytest
from bbx_blackboard.ctf import check_budget, history_ids
from bbx_blackboard.store.repository import now
from fastapi import HTTPException


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
    with pytest.raises(HTTPException, match="budget exhausted"):
        check_budget(task)


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
