"""Boundary checks for every shared model."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import pytest
from bbx_contracts import models as m
from bbx_contracts.logging import JsonFormatter
from bbx_contracts.profile import load_profile
from bbx_contracts.schemas import export_schemas
from pydantic import TypeAdapter, ValidationError

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TASK_ID = UUID("00000000-0000-0000-0000-000000000001")
EVIDENCE = {"type": "log", "path": "/workspace/agents/agent-1/evidence.log", "summary": "log"}
FACT = {
    "id": "F1",
    "kind": "observation",
    "statement": "Gateway returned 502",
    "evidence": [EVIDENCE],
    "author": "agent-1",
}
INTENT = {
    "id": "I1",
    "statement": "Check pool exhaustion",
    "based_on": ["F1"],
    "expected": "Pool becomes full",
    "method": "Inspect logs",
    "relates_to": ["A1"],
    "author": "agent-1",
}
VERDICT = {"id": "A1", "verdict": "unmet", "reason": "No trace", "missing": "Add trace"}
MODEL = {
    "provider": "deepseek",
    "model": "deepseek-flash",
    "base_url": "https://api.deepseek.com",
    "reasoning_effort": "high",
    "price": {},
}
PROFILE = {
    "models": {"explore": MODEL, "derive": MODEL, "close": MODEL},
    "params": {},
    "prompts": {"explore": "explore", "derive": "derive", "close": "close"},
    "prompt_templates": {"explore": "explore", "derive": "derive", "close": "close"},
    "exec_image": "image",
    "exec_resources": {"cpus": 2, "mem": "4g", "pids": 256},
}


@pytest.mark.parametrize(
    ("model", "valid", "invalid"),
    [
        (m.Evidence, EVIDENCE, {**EVIDENCE, "type": "unknown"}),
        (m.Fact, FACT, {**FACT, "evidence": []}),
        (
            m.IntentNote,
            {"by": "agent-1", "at": NOW, "text": "Done"},
            {"by": "", "at": NOW, "text": "Done"},
        ),
        (m.Intent, INTENT, {**INTENT, "based_on": []}),
        (m.AcceptanceItem, {"id": "A1", "desc": "Explain failure"}, {"id": "A1", "desc": ""}),
        (m.AcceptanceItemState, {}, {"status": "unknown"}),
        (m.VerdictItem, VERDICT, {**VERDICT, "missing": None}),
        (m.Usage, {}, {"cost": -1}),
        (m.Budget, {"max_cost": 10, "max_minutes": 60}, {"max_cost": 0, "max_minutes": 60}),
        (m.Params, {}, {"explore_max_steps": 0}),
        (
            m.TaskSpec,
            {
                "goal": "Find cause",
                "acceptance": [{"id": "A1", "desc": "Explain"}],
                "budget": {"max_cost": 10, "max_minutes": 60},
                "agent_profile": "default",
            },
            {
                "goal": "Find cause",
                "acceptance": [],
                "budget": {"max_cost": 10, "max_minutes": 60},
                "agent_profile": "default",
            },
        ),
        (
            m.AgentRun,
            {"task_id": TASK_ID, "id": "agent-1", "task_type": "explore", "status": "running"},
            {"task_id": TASK_ID, "id": "agent-1", "task_type": "unknown", "status": "running"},
        ),
        (
            m.Event,
            {
                "version": 1,
                "task_id": TASK_ID,
                "type": "fact.posted",
                "actor": "agent-1",
                "created_at": NOW,
            },
            {
                "version": 0,
                "task_id": TASK_ID,
                "type": "fact.posted",
                "actor": "agent-1",
                "created_at": NOW,
            },
        ),
        (
            m.ExploreReceiptData,
            {"intent_result": "none", "note": "done"},
            {"intent_result": "bad", "note": "done"},
        ),
        (
            m.ExploreReceipt,
            {"data": {"intent_result": "none", "note": "done"}},
            {"accepted": False, "data": {"intent_result": "none", "note": "done"}},
        ),
        (m.DeriveReceiptData, {"posted": ["I1"]}, {"posted": "I1"}),
        (
            m.DeriveReceipt,
            {"data": {"posted": ["I1"]}},
            {"accepted": False, "data": {"posted": ["I1"]}},
        ),
        (m.RefusalReceipt, {"reason": "Unavailable"}, {"reason": ""}),
        (
            m.PostFactRequest,
            {"kind": "observation", "statement": "Found", "evidence": [EVIDENCE]},
            {"kind": "inference", "statement": "Found", "evidence": [EVIDENCE]},
        ),
        (
            m.PostIntentRequest,
            {
                "statement": "Check",
                "based_on": ["F1"],
                "expected": "Result",
                "method": "Test",
                "relates_to": ["A1"],
                "claim": True,
            },
            {
                "statement": "Check",
                "based_on": [],
                "expected": "Result",
                "method": "Test",
                "relates_to": ["A1"],
            },
        ),
        (m.ReleaseRequest, {"intent_id": "I1", "note": "Done"}, {"intent_id": "I1", "note": ""}),
        (m.SubmitCloseRequest, {"verdicts": [VERDICT]}, {"verdicts": []}),
        (m.Price, {}, {"output_per_m": -1}),
        (m.ModelConfig, MODEL, {**MODEL, "model": ""}),
        (m.ModelSet, {"explore": MODEL, "derive": MODEL, "close": MODEL}, {"explore": MODEL}),
        (
            m.PromptPaths,
            {"explore": "e", "derive": "d", "close": "c"},
            {"explore": "", "derive": "d", "close": "c"},
        ),
        (
            m.ExecResources,
            {"cpus": 2, "mem": "4g", "pids": 256},
            {"cpus": 0, "mem": "4g", "pids": 256},
        ),
        (m.AgentProfile, PROFILE, {**PROFILE, "exec_image": ""}),
    ],
)
def test_model_boundaries(
    model: type[m.ContractModel], valid: dict[str, object], invalid: dict[str, object]
) -> None:
    assert model.model_validate(valid)
    with pytest.raises(ValidationError):
        model.model_validate(invalid)


@pytest.mark.parametrize(
    "enum",
    [
        m.FactKind,
        m.FactStatus,
        m.EvidenceType,
        m.IntentStatus,
        m.IntentResult,
        m.AcceptanceStatus,
        m.Verdict,
        m.TaskStatus,
        m.AgentTaskType,
        m.CloseMode,
        m.AgentStatus,
        m.EndReason,
        m.EventType,
    ],
)
def test_enums(enum: type[StrEnum]) -> None:
    adapter = TypeAdapter(enum)
    assert adapter.validate_python(next(iter(enum)))
    with pytest.raises(ValidationError):
        adapter.validate_python("invalid")


def test_receipt_union() -> None:
    adapter = TypeAdapter(m.Receipt)
    assert isinstance(
        adapter.validate_python(
            {"accepted": True, "data": {"intent_result": "none", "note": "done"}}
        ),
        m.ExploreReceipt,
    )
    assert isinstance(
        adapter.validate_python({"accepted": True, "data": {"posted": ["I1"]}}), m.DeriveReceipt
    )
    assert isinstance(
        adapter.validate_python({"accepted": False, "reason": "Unavailable"}), m.RefusalReceipt
    )


def test_fact_consistency() -> None:
    with pytest.raises(ValidationError):
        m.Fact.model_validate({**FACT, "resolves": "I1"})


def test_schema_export(tmp_path: Path) -> None:
    paths = export_schemas(tmp_path)
    assert len(paths) >= 25
    assert json.loads((tmp_path / "Receipt.json").read_text())["anyOf"]
    params = json.loads((tmp_path / "Params.json").read_text())["properties"]
    assert "close_reserve_ratio" in params
    assert "close_reserve_cost" not in params
    assert "max_concurrent_agents" not in params


def test_task_params_override_boundaries() -> None:
    task = {
        "goal": "Find cause",
        "acceptance": [{"id": "A1", "desc": "Explain"}],
        "budget": {"max_concurrent_agents": 5, "max_cost": 10, "max_minutes": 60},
        "agent_profile": "default",
    }
    assert str(m.Params().close_reserve_ratio) == "0.05"
    assert m.Params(close_reserve_ratio=Decimal(0)).close_reserve_ratio == 0
    assert m.TaskSpec.model_validate({**task, "params": {"close_reserve_ratio": 0.5}})
    disabled = m.TaskSpec.model_validate({**task, "params": {"derive_enabled": False}})
    assert disabled.params["derive_enabled"] is False
    assert m.Params().derive_enabled is True
    for invalid_params in (
        {"max_concurrent_agents": 6},
        {"close_reserve_ratio": 1},
        {"close_reserve_ratio": -0.1},
        {"close_reserve_cost": 0.05},
    ):
        with pytest.raises(ValidationError):
            m.TaskSpec.model_validate({**task, "params": invalid_params})


def test_default_profile() -> None:
    directory = Path(__file__).resolve().parents[3] / "profiles" / "default"
    profile, notices = load_profile(directory)
    assert profile.models.explore.model == "deepseek-flash"
    assert profile.params.context_threshold == 128000
    assert profile.prompt_templates.explore == (directory / profile.prompts.explore).read_text(
        encoding="utf-8"
    )
    assert not notices
    assert profile.models.explore.price.is_complete()
    assert profile.models.explore.price.currency == "CNY"
    assert profile.params.derive_enabled is True


def test_single_profile_only_disables_derive() -> None:
    root = Path(__file__).resolve().parents[3] / "profiles"
    default, _ = load_profile(root / "default")
    single, notices = load_profile(root / "single")
    expected = default.model_dump(mode="json")
    expected["params"]["derive_enabled"] = False
    assert single.model_dump(mode="json") == expected
    assert not notices


def test_json_log_format() -> None:
    import logging

    record = logging.LogRecord("blackboard", logging.INFO, __file__, 1, "created", (), None)
    record.fields = {"task_id": "task-1"}
    payload = json.loads(JsonFormatter("blackboard").format(record))
    assert {"time", "level", "service", "message", "task_id"} <= payload.keys()


def test_profile_rejects_mixed_accounting_currencies():
    profile = m.AgentProfile.model_validate(PROFILE).model_dump(mode="json")
    for model in profile["models"].values():
        model["price"]["currency"] = "CNY"
    assert m.AgentProfile.model_validate(profile).models.close.price.currency == "CNY"
    profile["models"]["close"]["price"]["currency"] = "USD"
    with pytest.raises(ValidationError, match="相同币种"):
        m.AgentProfile.model_validate(profile)
