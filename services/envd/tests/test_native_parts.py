"""Native inputs preserve official bytes and only commit complete container blocks."""

import hashlib
import importlib
import os
import struct
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bbx_contracts.traffic import TrafficNativeSourceIndex
from bbx_envd.native import NativeParts, complete_blocks
from bbx_envd.traffic import TrafficCapture


def block(kind, body, endian="<"):
    body += b"\0" * (-len(body) % 4)
    size = len(body) + 12
    return struct.pack(endian + "II", kind, size) + body + struct.pack(endian + "I", size)


def section(endian="<"):
    return block(0x0A0D0D0A, struct.pack(endian + "IHHq", 0x1A2B3C4D, 1, 0, -1), endian)


def packet(payload=b"synthetic", endian="<"):
    return block(
        6, struct.pack(endian + "IIIII", 0, 0, 1, len(payload), len(payload)) + payload, endian
    )


def make_native(tmp_path):
    return NativeParts(tmp_path, str(uuid.uuid4()), 1, 32 * 1024 * 1024)


@pytest.mark.parametrize("endian", ["<", ">"])
def test_complete_prefix_never_publishes_partial_block_or_changes_bytes(tmp_path, endian):
    native = make_native(tmp_path)
    original = section(endian) + block(1, struct.pack(endian + "HHI", 1, 0, 65535), endian)
    final = packet(endian=endian)
    path = tmp_path / "raw.pcapng"
    path.write_bytes(original + final[:-1])
    native.add_segment("source", "member-1", path.name)
    native.drain()
    assert native.sources["source"]["packet_count"] == 0
    assert native.segments[path.name]["offset"] == len(original)
    with path.open("ab") as output:
        output.write(final[-1:])
    native.drain(final=True)
    listing = native.listing()
    index = TrafficNativeSourceIndex.model_validate(listing["sources"][0])
    assert index.complete and index.packet_count == 1
    rebuilt = b""
    for part in index.parts:
        with os.fdopen(native.open_part(part.id), "rb") as source:
            content = source.read()
        assert hashlib.sha256(content).hexdigest() == part.sha256
        rebuilt += content
    assert rebuilt == path.read_bytes()


def test_rotation_source_numbers_ack_and_restart_retain_full_prefix(tmp_path):
    native = make_native(tmp_path)
    for i in range(2):
        path = tmp_path / f"raw-{i}.pcapng"
        path.write_bytes(section() + packet())
        native.add_segment("source", "member-1", path.name)
        native.drain_segment(path.name, final=True)
    parts = native.sources["source"]["parts"]
    assert [p["sequence"] for p in parts] == [1, 2]
    assert [p["first_packet"] for p in parts] == [1, 2]
    assert [p["offset"] for p in parts] == [0, 0]
    assert parts[0]["segment_id"] != parts[1]["segment_id"]
    native.ack([p["id"] for p in parts])
    native.ack([p["id"] for p in parts])
    recovered = NativeParts(tmp_path, native.task_id, 1, native.max_bytes)
    recovered.drain(final=True, error="capture epoch ended on restart")
    assert recovered.sources["source"]["parts"] == parts
    assert not recovered.pending
    assert not recovered.sources["source"]["complete"]
    assert recovered.sources["source"]["packet_count"] == 2


def test_invalid_trailer_and_truncated_stop_are_explicit(tmp_path):
    invalid = bytearray(section())
    invalid[-1] = 1
    with pytest.raises(ValueError, match="trailer"):
        complete_blocks(bytes(invalid), None)
    native = make_native(tmp_path)
    path = tmp_path / "raw.pcapng"
    path.write_bytes(section() + packet()[:-1])
    native.add_segment("source", None, path.name)
    native.drain(final=True)
    assert native.error is not None and "incomplete" in native.error
    assert not native.sources["source"]["complete"]
    assert native.sources["source"]["packet_count"] == 0


def test_keylog_complete_lines_private_and_not_in_raw_parts(tmp_path):
    native = make_native(tmp_path)
    path = tmp_path / "tls.keys"
    path.write_bytes(b"SYNTHETIC synthetic-value\nPARTIAL")
    native.drain()
    part = native.keylog_parts[0]
    assert part["offset"] == 0 and part["sequence"] == 1
    with os.fdopen(native.open_part(part["id"]), "rb") as source:
        assert source.read() == b"SYNTHETIC synthetic-value\n"
    assert (tmp_path / (part["id"] + ".bin")).stat().st_mode & 0o777 == 0o600
    with path.open("ab") as output:
        output.write(b"-COMPLETE\n")
    native.drain(final=True)
    assert not native.error and len(native.keylog_parts) == 2
    assert native.keylog_parts[1]["offset"] == part["size_bytes"]
    assert all(item["kind"] == "keylog" for item in native.pending.values())


def test_staging_limit_fails_without_advancing_and_retries_after_ack(tmp_path):
    native = make_native(tmp_path)
    content = section() + packet()
    native.max_bytes = len(content)
    for i in range(2):
        name = f"raw-{i}.pcapng"
        (tmp_path / name).write_bytes(content)
        native.add_segment("source", None, name)
    native.drain()
    assert native.error is not None and "capacity" in native.error
    assert native.sources["source"]["packet_count"] == 1
    assert native.segments["raw-1.pcapng"]["offset"] == 0
    native.ack(list(native.pending))
    native.drain()
    assert native.sources["source"]["packet_count"] == 2


def test_native_part_open_rejects_symlinks_and_unregistered_names(tmp_path):
    native = make_native(tmp_path)
    (tmp_path / "raw.pcapng").write_bytes(section() + packet())
    native.add_segment("source", None, "raw.pcapng")
    native.drain()
    identifier = next(iter(native.pending))
    target = tmp_path / (identifier + ".bin")
    target.unlink()
    target.symlink_to(tmp_path / "raw.pcapng")
    with pytest.raises(OSError):
        native.open_part(identifier)
    with pytest.raises(FileNotFoundError):
        native.open_part("../tls.keys")


def test_official_keylogger_wrapper_stops_at_limit_without_affecting_forwarding(
    tmp_path, monkeypatch
):
    control = tmp_path / "control"
    control.mkdir()
    keyfile = tmp_path / "tls.keys"
    keyfile.touch(mode=0o600)
    calls = []
    module = SimpleNamespace(log_master_secret=lambda connection, material: calls.append(material))
    monkeypatch.setenv("BBX_TRAFFIC_CONTROL", str(control))
    addon_module = importlib.import_module("bbx_envd.traffic_addon")
    monkeypatch.setattr("bbx_envd.traffic_addon.importlib.import_module", lambda _: module)
    addon = addon_module.TrafficAddon()
    addon.running()
    module.log_master_secret(None, b"synthetic-only")
    assert calls == [b"synthetic-only"]
    (control / "disabled").touch()
    module.log_master_secret(None, b"not-persisted")
    assert calls == [b"synthetic-only"]
    (control / "disabled").unlink()
    with keyfile.open("r+b") as output:
        output.truncate(16 * 1024 * 1024)
    module.log_master_secret(None, b"not-persisted")
    assert calls == [b"synthetic-only"]
    assert (control / "keylog-full").exists()


async def test_restart_recovers_unreported_rotation_with_registered_owner(tmp_path, monkeypatch):
    capture = TrafficCapture(tmp_path / "traffic", 32 * 1024 * 1024, 10000)
    monkeypatch.setattr(capture, "start_raw", AsyncMock())
    monkeypatch.setattr(capture, "start_proxy", AsyncMock())
    await capture.start(str(uuid.uuid4()), 1)
    capture.register_agent("agent-1", "member-1")
    assert capture.directory is not None
    path = capture.directory / "raw_agent-1_00001_20261015000000.pcapng"
    original = section() + packet()
    path.write_bytes(original)
    # No File stderr registration occurred before this simulated restart.
    restarted = TrafficCapture(capture.root, capture.max_bytes, 10000)
    restarted.restore()
    listing = restarted.native_listing()
    source = TrafficNativeSourceIndex.model_validate(listing["sources"][0])
    assert source.agent_id == "member-1" and source.packet_count == 1
    assert not source.complete and source.error is not None and "epoch ended" in source.error
    assert restarted.native is not None
    with os.fdopen(restarted.native.open_part(source.parts[0].id), "rb") as content:
        assert content.read() == original
    await capture.stop()


async def test_native_time_watermark_does_not_run_ahead_of_committed_bytes(tmp_path):
    native = make_native(tmp_path)
    path = tmp_path / "raw.pcapng"
    path.write_bytes(section() + packet())
    native.add_segment("source", "member-1", path.name)
    capture = TrafficCapture(tmp_path, native.max_bytes, 10000)
    capture.native = native
    timestamp = "2026-10-15T00:00:00+00:00"
    capture.packet_times["source"] = (2, timestamp)
    capture.drain_native()
    assert native.sources["source"]["packet_count"] == 1
    assert native.sources["source"]["last_packet_at"] is None
    with path.open("ab") as output:
        output.write(packet())
    capture.drain_native()
    assert native.sources["source"]["packet_count"] == 2
    assert native.sources["source"]["last_packet_at"] == timestamp
