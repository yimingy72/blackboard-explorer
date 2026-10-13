"""Build a traceable terminal report from recorded observations without model calls."""

import json
import re
from collections import Counter
from decimal import Decimal
from typing import Any

from fastapi.encoders import jsonable_encoder

SUMMARY_CHARS = 1200
ARTIFACT_FIELDS = ("id", "uri", "sha256", "path", "size", "created_version")


def _excerpt(value: Any) -> dict[str, Any]:
    text = str(value or "")
    return {
        "text": text[:SUMMARY_CHARS],
        "truncated": len(text) > SUMMARY_CHARS,
        "original_chars": len(text),
    }


def _artifact(ref: dict[str, Any]) -> dict[str, Any]:
    return {key: ref.get(key) for key in ARTIFACT_FIELDS}


def _verification(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value.get(key)
        for key in (
            "status",
            "source",
            "test_only",
            "required",
            "basis",
            "updated_at",
            "submission_id",
            "call_id",
            "user_id",
            "response_uri",
            "external_ref",
        )
    }
    result["summary"] = _excerpt(value.get("summary"))
    return result


def build_report_summary(state: dict[str, Any]) -> dict[str, Any]:
    task = state["task"]
    control = task.get("ctf_control")
    registered = {str(item["id"]): item for item in state["artifacts"]}
    linked = set()
    by_challenge: dict[str, list[dict[str, Any]]] = {}
    for record in state["records"]:
        if record.get("kind") != "artifact_registration":
            by_challenge.setdefault(str(record.get("challenge_id")), []).append(record)
    challenges = []
    for challenge in state["challenges"]:
        records = sorted(
            by_challenge.get(str(challenge["id"]), []),
            key=lambda item: (item.get("created_version", 0), str(item.get("id", ""))),
        )
        summaries = []
        claims = []
        for record in records:
            refs = [_artifact(ref) for ref in record.get("artifact_refs", [])]
            linked.update(str(ref["id"]) for ref in refs)
            summary = {
                key: record.get(key)
                for key in (
                    "id",
                    "kind",
                    "created_version",
                    "author_id",
                    "created_at",
                    "summary_applied",
                )
            }
            summary.update(body=_excerpt(record.get("body")), artifact_refs=refs)
            summary["help_observations"] = {
                key: _excerpt(record[key])
                for key in (
                    "attempted_routes",
                    "observations_and_basis",
                    "failure_conditions",
                    "current_blocker",
                    "help_needed",
                    "no_artifacts_reason",
                )
                if record.get(key)
            }
            summaries.append(summary)
            verification = record.get("verification") or {}
            if verification.get("source") == "agent":
                claims.append({"record_id": record["id"], **_verification(verification)})
        current = challenge.get("verification") or {}
        if current.get("source") == "agent":
            claims.append({"source_location": "challenge.verification", **_verification(current)})
        trusted = current.get("source") in {"platform", "user"} and not current.get("test_only")
        challenges.append(
            {
                "id": challenge["id"],
                "revision": challenge.get("revision"),
                "title": challenge.get("title"),
                "work_status": challenge.get("work_status"),
                "candidate_claims": claims,
                "trusted_verification": _verification(current) if trusted else None,
                "test_verification": _verification(current) if current.get("test_only") else None,
                "target_last_observation": challenge.get("target"),
                "records": summaries,
                "recording_note": None
                if records
                else "无共享记录；无法据此判断是否尝试或当前卡点。",
            }
        )
    settled = {
        str(request_id)
        for turn in state.get("turns", [])
        for request_id in turn.get("usage_requests", [])
    }
    pending_usage = set()
    for session in state.get("sessions", []):
        values = session.get("session", {}).get("state", {})
        for key in ("ctf_usage_outbox", "ctf_review_usage_outbox"):
            for index, entry in enumerate(values.get(key, [])):
                request_id = str(
                    entry.get("request_id", f"{session.get('agent_id')}:{key}:{index}")
                )
                if request_id not in settled:
                    pending_usage.add(request_id)

    def reservations(key: str) -> dict[str, Any]:
        if control is None:
            return {"data_available": False, "count": None, "reserved_cost": None, "entries": []}
        entries = control.get(key, {})
        return {
            "data_available": True,
            "count": len(entries),
            "reserved_cost": str(
                sum((Decimal(str(entry.get("cost", 0))) for entry in entries.values()), Decimal(0))
            ),
            "entries": [
                {
                    "reservation_id": reservation_id,
                    **{
                        field: entry.get(field)
                        for field in ("state", "cost", "agent_id", "turn_id", "generation")
                    },
                }
                for reservation_id, entry in entries.items()
            ],
        }

    return jsonable_encoder(
        {
            "format": "bbx.ctf-report.v1",
            "task_id": str(task["id"]),
            "run_number": task["run_number"],
            "task_status": task["status"],
            "conclusion": task.get("ctf_conclusion") or {},
            "budget": {
                "limit": task.get("budget"),
                "recorded_cost_estimate": task.get("usage", {}).get("cost"),
                "active_reservations": reservations("budget_reservations"),
                "unconfirmed_result_reservations": reservations("budget_unknown_reservations"),
                "unsettled_usage_outbox_count": len(pending_usage) if "sessions" in state else None,
                "note": "已入账金额是配置价格估算；在途/准入预留和结果未确认预留是待核实占额，"
                "不代表供应商实付，不推定旧条目真实已发送；待对账usage仅列计数，不重复计费。",
            },
            "counts": {
                "challenges": len(challenges),
                "work_status": dict(Counter(item["work_status"] for item in challenges)),
                "trusted_accepted": sum(
                    (item["trusted_verification"] or {}).get("status") == "accepted"
                    for item in challenges
                ),
                "registered_artifacts": len(registered),
                "challenge_referenced_artifacts": len(linked & registered.keys()),
            },
            "challenges": challenges,
            "unreferenced_artifacts": [
                _artifact(item) for key, item in registered.items() if key not in linked
            ],
            "source_note": "摘要来自已有记录；截取标记对应原文字符数，完整原文保留在归档"
            "state.records，可按record ID/version定位。工作完成声明、候选和可信验收分别呈现；"
            "外部目标仅为最后观察，容器清理不能证明外部目标已关闭。",
        }
    )


def render_report(summary: dict[str, Any]) -> str:
    def fenced(text: str, language: str = "text") -> str:
        # Keep recorded Markdown inside the data block, including embedded fences.
        fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", text)), default=3))
        return f"{fence}{language}\n{text}\n{fence}"

    def block(value: Any) -> str:
        return fenced(json.dumps(value, ensure_ascii=False, indent=2), "json")

    def inline(value: Any) -> str:
        text = " ".join(str(value if value is not None else "未记录").splitlines())
        return re.sub(r"([\\`*_{}\[\]()<>|#!])", r"\\\1", text)

    def excerpt(value: dict[str, Any]) -> str:
        note = (
            f"（已截取前 {SUMMARY_CHARS} 字符；原文 {value['original_chars']} 字符；"
            "完整原文见归档中对应来源）\n\n"
            if value["truncated"]
            else ""
        )
        return note + fenced(value["text"])

    def artifacts(refs: list[dict[str, Any]]) -> str:
        rows = ["| 产物 ID | URI | SHA256 |", "|---|---|---|"]
        rows.extend(
            "| " + " | ".join(inline(ref.get(key)) for key in ("id", "uri", "sha256")) + " |"
            for ref in refs
        )
        return "\n".join(rows) if refs else "无产物引用。"

    table = [
        "| 题目 | 工作状态 | 候选声明（agent） | 可信验收 | 记录数 | 证据数（去重） |",
        "|---|---|---|---|---|---|",
    ]
    for challenge in summary["challenges"]:
        claims = (
            " / ".join(
                dict.fromkeys(
                    claim.get("status") or "未记录" for claim in challenge["candidate_claims"]
                )
            )
            or "未记录"
        )
        verification = challenge["trusted_verification"]
        trusted = (
            f"{verification['status']}（{verification['source']}）" if verification else "未记录"
        )
        evidence = {ref["id"] for record in challenge["records"] for ref in record["artifact_refs"]}
        table.append(
            "| "
            + " | ".join(
                inline(value)
                for value in (
                    challenge["title"],
                    challenge["work_status"],
                    claims,
                    trusted,
                    len(challenge["records"]),
                    len(evidence),
                )
            )
            + " |"
        )

    parts = [
        "# CTF task conclusion",
        summary["source_note"],
        "## 任务结论\n\n" + block(summary["conclusion"]),
        "## 逐题汇总\n\n" + "\n".join(table),
        "测试适配结果仅在下方详情列出，不计入可信验收。证据数按该题记录引用的产物 ID 去重。",
        "## 统计\n\n" + block(summary["counts"]),
        "## 费用估算与待核实预留\n\n"
        + f"已入账费用估算：{inline(summary['budget']['recorded_cost_estimate'])}。\n\n"
        + block(summary["budget"]),
        "## 按题记录与证据",
    ]
    help_labels = {
        "attempted_routes": "已记录的尝试路线",
        "observations_and_basis": "观察及依据",
        "failure_conditions": "失败条件",
        "current_blocker": "当前卡点",
        "help_needed": "所需帮助",
        "no_artifacts_reason": "无产物原因",
    }
    for index, challenge in enumerate(summary["challenges"], 1):
        parts.append(f"### 题目 {index}：{inline(challenge['title'])}")
        parts.append(
            f"题目 ID：{inline(challenge['id'])}；工作状态：{inline(challenge['work_status'])}。"
        )
        if not challenge["records"]:
            parts.append(challenge["recording_note"])
        for record in challenge["records"]:
            parts.append(
                "**记录来源**："
                + "；".join(
                    f"{label}：{inline(record[key])}"
                    for label, key in (
                        ("ID", "id"),
                        ("version", "created_version"),
                        ("作者", "author_id"),
                        ("时间", "created_at"),
                        ("类型", "kind"),
                    )
                )
            )
            parts.append("**原记录摘要**\n\n" + excerpt(record["body"]))
            for key, value in record["help_observations"].items():
                parts.append(f"**{help_labels[key]}**（来源：上述记录）\n\n" + excerpt(value))
            parts.append("**该记录的证据索引**\n\n" + artifacts(record["artifact_refs"]))
        parts.append(
            "**观察与验证详情（原记录值）**\n\n"
            + block(
                {
                    key: value
                    for key, value in challenge.items()
                    if key not in {"records", "recording_note"}
                }
            )
        )
    parts.append(
        "## 已登记但未被题目记录引用的产物\n\n" + artifacts(summary["unreferenced_artifacts"])
    )
    return "\n\n".join(parts) + "\n"
