"""Content permissions are enforced before any OpenTelemetry exporter sees a span."""

import json
from types import SimpleNamespace

import pytest
from bbx_runtime.pi import Message, observability
from bbx_runtime.traffic import observation
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.mark.parametrize("external", [True, False])
def test_every_pi_preview_honors_existing_content_permissions(monkeypatch, external):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability, "get_tracer", lambda: provider.get_tracer("test.pi"))
    monkeypatch.setattr(observation, "_provider", None)
    monkeypatch.setattr(observation, "_collectors", {})
    monkeypatch.setattr(observation, "_sensitive_pipeline", False)
    records = []
    collector = SimpleNamespace(task_id="offline", run=1, error=None, enqueue=records.append)
    if external:
        monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
        observation.register(collector)
        assert observation._sensitive_pipeline is False
        assert "metadata" in collector.error
    else:
        monkeypatch.setattr(observation, "_sensitive_pipeline", True)
    try:
        with observation.observation_context("offline", 1, "agent-1", secrets=("test-private",)):
            with observability.observe_tool(
                "inspect", {"input": "task material test-private"}
            ) as span:
                observability.set_preview(span, "gen_ai.tool.call.result", "private evidence")
            observability.record_native_model(
                {
                    "message": {"content": [{"type": "text", "text": "task answer test-private"}]},
                    "telemetry": [
                        {
                            "name": "pi.ai.request",
                            "started_ms": 1000,
                            "ended_ms": 1001,
                            "attributes": {
                                "provider": "offline",
                                "usage.input": 10,
                                "response.id": "response-safe",
                                "debug.payload": "private evidence",
                                "api_key": "test-private",
                            },
                        }
                    ],
                },
                model=SimpleNamespace(model="offline"),
                messages=[Message("user", ["task question test-private"])],
            )
        tool, model = exporter.get_finished_spans()
        tool_attributes, model_attributes = (
            dict(tool.attributes or {}),
            dict(model.attributes or {}),
        )
        assert model_attributes["pi.usage.input"] == 10
        assert model_attributes["pi.response.id"] == "response-safe"
        assert "pi.debug.payload" not in model_attributes
        assert "pi.api_key" not in model_attributes
        keys = (
            (tool_attributes, "gen_ai.tool.call.arguments"),
            (tool_attributes, "gen_ai.tool.call.result"),
            (model_attributes, "gen_ai.input.messages"),
            (model_attributes, "gen_ai.output.messages"),
        )
        for recorded, key in keys:
            assert (key in recorded) is not external
            assert (key + ".truncated" in recorded) is not external
        serialized = json.dumps([tool_attributes, model_attributes])
        assert "test-private" not in serialized
        if external:
            assert "private evidence" not in serialized and "task question" not in serialized
            assert len(records) == 2
        else:
            assert "task question" in serialized and "[REDACTED]" in serialized
    finally:
        observation.unregister(collector)
        provider.shutdown()
