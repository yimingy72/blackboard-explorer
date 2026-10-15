"""Official EK conversion, live source numbering and bounded preview backpressure."""

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bbx_contracts.traffic import TrafficRecordDetail
from bbx_envd.packets import packet_record
from bbx_envd.traffic import TrafficCapture


def ek(number=1, raw=None):
    raw = raw if raw is not None else bytes(range(128)).hex()
    return {
        "timestamp": "1792062000000",
        "protocol": "UDP",
        "info": "12345 → 54321 Len=100",
        "layers": {
            "frame_raw": raw,
            "frame": {
                "frame_frame_number": str(number),
                "frame_frame_len": "128",
                "frame_frame_cap_len": "128",
                "frame_frame_protocols": "sll:ip:udp:data",
                "frame_frame_time_epoch": "2026-10-15T11:00:00.000000001Z",
            },
            "sll": {"sll_sll_ifindex": "1", "sll_sll_pkttype": "0"},
            "ip": {"ip_ip_src": "127.0.0.1", "ip_ip_dst": "127.0.0.1", "ip_ip_proto": "17"},
            "udp": {"udp_udp_srcport": "12345", "udp_udp_dstport": "54321"},
        },
    }


@pytest.fixture
async def capture(tmp_path, monkeypatch):
    capture = TrafficCapture(tmp_path / "traffic", 64 * 1024 * 1024, 10000)
    monkeypatch.setattr(capture, "start_raw", AsyncMock())
    monkeypatch.setattr(capture, "start_proxy", AsyncMock())
    await capture.start(str(uuid.uuid4()), 1)
    capture.packet_numbers["raw:agent-1"] = 0
    capture.raw_owners["raw:agent-1"] = "member-1"
    capture.interface_names["raw:agent-1"] = {"1": "lo"}
    yield capture
    await capture.stop()


def test_official_packet_fields_match_contract_and_do_not_guess_offsets():
    detail = packet_record(ek(), "source-1", 17, "member-1", "lo")
    detail.update(id="r-1", seq=1)
    validated = TrafficRecordDetail.model_validate(detail)
    assert validated.record_kind == "packet"
    assert validated.packet is not None
    assert validated.packet.number == 17 and validated.packet.native_frame_number == 1
    assert validated.packet.direction == "loopback"
    assert validated.packet.info == "12345 → 54321 Len=100"
    assert validated.packet_detail is not None
    assert validated.packet_detail.raw_hex == bytes(range(128)).hex()
    assert all(node.offset is None for node in validated.packet_detail.layers)


def test_tree_and_bytes_limits_are_explicit_and_only_text_is_redacted():
    document = ek(raw=(b"Authorization: Bearer synthetic-private\r\n" * 1024).hex())
    document["layers"]["http"] = {
        "http_http_request_line": ["Authorization: Bearer synthetic-private"],
        "http_http_cookie": ["synthetic-cookie"],
    }
    document["layers"]["many"] = {f"field-{index}": "x" * 1000 for index in range(1000)}
    detail = packet_record(document, "source-1", 1, "member-1", "lo")
    tree = json.dumps(detail["packet_detail"]["layers"])
    assert "synthetic-private" not in tree and "synthetic-cookie" not in tree
    assert len(detail["packet_detail"]["raw_hex"]) == 32768
    assert detail["packet_detail"]["raw_truncated"]
    assert detail["packet_detail"]["tree_truncated"]
    detail.update(id="r-1", seq=1)
    TrafficRecordDetail.model_validate(detail)
    assert len(json.dumps(detail).encode()) < 128 * 1024


async def test_live_source_number_survives_native_rotation_and_seals_before_raw(capture):
    stream = asyncio.StreamReader()
    for number in (1, 2, 1):
        stream.feed_data(b'{"index":{}}\n' + json.dumps(ek(number)).encode() + b"\n")
    stream.feed_eof()
    await capture.raw_files(SimpleNamespace(stdout=stream), "member-1", "raw:agent-1")
    await capture.packet_queue.join()
    records = list(capture.details.values())
    assert [item["packet"]["number"] for item in records] == [1, 2, 3]
    assert [item["packet"]["native_frame_number"] for item in records] == [1, 2, 1]
    assert all(item["agent_id"] == "member-1" for item in records)
    await asyncio.sleep(0.6)
    assert capture.chunks and not capture.artifacts
    assert capture.status()["last_packet_at"] is not None


async def test_full_preview_queue_is_drained_and_counted_separately(capture):
    capture.packet_worker.cancel()
    await asyncio.gather(capture.packet_worker, return_exceptions=True)
    stream = asyncio.StreamReader()
    data = json.dumps(ek()).encode() + b"\n"
    count = capture.packet_queue.maxsize + 144
    stream.feed_data(data * count)
    stream.feed_eof()
    await capture.raw_files(SimpleNamespace(stdout=stream), "member-1", "raw:agent-1")
    assert stream.at_eof()
    assert capture.packet_numbers["raw:agent-1"] == count
    assert capture.state["preview_dropped"] == 144
    assert capture.state["dropped_packets"] == 0
    while not capture.packet_queue.empty():
        capture.packet_queue.get_nowait()
        capture.packet_queue.task_done()


async def test_oversized_json_line_does_not_block_the_next_packet(capture):
    stream = asyncio.StreamReader()
    stream.feed_data(b'{"large":"' + b"x" * (2 * 1024 * 1024 + 100) + b'"}\n')
    stream.feed_data(json.dumps(ek()).encode() + b"\n")
    stream.feed_eof()
    await capture.raw_files(SimpleNamespace(stdout=stream), "member-1", "raw:agent-1")
    await capture.packet_queue.join()
    assert capture.state["preview_dropped"] == 1
    assert next(iter(capture.details.values()))["packet"]["number"] == 2


async def test_source_batches_respect_byte_limit_before_count_limit(capture):
    for number in range(13):
        detail = packet_record(ek(), "source-1", number + 1, "member-1", "lo")
        detail["packet_detail"]["raw_hex"] = "00" * 16384
        detail["packet_detail"]["layers"] = [
            {
                "name": f"field-{index}",
                "label": "field",
                "value": "x" * 10000,
                "offset": None,
                "length": None,
                "children": [],
            }
            for index in range(6)
        ]
        capture.record(detail)
    await capture.seal()
    assert len(capture.chunks) >= 2
    assert all(
        item["size_bytes"] <= 900 * 1024 and item["count"] <= 128
        for item in capture.chunks.values()
    )


async def test_dense_http_packets_measure_full_shared_wire_defaults_before_sealing(capture):
    document = ek(raw=(b"synthetic-large-HTTPS-body" * 5000).hex())
    document["protocol"] = "HTTP"
    document["layers"]["http"] = {f"http-field-{i}": "synthetic-value" for i in range(400)}
    document["layers"]["frame"].update(frame_frame_len="125000", frame_frame_cap_len="125000")
    document["layers"]["tls"] = {"tls_tls_record_content_type": "23"}
    for number in range(24):
        detail = packet_record(document, "source-1", number + 1, "member-1", "lo")
        detail["observation"] = {"scope": "wire", "attributes": {"command_id": "synthetic"}}
        capture.record(detail)
    await capture.seal()
    assert len(capture.chunks) >= 2
    normalized_count = 0
    for chunk in capture.chunks.values():
        with os.fdopen(capture.open_segment(chunk["id"], "chunks"), "rb") as source:
            content = source.read()
        assert len(content) <= 900 * 1024
        normalized = []
        for line in content.splitlines():
            produced = json.loads(line)
            full = TrafficRecordDetail.model_validate(produced).model_dump(mode="json")
            assert produced.keys() == full.keys()
            assert produced["packet"].keys() == full["packet"].keys()
            assert produced["packet_detail"].keys() == full["packet_detail"].keys()
            assert produced["observation"].keys() == full["observation"].keys()
            pending = list(
                zip(
                    produced["packet_detail"]["layers"],
                    full["packet_detail"]["layers"],
                    strict=True,
                )
            )
            while pending:
                before, after = pending.pop()
                assert before.keys() == after.keys()
                pending.extend(zip(before["children"], after["children"], strict=True))
            encoded_full = json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode()
            assert len(encoded_full) <= len(line)
            # Runtime owns global IDs, whose growth also fits the retained batch margin.
            full.update(seq=10**18 + normalized_count, id=f"r-{10**18 + normalized_count}")
            normalized.append(full)
            normalized_count += 1
        bundle = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
        assert len(bundle) <= 1024 * 1024
    assert normalized_count == 24
