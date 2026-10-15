"""Task-scoped traffic archives shared by collectors, runtime and readers."""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .models import ContractModel

TRAFFIC_PREVIEW_LIMIT = 16 * 1024
TRAFFIC_PACKET_BATCH_LIMIT = 1024 * 1024
TRAFFIC_PACKET_DETAIL_LIMIT = 128 * 1024
TrafficId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]
TrafficStatus = Literal["collecting", "complete", "partial", "error", "archived"]
TrafficDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_SECRET = re.compile(
    r"authorization|authbasic|cookie|password|passwd|secret|token|api[_-]?key|credential", re.I
)
_ASSIGNMENT = re.compile(
    r"""(?i)(?<![\w.-])((?:["']?)[\w.-]{0,64}"""
    r"""(?:password|passwd|secret|token|api[_-]?key|credential)"""
    r"""[\w.-]{0,64}["']?\s*[:=]\s*)("[^"\n]*"|'[^'\n]*'|[^&\s,;}]+)"""
)


def redact_traffic_text(value: str, limit: int = TRAFFIC_PREVIEW_LIMIT) -> str:
    """Redact common credential fields before bounding a display copy."""
    value = re.sub(
        r"(?im)(authorization|proxy-authorization|cookie|set-cookie):[^\r\n]*",
        r"\1: [redacted]",
        value,
    )
    value = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [redacted]", value)
    value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[redacted]@", value)
    value = _ASSIGNMENT.sub(r"\1[redacted]", value)
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def redact_traffic_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.netloc.rsplit("@", 1)[-1]
        query = urlencode(
            [
                (key, "[redacted]" if _SECRET.search(key) else item)
                for key, item in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return redact_traffic_text(urlunsplit((parts.scheme, host, parts.path, query, "")), 4096)
    except ValueError:
        return "[invalid URL]"


class TrafficMessage(ContractModel):
    headers: list[tuple[str, str]] = Field(default_factory=list, max_length=256)
    body_preview: str = ""
    body_bytes: int = Field(default=0, ge=0)
    body_encoding: Literal["utf-8", "base64", "omitted"] = "omitted"
    body_truncated: bool = False

    @model_validator(mode="after")
    def bounded_display(self) -> TrafficMessage:
        headers = []
        remaining = TRAFFIC_PREVIEW_LIMIT
        for name, value in self.headers:
            name = redact_traffic_text(name, min(256, remaining))
            remaining -= len(name.encode("utf-8"))
            value = "[redacted]" if _SECRET.search(name) else redact_traffic_text(value)
            value = value.encode("utf-8")[: max(0, remaining)].decode("utf-8", errors="ignore")
            remaining -= len(value.encode("utf-8"))
            if name:
                headers.append((name, value))
            if remaining <= 0:
                break
        self.headers = headers
        original_length = len(self.body_preview.encode("utf-8"))
        self.body_preview = redact_traffic_text(self.body_preview)
        self.body_truncated |= original_length > TRAFFIC_PREVIEW_LIMIT
        return self


class TrafficObservation(ContractModel):
    scope: Literal["wire", "logical_call"] = "logical_call"
    trace_id: str | None = Field(default=None, max_length=128)
    span_id: str | None = Field(default=None, max_length=128)
    parent_span_id: str | None = Field(default=None, max_length=128)
    attributes: dict[str, str] = Field(default_factory=dict, max_length=128)
    status: str = Field(default="unknown", max_length=128)
    error: str | None = None
    retry_visibility: Literal["known", "unknown"] = "unknown"

    @model_validator(mode="after")
    def safe_attributes(self) -> TrafficObservation:
        self.attributes = {
            redact_traffic_text(key, 128): "[redacted]"
            if _SECRET.search(key)
            else redact_traffic_text(value, 1024)
            for key, value in self.attributes.items()
        }
        if sum(len(k.encode()) + len(v.encode()) for k, v in self.attributes.items()) > 16384:
            raise ValueError("Traffic observation attributes exceed 16 KiB")
        if self.error is not None:
            self.error = redact_traffic_text(self.error, 2048)
        return self


class TrafficPacketMeta(ContractModel):
    source_id: str = Field(min_length=1, max_length=160)
    number: int = Field(ge=1, le=2**63 - 1)
    native_frame_number: int | None = Field(default=None, ge=1)
    frame_length: int = Field(ge=0)
    captured_length: int = Field(ge=0)
    direction: Literal["inbound", "outbound", "loopback", "unknown"] = "unknown"
    interface: str | None = Field(default=None, max_length=128)
    info: str = Field(default="", max_length=2048)

    @model_validator(mode="after")
    def safe_info(self) -> TrafficPacketMeta:
        self.info = redact_traffic_text(self.info, 2048)
        return self


class TrafficPacketNode(ContractModel):
    name: str = Field(min_length=1, max_length=256)
    label: str = Field(default="", max_length=4096)
    value: str | None = Field(default=None, max_length=16384)
    offset: int | None = Field(default=None, ge=0)
    length: int | None = Field(default=None, ge=0)
    data_source_id: int = Field(default=0, ge=0)
    filter_expr: str | None = Field(default=None, max_length=4096)
    generated: bool = False
    severity: str | None = Field(default=None, max_length=32)
    children: list[TrafficPacketNode] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def safe_display(self) -> TrafficPacketNode:
        if _SECRET.search(self.name):
            self.label = f"{self.name}: [redacted]"
            self.value = "[redacted]" if self.value is not None else None
            self.filter_expr = None
        else:
            self.label = redact_traffic_text(self.label, 4096)
            if self.value is not None:
                self.value = redact_traffic_text(self.value)
            if (
                self.filter_expr is not None
                and redact_traffic_text(self.filter_expr, 4096) != self.filter_expr
            ):
                self.filter_expr = None
        return self


class TrafficNativePart(ContractModel):
    id: TrafficId
    segment_id: TrafficId
    sequence: int = Field(ge=1)
    offset: int = Field(ge=0)
    size_bytes: int = Field(ge=1, le=4 * 1024 * 1024)
    sha256: TrafficDigest
    first_packet: int | None = Field(default=None, ge=1)
    last_packet: int | None = Field(default=None, ge=1)


class TrafficNativeKeylogPart(ContractModel):
    id: TrafficId
    sequence: int = Field(ge=1)
    offset: int = Field(ge=0)
    size_bytes: int = Field(ge=1, le=4 * 1024 * 1024)
    sha256: TrafficDigest


class TrafficNativeSourceIndex(ContractModel):
    schema_version: Literal[1] = 1
    task_id: UUID
    run: int = Field(ge=1)
    source_id: TrafficId
    agent_id: str | None = Field(default=None, min_length=1, max_length=160)
    complete: bool = False
    error: str | None = None
    packet_count: int = Field(default=0, ge=0)
    last_packet_at: AwareDatetime | None = None
    parts: list[TrafficNativePart] = Field(default_factory=list, max_length=100000)
    keylog_parts: list[TrafficNativeKeylogPart] = Field(default_factory=list, max_length=100000)

    @model_validator(mode="after")
    def continuous_inputs(self) -> TrafficNativeSourceIndex:
        segments: set[str] = set()
        segment, offset, packet = None, 0, 0
        part_ids: set[str] = set()
        for sequence, part in enumerate(self.parts, 1):
            if part.id in part_ids or part.sequence != sequence:
                raise ValueError("Native capture parts are duplicated or out of sequence")
            part_ids.add(part.id)
            if part.segment_id != segment:
                if part.segment_id in segments:
                    raise ValueError("Native capture segments are out of order")
                segment, offset = part.segment_id, 0
                segments.add(segment)
            if part.offset != offset:
                raise ValueError("Native capture byte prefix has a gap or overlap")
            offset += part.size_bytes
            if (part.first_packet is None) != (part.last_packet is None):
                raise ValueError("Native capture packet range is incomplete")
            if part.first_packet is not None and part.last_packet is not None:
                if part.first_packet != packet + 1 or part.last_packet < part.first_packet:
                    raise ValueError("Native capture packet ranges are discontinuous")
                packet = part.last_packet
        if packet != self.packet_count:
            raise ValueError("Native capture packet count does not match its prefix")
        offset = 0
        key_ids: set[str] = set()
        for sequence, part in enumerate(self.keylog_parts, 1):
            if part.id in key_ids or part.sequence != sequence or part.offset != offset:
                raise ValueError("Native keylog prefix has a gap, overlap or duplicate")
            key_ids.add(part.id)
            offset += part.size_bytes
        if self.error is not None:
            self.error = redact_traffic_text(self.error, 2048)
        return self


class TrafficNativeSourceRef(ContractModel):
    source_id: TrafficId
    agent_id: str | None = Field(default=None, min_length=1, max_length=160)
    revision: TrafficDigest
    available_bytes: int = Field(ge=0)
    packet_count: int = Field(ge=0)
    complete: bool = False
    last_packet_at: AwareDatetime | None = None


class TrafficPacketDetail(ContractModel):
    layers: list[TrafficPacketNode] = Field(default_factory=list, max_length=512)
    raw_hex: str = Field(default="", max_length=32768, pattern=r"^(?:[0-9a-fA-F]{2})*$")
    raw_truncated: bool = False
    tree_truncated: bool = False
    decode_error: str | None = None

    @model_validator(mode="after")
    def bounded_tree(self) -> TrafficPacketDetail:
        pending = [(node, 1) for node in self.layers]
        count, text_size = 0, 0
        while pending:
            node, depth = pending.pop()
            count += 1
            text_size += sum(
                len(value.encode("utf-8"))
                for value in (
                    node.name,
                    node.label,
                    node.value or "",
                )
            )
            if count > 512 or depth > 8 or text_size > 64 * 1024:
                raise ValueError("Traffic packet tree exceeds its display limits")
            pending.extend((child, depth + 1) for child in node.children)
        if self.decode_error is not None:
            self.decode_error = redact_traffic_text(self.decode_error, 2048)
        return self


class TrafficRecordSummary(ContractModel):
    id: str = Field(pattern=r"^r-[1-9][0-9]{0,18}$")
    seq: int = Field(ge=1, le=2**63 - 1)
    timestamp: AwareDatetime
    agent_id: str | None = Field(default=None, max_length=160)
    attribution: Literal["exact", "shared", "unknown"] = "unknown"
    record_kind: Literal["packet", "http", "logical"] | None = None
    packet: TrafficPacketMeta | None = None
    protocol: str = Field(max_length=64)
    transport_protocol: str | None = Field(default=None, max_length=64)
    application_protocol: str | None = Field(default=None, max_length=64)
    source_ip: str = Field(default="", max_length=128)
    source_port: int | None = Field(default=None, ge=0, le=65535)
    destination_ip: str = Field(default="", max_length=128)
    destination_port: int | None = Field(default=None, ge=0, le=65535)
    method: str | None = Field(default=None, max_length=64)
    url: str | None = None
    status_code: int | None = Field(default=None, ge=100, le=599)
    summary: str = ""
    request_bytes: int = Field(default=0, ge=0)
    response_bytes: int = Field(default=0, ge=0)
    plaintext: Literal["decoded", "encrypted", "not_applicable"] = "not_applicable"

    @model_validator(mode="after")
    def safe_summary(self) -> TrafficRecordSummary:
        if self.id != f"r-{self.seq}":
            raise ValueError("Traffic record ID does not match its sequence")
        if (self.agent_id is not None) != (self.attribution == "exact"):
            raise ValueError("Agent attribution requires an exact identity")
        if (self.record_kind == "packet") != (self.packet is not None):
            raise ValueError("Packet records require their capture metadata")
        self.summary = redact_traffic_text(self.summary, 2048)
        if self.url is not None:
            self.url = redact_traffic_url(self.url)
        return self


class TrafficRecordDetail(TrafficRecordSummary):
    request: TrafficMessage | None = None
    response: TrafficMessage | None = None
    observation: TrafficObservation | None = None
    packet_detail: TrafficPacketDetail | None = None

    @model_validator(mode="after")
    def plaintext_only(self) -> TrafficRecordDetail:
        if self.plaintext == "encrypted" and (
            self.request is not None or self.response is not None
        ):
            raise ValueError("Encrypted traffic cannot contain plaintext messages")
        if (self.record_kind == "packet") != (self.packet_detail is not None):
            raise ValueError("Packet records require their bounded packet detail")
        if self.packet is not None and self.packet_detail is not None:
            self.packet_detail.raw_truncated |= (
                len(self.packet_detail.raw_hex) // 2 < self.packet.captured_length
            )
            if len(self.model_dump_json().encode("utf-8")) > TRAFFIC_PACKET_DETAIL_LIMIT:
                raise ValueError("Traffic packet detail exceeds 128 KiB")
        return self


class TrafficChunk(ContractModel):
    id: TrafficId
    first_seq: int = Field(ge=1)
    last_seq: int = Field(ge=1)
    count: int = Field(ge=1, le=1000)
    source_id: str | None = Field(default=None, max_length=160)
    local_chunk_id: str | None = Field(default=None, max_length=160)
    detail_storage: Literal["record", "chunk"] = "record"

    @model_validator(mode="after")
    def bounded_bundle(self) -> TrafficChunk:
        if self.detail_storage == "chunk" and self.count > 128:
            raise ValueError("Traffic detail chunks cannot exceed 128 records")
        return self


class TrafficFile(ContractModel):
    id: TrafficId
    kind: Literal["pcap"] = "pcap"
    agent_id: str | None = Field(default=None, min_length=1, max_length=160)
    source_id: str | None = Field(default=None, min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    content_type: Literal["application/vnd.tcpdump.pcap", "application/octet-stream"] = (
        "application/vnd.tcpdump.pcap"
    )


class TrafficCapture(ContractModel):
    status: TrafficStatus
    https: Literal["enabled", "unavailable"]
    started_at: AwareDatetime
    updated_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    record_count: int = Field(default=0, ge=0)
    captured_bytes: int = Field(default=0, ge=0)
    dropped_packets: int = Field(default=0, ge=0)
    preview_dropped: int = Field(default=0, ge=0)
    pending_chunks: int = Field(default=0, ge=0)
    last_packet_at: AwareDatetime | None = None
    error: str | None = None

    @model_validator(mode="after")
    def safe_error(self) -> TrafficCapture:
        if self.error is not None:
            self.error = redact_traffic_text(self.error, 2048)
        return self


class TrafficManifest(TrafficCapture):
    schema_version: Literal[1] = 1
    task_id: UUID
    run: int = Field(ge=1)
    last_seq: int = Field(default=0, ge=0)
    chunks: list[TrafficChunk] = Field(default_factory=list, max_length=100000)
    files: list[TrafficFile] = Field(default_factory=list, max_length=10000)
    native: list[TrafficNativeSourceRef] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def valid_index(self) -> TrafficManifest:
        previous = 0
        for chunk in self.chunks:
            if chunk.first_seq <= previous or chunk.last_seq < chunk.first_seq:
                raise ValueError("Traffic chunk ranges overlap or are out of order")
            if chunk.count > chunk.last_seq - chunk.first_seq + 1:
                raise ValueError("Traffic chunk count exceeds its sequence range")
            previous = chunk.last_seq
        if self.record_count != sum(chunk.count for chunk in self.chunks):
            raise ValueError("Traffic record count does not match its chunks")
        if self.last_seq == 0:
            self.last_seq = previous
        if self.last_seq < previous:
            raise ValueError("Traffic sequence checkpoint precedes its chunks")
        for ids in (
            [item.id for item in self.chunks],
            [item.id for item in self.files],
            [item.source_id for item in self.native],
        ):
            if len(set(ids)) != len(ids):
                raise ValueError("Duplicate traffic object identifier")
        return self


class TrafficListResponse(ContractModel):
    task_id: UUID
    run: int = Field(ge=1)
    availability: Literal["recorded", "not_recorded"]
    capture: TrafficCapture | None = None
    records: list[TrafficRecordSummary] = Field(default_factory=list)
    next_before: int | None = None
    snapshot_seq: int = Field(default=0, ge=0)
    next_after: int | None = Field(default=None, ge=0)
    has_more: bool = False
    files: list[TrafficFile] = Field(default_factory=list)


class TrafficNativeQueryRequest(ContractModel):
    run: int | None = Field(default=None, ge=1)
    agent_id: str | None = Field(default=None, min_length=1, max_length=160)
    display_filter: str = Field(default="", max_length=4096)


class TrafficNativeCoverage(ContractModel):
    source_id: TrafficId
    agent_id: str | None = None
    packet_count: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    complete: bool
    decryption: Literal["available", "unavailable"]
    error: str | None = None


class TrafficNativeQueryView(ContractModel):
    id: UUID
    run: int
    status: Literal["building", "ready", "partial", "error"]
    revision: str | None = None
    requested_revision: str
    display_filter: str
    engine_version: str | None = None
    coverage: list[TrafficNativeCoverage] = Field(default_factory=list)
    matched_packets: int = Field(default=0, ge=0)
    results_truncated: bool = False
    reset_required: bool = False
    error: str | None = None


class TrafficNativePacket(ContractModel):
    source_id: TrafficId
    source_number: int = Field(ge=1)
    agent_id: str | None = None
    attribution: Literal["exact", "shared", "unknown"]
    columns: list[str]
    background: str | None = None
    foreground: str | None = None


class TrafficNativePacketPage(ContractModel):
    query_id: UUID
    revision: str
    records: list[TrafficNativePacket]
    total: int
    next_offset: int | None = None


class TrafficNativeDataSource(ContractModel):
    id: int = Field(ge=0)
    name: str = Field(max_length=256)
    length: int = Field(ge=0)
    raw_hex: str = Field(max_length=32768, pattern=r"^(?:[0-9a-fA-F]{2})*$")
    truncated: bool


class TrafficNativeFrameDetail(ContractModel):
    source_id: TrafficId
    source_number: int = Field(ge=1)
    agent_id: str | None = None
    revision: str
    columns: list[str]
    layers: list[TrafficPacketNode]
    data_sources: list[TrafficNativeDataSource]
    tree_truncated: bool = False
    decode_error: str | None = None
