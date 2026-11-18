"""Grouped failures retain safe child types without exposing RPC or command text."""

import json

from bbx_contracts.ctf import CtfFailureDiagnostic
from bbx_runtime.ctf.coordinator import failure_diagnostic
from bbx_runtime.ctf.execution import UnknownCommandOutcome
from bbx_runtime.ctf.failures import CtfToolFailure, grouped_failure_types


def test_grouped_failure_diagnostics_include_types_but_no_messages(caplog):
    error = ExceptionGroup(
        "secret outer body",
        [ExceptionGroup("secret nested", [UnknownCommandOutcome("secret RPC payload")])],
    )
    diagnostic = failure_diagnostic("turn", error)
    assert diagnostic["leaf_error_types"] == ["UnknownCommandOutcome"]
    CtfFailureDiagnostic.model_validate(diagnostic)
    assert "secret" not in json.dumps(diagnostic) + caplog.text


def test_grouped_failure_types_unwrap_only_known_tool_boundary_and_remain_bounded():
    error = ExceptionGroup(
        "private", [CtfToolFailure(ValueError("private")), RuntimeError("private")]
    )
    assert set(grouped_failure_types(error)) == {"ValueError", "RuntimeError"}
    assert grouped_failure_types(ValueError("private")) == []
    many = ExceptionGroup("private", [ValueError(str(i)) for i in range(100)])
    assert grouped_failure_types(many) == ["ValueError"]


def test_unrecognized_type_names_cannot_break_diagnostic_contract():
    custom = type("处理错误", (Exception,), {})
    diagnostic = failure_diagnostic("turn", ExceptionGroup("private", [custom("private")]))
    assert diagnostic["leaf_error_types"] == ["OtherException"]
    CtfFailureDiagnostic.model_validate(diagnostic)
