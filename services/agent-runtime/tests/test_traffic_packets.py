"""Packet bundles publish independently of raw uploads with bounded batch work."""

import asyncio
import gzip
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from bbx_contracts.traffic import TrafficRecordDetail
from bbx_runtime.traffic.collector import BATCH_BYTES, TrafficCollector, encode, prepare_records
from test_traffic import Envd, Objects, collector


def packet(number: int) -> dict[str, Any]:
    return {
        "id": f"r-{number}",
        "seq": number,
        "timestamp": "2026-10-15T00:00:00Z",
        "record_kind": "packet",
        "agent_id": "member-1",
        "attribution": "exact",
        "protocol": "udp",
        "transport_protocol": "udp",
        "plaintext": "not_applicable",
        "packet": {
            "source_id": "namespace-source",
            "number": number + 100,
            "native_frame_number": number,
            "frame_length": 42,
            "captured_length": 42,
            "direction": "loopback",
            "interface": "lo",
            "info": "UDP packet",
        },
        "packet_detail": {
            "raw_hex": "01234567",
            "layers": [
                {
                    "name": "udp",
                    "label": "User Datagram Protocol",
                    "children": [
                        {"name": "udp.srcport", "label": "Source port", "value": "12345"},
                    ],
                },
            ],
        },
    }


class PacketEnvd(Envd):
    def __init__(self, batches: int = 1) -> None:
        super().__init__()
        self.file = False
        self.batches = {f"c-{number}": [packet(number)] for number in range(1, batches + 1)}
        self.acks: list[list[str]] = []
        self.preview_dropped = 0

    async def traffic(self, operation: str, **payload: Any) -> dict[str, Any]:
        if operation == "chunks":
            return {
                "items": [
                    {
                        "id": key,
                        "first_seq": value[0]["seq"],
                        "count": len(value),
                        "size_bytes": len(self.content(key)),
                    }
                    for key, value in self.batches.items()
                ]
            }
        if operation == "ack" and payload.get("chunks"):
            self.calls.append(operation)
            if self.fail_ack:
                self.fail_ack = False
                raise OSError("ACK response lost")
            self.acks.append(payload["chunks"])
            for key in payload["chunks"]:
                self.batches.pop(key, None)
            return {}
        value = await super().traffic(operation, **payload)
        value.update(preview_dropped=self.preview_dropped, last_packet_at="2026-10-15T00:00:00Z")
        return value

    def content(self, identifier: str) -> bytes:
        return b"".join((json.dumps(row) + "\n").encode() for row in self.batches[identifier])

    @asynccontextmanager
    async def traffic_stream(self, kind: str, identifier: str):
        if kind == "chunks":
            yield httpx.Response(200, content=self.content(identifier))
        else:
            async with super().traffic_stream(kind, identifier) as response:
                yield response


@pytest.mark.asyncio
async def test_bundle_omits_schema_defaults_without_changing_native_tree() -> None:
    envd, objects = PacketEnvd(), Objects()
    records = []
    for number in range(1, 31):
        record = packet(number)
        record["packet_detail"]["layers"] = [{"name": "udp", "label": "UDP"} for _ in range(300)]
        records.append(record)
    envd.batches["c-1"] = records
    expected = prepare_records(records, 0)
    assert len(encode(expected)) > BATCH_BYTES
    assert len(envd.content("c-1")) < BATCH_BYTES
    value = await collector(envd, objects)
    try:
        await value.flush()
        descriptor = value.manifest["chunks"][0]
        payload = gzip.decompress(
            objects.data[value.prefix + f"detail-chunks/{descriptor['id']}.json.gz"]
        )
        assert len(payload) <= BATCH_BYTES and not envd.batches
        assert [
            TrafficRecordDetail.model_validate(row).model_dump(mode="json")
            for row in json.loads(payload)
        ] == expected
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_packet_and_http_mixed_batch_uses_one_detail_bundle_before_manifest_ack() -> None:
    envd, objects = PacketEnvd(), Objects()
    envd.batches["c-1"].append(envd.detail)
    value = await collector(envd, objects)
    try:
        await value.flush()
        descriptor = value.manifest["chunks"][0]
        assert descriptor["detail_storage"] == "chunk" and descriptor["count"] == 2
        assert not any("/records/" in key for key in objects.data)
        key = value.prefix + f"detail-chunks/{descriptor['id']}.json.gz"
        rows = json.loads(gzip.decompress(objects.data[key]))
        assert len(rows) == 2 and rows[0]["packet"]["number"] == 101
        assert rows[0]["packet"]["native_frame_number"] == 1
        assert rows[0]["packet_detail"]["raw_hex"] == "01234567"
        assert all(TrafficRecordDetail.model_validate(row) for row in rows)
        writes = objects.calls[objects.calls.index(key) :]
        assert writes.index(value.prefix + "manifest.json") > 0
        assert envd.acks == [["c-1"]]
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_burst_tick_has_eight_batch_budget_and_single_manifest_commit() -> None:
    envd, objects = PacketEnvd(20), Objects()
    value = await collector(envd, objects)
    try:
        begin = len(objects.calls)
        await value.flush()
        assert value.manifest["record_count"] == 8
        assert value.manifest["pending_chunks"] == 12 and len(envd.batches) == 12
        assert objects.calls[begin:].count(value.prefix + "manifest.json") == 1
        await value.flush()
        await value.flush()
        assert value.manifest["record_count"] == value.manifest["last_seq"] == 20
        assert value.manifest["pending_chunks"] == 0
        assert [item["first_seq"] for item in value.manifest["chunks"]] == list(range(1, 21))
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_slow_binary_upload_does_not_hold_metadata_manifest_lock() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class SlowObjects(Objects):
        async def put(self, uri: str, data: Any, **kwargs: Any) -> None:
            if "/pcap/" in uri:
                started.set()
                await release.wait()
            await super().put(uri, data, **kwargs)

    envd, objects = PacketEnvd(), SlowObjects()
    envd.file = True
    value = await collector(envd, objects)
    binary = asyncio.create_task(value._artifacts_tick())
    try:
        async with asyncio.timeout(1):
            await started.wait()
            await value._metadata_tick()
        assert value.manifest["record_count"] == 1
        assert value.manifest["files"] == [] and not binary.done()
        assert envd.acks == [["c-1"]]
    finally:
        release.set()
        await binary
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_packet_bundles_recover_ack_loss_without_reassigning_source_numbers() -> None:
    envd, objects = PacketEnvd(2), Objects()
    value = await collector(envd, objects)
    envd.fail_ack = True
    try:
        with pytest.raises(OSError):
            await value.flush()
        assert value.manifest["record_count"] == 2 and len(envd.batches) == 2
        await value.suspend()
        # Simulate replay of a source chunk after the durable global sequence was committed.
        envd.batches["c-1"] = [packet(1)]
        restored = TrafficCollector(envd, objects, value.task_id, 1, poll_seconds=60)  # type: ignore
        await restored.start()
        try:
            await restored.flush()
            assert restored.manifest["record_count"] == restored.manifest["last_seq"] == 2
            next_packet = packet(3)
            next_packet["packet"]["native_frame_number"] = 1
            envd.batches["new"] = [next_packet]
            await restored.flush()
            assert restored.manifest["last_seq"] == 3
            latest = restored.manifest["chunks"][-1]
            rows = json.loads(
                gzip.decompress(
                    objects.data[restored.prefix + f"detail-chunks/{latest['id']}.json.gz"]
                )
            )
            assert rows[0]["packet"]["number"] == 103 and rows[0]["seq"] == 3
            assert rows[0]["packet"]["native_frame_number"] == 1
        finally:
            await restored.close(discard=True)
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_packet_batch_bytes_and_record_count_are_rejected_before_ack() -> None:
    for oversized in ([packet(1)] * 129, [dict(packet(1), summary="x" * BATCH_BYTES)]):
        envd, objects = PacketEnvd(), Objects()
        envd.batches["c-1"] = oversized
        value = await collector(envd, objects)
        try:
            with pytest.raises(ValueError, match="exceeds"):
                await value.flush()
            assert value.manifest["record_count"] == 0 and not envd.acks
        finally:
            await value.close(discard=True)


@pytest.mark.asyncio
async def test_preview_loss_and_capture_timestamp_are_published_separately_from_raw_loss() -> None:
    envd, objects = PacketEnvd(), Objects()
    envd.preview_dropped = 3
    value = await collector(envd, objects)
    try:
        await value.close()
        assert value.manifest["preview_dropped"] == 3
        assert value.manifest["dropped_packets"] == 0
        assert value.manifest["last_packet_at"] == "2026-10-15T00:00:00Z"
        assert value.manifest["status"] == "partial"
    finally:
        await value.close(discard=True)


@pytest.mark.asyncio
async def test_runtime_preview_loss_count_survives_checkpoint_restart() -> None:
    envd, objects = PacketEnvd(), Objects()
    envd.preview_dropped = 3
    value = await collector(envd, objects)
    value.dropped_observations = 2
    try:
        await value.suspend()
        restored = TrafficCollector(envd, objects, value.task_id, 1, poll_seconds=60)  # type: ignore
        await restored.start()
        try:
            assert restored.manifest["preview_dropped"] == 5
            assert restored.dropped_observations == 2
        finally:
            await restored.close(discard=True)
    finally:
        await value.close(discard=True)
