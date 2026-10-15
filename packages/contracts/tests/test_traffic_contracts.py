"""Traffic trust boundaries preserve attribution, sequence and bounded display copies."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from bbx_contracts.schemas import export_schemas
from bbx_contracts.traffic import (
    TrafficCapture,
    TrafficChunk,
    TrafficFile,
    TrafficManifest,
    TrafficMessage,
    TrafficNativeSourceIndex,
    TrafficObservation,
    TrafficPacketDetail,
    TrafficPacketMeta,
    TrafficRecordDetail,
    TrafficRecordSummary,
    redact_traffic_text,
    redact_traffic_url,
)
from pydantic import ValidationError


def summary(**changes):
    return dict(id="r-1", seq=1, timestamp=datetime.now(UTC), protocol="https") | changes


def manifest(**changes):
    now = datetime.now(UTC)
    return dict(
        task_id=uuid4(),
        run=1,
        status="collecting",
        https="enabled",
        started_at=now,
        updated_at=now,
        **changes,
    )


def test_redaction_limits_and_original_body_size():
    message = TrafficMessage(
        headers=[
            ("Authorization", "Bearer test-secret"),
            ("Cookie", "session=hidden"),
            ("X-Data", "界" * 20000),
        ],
        body_preview='{"password":"hidden", "token":"hidden"}' + "界" * 20000,
        body_bytes=80000,
        body_encoding="utf-8",
    )
    assert "hidden" not in message.body_preview
    assert "[redacted]" in message.body_preview
    assert message.headers[:2] == [("Authorization", "[redacted]"), ("Cookie", "[redacted]")]
    assert sum(len(k.encode()) + len(v.encode()) for k, v in message.headers) <= 16384
    assert len(message.body_preview.encode()) <= 16384
    assert message.body_bytes == 80000 and message.body_truncated
    url = redact_traffic_url("https://user:hidden@example.test/path?token=hidden&ok=1#hidden")
    assert "hidden" not in url and "ok=1" in url
    assert "hidden" not in redact_traffic_text("Authorization: Bearer hidden\npassword=hidden")
    observation = TrafficObservation(attributes={"api_key": "hidden"}, error="password=hidden")
    assert observation.attributes["api_key"] == "[redacted]"
    assert "hidden" not in (observation.error or "")


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "r-2"},
        {"agent_id": "a"},
        {"attribution": "exact"},
        {"attribution": "shared", "agent_id": "lead"},
        {"source_port": 65536},
        {"id": "../r-1"},
    ],
)
def test_summary_rejects_fabricated_identity_and_invalid_sequence(changes):
    with pytest.raises(ValidationError):
        TrafficRecordSummary.model_validate(summary() | changes)


def test_encrypted_data_never_claims_plaintext():
    with pytest.raises(ValidationError):
        TrafficRecordDetail.model_validate(summary(plaintext="encrypted", request={}))


def test_shared_and_unknown_records_do_not_claim_an_agent():
    for attribution in ("shared", "unknown"):
        record = TrafficRecordSummary.model_validate(summary(attribution=attribution))
        assert record.agent_id is None and record.attribution == attribution


def test_observation_distinguishes_wire_command_sources_from_logical_calls():
    assert TrafficObservation().scope == "logical_call"
    wire = TrafficObservation(scope="wire", attributes={"command_id": "command-1"})
    assert wire.scope == "wire" and wire.attributes["command_id"] == "command-1"
    with pytest.raises(ValidationError):
        TrafficObservation.model_validate({"scope": "guessed"})


def test_protocol_layers_are_optional_and_never_inferred_from_a_display_label():
    legacy = TrafficRecordSummary.model_validate(summary(protocol="tls"))
    assert legacy.transport_protocol is None and legacy.application_protocol is None
    layered = TrafficRecordSummary.model_validate(
        summary(protocol="udp", transport_protocol="udp", application_protocol="dns")
    )
    assert layered.transport_protocol == "udp" and layered.application_protocol == "dns"
    for key in ("transport_protocol", "application_protocol"):
        with pytest.raises(ValidationError):
            TrafficRecordSummary.model_validate(summary() | {key: "x" * 65})


@pytest.mark.parametrize(
    "changes",
    [
        {"files": [{"id": "../secret", "name": "test.pcap", "size_bytes": 0}]},
        {"record_count": 1},
        {
            "record_count": 2,
            "chunks": [
                {"id": "a", "first_seq": 1, "last_seq": 2, "count": 1},
                {"id": "b", "first_seq": 2, "last_seq": 3, "count": 1},
            ],
        },
        {
            "record_count": 2,
            "chunks": [
                {"id": "a", "first_seq": 1, "last_seq": 1, "count": 1},
                {"id": "a", "first_seq": 2, "last_seq": 2, "count": 1},
            ],
        },
    ],
)
def test_manifest_rejects_paths_and_inconsistent_indexes(changes):
    with pytest.raises(ValidationError):
        TrafficManifest.model_validate(manifest() | changes)


def test_checkpoint_and_export(tmp_path):
    data = TrafficManifest.model_validate(
        manifest(record_count=1, chunks=[{"id": "a", "first_seq": 3, "last_seq": 3, "count": 1}])
    )
    assert data.last_seq == 3
    assert "TrafficListResponse.json" in {path.name for path in export_schemas(tmp_path)}


def test_file_owner_is_optional_and_uses_the_canonical_agent_identity():
    agent_id = "member-2"
    item = TrafficFile(
        id="agent-pcap",
        name="capture.pcapng",
        size_bytes=7,
        agent_id=agent_id,
        source_id="source-netns-1",
    )
    assert item.agent_id == agent_id and item.source_id == "source-netns-1"
    root = TrafficFile(id="root-pcap", name="root.pcapng", size_bytes=0)
    assert root.agent_id is None and root.source_id is None
    for changes in ({"agent_id": ""}, {"source_id": ""}):
        with pytest.raises(ValidationError):
            TrafficFile.model_validate(dict(id="raw", name="raw.pcap", size_bytes=0) | changes)


def packet_metadata():
    return dict(
        source_id="source-agent-1",
        number=101,
        native_frame_number=1,
        frame_length=30,
        captured_length=30,
        direction="outbound",
        interface="lo",
        info="TCP packet",
    )


def test_packet_metadata_is_explicit_and_not_inferred_for_legacy_records():
    legacy = TrafficRecordSummary.model_validate(summary())
    assert legacy.record_kind is None and legacy.packet is None
    metadata = packet_metadata()
    packet = TrafficRecordSummary.model_validate(summary(record_kind="packet", packet=metadata))
    assert packet.packet is not None
    assert packet.packet.number == 101 and packet.packet.native_frame_number == 1
    for change in (
        {"record_kind": "packet"},
        {"packet": metadata},
        {"record_kind": "logical", "packet": metadata},
    ):
        with pytest.raises(ValidationError):
            TrafficRecordSummary.model_validate(summary() | change)
    with pytest.raises(ValidationError):
        TrafficPacketMeta.model_validate(metadata | {"direction": "guessed"})


def test_packet_hex_is_original_while_tree_display_is_redacted_and_bounded():
    raw_hex = b"password=private-raw-secret".hex()
    packet = TrafficRecordDetail.model_validate(
        summary(
            record_kind="packet",
            packet=packet_metadata(),
            packet_detail={
                "raw_hex": raw_hex,
                "layers": [
                    {
                        "name": "http.authorization",
                        "label": "Authorization: Basic private",
                        "value": "private",
                        "offset": 4,
                        "length": 8,
                    },
                ],
            },
        )
    )
    assert packet.packet_detail is not None
    assert packet.packet_detail.raw_hex == raw_hex
    assert packet.packet_detail.raw_truncated
    assert packet.packet_detail.layers[0].value == "[redacted]"
    assert "private" not in packet.packet_detail.layers[0].label
    assert packet.packet_detail.layers[0].offset == 4
    basic = TrafficPacketDetail.model_validate(
        {
            "layers": [
                {
                    "name": "http.authbasic",
                    "label": "Basic credentials: private",
                    "value": "private",
                }
            ]
        }
    )
    assert "private" not in basic.model_dump_json()
    assert (
        "private"
        not in TrafficPacketMeta.model_validate(
            packet_metadata() | {"info": "GET http://user:private@example.test/path"}
        ).info
    )
    for bad in ("a", "zz", "ff" * 16385):
        with pytest.raises(ValidationError):
            TrafficPacketDetail(raw_hex=bad)


def test_packet_tree_limits_cover_count_depth_and_total_text():
    node = {"name": "data", "value": "ok"}
    for _ in range(8):
        node = {"name": "data", "children": [node]}
    cases = [
        {"layers": [{"name": "data"}] * 513},
        {"layers": [node]},
        {"layers": [{"name": "data", "value": "x" * 9000}] * 8},
    ]
    for value in cases:
        with pytest.raises(ValidationError):
            TrafficPacketDetail.model_validate(value)
    with pytest.raises(ValidationError, match="128 KiB"):
        TrafficRecordDetail.model_validate(
            summary(
                record_kind="packet",
                packet=packet_metadata(),
                packet_detail={"raw_hex": "00" * 16384, "layers": [{"name": "x" * 120}] * 512},
            )
        )


def test_packet_bundle_and_capture_counters_keep_legacy_defaults():
    old = TrafficChunk(id="old", first_seq=1, last_seq=200, count=200)
    assert old.detail_storage == "record"
    with pytest.raises(ValidationError):
        TrafficChunk(id="new", first_seq=1, last_seq=129, count=129, detail_storage="chunk")
    capture = TrafficCapture.model_validate(
        {key: value for key, value in manifest().items() if key not in {"task_id", "run"}}
    )
    assert capture.preview_dropped == capture.pending_chunks == 0
    assert capture.last_packet_at is None
    for key in ("preview_dropped", "pending_chunks"):
        with pytest.raises(ValidationError):
            TrafficCapture.model_validate(capture.model_dump() | {key: -1})


def native_index() -> dict[str, Any]:
    return dict(
        task_id=str(uuid4()),
        run=1,
        source_id="source-a",
        agent_id="lead",
        packet_count=2,
        parts=[
            dict(
                id="p1",
                segment_id="s1",
                sequence=1,
                offset=0,
                size_bytes=4,
                sha256="0" * 64,
                first_packet=1,
                last_packet=2,
            )
        ],
    )


@pytest.mark.parametrize("damage", ["path", "gap", "sequence", "range", "count", "keylog"])
def test_native_input_indices_reject_paths_gaps_and_false_packet_coverage(damage):
    value = native_index()
    if damage == "path":
        value["parts"][0]["id"] = "../other-task"
    elif damage == "gap":
        value["parts"][0]["offset"] = 1
    elif damage == "sequence":
        value["parts"][0]["sequence"] = 2
    elif damage == "range":
        value["parts"][0]["first_packet"] = None
    elif damage == "count":
        value["packet_count"] = 3
    elif damage == "keylog":
        value["keylog_parts"] = [dict(id="k", sequence=1, offset=1, size_bytes=4, sha256="1" * 64)]
    with pytest.raises(ValidationError):
        TrafficNativeSourceIndex.model_validate(value)


def test_native_input_indices_accept_complete_prefixes_across_capture_sections():
    value = native_index()
    value["parts"].append(
        dict(
            id="p2",
            segment_id="s2",
            sequence=2,
            offset=0,
            size_bytes=8,
            sha256="2" * 64,
            first_packet=3,
            last_packet=4,
        )
    )
    value["packet_count"] = 4
    assert TrafficNativeSourceIndex.model_validate(value).packet_count == 4
