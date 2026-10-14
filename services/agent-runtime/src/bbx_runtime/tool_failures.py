"""Explicit ordinary-tool refusals and MAF's public fail-closed bridge."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from agent_framework import Content, MiddlewareFailure

from bbx_runtime.clients.blackboard import RemoteError

ToolResult = str | Content

_DOMAIN_HINTS = {
    "invalid_reference": "引用不存在。用 get/search 核对本任务已有事实、意图和验收编号后修正。",
    "intent_unavailable": "意图已被认领或关闭。先读取意图状态，再选择 open 意图。",
    "already_holding": "你已持有意图。先关闭或释放当前意图，再决定是否认领下一条。",
    "not_holder": "你不是该意图的持有者。先读取意图及认领状态，不要替其他 Agent 关闭或释放。",
    "note_required": "释放意图需要说明交接情况，请补充 note。",
    "evidence_required": "事实必须附已保存证据，请先登记证据。",
    "derived_from_required": "推断事实必须引用依据，请填写 derived_from。",
    "result_required": "resolves 和 result 必须同时填写，或同时省略。",
    "intent_incomplete": "请补齐意图的 statement、expected、method、based_on 和 relates_to。",
    "invalid_retry": "retry_of 必须引用已关闭为 inconclusive 的意图，且采用不同方法。",
    "verdicts_incomplete": "裁定必须恰好覆盖所有验收项，请补齐 verdicts。",
    "invalid_evidence_fact": "met 必须引用至少一条未被争议的支撑事实，请核对 evidence_facts。",
    "missing_required": "unmet 裁定必须说明尚缺什么，请填写 missing。",
    "invalid_explicit_completion": "明确完成需要满足裁定、具体理由及支撑事实。",
    "report_required": "终结模式必须提供已保存的报告，请补齐 report。",
}


def tool_error_content(code: str, message: str, **detail: Any) -> Content:
    return Content.from_text(
        json.dumps({"error": {"code": code, "message": message, **detail}}, ensure_ascii=False),
        additional_properties={"bbx_tool_error": code},
    )


def remote_tool_feedback(error: RemoteError) -> Content:
    """Translate only known RuleViolation codes, never authentication or control failures."""
    if error.status == 422 and error.code is not None and error.code in _DOMAIN_HINTS:
        return tool_error_content(error.code, _DOMAIN_HINTS[error.code])
    if error.status == 404 and error.code == "not_found":
        return tool_error_content("not_found", "对象不存在。请用 get/search 核对本任务的对象编号。")
    raise error


class BlackboardToolFailure(MiddlewareFailure):
    def __init__(self, original: Exception) -> None:
        super().__init__("Blackboard tool execution stopped; the outcome requires recovery.")
        self.original_error = original


@contextmanager
def propagate_tool_failure() -> Iterator[None]:
    try:
        yield
    except BlackboardToolFailure as error:
        raise error.original_error from error.original_error.__cause__
