"""Route MAF OpenTelemetry logical spans to their task capture without network exporters."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from bbx_runtime.traffic import redaction

_identity: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "traffic_identity", default=None
)
_collectors: dict[tuple[str, int], Any] = {}
_provider: Any = None
_sensitive_pipeline = False


def runtime_secrets(settings: Any, *extra: str) -> tuple[str, ...]:
    values = list(extra)
    for name in ("service_token", "envd_token_secret", "deepseek_api_key", "minio_root_password"):
        secret = getattr(settings, name, None)
        if secret is not None and hasattr(secret, "get_secret_value"):
            values.append(secret.get_secret_value())
    return tuple(value for value in values if isinstance(value, str) and value)


@contextmanager
def observation_context(
    task_id: str,
    run: int,
    agent_id: str,
    *,
    turn_id: str | None = None,
    secrets: tuple[str, ...] = (),
) -> Iterator[None]:
    token = _identity.set(
        {
            "task_id": task_id,
            "run": run,
            "agent_id": agent_id,
            "turn_id": turn_id,
            "secrets": secrets,
        }
    )
    try:
        yield
    finally:
        _identity.reset(token)


def register(collector: Any) -> None:
    global _provider, _sensitive_pipeline
    if _provider is None:
        from agent_framework.observability import enable_instrumentation
        from opentelemetry import trace
        from opentelemetry.sdk.trace import SpanLimits, SpanProcessor, TracerProvider

        class Processor(SpanProcessor):
            def __init__(self) -> None:
                self.secrets: dict[int, tuple[str, ...]] = {}

            def on_start(self, span: Any, parent_context: Any = None) -> None:
                identity = _identity.get()
                if identity is not None:
                    self.secrets[span.context.span_id] = identity["secrets"]
                    for key in ("task_id", "run", "agent_id", "turn_id"):
                        if identity[key] is not None:
                            span.set_attribute(f"bbx.{key}", identity[key])

            def on_end(self, span: Any) -> None:
                attributes = dict(span.attributes or {})
                task, run = attributes.get("bbx.task_id"), attributes.get("bbx.run")
                secrets = self.secrets.pop(span.context.span_id, ())
                target = _collectors.get((str(task), int(run or 0)))
                if target is None:
                    return
                # Only this sanitizing processor is installed; no raw message logger/exporter.
                try:
                    target.enqueue(_record(span, attributes, secrets))
                except Exception:
                    # Telemetry failures must not replace an Agent/model/tool outcome.
                    target.error = "A logical call observation could not be recorded"

        current = trace.get_tracer_provider()
        owned = not isinstance(current, TracerProvider)
        _sensitive_pipeline = owned
        if isinstance(current, TracerProvider):
            _provider = current
        else:
            _provider = TracerProvider(span_limits=SpanLimits(max_attribute_length=32768))
            trace.set_tracer_provider(_provider)
        _provider.add_span_processor(Processor())
        enable_instrumentation(enable_sensitive_data=owned, enable_message_events=False)
    if not _sensitive_pipeline:
        collector.error = "An external OpenTelemetry provider limits logical calls to metadata"
    _collectors[(collector.task_id, collector.run)] = collector


def unregister(collector: Any) -> None:
    if _collectors.get((collector.task_id, collector.run)) is collector:
        _collectors.pop((collector.task_id, collector.run), None)


def _record(span: Any, attributes: dict[str, Any], secrets: tuple[str, ...]) -> dict[str, Any]:
    operation = str(attributes.get("gen_ai.operation.name", ""))
    protocol = (
        "mcp" if "mcp.method.name" in attributes else "tool" if "tool" in operation else "model"
    )
    safe: dict[str, str] = {}
    remaining = 16000
    for key, value in attributes.items():
        if key in {"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions"}:
            continue
        item = (
            "[REDACTED]"
            if redaction.SENSITIVE.search(key)
            else str(redaction.clean(value, secrets))
        )
        item = redaction.bounded(item, min(1024, max(0, remaining - len(key.encode()))))
        remaining -= len(key.encode()) + len(item.encode())
        if remaining < 0 or len(safe) >= 120:
            break
        safe[key] = item
    timestamp = datetime.fromtimestamp(span.start_time / 1e9, UTC).isoformat()
    error = None
    if span.status.status_code.name == "ERROR":
        error = redaction.text(
            str(span.status.description or attributes.get("error.type", "error")), secrets
        )
    agent = attributes.get("bbx.agent_id")

    def message(key: str) -> dict[str, Any] | None:
        value = attributes.get(key)
        if value is None:
            return None
        original = str(value)
        body = str(redaction.clean(original, secrets))
        return {
            "headers": [],
            "body_preview": redaction.bounded(body),
            "body_bytes": len(original.encode()),
            "body_encoding": "utf-8",
            "body_truncated": len(original.encode()) > redaction.LIMIT,
        }

    return {
        "timestamp": timestamp,
        "agent_id": agent,
        "attribution": "exact" if agent else "unknown",
        "protocol": protocol,
        "record_kind": "logical",
        "source_ip": "",
        "destination_ip": "",
        "method": operation[:64] or None,
        "summary": redaction.text(span.name, secrets),
        "plaintext": "not_applicable",
        "request_bytes": 0,
        "response_bytes": 0,
        "request": message("gen_ai.input.messages") or message("gen_ai.tool.call.arguments"),
        "response": message("gen_ai.output.messages") or message("gen_ai.tool.call.result"),
        "observation": {
            "scope": "logical_call",
            "trace_id": f"{span.context.trace_id:032x}",
            "span_id": f"{span.context.span_id:016x}",
            "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
            "attributes": safe,
            "status": span.status.status_code.name.lower(),
            "error": error,
            "retry_visibility": "unknown",
        },
    }
