"""Automatic matches remain candidates until a reviewer confirms them."""

import json
from pathlib import Path

import pytest

from eval.runner.score import score_directory, score_run

TASK_ID = "11111111-1111-4111-8111-111111111111"


def write_run(directory: Path, *, facts=None, report="", usage=None, events=None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(
        json.dumps(
            {
                "task_id": TASK_ID,
                "profile_name": "default",
                "profile": {"models": {}},
                "status": "finished",
                "outcome": "success",
                "elapsed_seconds": 30,
            }
        )
    )
    (directory / "state.json").write_text(
        json.dumps(
            {
                "task": {
                    "id": TASK_ID,
                    "usage": usage or {},
                    "acceptance_state": {
                        "A1": {"status": "met", "evidence_facts": ["F1"]},
                        "A2": {"status": "unmet", "evidence_facts": []},
                    },
                },
                "facts": facts or {},
                "intents": {},
                "agents": {
                    "agent-1": {"end_reason": "normal"},
                    "agent-2": {"end_reason": "runtime_error"},
                },
            }
        )
    )
    (directory / "events.json").write_text(json.dumps(events or []))
    (directory / "report.md").write_text(report)


def example_facts() -> dict:
    return {
        "F1": {
            "id": "F1",
            "statement": "shop/detail.py order_detail lacks owner user_id for order_id lookup",
            "provenance": "tool_backed",
            "satisfies": ["A1"],
            "evidence": [
                {"uri": f"evidence/{TASK_ID}/agent-1/proof.txt"},
                {"path": "/workspace/agents/agent-1/local-only.txt"},
                {"uri": "evidence/other-task/agent-1/invalid.txt"},
            ],
        },
        "F2": {
            "id": "F2",
            "statement": "shop/refunds.py refund_order accepts negative amount without a limit",
            "provenance": "self_reported",
            "satisfies": ["A2"],
            "evidence": [],
        },
    }


def example_report() -> str:
    return (
        "The tests/test_cases.py example_total uses eval in a test "
        "and is not imported by service.\n\n"
        "A separate newly observed issue needs human review before it can be counted."
    )


def test_candidates_are_prioritized_but_never_auto_confirmed(tmp_path: Path) -> None:
    write_run(
        tmp_path,
        facts=example_facts(),
        report=example_report(),
        usage={"cost": "1.25", "cache_hit_tokens": 60, "cache_miss_tokens": 40},
        events=[{"type": "fact.disputed"}, {"type": "acceptance.judged"}],
    )
    score, template = score_run(tmp_path)
    assert score["profile"] == "default" and score["task_id"] == TASK_ID
    assert score["metrics"]["recall"] is None
    assert score["metrics"]["precision"] is None
    assert score["metrics"]["reproducibility"] is None
    assert score["provisional"] is True
    assert set(score["review_pending"]) == {"F1", "F2", "R1", "R2"}
    assert score["findings"][0]["id"] == "F1"
    assert score["findings"][0]["evidence_uris"] == [f"evidence/{TASK_ID}/agent-1/proof.txt"]
    assert score["findings"][1]["evidence_uris"] == []
    assert {item["answer_id"] for item in score["findings"][0]["candidates"]} == {"P1"}
    distractor = next(item for item in score["findings"] if item["id"] == "R1")
    assert {item["answer_id"] for item in distractor["candidates"]} == {"D1"}
    assert (
        distractor["review"] is None
    )  # A negated distractor is not automatically a false positive.
    assert template["expected_interfaces"] is None
    assert template["expected_interfaces_source"] == "manual"
    assert template["checked_interfaces"] is None
    assert all(item["classification"] is None for item in template["findings"])


def test_review_controls_partial_credit_precision_and_other_metrics(tmp_path: Path) -> None:
    write_run(
        tmp_path,
        facts=example_facts(),
        report=example_report(),
        usage={"cost": "1.25", "cache_hit_tokens": 60, "cache_miss_tokens": 40},
        events=[
            {"type": "fact.disputed"},
            {"type": "acceptance.judged"},
            {"type": "acceptance.judged"},
        ],
    )
    (tmp_path / "review.json").write_text(
        json.dumps(
            {
                "expected_interfaces": ["GET /orders/{id}", "POST /refund"],
                "checked_interfaces": ["GET /orders/{id}"],
                "findings": [
                    {
                        "id": "F1",
                        "classification": "problem",
                        "answer_id": "P1",
                        "location": True,
                        "mechanism": True,
                        "reproducible": True,
                    },
                    {
                        "id": "F2",
                        "classification": "problem",
                        "answer_id": "P4",
                        "location": True,
                        "mechanism": True,
                        "reproducible": False,
                    },
                    {"id": "R1", "classification": "distractor", "answer_id": "D1"},
                    {
                        "id": "R2",
                        "classification": "new",
                        "location": True,
                        "mechanism": True,
                        "reproducible": True,
                    },
                ],
            }
        )
    )
    (tmp_path / "replay-results.json").write_text(
        json.dumps({"scripts": [{"id": "F1", "status": "passed"}], "reproducibility": 0.5})
    )
    score, _ = score_run(tmp_path)
    metrics = score["metrics"]
    assert metrics["recall"] == 0.25  # (P1 full + P4 half) / six known problems.
    assert metrics["precision"] == 0.75  # Three reviewed positives, one distractor.
    assert metrics["reproducibility"] == 0.5
    assert metrics["interface_coverage"] == 0.5
    assert metrics["cache_hit_rate"] == 0.6
    assert metrics["cost"] == 1.25 and metrics["elapsed_seconds"] == 30
    assert metrics["disputes"] == 1 and metrics["judgments"] == 2
    assert metrics["satisfies_hit_rate"] == 0.5
    assert metrics["end_reasons"] == {"normal": 1, "runtime_error": 1}
    assert score["review_pending"] == [] and score["provisional"] is False


def test_incomplete_review_and_missing_denominators_remain_unknown(tmp_path: Path) -> None:
    write_run(tmp_path, facts=example_facts(), report="", usage={})
    (tmp_path / "review.json").write_text(
        json.dumps(
            {
                "findings": [
                    {"id": "F1", "classification": "problem", "answer_id": "P1", "location": True}
                ]
            }
        )
    )
    score, _ = score_run(tmp_path)
    assert score["metrics"]["recall"] is None
    assert score["metrics"]["precision"] is None
    assert score["metrics"]["cache_hit_rate"] is None
    assert score["metrics"]["interface_coverage"] is None
    assert score["metrics"]["reproducibility"] is None
    assert score["review_pending"] == ["F1", "F2"]


def test_cli_scans_runs_and_writes_stable_files(tmp_path: Path) -> None:
    write_run(tmp_path / "run-001", facts=example_facts())
    write_run(tmp_path / "run-002")
    written = score_directory(tmp_path)
    assert len(written) == 4
    first = json.loads((tmp_path / "run-001/score.json").read_text())
    empty = json.loads((tmp_path / "run-002/score.json").read_text())
    assert set(first) == {
        "profile",
        "task_id",
        "metrics",
        "review_pending",
        "provisional",
        "findings",
    }
    assert first["metrics"]["recall"] is None
    assert empty["metrics"]["satisfies_hit_rate"] is None
    assert empty["metrics"]["precision"] is None
    assert (tmp_path / "run-001/review-template.json").is_file()


def test_invalid_manual_answer_id_is_rejected(tmp_path: Path) -> None:
    write_run(tmp_path, facts=example_facts())
    (tmp_path / "review.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "id": "F1",
                        "classification": "problem",
                        "answer_id": "D1",
                        "location": True,
                        "mechanism": True,
                        "reproducible": True,
                    }
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="P answer id"):
        score_run(tmp_path)


def test_trusted_interface_denominator_cannot_be_shrunk_by_review(tmp_path: Path) -> None:
    write_run(tmp_path, report="GET /orders/{id} and POST /refund.")
    (tmp_path / "interfaces.json").write_text(
        json.dumps(
            {"expected_interfaces": ["GET /orders/{id}", "POST /refund", "PATCH /orders/{id}"]}
        )
    )
    (tmp_path / "review.json").write_text(
        json.dumps(
            {
                "expected_interfaces": ["GET /orders/{id}"],
                "checked_interfaces": ["GET /orders/{id}"],
                "findings": [],
            }
        )
    )
    score, template = score_run(tmp_path)
    assert score["metrics"]["interface_coverage"] == pytest.approx(1 / 3)
    assert template["expected_interfaces"] == [
        "GET /orders/{id}",
        "POST /refund",
        "PATCH /orders/{id}",
    ]
    assert template["expected_interfaces_source"] == "interfaces.json"
    assert template["interface_candidates"] == ["GET /orders/{id}", "POST /refund"]


def test_precision_deduplicates_reviewed_problem_and_distractor_mentions(tmp_path: Path) -> None:
    original = example_facts()["F1"]
    facts = {"F1": original, "F3": {**original, "id": "F3", "satisfies": []}}
    distractor = "The tests/test_cases.py example_total uses eval in a test but is not imported."
    write_run(tmp_path, facts=facts, report=f"{distractor}\n\n{distractor}")
    (tmp_path / "review.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "id": fid,
                        "classification": "problem",
                        "answer_id": "P1",
                        "location": True,
                        "mechanism": True,
                        "reproducible": True,
                    }
                    for fid in ("F1", "F3")
                ]
                + [
                    {"id": rid, "classification": "distractor", "answer_id": "D1"}
                    for rid in ("R1", "R2")
                ]
            }
        )
    )
    score, _ = score_run(tmp_path)
    assert score["review_pending"] == []
    assert score["metrics"]["recall"] == pytest.approx(1 / 6)
    assert score["metrics"]["precision"] == 0.5


def test_new_findings_can_share_an_explicit_unique_key(tmp_path: Path) -> None:
    report = (
        "A new issue was observed in the first investigation.\n\n"
        "The same new issue was restated in the final summary.\n\n"
        "The tests/test_cases.py example_total uses eval only in a test and is not imported."
    )
    write_run(tmp_path, report=report)
    (tmp_path / "review.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "id": identifier,
                        "classification": "new",
                        "unique_key": "new-issue-one",
                        "location": True,
                        "mechanism": True,
                        "reproducible": True,
                    }
                    for identifier in ("R1", "R2")
                ]
                + [{"id": "R3", "classification": "distractor", "answer_id": "D1"}]
            }
        )
    )
    score, _ = score_run(tmp_path)
    assert score["review_pending"] == []
    assert score["metrics"]["precision"] == 0.5


def test_one_fact_can_review_multiple_distinct_problems_and_new_findings(tmp_path: Path) -> None:
    write_run(
        tmp_path,
        facts=example_facts(),
        report=(
            "The tests/test_cases.py example_total uses eval only in a test and is not imported."
        ),
    )
    assessments = [
        {
            "classification": "problem",
            "answer_id": answer_id,
            "location": True,
            "mechanism": True,
            "reproducible": True,
        }
        for answer_id in ("P4", "P5")
    ] + [
        {
            "classification": "new",
            "location": True,
            "mechanism": True,
            "reproducible": True,
        }
        for _ in range(2)
    ]
    review = {
        "findings": [
            {
                "id": "F1",
                "classification": "problem",
                "answer_id": "P1",
                "location": True,
                "mechanism": True,
                "reproducible": True,
            },
            {"id": "F2", "assessments": assessments},
            {"id": "R1", "classification": "distractor", "answer_id": "D1"},
        ]
    }
    (tmp_path / "review.json").write_text(json.dumps(review))
    score, template = score_run(tmp_path)
    assert score["review_pending"] == []
    assert score["metrics"]["recall"] == pytest.approx(3 / 6)
    assert score["metrics"]["precision"] == pytest.approx(5 / 6)
    assert score["findings"][1]["review"]["assessments"] == assessments
    assert all("assessments" in item for item in template["findings"])

    assessments.extend(
        [
            {"classification": "false_positive"},
            {"classification": "false_positive"},
        ]
    )
    (tmp_path / "review.json").write_text(json.dumps(review))
    score, _ = score_run(tmp_path)
    assert score["review_pending"] == []
    assert score["metrics"]["precision"] == pytest.approx(5 / 8)
    for assessment in assessments[-2:]:
        assessment["unique_key"] = "same-unsupported-claim"
    (tmp_path / "review.json").write_text(json.dumps(review))
    score, _ = score_run(tmp_path)
    assert score["metrics"]["precision"] == pytest.approx(5 / 7)
    assessments.pop()
    assessments.pop()

    for assessment in assessments[-2:]:
        assessment["unique_key"] = "same-new-issue"
    (tmp_path / "review.json").write_text(json.dumps(review))
    score, _ = score_run(tmp_path)
    assert score["metrics"]["precision"] == pytest.approx(4 / 5)

    del assessments[1]["reproducible"]
    (tmp_path / "review.json").write_text(json.dumps(review))
    score, _ = score_run(tmp_path)
    assert score["review_pending"] == ["F2"]
    assert score["metrics"]["recall"] is None
    assert score["metrics"]["precision"] is None


def test_legacy_false_positive_counts_against_precision(tmp_path: Path) -> None:
    write_run(tmp_path, facts=example_facts())
    (tmp_path / "review.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "id": "F1",
                        "classification": "problem",
                        "answer_id": "P1",
                        "location": True,
                        "mechanism": True,
                        "reproducible": True,
                    },
                    {"id": "F2", "classification": "false_positive"},
                ]
            }
        )
    )
    score, _ = score_run(tmp_path)
    assert score["review_pending"] == []
    assert score["metrics"]["recall"] == pytest.approx(1 / 6)
    assert score["metrics"]["precision"] == 0.5
