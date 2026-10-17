"""Profile publication changes only explicitly reviewed fields."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.ctf_profile_publish import FAMILIES, HANDOFF_MARKERS, publication_plan
from bbx_blackboard.profiles import _content, ctf_profile
from bbx_contracts.ctf import load_ctf_profile
from test_ctf_api import settings


def profile_rows():
    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    profile.model.context_window = 1_000_000
    profile.prompt_templates.lead += "\nsynthetic-private-source-prompt"
    source = _content(profile)
    source["models"]["teammate"] = deepcopy(source["models"]["lead"])
    target = deepcopy(source)
    for role, marker in HANDOFF_MARKERS.items():
        target["prompt_templates"][role] = (
            "\n".join(
                line for line in source["prompt_templates"][role].splitlines() if marker not in line
            )
            + "\nSynthetic prior user-authored template note"
        )
    target["models"]["lead"]["model"] = "synthetic-task-model"
    target["models"]["teammate"]["model"] = "synthetic-task-model"
    target["models"]["lead"]["price"]["cache_hit_per_m"] = "7"
    target["models"]["teammate"]["price"]["cache_hit_per_m"] = "7"
    for model in target["models"].values():
        model["price"]["billing_mode"] = "fixed"
    target["params"].update(context_threshold=400000, max_steps=4000, max_teammates=12)
    target["exec_image"] = "synthetic-task-image:latest"
    target["exec_resources"]["mem"] = "8g"
    target["privileged_allowlist"] = ["synthetic-command"]
    target["worker_tools"]["lead"]["builtin"].remove("list_challenges")
    return {
        "ctf": {"name": "ctf", "version": 13, **source},
        "ctf-task-settings": {"name": "ctf-task-settings", "version": 6, **target},
    }


def test_template_sync_preserves_distinct_family_settings_and_original_rows():
    rows = profile_rows()
    before = deepcopy(rows)
    candidates, summary = publication_plan(rows)
    assert rows == before
    for name in FAMILIES:
        expected = {
            key: value for key, value in rows[name].items() if key not in {"name", "version"}
        }
        expected["prompt_templates"] = rows["ctf"]["prompt_templates"]
        assert _content(candidates[name]) == expected
    assert summary["profiles"]["ctf"]["version"] == 13
    assert summary["profiles"]["ctf-task-settings"]["version"] == 7
    assert (
        summary["profiles"]["ctf"]["template_digest_after"]
        == summary["profiles"]["ctf-task-settings"]["template_digest_after"]
    )
    rendered = json.dumps(summary)
    assert "synthetic-private-source-prompt" not in rendered
    assert "synthetic-task-model" not in rendered
    assert "synthetic-task-image" not in rendered


def test_explicit_target_threshold_raises_capacity_without_changing_source_or_output():
    rows = profile_rows()
    candidates, summary = publication_plan(
        rows, context_threshold=606784, reserved_output_tokens=393216
    )
    assert candidates["ctf"].options.context_threshold == rows["ctf"]["params"]["context_threshold"]
    target = summary["profiles"]["ctf-task-settings"]
    assert target["context_threshold"] == target["available_input_tokens"] == 606784
    assert target["hard_input_limit_tokens"] == 485427
    assert (
        _content(candidates["ctf-task-settings"])["models"] == rows["ctf-task-settings"]["models"]
    )


@pytest.mark.parametrize("threshold", [0, 399999, 1_000_001])
def test_threshold_changes_cannot_lower_or_exceed_configured_capacity(threshold):
    with pytest.raises(ValueError, match="threshold"):
        publication_plan(profile_rows(), context_threshold=threshold)


@pytest.mark.parametrize("reserve", [0, 1_000_000])
def test_invalid_output_reserve_is_rejected_before_publication(reserve):
    with pytest.raises(ValueError, match="reserve"):
        publication_plan(profile_rows(), reserved_output_tokens=reserve)


def test_required_handoff_guidance_is_checked_before_template_replacement():
    rows = profile_rows()
    rows["ctf"]["prompt_templates"]["lead"] = "\n".join(
        line
        for line in rows["ctf"]["prompt_templates"]["lead"].splitlines()
        if HANDOFF_MARKERS["lead"] not in line
    )
    with pytest.raises(ValueError, match="guidance"):
        publication_plan(rows)


def test_template_digest_ignores_json_key_order():
    rows = profile_rows()
    _, before = publication_plan(rows)
    rows["ctf"]["prompt_templates"] = dict(reversed(rows["ctf"]["prompt_templates"].items()))
    _, after = publication_plan(rows)
    assert before == after


def test_legacy_unrepresentable_model_split_is_rejected_instead_of_silently_overwritten():
    rows = profile_rows()
    rows["ctf-task-settings"]["models"]["teammate"]["model"] = "synthetic-distinct-model"
    with pytest.raises(ValueError, match="unspecified fields"):
        publication_plan(rows)


def test_unknown_source_window_does_not_block_or_inherit_target_capacity():
    rows = profile_rows()
    for model in rows["ctf"]["models"].values():
        model["context_window"] = None
    candidates, summary = publication_plan(
        rows, context_threshold=606784, reserved_output_tokens=393216
    )
    assert candidates["ctf"].model.context_window is None
    assert summary["profiles"]["ctf"]["hard_input_limit_tokens"] is None
    assert summary["profiles"]["ctf"]["reserved_output_tokens"] is None
    assert summary["profiles"]["ctf-task-settings"]["hard_input_limit_tokens"] == 485427


async def test_new_task_snapshots_latest_ctf_instead_of_reusing_old_task_family(monkeypatch):
    rows = profile_rows()
    config = settings()
    tid = uuid4()
    profiles = SimpleNamespace(
        get=AsyncMock(return_value=rows["ctf"]),
        create=AsyncMock(return_value={"name": "ctf-task-settings", "version": 7}),
    )
    model = ctf_profile(rows["ctf"]).model.model_dump(mode="json")
    platform = SimpleNamespace(
        default_model_name=AsyncMock(return_value="synthetic-model"),
        get=AsyncMock(return_value={"enabled": True, "config": model}),
        public=lambda row: row,
    )
    backend = SimpleNamespace(create_task=AsyncMock(return_value=tid), provision=AsyncMock())
    app = api.create_app(config)
    app.state.profile_store = profiles
    app.state.platform_store = platform
    app.state.ctf_service = backend
    monkeypatch.setattr(
        api,
        "_task",
        AsyncMock(
            return_value={
                "id": tid,
                "mode": "ctf",
                "status": "created",
                "agent_profile": "ctf-task-settings",
                "agent_profile_version": 7,
            }
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.post(
            "/api/tasks",
            headers={"Authorization": f"Bearer {issue_user_token(config, 'alice')}"},
            json={
                "mode": "ctf",
                "goal": "Synthetic new task",
                "budget": {"max_cost": "10", "max_minutes": 5},
            },
        )
    assert response.status_code == 200, response.text
    profiles.get.assert_awaited_once_with("ctf", None)
    published = profiles.create.call_args.args[1]
    assert published.prompt_templates.model_dump() == rows["ctf"]["prompt_templates"]
    assert published.prompt_templates.model_dump() != rows["ctf-task-settings"]["prompt_templates"]
    assert published.options.context_threshold == 1_000_000
    assert response.json()["agent_profile_version"] == 7
    assert backend.create_task.call_args.kwargs["profile_version"] == 7
