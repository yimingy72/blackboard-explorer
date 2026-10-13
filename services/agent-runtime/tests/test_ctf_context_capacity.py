"""Available-window configuration preserves a longer stable prefix without relaxing gates."""

from copy import deepcopy
from typing import Any, cast

from bbx_runtime.ctf.context import estimate_messages_tokens
from bbx_runtime.ctf.middleware import CtfHistoryProvider
from test_ctf_context import history, model, provider, text


async def test_available_window_avoids_unnecessary_compaction_on_same_saved_history():
    messages = [
        text("old-input", "a" * 500_000),
        text("old-complete", "Complete", "assistant"),
        text("recent-input", "b" * 450_000),
        text("recent-complete", "Complete", "assistant"),
    ]
    original = deepcopy([message.to_dict() for message in messages])
    configured = model().model_copy(update={"context_window": 1_000_000})
    views = []
    for threshold in (400_000, 606_784):
        checkpoint = provider(messages).checkpoint
        value = CtfHistoryProvider(
            cast(Any, checkpoint.service), checkpoint, {"id": "turn"}, configured, threshold
        )
        projected = await history(value)
        projected = value.prepare_provider_messages(projected)
        assert (
            estimate_messages_tokens(projected, provider=configured.provider) <= value.input_limit
        )
        assert [message.to_dict() for message in messages] == original
        views.append((value, projected))
    limited, available = views
    assert limited[0].input_limit == 320_000
    assert available[0].input_limit == 485_427
    assert limited[0].checkpoint.session.state["ctf_history_view"]["epoch"] == 1
    assert available[0].checkpoint.session.state["ctf_history_view"]["epoch"] == 0
    assert available[1] == messages
