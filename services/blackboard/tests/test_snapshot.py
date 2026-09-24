"""Compact snapshot selection and YAML shape."""

import copy

import pytest
import yaml
from bbx_blackboard.domain.snapshot import snapshot


def fact(fid: str, version: int, statement: str = "fact") -> dict:
    return {
        "kind": "observation",
        "status": "proposed",
        "relied_by": 2,
        "author": "agent-1",
        "statement": statement,
        "version": version,
    }


def intent(version: int, status: str, based_on: list[str]) -> dict:
    return {
        "status": status,
        "result": None,
        "holder": "agent-2" if status == "claimed" else None,
        "based_on": based_on,
        "statement": "intent",
        "version": version,
    }


def test_snapshot_is_valid_yaml_and_truncates_statement() -> None:
    state = {
        "facts": {"F1": fact("F1", 1, "测" * 81)},
        "intents": {"I1": intent(2, "claimed", ["F1"])},
    }
    original = copy.deepcopy(state)
    rendered = snapshot(state, 10)
    data = yaml.safe_load(rendered)
    assert data["facts"][0] == {
        "id": "F1",
        "kind": "observation",
        "status": "proposed",
        "relied_by": 2,
        "by": "agent-1",
        "s": "测" * 79 + "…",
    }
    assert len(data["facts"][0]["s"]) == 80
    assert data["intents"][0]["based_on"] == ["F1"]
    assert state == original


def test_snapshot_keeps_active_intents_then_based_facts_then_recent() -> None:
    facts = {f"F{n}": fact(f"F{n}", n) for n in range(1, 61)}
    state = {
        "facts": facts,
        "intents": {
            "I1": intent(61, "open", ["F1"]),
            "I2": intent(62, "claimed", ["F2"]),
            "I3": intent(63, "closed", ["F3"]),
        },
    }
    rendered = snapshot(state, 8)
    data = yaml.safe_load(rendered)
    assert len(rendered.splitlines()) == 8
    assert {item["id"] for item in data["intents"]} == {"I1", "I2", "I3"}
    assert {item["id"] for item in data["facts"]} == {"F1", "F2"}
    assert rendered.endswith("# omitted 58 items\n")


def test_snapshot_caps_other_items_at_recent_fifty() -> None:
    state = {"facts": {f"F{n}": fact(f"F{n}", n) for n in range(1, 101)}, "intents": {}}
    rendered = snapshot(state, 90)
    ids = [item["id"] for item in yaml.safe_load(rendered)["facts"]]
    assert ids == [f"F{n}" for n in range(51, 101)]
    assert rendered.endswith("# omitted 50 items\n")


def test_snapshot_honors_one_and_two_line_limits() -> None:
    assert yaml.safe_load(snapshot({"facts": {}, "intents": {}}, 3)) == {
        "facts": [],
        "intents": [],
    }
    state = {"facts": {"F1": fact("F1", 1)}, "intents": {}}
    for max_lines in (1, 2):
        rendered = snapshot(state, max_lines)
        assert len(rendered.splitlines()) == max_lines
        assert yaml.safe_load(rendered) == {"facts": [], "intents": []}
        assert rendered.endswith("# omitted 1 item\n")
    with pytest.raises(ValueError, match="max_lines"):
        snapshot(state, 0)
