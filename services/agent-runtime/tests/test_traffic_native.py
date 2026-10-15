"""Native inputs stay continuous, private, bounded and durable before source ACK."""

import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from bbx_contracts.traffic import TrafficNativeSourceIndex
from bbx_runtime.traffic import native as native_module
from bbx_runtime.traffic.collector import TrafficCollector
from test_traffic import Envd, Objects, collector


class NativeObjects(Objects):
    async def list(self, prefix: str) -> list[str]:
        return [key for key in self.data if key.startswith(prefix)]

    async def remove(self, key: str) -> None:
        self.data.pop(key, None)


class NativeEnvd(Envd):
    def __init__(self, parts: int = 1) -> None:
        super().__init__()
        self.chunk = self.file = False
        self.task_id = ""
        self.run = 1
        self.complete = False
        self.refuse_complete = False
        self.native_acks: list[list[str]] = []
        self.ack_fails = False
        self.downloads: list[str] = []
        self.payloads = {"key-1": b"CLIENT_TRAFFIC_SECRET_0 synthetic private-material\n"}
        self.parts: list[dict[str, Any]] = []
        self.keys = [
            {
                "id": "key-1",
                "sequence": 1,
                "offset": 0,
                "size_bytes": len(self.payloads["key-1"]),
                "sha256": hashlib.sha256(self.payloads["key-1"]).hexdigest(),
            }
        ]
        self.pending = {"key-1"}
        for number in range(1, parts + 1):
            self.append(number)

    def append(self, number: int, segment: str = "segment-1") -> None:
        identifier = f"part-{number}"
        self.payloads[identifier] = bytes([number % 256]) * 4
        previous = [part for part in self.parts if part["segment_id"] == segment]
        self.parts.append(
            {
                "id": identifier,
                "segment_id": segment,
                "sequence": number,
                "offset": sum(part["size_bytes"] for part in previous),
                "size_bytes": 4,
                "sha256": hashlib.sha256(self.payloads[identifier]).hexdigest(),
                "first_packet": number,
                "last_packet": number,
            }
        )
        self.pending.add(identifier)

    async def traffic(self, operation: str, **payload: Any) -> dict[str, Any]:
        if operation == "start":
            self.task_id, self.run = payload["task_id"], payload["run_number"]
        if operation == "stop" and not self.refuse_complete:
            self.complete = True
        result = await super().traffic(operation, **payload)
        result["native_enabled"] = True
        return result

    def index(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "task_id": self.task_id,
            "run": self.run,
            "source_id": self.source + "-raw-agent-1",
            "agent_id": "member-1",
            "complete": self.complete,
            "error": None,
            "packet_count": len(self.parts),
            "last_packet_at": "2026-10-15T00:00:00Z",
            "parts": self.parts,
            "keylog_parts": self.keys,
        }

    async def native_traffic(self) -> dict[str, Any]:
        pending = [
            {
                "id": part["id"],
                "kind": "pcapng",
                "source_id": self.index()["source_id"],
                "descriptor": part,
            }
            for part in self.parts
            if part["id"] in self.pending
        ]
        pending.extend(
            {"id": part["id"], "kind": "keylog", "source_id": None, "descriptor": part}
            for part in self.keys
            if part["id"] in self.pending
        )
        return copy.deepcopy(
            {"schema_version": 1, "sources": [self.index()], "pending": pending, "error": None}
        )

    @asynccontextmanager
    async def native_stream(self, identifier: str):
        self.downloads.append(identifier)
        yield httpx.Response(200, content=self.payloads[identifier])

    async def native_ack(self, identifiers: list[str]) -> None:
        if self.ack_fails:
            self.ack_fails = False
            raise OSError("Native ACK response lost")
        self.native_acks.append(identifiers)
        self.pending.difference_update(identifiers)


@pytest.mark.asyncio
async def test_native_objects_index_manifest_and_ack_commit_order() -> None:
    envd, objects = NativeEnvd(), NativeObjects()
    value = await collector(envd, objects)
    try:
        await value.flush()
        ref = value.manifest["native"][0]
        key = value.prefix + f"native/{ref['source_id']}/indexes/{ref['revision']}.json"
        raw = objects.data[key]
        assert hashlib.sha256(raw).hexdigest() == ref["revision"]
        index = TrafficNativeSourceIndex.model_validate_json(raw)
        assert index.packet_count == ref["packet_count"] == 1 and ref["available_bytes"] == 4
        assert index.agent_id == "member-1" and not index.complete
        part_key = value.prefix + f"native/{ref['source_id']}/parts/part-1.bin"
        keylog_key = value.prefix + "native/keylog/key-1.log"
        assert objects.data[part_key] == envd.payloads["part-1"]
        assert objects.data[keylog_key] == envd.payloads["key-1"]
        assert objects.calls.index(part_key) < objects.calls.index(key)
        assert objects.calls.index(keylog_key) < objects.calls.index(key)
        assert objects.calls[objects.calls.index(key) :].index(value.prefix + "manifest.json") > 0
        assert not envd.pending and envd.native_acks
        assert "private-material" not in json.dumps(value.manifest)
        await value.close()
        assert value.manifest["native"][0]["complete"] and value.manifest["finished_at"]
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_native_commit_failure_never_acks_and_retry_reuses_uploaded_parts() -> None:
    envd, objects = NativeEnvd(), NativeObjects()
    value = await collector(envd, objects)
    try:
        objects.fail = "manifest.json"
        with pytest.raises(OSError):
            await value._native_tick()
        assert not envd.native_acks and not value.manifest.get("native")
        downloads = list(envd.downloads)
        await value._native_tick()
        assert envd.downloads == downloads
        assert not envd.pending
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_native_restart_recovers_ack_and_cross_segment_source_numbering() -> None:
    envd, objects = NativeEnvd(), NativeObjects()
    value = await collector(envd, objects)
    try:
        envd.ack_fails = True
        with pytest.raises(OSError):
            await value._native_tick()
        # Cancel pools without another upload; emulate restart after an uncertain ACK response.
        await value._cancel_workers()
        restored = TrafficCollector(envd, objects, value.task_id, 1, poll_seconds=60)  # type: ignore
        await restored.start()
        try:
            downloads = list(envd.downloads)
            await restored._native_tick()
            assert envd.downloads == downloads and not envd.pending
            envd.append(2, "segment-2")
            await restored._native_tick()
            ref = restored.manifest["native"][0]
            raw = objects.data[
                restored.prefix + f"native/{ref['source_id']}/indexes/{ref['revision']}.json"
            ]
            index = TrafficNativeSourceIndex.model_validate_json(raw)
            assert index.parts[-1].offset == 0 and index.parts[-1].first_packet == 2
            assert index.packet_count == 2 and restored.manifest["last_seq"] == 0
        finally:
            await restored.close(discard=True)
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_native_gap_or_changed_committed_bytes_is_rejected() -> None:
    envd, objects = NativeEnvd(), NativeObjects()
    value = await collector(envd, objects)
    try:
        await value._native_tick()
        ref = copy.deepcopy(value.manifest["native"])
        envd.parts[0]["sha256"] = "0" * 64
        with pytest.raises(ValueError, match="regressed"):
            await value._native_tick()
        assert value.manifest["native"] == ref
        envd.parts[0]["sha256"] = hashlib.sha256(envd.payloads["part-1"]).hexdigest()
        envd.append(2)
        envd.parts[-1]["offset"] += 4
        with pytest.raises(ValueError, match="gap"):
            await value._native_tick()
        assert "part-2" in envd.pending
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_slow_native_transfer_and_unconfirmed_finish_keep_environment() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class Slow(NativeObjects):
        async def put(self, uri: str, data: Any, **kwargs: Any) -> None:
            if "/native/" in uri and "/parts/" in uri:
                started.set()
                await release.wait()
            await super().put(uri, data, **kwargs)

    envd, objects = NativeEnvd(), Slow()
    value = await collector(envd, objects)
    native = asyncio.create_task(value._native_tick())
    try:
        async with asyncio.timeout(1):
            await started.wait()
            envd.chunk = True
            await value._metadata_tick()
        assert value.manifest["record_count"] == 1 and not native.done()
        release.set()
        await native
        assert value.manifest["record_count"] == 1
        envd.refuse_complete = True
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await value.close()
        assert value.manifest["finished_at"] is None and "close" not in envd.calls
    finally:
        release.set()
        await asyncio.gather(native, return_exceptions=True)
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_native_backlog_publishes_only_continuous_bounded_prefixes() -> None:
    envd, objects = NativeEnvd(20), NativeObjects()
    value = await collector(envd, objects)
    try:
        first = await value._native_tick()
        assert first is not None and first.pending > 0 and first.progress
        assert 0 < value.manifest["native"][0]["packet_count"] < 20
        await value.close()
        assert value.manifest["native"][0]["packet_count"] == 20
        assert value.manifest["native"][0]["complete"] and not envd.pending
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_restart_discovers_orphan_indices_and_retains_them_for_pin_window(
    monkeypatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(native_module, "time", lambda: clock[0])
    envd, objects = NativeEnvd(), NativeObjects()
    value = await collector(envd, objects)
    restored = None
    try:
        await value._native_tick()
        orphan = value.prefix + "native/old-source/indexes/" + "0" * 64 + ".json"
        outside = orphan.replace("run-1/", "run-2/")
        objects.data[orphan] = objects.data[outside] = b"old index"
        await value._cancel_workers()
        restored = TrafficCollector(envd, objects, value.task_id, 1, poll_seconds=60)  # type: ignore
        await restored.start()
        archive = restored.native_archive
        assert archive is not None and orphan in archive.history
        assert orphan in objects.data
        clock[0] += 181
        await archive.collect_indices(force=True)
        assert orphan not in objects.data and outside in objects.data
        ref = restored.manifest["native"][0]
        assert archive._index_key(ref["source_id"], ref["revision"]) in objects.data
        assert any("/parts/" in key for key in objects.data)
        assert value.prefix + "native/keylog/key-1.log" in objects.data
    finally:
        if restored is not None:
            await restored.close(discard=True)
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_index_gc_preserves_current_recent_and_all_inputs(
    monkeypatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(native_module, "time", lambda: clock[0])
    envd, objects = NativeEnvd(), NativeObjects()
    value = await collector(envd, objects)
    try:
        await value._native_tick()
        previous = value.manifest["native"][0]
        old_key = (
            value.prefix + f"native/{previous['source_id']}/indexes/{previous['revision']}.json"
        )
        clock[0] += 1
        envd.append(2)
        await value._native_tick()
        assert old_key in objects.data
        clock[0] += 181
        archive = value.native_archive
        assert archive is not None
        await archive.collect_indices(force=True)
        assert old_key not in objects.data
        ref = value.manifest["native"][0]
        assert (
            value.prefix + f"native/{ref['source_id']}/indexes/{ref['revision']}.json"
            in objects.data
        )
        assert any("/parts/" in key for key in objects.data)
        assert value.prefix + "native/keylog/key-1.log" in objects.data
        before = copy.deepcopy(value.manifest)
        objects.remove = lambda _key: (_ for _ in ()).throw(OSError("cleanup offline"))  # type: ignore
        archive.history[old_key] = 1000
        await archive.collect_indices(force=True)
        assert archive.gc_error == "OSError" and value.manifest == before
    finally:
        await value.close(discard=True)
