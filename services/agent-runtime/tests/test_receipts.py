"""Final reply extraction and task-specific receipt validation."""

from bbx_runtime.receipts import parse_receipt


def test_explore_uses_last_complete_json_object() -> None:
    text = (
        '先前草稿：{"accepted": false, "reason": "不再适用"}\n'
        '```json\n{"accepted": true, "data": {"intent_result": "confirmed", '
        '"posted": ["F2", "I1"], "note": "已提交"}}\n```\n结束。'
    )
    assert parse_receipt(text, "explore") == {
        "accepted": True,
        "data": {"intent_result": "confirmed", "posted": ["F2", "I1"], "note": "已提交"},
    }


def test_derive_close_and_refusal_models() -> None:
    assert parse_receipt(
        '{"accepted": true, "data": {"posted": ["I9"], "excluded": ["已排除 I3"]}}',
        "derive",
    ) == {"accepted": True, "data": {"posted": ["I9"], "excluded": ["已排除 I3"]}}
    assert parse_receipt('{"accepted": true, "data": {"note": "裁定已提交"}}', "close") == {
        "accepted": True,
        "data": {"note": "裁定已提交"},
    }
    assert parse_receipt('{"accepted": false, "reason": "工具不可用"}', "explore") == {
        "accepted": False,
        "reason": "工具不可用",
    }


def test_invalid_final_object_falls_back_and_preserves_raw_text() -> None:
    text = (
        '{"accepted": true, "data": {"intent_result": "confirmed", "note": "有效"}}'
        '\n最终：{"accepted": true, "data": {"intent_result": "maybe"}}'
    )
    assert parse_receipt(text, "explore") == {
        "accepted": True,
        "data": {"note": "回执格式错误"},
        "raw_text": text,
    }
    malformed = "没有任何 JSON，只有总结。"
    assert parse_receipt(malformed, "close")["raw_text"] == malformed


def test_wrong_task_receipt_is_rejected() -> None:
    text = '{"accepted": true, "data": {"posted": [], "excluded": []}}'
    assert parse_receipt(text, "explore")["data"] == {"note": "回执格式错误"}
