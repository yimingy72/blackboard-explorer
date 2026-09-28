from copy import deepcopy

import pytest
from bbx_contracts.completion import can_accept, current_review, explicit_completion


def board():
    task = {
        "acceptance_state": {
            "A1": {"status": "met", "completion_basis": "inferred", "evidence_facts": ["F1"]}
        },
        "last_change_version": 10,
        "last_judgment_version": 10,
    }
    review = {
        "id": "agent-2",
        "task_type": "derive",
        "derive_review": True,
        "derive_from_version": 10,
        "finished_version": 15,
        "status": "finished",
        "end_reason": "normal",
        "receipt": {
            "accepted": True,
            "data": {"posted": [], "excluded": ["Checked remaining scope and assumptions"]},
        },
    }
    return task, {"agent-2": review}


def test_explicit_requires_scope_reason_evidence_and_current_judgment():
    task, agents = board()
    assert not can_accept(task, {}, {})
    task["acceptance_state"]["A1"].update(
        completion_basis="explicit", completion_reason="Direct criterion measured"
    )
    assert explicit_completion(task)
    assert can_accept(task, agents, {"I1": {"status": "claimed"}})
    task["last_change_version"] = 11
    assert not explicit_completion(task)


def test_review_must_be_followed_by_judgment_and_no_remaining_work():
    task, agents = board()
    assert current_review(task, agents) is not None
    assert not can_accept(task, agents, {})
    task["last_judgment_version"] = 20
    assert can_accept(task, agents, {})
    assert not can_accept(task, agents, {"I1": {"status": "open"}})
    agents["agent-3"] = {"task_type": "explore", "status": "concluding"}
    assert not can_accept(task, agents, {})


@pytest.mark.parametrize(
    "change",
    [
        {"derive_review": False},
        {"status": "running"},
        {"end_reason": "runtime_restart"},
        {"derive_from_version": 9},
        {"finished_version": None},
        {"receipt": {"accepted": True, "raw_text": "no JSON", "data": {}}},
        {"receipt": {"accepted": True, "data": {"posted": [], "excluded": []}}},
        {"receipt": {"accepted": True, "data": {"posted": ["I1"], "excluded": ["done"]}}},
    ],
)
def test_incomplete_or_outdated_reviews_do_not_open_completion_gate(change):
    task, agents = board()
    agents["agent-2"].update(deepcopy(change))
    assert current_review(task, agents) is None
