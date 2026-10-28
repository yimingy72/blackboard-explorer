"""Resolve snapshotted model limits consistently across requests and admission."""

from .models import ModelConfig


def effective_max_output_tokens(model: ModelConfig) -> int:
    """Prefer the platform limit, with a bounded fallback for legacy snapshots."""
    if model.max_output_tokens is not None:
        return model.max_output_tokens
    maximum = (
        393216
        if model.model
        in {
            "deepseek-flash",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "deepseek-v4-pro",
        }
        else 8192
    )
    if model.context_window is not None:
        # Legacy snapshots have no output setting; preserve room for their input.
        maximum = min(maximum, max(1, model.context_window // 2))
    return maximum


def effective_max_input_tokens(model: ModelConfig) -> int | None:
    """Apply the input setting and reserve the same output bound sent to the model."""
    maximum = model.max_input_tokens
    if model.context_window is not None:
        available = max(0, model.context_window - effective_max_output_tokens(model))
        maximum = available if maximum is None else min(maximum, available)
    return maximum
