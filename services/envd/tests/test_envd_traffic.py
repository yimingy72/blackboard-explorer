"""Traffic identity, streaming, segment durability and authenticated file boundaries."""

import asyncio
import importlib
import json
import os
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from bbx_envd.packets import native_application, packet_record, raw_protocol
from bbx_envd.traffic import TrafficCapture, atomic_json
from bbx_envd.traffic_format import PREVIEW_BYTES, message_preview, redact_url


@pytest.fixture
async def capture(tmp_path, monkeypatch):
    capture = TrafficCapture(tmp_path / "traffic", 1024 * 1024, 10000)
    monkeypatch.setattr(capture, "start_raw", AsyncMock())
    monkeypatch.setattr(capture, "start_proxy", AsyncMock())
    await capture.start(str(uuid.uuid4()), 1)
    yield capture
    await capture.stop()


def record():
    return {
        "timestamp": "2026-10-15T00:00:00+00:00",
        "agent_id": None,
        "attribution": "unknown",
        "protocol": "tcp",
        "source_ip": "127.0.0.1",
        "source_port": 1234,
        "destination_ip": "127.0.0.1",
        "destination_port": 80,
        "method": None,
        "url": None,
        "status_code": None,
        "summary": "TCP packet",
        "request_bytes": 10,
        "response_bytes": 0,
        "plaintext": "not_applicable",
        "request": None,
        "response": None,
    }


async def test_segments_ack_restart_and_active_recovery(capture):
    capture.record(record())
    capture.record(record())
    restarted = TrafficCapture(capture.root, capture.max_bytes, 10000)
    restarted.restore()
    assert restarted.status()["status"] == "partial"
    assert restarted.status()["source_id"] == capture.status()["source_id"]
    assert restarted.status()["record_count"] == 2
    assert "unknown" in restarted.status()["error"]
    chunk_id = next(iter(restarted.chunks))
    with os.fdopen(restarted.open_segment(chunk_id, "chunks")) as source:
        assert [json.loads(line)["seq"] for line in source] == [1, 2]
    detail = restarted.detail("r-1")
    assert detail is not None and detail["agent_id"] is None
    restarted.ack([chunk_id], [])
    restarted.ack([chunk_id], [])
    assert restarted.chunks == {}
    assert restarted.status()["record_count"] == 2
    capture.current = []


async def test_restart_preserves_unindexed_raw_snapshot_once(capture):
    raw = capture.directory / "raw_agent-1_00001_20261015000000.pcapng"
    raw.write_bytes(b"synthetic-incomplete-pcap")
    capture.register_agent("agent-1", "member-1")
    restarted = TrafficCapture(capture.root, capture.max_bytes, 10000)
    restarted.restore()
    assert len(restarted.artifacts) == 1
    artifact = next(iter(restarted.artifacts.values()))
    assert artifact["agent_id"] == "member-1"
    assert artifact["sealed"] and "recovered" in artifact["name"]
    assert restarted.status()["status"] == "partial"
    again = TrafficCapture(capture.root, capture.max_bytes, 10000)
    again.restore()
    assert list(again.artifacts) == list(restarted.artifacts)


async def test_limit_keeps_proxy_identity_but_does_not_add_records(capture):
    capture.proxy_port = 12345
    capture.ca_bundle = "/public/ca.pem"
    capture.state["proxy_serving"] = True
    capture.record(record())
    limited = await capture.limit()
    assert limited["status"] == "partial" and limited["capturing"] is False
    assert limited["proxy_serving"] is True
    assert capture.command_environment("agent-1", str(uuid.uuid4()))["HTTP_PROXY"]
    capture.record(record())
    assert capture.state["record_count"] == 1
    stopped = await capture.stop()
    assert stopped["proxy_serving"] is False


async def test_preview_loss_is_counted_without_becoming_a_capture_error(capture):
    capture.preview_lost("Oversized packet preview discarded; raw capture retained", 3)
    assert capture.status()["preview_dropped"] == 3
    assert capture.status().get("error") is None
    stopped = await capture.stop()
    assert stopped["status"] == "partial"
    assert stopped.get("error") is None


async def test_sealed_segment_rejects_symlink_fifo_and_bad_identifier(capture, tmp_path):
    capture.record(record())
    await capture.seal()
    identifier = next(iter(capture.chunks))
    path = capture.directory / capture.paths[identifier]
    path.unlink()
    secret = tmp_path / "secret"
    secret.write_text("never expose")
    path.symlink_to(secret)
    with pytest.raises(OSError):
        capture.open_segment(identifier, "chunks")
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError):
        capture.open_segment(identifier, "chunks")
    with pytest.raises(FileNotFoundError):
        capture.open_segment("../secret", "chunks")
    with pytest.raises(ValueError):
        capture.ack(["../secret"], [])


async def test_command_identity_is_unique_and_credentials_are_not_in_display(capture):
    capture.proxy_port = 12345
    capture.ca_bundle = "/public/ca.pem"
    first = capture.command_environment("agent-1", str(uuid.uuid4()))
    second = capture.command_environment("agent-1", str(uuid.uuid4()))
    assert first["HTTP_PROXY"] != second["HTTP_PROXY"]
    assert first["HTTP_PROXY"] == first["HTTPS_PROXY"] == first["http_proxy"]
    assert first["SSL_CERT_FILE"] == first["REQUESTS_CA_BUNDLE"]
    for identity in capture.credentials.values():
        assert identity["password"].encode() not in capture.redact_output(
            (first["HTTP_PROXY"] + second["HTTP_PROXY"]).encode()
        )
    assert "password" not in json.dumps(capture.status())
    assert (capture.directory / "control" / "credentials.json").stat().st_mode & 0o777 == 0o600


def test_file_output_redaction_matches_bytes_across_copy_boundaries(tmp_path, monkeypatch):
    import tempfile

    from bbx_envd import traffic

    capture = TrafficCapture(tmp_path / "traffic", 1024 * 1024, 100)
    capture.credentials["first"] = {"password": "cross-boundary-password"}
    body = (
        b"a" * 65530
        + b"cross-boundary-password\n"
        + b"https://"
        + b"u" * 70000
        + b":"
        + b"p" * 70000
        + b"@host.invalid/path\n"
        + b"cross-boundary-password\n"
    )
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
        source.write(body)
        redact_file = traffic._redact_file

        def truncate_process_output(snapshot, target, pattern):
            assert snapshot is not source
            source.truncate(0)
            redact_file(snapshot, target, pattern)

        monkeypatch.setattr(traffic, "_redact_file", truncate_process_output)
        capture.redact_output_file(source, output)
        output.seek(0)
        assert output.read() == capture.redact_output(body)


def test_display_redaction_and_binary_size():
    body = b'{"password":"synthetic-secret","nested":{"apiKey":"synthetic-key"}}'
    shown = message_preview(
        [("Authorization", "Bearer synthetic-token"), ("Set-Cookie", "private=1")], body, len(body)
    )
    assert "synthetic-secret" not in json.dumps(shown)
    assert "synthetic-key" not in json.dumps(shown)
    assert "synthetic-token" not in json.dumps(shown)
    assert "private=1" not in json.dumps(shown)
    url = redact_url("https://user:pass@example.test/a?token=synthetic-token&ok=1#private")
    assert "user" not in url and "pass" not in url and "synthetic-token" not in url
    assert "ok=1" in url
    shown = message_preview([], b"\xff" * (PREVIEW_BYTES + 20), PREVIEW_BYTES + 20)
    assert shown["body_encoding"] == "base64"
    assert shown["body_truncated"] is True
    assert shown["body_bytes"] == PREVIEW_BYTES + 20


def test_addon_streams_without_changing_chunks_and_authenticates_each_command(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BBX_TRAFFIC_CONTROL", str(tmp_path))
    (tmp_path / "inbox").mkdir()
    atomic_json(
        tmp_path / "credentials.json",
        {
            "command-1": {
                "agent_id": "agent-1",
                "command_id": "command-1",
                "password": "synthetic-1",
            },
            "command-2": {
                "agent_id": "agent-2",
                "command_id": "command-2",
                "password": "synthetic-2",
            },
        },
    )
    addon = importlib.import_module("bbx_envd.traffic_addon").TrafficAddon()
    headers = SimpleNamespace(items=lambda **kwargs: [("Cookie", "synthetic-cookie")])
    flows = []
    for number in (1, 2):
        request = SimpleNamespace(
            scheme="https",
            method="POST",
            host="example.test",
            port=443,
            pretty_url="https://example.test/",
            headers=headers,
            timestamp_start=time.time(),
        )
        flow = SimpleNamespace(
            id=str(number),
            metadata={"proxyauth": (f"command-{number}", f"synthetic-{number}")},
            request=request,
            response=SimpleNamespace(status_code=200, headers=headers),
            client_conn=SimpleNamespace(id=str(number), peername=("127.0.0.1", 1234)),
            server_conn=SimpleNamespace(peername=("127.0.0.2", 443)),
        )
        addon.requestheaders(flow)
        addon.responseheaders(flow)
        assert flow.request.stream(b"upload") == b"upload"
        for _ in range(5):
            assert flow.response.stream(b"x" * PREVIEW_BYTES) == b"x" * PREVIEW_BYTES
        flows.append(flow)
    assert len(addon.active["1"]["response"]) == PREVIEW_BYTES
    for flow in reversed(flows):
        addon.response(flow)
    items = [json.loads(path.read_text()) for path in (tmp_path / "inbox").glob("*.json")]
    assert {item["agent_id"] for item in items} == {"agent-1", "agent-2"}
    assert all(item["attribution"] == "exact" for item in items)
    assert all(item["response"]["body_truncated"] for item in items)
    assert all(item["response_bytes"] == 5 * PREVIEW_BYTES for item in items)
    assert "synthetic-1" not in json.dumps(items) and "synthetic-cookie" not in json.dumps(items)
    flows[0].metadata = {"proxyauth": ("command-1", "wrong-password")}
    addon.requestheaders(flows[0])
    assert not addon.active


async def test_authenticated_routes_lists_summary_only_and_ack(capture, monkeypatch):
    monkeypatch.setenv("ENVD_TOKEN", "replace-me")
    service = importlib.import_module("bbx_envd.app")
    monkeypatch.setattr(service, "traffic_capture", capture)
    capture.record({**record(), "request": {"body_preview": "bounded-detail-only"}})
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/traffic/status")).status_code == 401
        client.headers["Authorization"] = "Bearer replace-me"
        listing = (await client.get("/traffic/records")).json()
        assert "bounded-detail-only" not in json.dumps(listing)
        assert (await client.get("/traffic/records/r-1")).json()["request"]["body_preview"]
        assert (await client.post("/traffic/seal", json={})).status_code == 200
        identifier = next(iter(capture.chunks))
        chunk = await client.get(f"/traffic/chunks/{identifier}")
        assert json.loads(chunk.content)["id"] == "r-1"
        compact = (await client.get("/traffic/status")).json()
        assert "chunks" not in compact and "files" not in compact
        expanded = (await client.get("/traffic/status?include_items=true")).json()
        assert expanded["chunks"] and "files" in expanded
        listing = (await client.get("/traffic/chunks?limit=1")).json()
        assert listing["total_items"] == 1 and len(listing["items"]) == 1
        assert (await client.get("/traffic/chunks/unknown")).status_code == 404
        assert (await client.post("/traffic/ack", json={"chunks": [identifier]})).status_code == 200
        assert (await client.get(f"/traffic/chunks/{identifier}")).status_code == 404


async def test_direct_execute_route_uses_verified_agent_header(capture, monkeypatch):
    monkeypatch.setenv("ENVD_TOKEN", "replace-me")
    service = importlib.import_module("bbx_envd.app")
    monkeypatch.setattr(service.settings, "envd_mode", "blackboard")
    execute = AsyncMock(return_value={"exit_code": 0, "stdout": "ok"})
    monkeypatch.setattr(service, "execute_command", execute)
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers.update({"Authorization": "Bearer replace-me", "X-Agent-Id": "agent-1"})
        response = await client.post(
            "/execute",
            json={"command": "printf ok", "cwd": None, "timeout_sec": 30, "privileged": False},
        )
    assert response.status_code == 200 and response.json()["exit_code"] == 0
    assert execute.await_args is not None
    assert execute.await_args.args[:2] == ("agent-1", "printf ok")


async def test_missing_capture_tools_are_explicit_partial(tmp_path, monkeypatch):
    capture = TrafficCapture(tmp_path / "traffic", 1024 * 1024, 3)
    monkeypatch.setattr("bbx_envd.traffic.shutil.which", lambda _: None)
    await capture.start(str(uuid.uuid4()), 1)
    assert capture.status()["https"] == "unavailable"
    assert "dumpcap/tshark" in capture.status()["error"]
    for _ in range(5):
        capture.record(record())
    stopped = await capture.stop()
    assert stopped["status"] == "partial"
    assert stopped["record_count"] == 3
    assert "capacity" in stopped["error"]


@pytest.mark.parametrize(
    "native,stack,ipv4,ipv6,expected",
    [
        ("STUN", "sll:ip:udp:stun", "17", "", "udp"),
        ("DNS", "sll:ip:udp:dns", "17", "", "udp"),
        ("QUIC", "sll:ipv6:udp:quic", "", "17", "udp"),
        ("HTTP2", "sll:ip:udp:data", "17", "", "udp"),
        ("TLSv1.3", "sll:ip:tcp:tls", "6", "", "tcp"),
        ("HTTP", "sll:ip:tcp:http", "6", "", "tcp"),
        ("DNS", "sll:ip:icmp:ip:udp:dns", "1", "", "icmp"),
        ("ICMPv6", "sll:ipv6:icmpv6", "", "58", "icmp"),
        ("ArbitraryApp", "", "17", "", "udp"),
        ("ARP", "sll:ethertype:arp", "", "", "arp"),
    ],
)
def test_raw_protocol_uses_official_transport_instead_of_port_heuristics(
    native,
    stack,
    ipv4,
    ipv6,
    expected,
):
    assert raw_protocol(native, stack, ipv4, ipv6) == expected


async def test_udp_application_guess_does_not_change_session_transport_or_owner(capture):
    for agent in ("agent-1", "agent-2"):
        for number, native in ((1, "UDP"), (2, "STUN")):
            document = {
                "timestamp": "1000000",
                "protocol": native,
                "info": "UDP port 5349",
                "layers": {
                    "frame_raw": "00" * 80,
                    "frame": {
                        "frame_frame_number": str(number),
                        "frame_frame_len": "80",
                        "frame_frame_cap_len": "80",
                        "frame_frame_protocols": "sll:ip:udp:data",
                    },
                    "udp": {"udp_udp_srcport": "49152", "udp_udp_dstport": "5349"},
                    "ip": {"ip_ip_src": "127.0.0.1", "ip_ip_dst": "127.0.0.1", "ip_ip_proto": "17"},
                },
            }
            capture.record(packet_record(document, "source-" + agent, number, agent, "lo"))
    assert len(capture.details) == 4
    for detail in capture.details.values():
        assert detail["protocol"] == "udp" and detail["attribution"] == "exact"
        assert detail["transport_protocol"] == "udp" and detail["request_bytes"] == 80
    assert {item["agent_id"] for item in capture.details.values()} == {"agent-1", "agent-2"}
    assert sum(item["application_protocol"] == "stun" for item in capture.details.values()) == 2


@pytest.mark.parametrize(
    "native,transport,application",
    [
        ("TLSv1.3", "tcp", "tls"),
        ("mDNS", "udp", "dns"),
        ("HTTP2", "tcp", "http"),
        ("QUIC", "udp", "quic"),
        ("STUN", "udp", "stun"),
        ("UDP", "udp", None),
        ("DATA", "udp", None),
        ("ICMPv6", "icmp", None),
    ],
)
def test_native_application_is_kept_separate_from_transport(native, transport, application):
    assert native_application(native, transport) == application


async def test_capture_readiness_requires_dumpcap_file_confirmation(capture):
    reader = asyncio.StreamReader()
    role = "raw:agent-1"
    event = capture.raw_readiness[role] = asyncio.Event()
    process = SimpleNamespace(stderr=reader)
    monitor = asyncio.create_task(capture.raw_errors(process, role))
    reader.feed_data(b"Capturing on 'any'\n")
    await asyncio.sleep(0)
    assert not event.is_set() and role not in capture.raw_ready
    reader.feed_data(b"File: /synthetic/raw_agent-1.pcapng\n")
    await asyncio.wait_for(event.wait(), 1)
    assert role in capture.raw_ready
    reader.feed_data(b"Packets received/dropped on interface 'any': 8/2\n")
    reader.feed_eof()
    await monitor
    assert capture.state["dropped_packets"] == 2
