"""Settings describe the actual schemas and preserve runtime authorization boundaries."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from bbx_blackboard.board_runner_tools import schemas as board_schemas
from bbx_blackboard.native_ctf_publication import NativeCtfPublication
from bbx_blackboard.worker_settings import tool_catalog, validate_prompt
from bbx_contracts.progress import PROGRESS_TOOL_NAMES
from bbx_contracts.runner_vpn import NETWORK_USE_DEFAULT, VPN_DYNAMIC_PROFILE, VPN_TOOLS
from bbx_runtime.progress.tools import progress_tools

ROOT = Path(__file__).resolve().parents[3]


def test_tool_catalog_matches_bound_role_tools_and_real_parameters():
    published = NativeCtfPublication.model_construct(
        vpn_profile=VPN_DYNAMIC_PROFILE,
        runtime_resource_config={"runner_profile": "node-sandbox-host-v1"},
    )
    assert "lead" not in tool_catalog(None)["roles"]
    roles = tool_catalog(published)["roles"]
    explore = {item["name"]: item for item in roles["explore"]["tools"]}
    assert explore["post_fact"]["input_schema"] == board_schemas()["post_fact"]
    assert explore["post_fact"]["required"] and not explore["post_fact"]["configurable"]
    assert {name for name, item in explore.items() if item["group"] == "sandbox"} == {
        "exec_command",
        "read_file",
        "write_file",
    }
    assert {item["setting_key"] for item in explore.values() if item["group"] == "sandbox"} == {
        "execute_command"
    }
    assert "view_image" not in explore and "execute_command" not in explore
    for role in ("derive", "close", "progress"):
        assert not roles[role]["external_mcp"]
        assert all(item["group"] == "platform" for item in roles[role]["tools"])
    lead = {item["name"]: item for item in roles["lead"]["tools"]}
    teammate = {item["name"]: item for item in roles["teammate"]["tools"]}
    assert NETWORK_USE_DEFAULT in lead and set(VPN_TOOLS) <= set(lead)
    assert not ({NETWORK_USE_DEFAULT, *VPN_TOOLS} & set(teammate))
    assert all(not item["configurable"] for item in (*lead.values(), *teammate.values()))


def test_progress_shared_schemas_equal_existing_runtime_validation_schemas():
    actual = {
        item.name: item
        for item in progress_tools(
            cast(Any, SimpleNamespace(job={})), cast(Any, None), {}, list(PROGRESS_TOOL_NAMES)
        )
    }
    catalog = tool_catalog(None)["roles"]["progress"]["tools"]
    assert set(actual) == {item["name"] for item in catalog}
    for item in catalog:
        runtime = actual[item["name"]]
        assert item["input_schema"] == runtime.input_schema
        # The already deployed Runtime uses the same signature-derived validation model.
        assert item["input_schema"] == runtime.input_model.model_json_schema()


def test_bundled_worker_prompts_use_supported_variables_and_no_old_sandbox_capabilities():
    for family in ("default", "single"):
        for role in ("explore", "derive", "close"):
            text = (ROOT / "profiles" / family / "prompts" / f"{role}.md.j2").read_text()
            validate_prompt(text)
            assert not any(name in text for name in ("execute_command", "view_image", "sudo"))
            if role == "explore":
                assert all(name in text for name in ("exec_command", "read_file", "write_file"))
            else:
                assert "exec_command" not in text and "write_file" not in text
