"""Reasoning choices follow provider capabilities and compatible model IDs."""

import pytest
from bbx_contracts.providers import PROVIDERS, reasoning_efforts


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("deepseek", "deepseek-flash", ["low", "high", "max"]),
        ("deepseek", "custom", ["low", "high", "max"]),
        ("openai_compatible", "vendor/DeepSeek-v4", ["low", "high", "max"]),
        ("openai_responses", "deepseek-flash", ["low", "high", "max"]),
        ("openai_chat", "gpt-5", ["minimal", "low", "medium", "high", "xhigh"]),
        ("anthropic", "deepseek-flash", []),
        ("unknown", "deepseek-flash", []),
    ],
)
def test_reasoning_choices_respect_connection_capabilities(provider, model, expected):
    result = reasoning_efforts(provider, model)
    assert result == expected
    result.append("test-only")
    assert "test-only" not in PROVIDERS["deepseek"]["reasoning_efforts"]
    assert "test-only" not in PROVIDERS["openai_chat"]["reasoning_efforts"]
