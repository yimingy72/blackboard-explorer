"""Automatic plans are bounded without changing frozen manual snapshots."""

from uuid import uuid4

import pytest
from bbx_contracts.ctf import CtfOptions, CtfTaskCreate, CtfTeamPlanRequest, CtfWorkItem


def discover():
    return {
        "work_id": str(uuid4()),
        "kind": "discover",
        "source": {"namespace": "fixture"},
        "scope": {"objective": "List supplied challenges", "boundary": "Supplied inputs"},
        "deliverable": {"criteria": "Registered challenges"},
        "solve_template": {
            "source_namespace": "fixture",
            "boundary": "Supplied inputs",
            "deliverable": {"criteria": "Verified result"},
        },
    }


def test_old_options_roundtrip_and_explicit_mode_override():
    old = {"max_teammates": 4, "max_steps": 60, "context_threshold": 128000}
    assert CtfOptions.model_validate(old).model_dump() == old
    assert CtfOptions.model_validate(old).dispatch_mode == "manual"
    assert CtfOptions(dispatch_mode="manual").model_dump(exclude_unset=True) == {
        "dispatch_mode": "manual"
    }
    assert CtfOptions(dispatch_mode="automatic").first_plan_max_calls == 3
    with pytest.raises(ValueError):
        CtfOptions(dispatch_mode="automatic", max_teammates=0)


def test_frozen_solve_template_cannot_widen_or_wait_for_parent():
    data = discover()
    item = CtfWorkItem.model_validate(data)
    assert item.solve_template is not None
    assert item.solve_template.max_auto_solve_items == 20
    for changes in (
        {"source_namespace": "other"},
        {"resource_refs": ["new-target"]},
        {"permission_profile": "env_prepare"},
        {"requires": [{"kind": "work_completed", "ref": data["work_id"]}]},
        {"max_auto_solve_items": 201},
        {"max_auto_solve_items": 1.5},
    ):
        with pytest.raises(ValueError):
            CtfWorkItem.model_validate(
                {**data, "solve_template": {**data["solve_template"], **changes}}
            )


def test_plan_rejects_duplicate_or_conflicting_work():
    item = discover()
    base = {"request_id": uuid4(), "expected_plan_revision": 0}
    assert CtfTeamPlanRequest.model_validate({**base, "upsert": [item]})
    for changes in (
        {"upsert": [item, item]},
        {"upsert": [item], "cancel_ids": [item["work_id"]]},
        {"upsert": [discover() for _ in range(21)]},
    ):
        with pytest.raises(ValueError):
            CtfTeamPlanRequest.model_validate({**base, **changes})


def test_public_input_ids_must_be_selected_task_attachments():
    fid = uuid4()
    base = {
        "mode": "ctf",
        "goal": "Read public material",
        "budget": {"max_cost": "1", "max_minutes": 10},
        "input_group_id": uuid4(),
        "input_file_ids": [fid],
    }
    assert CtfTaskCreate.model_validate({**base, "public_input_ids": [fid]})
    with pytest.raises(ValueError):
        CtfTaskCreate.model_validate({**base, "public_input_ids": [uuid4()]})
