"""Terminal reports preserve evidence attribution and do not invent verification."""

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bbx_blackboard.ctf_lifecycle import CtfLifecycleMixin
from bbx_blackboard.ctf_report import SUMMARY_CHARS, build_report_summary, render_report


def report_state():
    artifact = {"id": "evidence", "uri": "evidence/task/file", "sha256": "abc", "size": 5}
    return {
        "task": {
            "id": "task",
            "status": "finished",
            "run_number": 2,
            "ctf_conclusion": {"end_reason": "budget_exhausted", "lead_claim": False},
            "usage": {"cost": "3.25"},
            "budget": {"max_cost": "10"},
            "ctf_control": {
                "budget_reservations": {"active": {"cost": "1.5", "state": "admitted"}},
                "budget_unknown_reservations": {"unknown": {"cost": "2", "state": "unknown"}},
            },
        },
        "challenges": [
            {
                "id": "candidate",
                "title": "Recorded candidate",
                "work_status": "in_progress",
                "verification": {"status": "candidate", "source": "agent", "summary": "Claim"},
            },
            {
                "id": "tested",
                "title": "Test observation",
                "work_status": "completed",
                "verification": {"status": "accepted", "source": "platform", "test_only": True},
            },
            {
                "id": "trusted",
                "work_status": "completed",
                "verification": {"status": "accepted", "source": "user"},
            },
        ],
        "records": [
            {
                "id": "record",
                "challenge_id": "candidate",
                "kind": "help",
                "body": "Original ```text\n" + "x" * SUMMARY_CHARS,
                "current_blocker": "Waiting for an observation",
                "author_id": "member",
                "created_version": 12,
                "created_at": "2026-10-12T00:00:00Z",
                "artifact_refs": [artifact],
            },
            {"kind": "artifact_registration", "artifact": artifact},
            {"kind": "artifact_registration", "artifact": {"id": "orphan", "uri": "orphan"}},
        ],
        "artifacts": [artifact, {"id": "orphan", "uri": "orphan"}],
        "turns": [{"usage_requests": ["billed"]}],
        "sessions": [
            {
                "agent_id": "member",
                "session": {
                    "state": {
                        "ctf_usage_outbox": [{"request_id": "pending"}, {"request_id": "billed"}],
                        "ctf_review_usage_outbox": [{"request_id": "pending"}],
                    }
                },
            }
        ],
    }


def test_report_links_recorded_evidence_without_upgrading_candidate_or_test_result():
    state = report_state()
    original = deepcopy(state)
    summary = build_report_summary(state)
    candidate, tested, trusted = summary["challenges"]
    assert candidate["work_status"] == "in_progress"
    assert candidate["candidate_claims"][0]["status"] == "candidate"
    assert candidate["trusted_verification"] is None
    assert tested["trusted_verification"] is None
    assert tested["test_verification"]["status"] == "accepted"
    assert trusted["trusted_verification"]["source"] == "user"
    assert summary["counts"]["trusted_accepted"] == 1
    assert summary["counts"]["challenge_referenced_artifacts"] == 1
    assert summary["unreferenced_artifacts"][0]["id"] == "orphan"
    record = candidate["records"][0]
    assert record["id"] == "record" and record["created_version"] == 12
    assert record["author_id"] == "member" and record["created_at"]
    assert record["artifact_refs"][0]["uri"] == "evidence/task/file"
    assert record["artifact_refs"][0]["sha256"] == "abc"
    assert record["body"]["truncated"] is True
    assert record["body"]["original_chars"] == len(state["records"][0]["body"])
    assert record["help_observations"]["current_blocker"]["text"] == "Waiting for an observation"
    assert "无法据此判断是否尝试" in tested["recording_note"]
    assert "````text" in render_report(summary)
    assert state == original


def test_report_budget_separates_estimate_reservations_and_unsettled_usage():
    budget = build_report_summary(report_state())["budget"]
    assert budget["recorded_cost_estimate"] == "3.25"
    assert budget["active_reservations"]["reserved_cost"] == "1.5"
    assert budget["unconfirmed_result_reservations"]["reserved_cost"] == "2"
    assert budget["unconfirmed_result_reservations"]["count"] == 1
    assert budget["unsettled_usage_outbox_count"] == 1
    assert "不代表供应商实付" in budget["note"]
    assert "不推定旧条目真实已发送" in budget["note"]


def test_report_does_not_treat_unavailable_budget_snapshot_as_zero():
    state = report_state()
    del state["task"]["ctf_control"]
    del state["sessions"]
    del state["task"]["usage"]["cost"]
    summary = build_report_summary(state)
    budget = summary["budget"]
    assert budget["recorded_cost_estimate"] is None
    assert budget["active_reservations"]["data_available"] is False
    assert budget["unconfirmed_result_reservations"]["reserved_cost"] is None
    assert budget["unsettled_usage_outbox_count"] is None
    assert "已入账费用估算：未记录。" in render_report(summary)


def test_markdown_report_has_readable_counts_sources_blockers_and_safe_recorded_text():
    state = report_state()
    state["challenges"][0]["title"] = "Bad | [link](https://invalid)\n## Forged heading"
    state["records"][0]["body"] = "```\n## Forged heading\n```\n" + "x" * SUMMARY_CHARS
    report = render_report(build_report_summary(state))
    assert report.startswith("# CTF task conclusion\n")
    assert "| 题目 | 工作状态 | 候选声明（agent） | 可信验收 | 记录数 | 证据数（去重） |" in report
    assert "| in\\_progress | candidate | 未记录 | 1 | 1 |" in report
    assert "| completed | 未记录 | accepted（user） | 0 | 0 |" in report
    assert "Bad \\| \\[link\\]\\(https://invalid\\) \\#\\# Forged heading" in report
    assert "ID：record；version：12；作者：member；时间：2026-10-12T00:00:00Z；类型：help" in report
    assert "**当前卡点**（来源：上述记录）" in report
    assert "Waiting for an observation" in report
    assert "已截取前 1200 字符" in report and "完整原文见归档中对应来源" in report
    assert "| evidence | evidence/task/file | abc |" in report
    assert "| orphan | orphan | 未记录 |" in report
    assert "无共享记录；无法据此判断是否尝试或当前卡点。" in report
    assert "````text\n```\n## Forged heading\n```\n" in report


@pytest.mark.parametrize("run", [1, 2])
async def test_archive_report_keeps_existing_uri_and_full_records(run):
    state = report_state()
    state["task"]["run_number"] = run
    original = deepcopy(state["records"])

    async def execute(statement):
        table = statement.get_final_froms()[0].name
        key = {
            "ctf_members": "members",
            "ctf_turns": "turns",
            "ctf_messages": "messages",
            "ctf_challenges": "challenges",
            "ctf_records": "records",
            "agent_sessions": "sessions",
            "events": "events",
            "task_runs": "task_runs",
        }[table]
        rows = state.get(key, [])
        if key in {"challenges", "records"}:
            rows = [{"data": row} for row in rows]
        return SimpleNamespace(mappings=lambda: rows)

    @asynccontextmanager
    async def begin():
        yield SimpleNamespace(execute=execute)

    service = CtfLifecycleMixin()
    service._terminal = AsyncMock(return_value=state["task"])
    service.repo = SimpleNamespace(engine=SimpleNamespace(begin=begin), append=AsyncMock())
    service.objects = SimpleNamespace(put=AsyncMock())
    snapshot = await service.export_archive("task")
    expected = "reports/task.md" if run == 1 else "reports/task/run-2.md"
    assert snapshot["state"]["task"]["report_uri"] == expected
    assert snapshot["report_summary"]["format"] == "bbx.ctf-report.v1"
    assert snapshot["state"]["records"] == original
    assert snapshot["state"]["records"][0]["body"].endswith("x" * SUMMARY_CHARS)
    args = service.objects.put.await_args
    assert args.args[0] == expected
    assert "record" in args.args[1].decode() and "abc" in args.args[1].decode()
    assert args.kwargs["content_type"] == "text/markdown"
