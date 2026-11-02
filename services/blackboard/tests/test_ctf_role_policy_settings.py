"""Settings and task snapshots retain opt-in role policies without legacy drift."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.ctf_profile_publish import publication_plan
from bbx_blackboard.profiles import _content, _digest, ctf_profile
from bbx_contracts.ctf import CtfAgentProfile, load_ctf_profile
from test_ctf_api import settings
from test_ctf_profile_publish import profile_rows


def profile():
    return load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")


def test_legacy_metadata_roundtrip_and_distinct_policies_survive_publication():
    rows = profile_rows()
    for row in rows.values():
        assert "role_run_options" not in row["prompts"]
        assert _digest(_content(ctf_profile(row))) == _digest(
            {key: value for key, value in row.items() if key not in {"name", "version"}}
        )
    rows["ctf"]["prompts"]["role_run_options"] = {"lead": {"max_output_tokens": 4096}}
    rows["ctf-task-settings"]["prompts"]["role_run_options"] = {
        "teammate": {"max_output_tokens": 2048}
    }
    before = deepcopy(rows)
    candidates, summary = publication_plan(rows)
    assert rows == before
    for name, candidate in candidates.items():
        content = _content(candidate)
        assert content["prompts"] == rows[name]["prompts"]
        assert content["models"] == rows[name]["models"]
        assert content["params"] == rows[name]["params"]
        assert summary["profiles"][name]["role_run_options"]


@pytest.mark.parametrize(
    "change,expected",
    [
        ({}, {"max_output_tokens": 4096}),
        ({"run_options": None}, None),
        ({"run_options": {}}, None),
        (
            {"run_options": {"reasoning_effort": "low", "max_output_tokens": 2048}},
            {"reasoning_effort": "low", "max_output_tokens": 2048},
        ),
    ],
)
async def test_role_settings_preserve_clear_or_replace_only_explicit_policy(change, expected):
    value = CtfAgentProfile.model_validate(
        {
            **profile().model_dump(),
            "role_run_options": {
                "lead": {"max_output_tokens": 4096},
                "teammate": {"max_output_tokens": 8192},
            },
        }
    )
    row = {"name": "ctf", "version": 3, **_content(value)}
    original = deepcopy(row)

    async def create(name, selected, author, **kwargs):
        return {"name": name, "version": 4, **_content(selected)}

    store = SimpleNamespace(get=AsyncMock(return_value=row), create=AsyncMock(side_effect=create))
    app = api.create_app(settings())
    app.state.profile_store = store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/api/settings/ctf/workers/lead",
            headers={"Authorization": "Bearer service-test"},
            json={
                "expected_revision": 3,
                "prompt": value.prompt_templates.lead,
                "tools": value.worker_tools["lead"].model_dump(mode="json"),
                **change,
            },
        )
        read = await client.get(
            "/api/settings/ctf/workers", headers={"Authorization": "Bearer service-test"}
        )
    assert response.status_code == read.status_code == 200, response.text
    selected = store.create.call_args.args[1]
    assert selected.model == value.model
    assert selected.role_run_options["teammate"] == value.role_run_options["teammate"]
    lead = selected.role_run_options.get("lead")
    assert (lead.model_dump(exclude_none=True) if lead else None) == expected
    assert read.json()["profile"]["model"] == value.model.model_dump(mode="json")
    assert (
        read.json()["profile"]["role_run_options"]
        == value.model_dump(mode="json")["role_run_options"]
    )
    assert row == original


@pytest.mark.parametrize(
    "run_options",
    [{"reasoning_effort": "unsupported"}, {"max_output_tokens": 10_000_000}],
)
async def test_invalid_role_override_never_publishes_or_echoes_profile(run_options):
    value = profile()
    value.prompt_templates.lead += "\nsynthetic-private-profile-marker"
    row = {"name": "ctf", "version": 3, **_content(value)}
    original = deepcopy(row)
    store = SimpleNamespace(get=AsyncMock(return_value=row), create=AsyncMock())
    app = api.create_app(settings())
    app.state.profile_store = store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/api/settings/ctf/workers/lead",
            headers={"Authorization": "Bearer service-test"},
            json={
                "expected_revision": 3,
                "prompt": value.prompt_templates.lead,
                "tools": value.worker_tools["lead"].model_dump(mode="json"),
                "run_options": run_options,
            },
        )
    assert response.status_code == 422
    assert "synthetic-private-profile-marker" not in response.text
    store.create.assert_not_awaited()
    assert row == original


@pytest.mark.parametrize("invalid", [True, False])
async def test_new_task_revalidates_role_limit_against_selected_model(monkeypatch, invalid):
    value = CtfAgentProfile.model_validate(
        {**profile().model_dump(), "role_run_options": {"lead": {"max_output_tokens": 4096}}}
    )
    value.prompt_templates.lead += "\nsynthetic-private-profile-marker"
    row = {"name": "ctf", "version": 3, **_content(value)}
    model = value.model.model_dump(mode="json")
    model["max_output_tokens"] = 2048 if invalid else 8192
    store = SimpleNamespace(
        get=AsyncMock(return_value=row),
        create=AsyncMock(return_value={"name": "ctf-task-settings", "version": 4}),
    )
    tid = uuid4()
    backend = SimpleNamespace(create_task=AsyncMock(return_value=tid), provision=AsyncMock())
    app = api.create_app(settings())
    app.state.profile_store = store
    app.state.platform_store = SimpleNamespace(
        default_model_name=AsyncMock(return_value="synthetic"),
        get=AsyncMock(return_value={"enabled": True, "config": model}),
        public=lambda row: row,
    )
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
                "agent_profile_version": 4,
            }
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/tasks",
            headers={"Authorization": "Bearer service-test"},
            json={"mode": "ctf", "goal": "Synthetic", "budget": {"max_cost": 1, "max_minutes": 1}},
        )
    assert response.status_code == (422 if invalid else 200), response.text
    if invalid:
        assert "synthetic-private-profile-marker" not in response.text
        store.create.assert_not_awaited()
        backend.create_task.assert_not_awaited()
    else:
        selected = store.create.call_args.args[1]
        assert selected.model.max_output_tokens == 8192
        assert selected.role_run_options == value.role_run_options
    assert value.model.max_output_tokens is None
