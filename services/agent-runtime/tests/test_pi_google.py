"""Real Pi/Google SDK errors over synthetic fetch and synthetic auth only."""

import asyncio
import json
from pathlib import Path

import pytest
from bbx_runtime.model_errors import ModelStreamError, model_error_metadata
from bbx_runtime.models import load_runtime_profile, model_run_options
from bbx_runtime.pi import Message
from bbx_runtime.testing.pi_client import make_test_client as make_client
from test_ctf_provider_recovery import RecoveryService
from test_ctf_runner_budget_retry import MEMBER, TURN, runner


@pytest.fixture
def offline_google_worker(monkeypatch, tmp_path):
    source = Path(__file__).resolve().parents[1] / "pi/worker.mjs"
    worker = tmp_path / "google-worker.mjs"
    worker.write_text(
        r"""
import { createRequire } from "node:module";
const require = createRequire(WORKER);
const googleRequire = createRequire(require.resolve("@google/genai"));
const { GoogleAuth } = googleRequire("google-auth-library");
GoogleAuth.prototype.getRequestHeaders = async () =>
  new Headers({authorization: "Bearer test-only"});
globalThis.fetch = async (url, init) => {
  const request = new Request(url, init);
  const body = await request.json();
  const status = Number(`${request.url} ${body.model}`.match(/http-(\d{3})/)[1]);
  await new Promise((resolve) => setTimeout(resolve, status === 429 ? 25 : 1));
  const headers = {"x-request-id": `req-${status}`, "retry-after": "1"};
  if (status !== 200) {
    return new Response(JSON.stringify({error: {
      code: status, status: "SYNTHETIC_FAILURE", message: "private provider body test-only"
    }}), {status, headers: {...headers, "content-type": "application/json"}});
  }
  return new Response(`data: ${JSON.stringify({
    candidates: [{content: {role: "model", parts: [{text: "done"}]}, finishReason: "STOP"}],
    usageMetadata: {promptTokenCount: 10, candidatesTokenCount: 2, totalTokenCount: 12}
  })}\n\n`, {headers: {...headers, "content-type": "text/event-stream"}});
};
await import(WORKER);
""".replace("WORKER", json.dumps(source.as_uri()))
    )
    monkeypatch.setenv("BBX_PI_WORKER", str(worker))


def google_client(provider, status):
    model = load_runtime_profile(
        Path(__file__).resolve().parents[3] / "profiles/default"
    ).models.explore.model_copy(
        update={
            "provider": provider,
            "model": f"gemini-http-{status}",
            "base_url": "https://offline.invalid",
            "reasoning_effort": "off",
            "provider_options": {"project": "offline", "location": "us-central1"},
        }
    )
    model = model.model_copy(
        update={"price": model.price.model_copy(update={"billing_mode": "fixed"})}
    )
    return make_client(
        model,
        credentials={"api_key": "test-only", "service_account_json": "{}"},
        explore_max_steps=1,
        conclude_grace_calls=0,
        max_duration_seconds=10,
        max_retries=0,
    )


@pytest.mark.parametrize("provider", ["gemini", "gemini_vertex"])
@pytest.mark.parametrize(
    "status,category,transient",
    [(429, "rate_limit", True), (503, "server_error", True), (401, "authentication", False)],
)
async def test_google_http_status_survives_native_sdk_boundary(
    offline_google_worker, provider, status, category, transient
):
    client = google_client(provider, status)
    try:
        with pytest.raises(ModelStreamError) as caught:
            await client.get_response([Message("user", ["offline"])])
        metadata = model_error_metadata(caught.value)
        assert metadata["http_status"] == status
        assert metadata["category"] == category
        assert metadata["transient"] is transient
        assert metadata["request_id"] == f"req-{status}"
        assert metadata["retry_after"] == "1"
        assert "private provider body" not in json.dumps(metadata)
    finally:
        await client.close()


@pytest.mark.parametrize("provider", ["gemini", "gemini_vertex"])
async def test_google_header_observer_preserves_stream_body_and_usage(
    offline_google_worker, provider
):
    client = google_client(provider, 200)
    try:
        stream = client.get_response(
            [Message("user", ["offline"])], options=model_run_options(client.model), stream=True
        )
        text = "".join([update.text async for update in stream])
        response = await stream.get_final_response()
        assert text == response.text == "done"
        assert response.usage_details["input_token_count"] == 10
        assert response.usage_details["output_token_count"] == 2
    finally:
        await client.close()


async def test_concurrent_google_calls_keep_their_own_response_headers(offline_google_worker):
    first = google_client("gemini", 429)
    second = google_client("gemini_vertex", 503)
    second.bridge = first.bridge
    try:
        responses = await asyncio.gather(
            first.get_response([Message("user", ["offline"])]),
            second.get_response([Message("user", ["offline"])]),
            return_exceptions=True,
        )
        metadata = [model_error_metadata(error) for error in responses]
        assert [item["http_status"] for item in metadata] == [429, 503]
        assert [item["request_id"] for item in metadata] == ["req-429", "req-503"]
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("provider", ["gemini", "gemini_vertex"])
async def test_google_rate_limit_uses_ctf_recovery_and_retains_uncertain_budget(
    offline_google_worker, monkeypatch, provider
):
    client = google_client(provider, 429)

    class Service(RecoveryService):
        async def get_profile(self, *_args):
            document = await super().get_profile()
            document["profile"]["model"] = client.model.model_dump(mode="json")
            return document

        async def runtime(self, _, operation, **body):
            if operation == "claim_messages":
                return [
                    {
                        "id": "assignment",
                        "claim_token": "test-claim",
                        "body": "synthetic assignment",
                        "sender_kind": "agent",
                        "sender_id": "lead",
                        "kind": "instruction",
                    }
                ]
            return await super().runtime(_, operation, **body)

    service = Service()
    try:
        await runner(service, client, monkeypatch).run("task", MEMBER, TURN)
        assert service.finished[-1]["end_reason"] == "provider_transient"
        assert service.unknown == {"turn:1": {"turn_id": "turn", "state": "unknown", "cost": "0.6"}}
        assert not service.reservations and not service.billed
        error = next(row["body"] for row in service.observations if row["kind"] == "model_error")
        assert error["category"] == "rate_limit" and error["transient"] is True
    finally:
        await client.close()
