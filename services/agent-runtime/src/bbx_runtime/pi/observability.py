"""Connect Pi-hosted model and tool boundaries to existing task traffic spans."""

import json
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from bbx_runtime.traffic import redaction


def get_tracer():
    return trace.get_tracer("bbx.pi")


def set_preview(span, key, value, *, partial=False):
    from bbx_runtime.traffic.observation import _identity

    identity = _identity.get()
    secrets = identity["secrets"] if identity else ()
    body = json.dumps(redaction.clean(value, secrets), ensure_ascii=False, default=str)
    span.set_attribute(key, redaction.bounded(body))
    span.set_attribute(key + ".truncated", partial or len(body.encode()) > redaction.LIMIT)


def record_native_model(result, *, model, messages) -> None:
    """Export Pi-owned request metadata through the existing sanitizing processor."""
    for native in result.get("telemetry", []):
        span = get_tracer().start_span(
            native["name"],
            start_time=int(native["started_ms"] * 1_000_000),
            attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": model.model},
        )
        try:
            for key, value in native.get("attributes", {}).items():
                if isinstance(value, (str, int, float, bool)):
                    span.set_attribute("pi." + key, value)
            previews = []
            for message in messages[-16:]:
                body = message.to_dict()
                for content in body["contents"]:
                    if content.get("type") in {"data", "uri"} and str(
                        content.get("uri", "")
                    ).startswith("data:"):
                        content["uri"] = "[binary image omitted from logical preview]"
                previews.append(body)
            set_preview(span, "gen_ai.input.messages", previews, partial=len(messages) > 16)
            set_preview(
                span, "gen_ai.output.messages", result.get("message", {}).get("content", [])
            )
            if native.get("error"):
                span.set_status(Status(StatusCode.ERROR, "Pi model response failed"))
        finally:
            span.end(end_time=int(native["ended_ms"] * 1_000_000))


@contextmanager
def observe_tool(name, arguments):
    with get_tracer().start_as_current_span(
        "Pi tool " + name, record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", name)
        set_preview(span, "gen_ai.tool.call.arguments", arguments)
        try:
            yield span
        except BaseException:
            span.set_status(Status(StatusCode.ERROR, "Platform tool failed"))
            raise
