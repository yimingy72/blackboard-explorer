"""Conservative candidate matching and human-reviewed mini-shop scores."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

ANSWER = Path(__file__).resolve().parents[1] / "answers/mini-shop/answer.yaml"
CLASSIFICATIONS = {"problem", "distractor", "new", "false_positive", "ignore"}
INTERFACE = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+/[\w/{}/.-]+", re.I)


def _read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number < float("inf") else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        rows = value.values()
    elif isinstance(value, list):
        rows = value
    else:
        return []
    return [row for row in rows if isinstance(row, dict)]


def _persisted_uris(fact: dict[str, Any], task_id: str) -> list[str]:
    prefixes = (f"evidence/{task_id}/", f"toolcalls/{task_id}/")
    return sorted(
        {
            uri
            for item in _rows(fact.get("evidence"))
            if isinstance(uri := item.get("uri"), str)
            and uri.startswith(prefixes)
            and all(part not in {"", ".", ".."} for part in uri.split("/"))
        }
    )


def _candidates(text: str, answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lowered = text.casefold()
    found = []
    for answer in answers:
        file_name = str(answer["file"])
        file_match = file_name.casefold() in lowered or Path(file_name).name.casefold() in lowered
        function_match = bool(
            re.search(rf"(?<!\w){re.escape(str(answer['function']))}(?!\w)", text, re.I)
        )
        keywords = [
            keyword
            for keyword in answer.get("mechanism_keywords", [])
            if str(keyword).casefold() in lowered
        ]
        if sum((file_match, function_match, bool(keywords))) >= 2:
            found.append(
                {
                    "answer_id": answer["id"],
                    "kind": "problem" if answer["id"].startswith("P") else "distractor",
                    "file_match": file_match,
                    "function_match": function_match,
                    "mechanism_keywords": keywords,
                }
            )
    return found


def _findings(
    state: dict[str, Any], report: str, answers: list[dict[str, Any]], task_id: str
) -> list[dict[str, Any]]:
    facts = _rows(state.get("facts"))
    persisted = {uri for fact in facts for uri in _persisted_uris(fact, task_id)}
    findings = []
    for fact in facts:
        identifier = str(fact.get("id", f"F?{len(findings) + 1}"))
        text = str(fact.get("statement") or "")
        findings.append(
            {
                "id": identifier,
                "source": "fact",
                "text": text,
                "satisfies": list(fact.get("satisfies") or []),
                "provenance": fact.get("provenance"),
                "evidence_uris": _persisted_uris(fact, task_id),
                "candidates": _candidates(text, answers),
            }
        )
    for number, paragraph in enumerate(re.split(r"\n\s*\n", report), start=1):
        text = paragraph.strip()
        if len(text) < 20:
            continue
        findings.append(
            {
                "id": f"R{number}",
                "source": "report",
                "text": text,
                "satisfies": [],
                "provenance": None,
                "evidence_uris": sorted(uri for uri in persisted if uri in text),
                "candidates": _candidates(text, answers),
            }
        )
    return sorted(
        findings,
        key=lambda item: (
            item["source"] != "fact",
            not bool(item["satisfies"]),
            item["provenance"] != "tool_backed",
            item["id"],
        ),
    )


def _review_map(review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = review.get("findings")
    if isinstance(rows, dict):
        return {key: value for key, value in rows.items() if isinstance(value, dict)}
    return {str(row["id"]): row for row in _rows(rows) if isinstance(row.get("id"), str)}


def _assessments(row: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if row is None:
        return None
    if "assessments" not in row or row["assessments"] is None:
        return [row]
    values = row["assessments"]
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(item, dict) for item in values)
    ):
        return None
    return values


def _reviewed(row: dict[str, Any], problem_ids: set[str], distractor_ids: set[str]) -> bool:
    if row.get("classification") not in CLASSIFICATIONS:
        return False
    classification = row["classification"]
    answer_id = row.get("answer_id")
    if classification == "problem" and answer_id not in problem_ids:
        raise ValueError(f"Reviewed problem needs a P answer id: {answer_id}")
    if classification == "distractor" and answer_id not in distractor_ids:
        raise ValueError(f"Reviewed distractor needs a D answer id: {answer_id}")
    if classification in {"problem", "new"}:
        return all(
            isinstance(row.get(field), bool) for field in ("location", "mechanism", "reproducible")
        )
    return True


def _credit(row: dict[str, Any]) -> float:
    if row.get("location") and row.get("mechanism"):
        return 1.0 if row.get("reproducible") else 0.5
    return 0.0


def _review_metrics(
    findings: list[dict[str, Any]], review: dict[str, Any] | None, answers: dict[str, Any]
) -> tuple[float | None, float | None, list[str], list[dict[str, Any]]]:
    problem_ids = {row["id"] for row in answers["problems"]}
    distractor_ids = {row["id"] for row in answers["distractors"]}
    by_id = _review_map(review or {})
    pending = []
    credited: dict[str, float] = {}
    reviewed_groups: dict[tuple[str, str], float] = {}
    scored = []
    for finding in findings:
        row = by_id.get(finding["id"])
        assessments = _assessments(row)
        confirmed = assessments is not None and all(
            [_reviewed(item, problem_ids, distractor_ids) for item in assessments]
        )
        item = {**finding, "review": row if confirmed else None}
        if not confirmed:
            pending.append(finding["id"])
        else:
            assert row is not None
            assert assessments is not None
            for index, assessment in enumerate(assessments):
                kind = assessment["classification"]
                if kind == "ignore":
                    continue
                credit = _credit(assessment) if kind in {"problem", "new"} else 0.0
                if kind == "problem":
                    key = str(assessment["answer_id"])
                    credited[key] = max(credited.get(key, 0.0), credit)
                elif kind == "distractor":
                    key = str(assessment["answer_id"])
                else:
                    fallback = (
                        finding["id"]
                        if row.get("assessments") is None
                        else f"{finding['id']}:{index}"
                    )
                    key = str(assessment.get("unique_key") or fallback)
                group = (kind, key)
                reviewed_groups[group] = max(reviewed_groups.get(group, 0.0), credit)
        scored.append(item)
    if review is None or pending:
        return None, None, pending, scored
    recall = sum(credited.values()) / len(problem_ids) if problem_ids else None
    precision = (
        sum(credit >= 0.5 for credit in reviewed_groups.values()) / len(reviewed_groups)
        if reviewed_groups
        else None
    )
    return recall, precision, pending, scored


def _interface_coverage(
    review: dict[str, Any] | None, trusted_expected: list[str] | None
) -> float | None:
    if review is None:
        return None
    expected = (
        trusted_expected if trusted_expected is not None else review.get("expected_interfaces")
    )
    checked = review.get("checked_interfaces")
    if not isinstance(expected, list) or not expected or not isinstance(checked, list):
        return None
    if not all(isinstance(item, str) for item in [*expected, *checked]):
        return None
    wanted = set(expected)
    return len(wanted.intersection(checked)) / len(wanted)


def _reproducibility(path: Path) -> float | None:
    replay = _read_json(path)
    if not isinstance(replay, dict):
        return None
    value = _number(replay.get("reproducibility"))
    return value if value is not None and value <= 1 else None


def _trusted_interfaces(path: Path) -> list[str] | None:
    document = _read_json(path)
    if document is None:
        return None
    expected = document.get("expected_interfaces") if isinstance(document, dict) else None
    if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
        raise ValueError("interfaces.json must contain expected_interfaces strings")
    return expected


def _interface_candidates(report: str) -> list[str]:
    found = []
    for match in INTERFACE.finditer(report):
        method, path = match.group().split(None, 1)
        found.append(f"{method.upper()} {path.rstrip('.,;:')}")
    return sorted(set(found))


def _quality_metrics(
    state: dict[str, Any], events: list[dict[str, Any]], run: dict[str, Any]
) -> dict[str, Any]:
    task = _mapping(state.get("task"))
    usage = _mapping(task.get("usage"))
    hit = _number(usage.get("cache_hit_tokens"))
    miss = _number(usage.get("cache_miss_tokens"))
    cache_rate = hit / (hit + miss) if hit is not None and miss is not None and hit + miss else None
    claims = {
        (str(fact.get("id")), str(acceptance))
        for fact in _rows(state.get("facts"))
        for acceptance in fact.get("satisfies") or []
    }
    acceptance = _mapping(task.get("acceptance_state"))
    hits = {
        (str(fid), str(aid))
        for aid, item in acceptance.items()
        if isinstance(item, dict) and item.get("status") == "met"
        for fid in item.get("evidence_facts") or []
    }
    reasons: dict[str, int] = {}
    for agent in _rows(state.get("agents")):
        reason = agent.get("end_reason")
        if isinstance(reason, str):
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "elapsed_seconds": _number(run.get("elapsed_seconds")),
        "cost": _number(usage.get("cost")),
        "cache_hit_rate": cache_rate,
        "disputes": sum(event.get("type") == "fact.disputed" for event in events),
        "satisfies_hit_rate": len(claims.intersection(hits)) / len(claims) if claims else None,
        "judgments": sum(event.get("type") == "acceptance.judged" for event in events),
        "end_reasons": dict(sorted(reasons.items())),
    }


def score_run(
    directory: Path, answers: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    run = _read_json(directory / "run.json")
    state = _read_json(directory / "state.json")
    events = _read_json(directory / "events.json")
    if not isinstance(run, dict) or not isinstance(state, dict) or not isinstance(events, list):
        raise ValueError(f"Missing or invalid run/state/events data in {directory}")
    report_path = directory / "report.md"
    if not report_path.is_file():
        raise ValueError(f"Missing report.md in {directory}")
    report = report_path.read_text(encoding="utf-8", errors="replace")
    if answers is None:
        answers = yaml.safe_load(ANSWER.read_text(encoding="utf-8"))
    if not isinstance(answers, dict):
        raise ValueError("Answer key must be an object")
    task_id = str(run.get("task_id") or _mapping(state.get("task")).get("id") or "")
    profile = run.get("profile_name") or run.get("profile")
    if isinstance(profile, dict):
        profile = profile.get("name")
    findings = _findings(state, report, [*answers["problems"], *answers["distractors"]], task_id)
    review = _read_json(directory / "review.json")
    if review is not None and not isinstance(review, dict):
        raise ValueError("review.json must be an object")
    recall, precision, pending, scored = _review_metrics(findings, review, answers)
    replay = _reproducibility(directory / "replay-results.json")
    trusted_interfaces = _trusted_interfaces(directory / "interfaces.json")
    coverage = _interface_coverage(review, trusted_interfaces)
    metrics = {
        "recall": recall,
        "precision": precision,
        "reproducibility": replay,
        "interface_coverage": coverage,
        **_quality_metrics(state, [event for event in events if isinstance(event, dict)], run),
    }
    template = {
        "profile": profile,
        "task_id": task_id,
        "expected_interfaces": trusted_interfaces,
        "expected_interfaces_source": "interfaces.json"
        if trusted_interfaces is not None
        else "manual",
        "expected_interfaces_note": (
            "The target diff defines this denominator; review.json cannot reduce it."
            if trusted_interfaces is not None
            else "Fill expected_interfaces manually; coverage stays null for an empty denominator."
        ),
        "checked_interfaces": None,
        "interface_candidates": _interface_candidates(report),
        "findings": [
            {
                "id": item["id"],
                "source": item["source"],
                "candidates": item["candidates"],
                "evidence_uris": item["evidence_uris"],
                "assessments": None,
                "classification": None,
                "answer_id": None,
                "location": None,
                "mechanism": None,
                "reproducible": None,
                "note": "",
            }
            for item in findings
        ],
    }
    output = {
        "profile": profile,
        "task_id": task_id,
        "metrics": metrics,
        "review_pending": pending,
        "provisional": bool(
            pending
            or review is None
            or replay is None
            or coverage is None
            or run.get("outcome") != "success"
        ),
        "findings": scored,
    }
    return output, template


def score_directory(directory: Path) -> list[Path]:
    runs = sorted(directory.rglob("run.json"))
    if not runs:
        raise ValueError(f"No run.json found under {directory}")
    answers = yaml.safe_load(ANSWER.read_text(encoding="utf-8"))
    written = []
    for run_path in runs:
        run_dir = run_path.parent
        score, template = score_run(run_dir, answers)
        for name, value in (("score.json", score), ("review-template.json", template)):
            path = run_dir / name
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score exported mini-shop runs without executing evidence"
    )
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    for path in score_directory(args.directory):
        print(path)


if __name__ == "__main__":
    main()
