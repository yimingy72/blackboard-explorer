"""Settings and task snapshots retain opt-in role policies without legacy drift."""

from copy import deepcopy
from pathlib import Path

from bbx_blackboard.ctf_profile_publish import publication_plan
from bbx_blackboard.profiles import _content, _digest, ctf_profile
from bbx_contracts.ctf import load_ctf_profile
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
