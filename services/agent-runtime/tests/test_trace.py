"""Trace text comes from actual model content."""

from typing import Any, cast

from bbx_runtime.pi import ChatResponse, Content, Message
from bbx_runtime.trace import model_content


def test_reasoning_requires_text_content_even_when_usage_reports_reasoning_tokens() -> None:
    response = ChatResponse(
        messages=[Message(role="assistant", contents=[Content.from_text("Visible")])],
        usage_details=cast(Any, {"reasoning_output_token_count": 50}),
    )
    assert model_content(response) == ("Visible", None)
    response = ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_text_reasoning(text="Returned reasoning"),
                    Content.from_text("Visible"),
                ],
            )
        ]
    )
    assert model_content(response) == ("Visible", "Returned reasoning")
