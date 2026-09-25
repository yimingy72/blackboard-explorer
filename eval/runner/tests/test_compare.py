"""Conclusions require complete reviewed groups rather than keyword candidates."""

from eval.runner.compare import render, summarize


def run(profile, recall, *, provisional=False):
    return {
        "profile": profile,
        "task_id": "example",
        "provisional": provisional,
        "review_pending": 0,
        "metrics": {"recall": recall, "cost": 0.2, "elapsed_seconds": 10},
    }


def test_unknown_metrics_are_not_zero_and_worst_direction_is_correct():
    rows = [run("default", 0.5), run("default", 1), run("default", None)]
    rows[1]["metrics"]["elapsed_seconds"] = 50
    result = summarize(rows)
    assert result["recall"] == {"n": 2, "mean": 0.75, "worst": 0.5}
    assert result["elapsed_seconds"]["worst"] == 50
    assert result["precision"] == {"n": 0, "mean": None, "worst": None}


def test_provisional_or_small_samples_cannot_claim_advantage():
    assert "不能判断" in render([run("default", 1), run("single", 0)])
    rows = [run("default", 1, provisional=True) for _ in range(5)]
    rows += [run("single", 0) for _ in range(5)]
    assert "不能判断" in render(rows)


def test_reviewed_recall_difference_uses_one_problem_threshold():
    rows = [run("default", 5 / 6) for _ in range(5)]
    rows += [run("single", 4 / 6) for _ in range(5)]
    assert "达到召回率判断标准" in render(rows)


def test_equal_recall_reports_speed_change_without_inventing_threshold():
    rows = [run("default", 1) for _ in range(5)]
    singles = [run("single", 1) for _ in range(5)]
    for item in singles:
        item["metrics"]["elapsed_seconds"] = 20
    assert "耗时减少 50.0%" in render(rows + singles)
    assert "人工判断" in render(rows + singles)
