"""Offline capture persistence, recovery and MAF context isolation."""

import asyncio
import gzip
import io
import json
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
from agent_framework.observability import get_tracer
from bbx_contracts.traffic import TrafficManifest, TrafficRecordDetail
from bbx_runtime.traffic.collector import TrafficCollector
from bbx_runtime.traffic.observation import observation_context


class Objects:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.fail: str | None = None

    async def exists(self, uri: str) -> bool:
        return uri in self.data

    async def get(self, uri: str) -> bytes:
        return self.data[uri]

    async def put(self, uri: str, data: bytes | io.BufferedIOBase, **_kwargs: Any) -> None:
        self.calls.append(uri)
        if self.fail and uri.endswith(self.fail):
            self.fail = None
            raise OSError("MinIO offline")
        self.data[uri] = data if isinstance(data, bytes) else data.read()


class Envd:
    def __init__(self) -> None:
        self.source = "source1"
        self.calls: list[str] = []
        self.detail = {
            "id": "r-1",
            "seq": 1,
            "timestamp": "2026-10-15T00:00:00+00:00",
            "agent_id": "member-1",
            "attribution": "exact",
            "protocol": "https",
            "url": "https://user:password@example.test/api?api_key=private",
            "summary": "GET /api",
            "plaintext": "decoded",
            "request": {
                "headers": [["Authorization", "Bearer private"]],
                "body_preview": '{"password":"private"}',
            },
        }
        self.chunk = True
        self.file = True
        self.fail_ack = False

    async def traffic(self, operation: str, **_payload: Any) -> dict[str, Any]:
        self.calls.append(operation)
        status = {
            "source_id": self.source,
            "https": "enabled",
            "captured_bytes": 4,
            "dropped_packets": 0,
            "status": "collecting",
            "error": None,
        }
        if operation == "limit":
            status.update(capturing=False, proxy_serving=True)
        if operation == "chunks":
            return {"items": [{"id": "c1", "size_bytes": 500}] if self.chunk else []}
        if operation == "artifacts":
            return {
                "items": [{"id": "f1", "size_bytes": 4, "name": "capture.pcap"}]
                if self.file
                else []
            }
        if operation == "ack":
            if self.fail_ack:
                self.fail_ack = False
                raise OSError("response lost")
            if _payload.get("chunks"):
                self.chunk = False
            if _payload.get("artifacts"):
                self.file = False
        return status

    @asynccontextmanager
    async def traffic_stream(self, kind: str, _identifier: str):
        yield httpx.Response(
            200, content=(json.dumps(self.detail) + "\n").encode() if kind == "chunks" else b"pcap"
        )

    async def close(self) -> None:
        self.calls.append("close")


async def collector(envd: Envd, objects: Objects, **kwargs: Any) -> TrafficCollector:
    value = TrafficCollector(envd, objects, str(uuid4()), 1, poll_seconds=60, **kwargs)  # type: ignore
    await value.start()
    return value


@pytest.mark.asyncio
async def test_capture_uploads_before_manifest_ack_and_preserves_canonical_identity() -> None:
    envd, objects = Envd(), Objects()
    value = await collector(envd, objects)
    try:
        assert value.manifest["status"] == "collecting"
        await value.close()
        manifest = TrafficManifest.model_validate_json(objects.data[value.prefix + "manifest.json"])
        assert manifest.status == "complete" and manifest.record_count == 1
        assert manifest.last_seq == 1 and manifest.files[0].size_bytes == 4
        assert manifest.chunks[0].agent_ids == ["member-1"]
        detail = TrafficRecordDetail.model_validate_json(
            gzip.decompress(objects.data[value.prefix + "records/r-1.json.gz"])
        )
        assert detail.agent_id == "member-1"
        assert "private" not in detail.model_dump_json()
        assert detail.url is not None and "user:password" not in detail.url
        assert objects.calls.index(value.prefix + "records/r-1.json.gz") < objects.calls.index(
            value.prefix + f"chunks/{manifest.chunks[0].id}.json.gz"
        )
        assert envd.calls.index("stop") < envd.calls.index("ack")
    finally:
        await value.close(discard=True)


async def test_preview_loss_uses_its_counter_without_inventing_a_capture_error():
    value = await collector(Envd(), Objects())
    try:
        value._status({"status": "partial", "preview_dropped": 5, "https": "enabled"})
        assert value.manifest["status"] == "partial"
        assert value.manifest["preview_dropped"] == 5
        assert value.manifest["error"] is None
        value._status({"status": "partial", "error": "Raw source stopped unexpectedly"})
        assert value.manifest["error"] == "Raw source stopped unexpectedly"
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_failed_manifest_retains_source_and_retry_does_not_duplicate() -> None:
    envd, objects = Envd(), Objects()
    value = await collector(envd, objects)
    try:
        objects.fail = "manifest.json"
        with pytest.raises(OSError):
            await value.flush()
        assert envd.chunk and "ack" not in envd.calls
        assert value.manifest["record_count"] == 0
        await value.flush()
        assert value.manifest["record_count"] == 1
        assert not envd.chunk and not envd.file
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_restart_deduplicates_an_ack_with_lost_response_and_new_source_keeps_sequence() -> (
    None
):
    envd, objects = Envd(), Objects()
    value = await collector(envd, objects)
    try:
        envd.fail_ack = True
        with pytest.raises(OSError):
            await value.flush()
        await value.suspend()
        # Simulate another response loss: the sealed source chunk remains listed.
        envd.chunk = True
        restored = TrafficCollector(envd, objects, value.task_id, 1, poll_seconds=60)  # type: ignore
        await restored.start()
        try:
            await restored.flush()
            assert restored.manifest["record_count"] == 1
            await restored.close()
            envd.source, envd.chunk, envd.file = "source2", True, True
            replacement = TrafficCollector(envd, objects, value.task_id, 1, poll_seconds=60)  # type: ignore
            await replacement.start()
            try:
                await replacement.flush()
                assert replacement.manifest["record_count"] == 2
                assert replacement.manifest["last_seq"] == 2
                assert len(replacement.manifest["files"]) == 2
            finally:
                await replacement.close(discard=True)
        finally:
            await restored.close(discard=True)
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_initial_manifest_failure_cannot_start_worker() -> None:
    envd, objects = Envd(), Objects()
    value = TrafficCollector(envd, objects, str(uuid4()), 1)  # type: ignore
    objects.fail = "manifest.json"
    with pytest.raises(OSError):
        await value.start()
    assert value.worker is None
    assert not any("/records/" in uri for uri in objects.calls)


@pytest.mark.asyncio
async def test_maf_spans_keep_concurrent_agent_identity_and_redact_body() -> None:
    envd, objects = Envd(), Objects()
    envd.chunk = envd.file = False
    value = await collector(envd, objects)
    try:

        async def call(agent: str) -> None:
            with observation_context(value.task_id, 1, agent, secrets=("private-key",)):
                with get_tracer().start_as_current_span("chat model") as span:
                    span.set_attribute("gen_ai.operation.name", "chat")
                    span.set_attribute("gen_ai.input.messages", '[{"content":"private-key"}]')
                    await asyncio.sleep(0)
                    span.set_attribute("gen_ai.output.messages", '[{"content":"response"}]')

        await asyncio.gather(call("agent-1"), call("agent-2"))
        await value.flush()
        records = [
            TrafficRecordDetail.model_validate_json(gzip.decompress(data))
            for uri, data in objects.data.items()
            if "/records/" in uri
        ]
        assert {item.agent_id for item in records} == {"agent-1", "agent-2"}
        assert all(
            item.observation is not None and item.observation.scope == "logical_call"
            for item in records
        )
        assert all(
            item.observation is not None and item.observation.retry_visibility == "unknown"
            for item in records
        )
        assert all("private-key" not in item.model_dump_json() for item in records)
        assert all(
            item.response is not None and "response" in item.response.body_preview
            for item in records
        )
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_bounded_observation_queue_reports_partial() -> None:
    envd, objects = Envd(), Objects()
    value = await collector(envd, objects)
    try:
        for _ in range(300):
            value.enqueue(dict(envd.detail))
        assert value.queue.qsize() == 256 and value.dropped_observations == 44
        await value.close()
        assert value.manifest["status"] == "partial"
        assert "capacity" in value.manifest["error"]
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_actual_maf_agent_stream_is_not_waiting_for_capture_upload() -> None:
    from agent_framework import Agent, tool
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall

    @tool
    async def echo(value: str) -> str:
        """Echo an offline test value."""
        return value

    envd, objects = Envd(), Objects()
    envd.chunk = envd.file = False
    value = await collector(envd, objects)
    try:
        client = ScriptedChatClient(
            [
                ScriptStep(calls=(ScriptToolCall("echo", {"value": "hello"}),)),
                ScriptStep(text="finished"),
            ]
        )
        with observation_context(value.task_id, 1, "agent-1"):
            async with Agent(client=client, name="agent-1", tools=[echo]) as agent:
                stream = agent.run("offline", stream=True)
                output = ""
                async for update in stream:
                    output += update.text
                await stream.get_final_response()
                assert output == "finished"
        # Runtime did not do any object-store work on the agent's streaming path.
        assert not any("/records/" in uri for uri in objects.calls)
        await value.flush()
        records = [
            TrafficRecordDetail.model_validate_json(gzip.decompress(data))
            for uri, data in objects.data.items()
            if "/records/" in uri
        ]
        assert any(item.protocol == "tool" and item.response is not None for item in records)
        assert any(item.protocol == "model" and item.response is not None for item in records)
        assert all(item.agent_id == "agent-1" for item in records)
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_packet_owner_and_capture_source_are_preserved() -> None:
    envd, objects = Envd(), Objects()
    value = await collector(envd, objects)
    try:
        await value._artifact(
            {
                "id": "owned",
                "name": "agent.pcapng",
                "size_bytes": 4,
                "agent_id": "member-1",
                "source_id": "netns-source",
            }
        )
        file = TrafficManifest.model_validate(value.manifest).files[0]
        assert file.agent_id == "member-1" and file.source_id == "netns-source"
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_capture_identity_change_is_not_acknowledged() -> None:
    envd, objects = Envd(), Objects()
    value = await collector(envd, objects)
    try:
        envd.source = "foreign-source"
        with pytest.raises(ValueError, match="ownership"):
            await value.flush()
        assert "ack" not in envd.calls
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_cumulative_raw_quota_limits_capture_without_stopping_proxy() -> None:
    envd, objects = Envd(), Objects()
    value = await collector(envd, objects, max_bytes=4)
    try:
        assert "limit" in envd.calls and value.limited and "stop" not in envd.calls
        await value.flush()
        assert value.manifest["status"] == "partial"
        assert "capacity" in value.manifest["error"]
        assert not envd.chunk and not envd.file
        assert envd.calls.count("limit") == 1 and "stop" not in envd.calls
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_shared_gateway_and_raw_encrypted_copy_are_not_attribution_failures() -> None:
    envd, objects = Envd(), Objects()
    envd.detail.update(agent_id=None, attribution="shared", plaintext="encrypted", request=None)
    value = await collector(envd, objects)
    try:
        await value.close()
        assert value.manifest["status"] == "complete"
        record = TrafficRecordDetail.model_validate_json(
            gzip.decompress(objects.data[value.prefix + "records/r-1.json.gz"])
        )
        assert record.attribution == "shared" and record.plaintext == "encrypted"
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_capacity_limit_retries_are_bounded_and_never_ack_unconfirmed_source() -> None:
    envd, objects = Envd(), Objects()
    value = await collector(envd, objects, max_bytes=8)
    original = envd.traffic
    attempts = 0

    async def failing_limit(operation: str, **payload: Any) -> dict[str, Any]:
        nonlocal attempts
        if operation == "limit":
            attempts += 1
            raise OSError("limit response unavailable")
        return await original(operation, **payload)

    envd.traffic = failing_limit  # type: ignore[method-assign]
    value.max_bytes = 4
    try:
        for _ in range(4):
            with pytest.raises((OSError, RuntimeError)):
                await value.flush()
        assert attempts == 3 and "ack" not in envd.calls
        assert value.manifest["status"] == "error"
        assert "unconfirmed" in value.manifest["error"]
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_wire_observation_keeps_command_correlation_and_source_scope() -> None:
    envd, objects = Envd(), Objects()
    envd.detail["observation"] = {
        "scope": "wire",
        "attributes": {"command_id": "command-123"},
        "status": "ok",
    }
    value = await collector(envd, objects)
    try:
        await value.close()
        record = TrafficRecordDetail.model_validate_json(
            gzip.decompress(objects.data[value.prefix + "records/r-1.json.gz"])
        )
        assert record.observation is not None and record.observation.scope == "wire"
        assert record.observation.attributes["command_id"] == "command-123"
    finally:
        await value.close(discard=True)


def test_display_redaction_handles_private_keys_and_sdk_truncated_secret_suffix() -> None:
    from bbx_runtime.traffic import redaction

    assert "private-credential" not in redaction.text(
        "echo private-credential", ("private-credential",)
    )
    assert "private-cred" not in redaction.text("echo private-cred", ("private-credential",))
    assert "RSA material" not in redaction.text("-----BEGIN PRIVATE KEY-----RSA material")
    assert "key material" not in str(redaction.clean({"private_key": "key material"}))


@pytest.mark.asyncio
async def test_authorized_discard_stops_worker_even_when_envd_is_unreachable() -> None:
    envd, objects = Envd(), Objects()
    value = await collector(envd, objects)
    original = envd.traffic

    async def unreachable_stop(operation: str, **payload: Any) -> dict[str, Any]:
        if operation == "stop":
            raise OSError("envd offline")
        return await original(operation, **payload)

    envd.traffic = unreachable_stop  # type: ignore[method-assign]
    await value.close(discard=True)
    assert value.worker is None and value.discarded
    assert "close" in envd.calls
    assert "ack" not in envd.calls
