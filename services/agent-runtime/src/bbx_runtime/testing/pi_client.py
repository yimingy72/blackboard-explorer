"""Inject an offline HTTP transport through pi-ai's supported fetch extension."""

from pathlib import Path

from bbx_runtime.models import load_runtime_profile, make_client
from bbx_runtime.pi import PiChatClient


def responses_client(
    *,
    model="offline",
    async_client=None,
    api_key="test-only",
    base_url="https://model.test/v1",
    responses_replay_mode="native",
    **_kwargs,
):
    definition = load_runtime_profile(
        Path(__file__).resolve().parents[5] / "profiles/default"
    ).models.explore
    if async_client is not None:
        base_url = str(async_client.base_url)
    definition = definition.model_copy(
        update={
            "provider": "openai_responses",
            "model": model,
            "base_url": base_url,
            "provider_options": {"responses_replay_mode": responses_replay_mode},
        }
    )
    client = make_client(
        definition,
        api_key=api_key,
        explore_max_steps=40,
        conclude_grace_calls=0,
        max_duration_seconds=120,
        max_retries=0,
    )
    if async_client is not None:
        client.request_transport = async_client._client._transport
    return client


def patch_transport(monkeypatch, transport) -> None:
    constructor = PiChatClient.__init__

    def configure(self, *args, **kwargs):
        constructor(self, *args, **kwargs)
        self.request_transport = transport

    monkeypatch.setattr(PiChatClient, "__init__", configure)
