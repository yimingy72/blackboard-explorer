"""Historical reconciliation recomputes token costs without trusting old totals."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from bbx_blackboard.billing import estimate_calls
from bbx_contracts.models import ModelConfig, Price


def test_reconciliation_uses_per_call_window_and_catches_missing_events():
    model = ModelConfig(
        provider="deepseek",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        reasoning_effort="high",
        price=Price(
            currency="CNY",
            cache_hit_per_m=Decimal("0.04"),
            cache_miss_per_m=Decimal("2"),
            output_per_m=Decimal("8"),
            off_peak=False,
        ),
    )
    models = {role: model.model_dump(mode="json") for role in ("explore", "derive", "close")}
    usage = {
        "cache_hit_tokens": 1000000,
        "cache_miss_tokens": 1000000,
        "output_tokens": 1000000,
        "reasoning_tokens": 500000,
        "cost": "10.04",
    }
    agents = {"agent-1": {"task_type": "derive", "usage": usage, "steps": 1}}
    events = [
        {
            "type": "agent.progress",
            "version": 15,
            "created_at": datetime(2026, 9, 28, 5, 30, tzinfo=UTC),
            "payload": {"agent_id": "agent-1", "steps": 1, "usage": usage},
        }
    ]
    estimate = estimate_calls(agents, events, models)
    assert Decimal(estimate["cost"]) == Decimal("5.02")
    assert estimate["legacy_timestamp_calls"] == 1
    # A response completed after the boundary is billed using request start if available.
    events[0]["payload"]["pricing"] = {
        "requested_at": "2026-09-28T03:59:00+00:00",
        "timestamp_basis": "request_start",
    }
    estimate = estimate_calls(agents, events, models)
    assert Decimal(estimate["cost"]) == Decimal("10.04")
    assert estimate["warning"] is None
    with pytest.raises(ValueError, match="缺少"):
        estimate_calls(agents, [], models)
    agents["agent-1"]["usage"] = {**usage, "cache_hit_tokens": 1}
    with pytest.raises(ValueError, match="不一致"):
        estimate_calls(agents, events, models)
