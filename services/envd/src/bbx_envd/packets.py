"""Bounded display records from official TShark EK packet documents."""

import json
import re
from datetime import UTC, datetime
from typing import Any

from bbx_envd.traffic_format import SENSITIVE, redact_text

RAW_BYTES = 16 * 1024
TREE_BYTES = 64 * 1024
TREE_NODES = 512
TREE_DEPTH = 8


def raw_protocol(native: str, stack: str, ipv4_protocol: str, ipv6_next: str) -> str:
    transports = {"tcp": "tcp", "udp": "udp", "icmp": "icmp", "icmpv6": "icmp"}
    for layer in stack.split(":"):
        if layer in transports:
            return transports[layer]
    numbers = {"1": "icmp", "6": "tcp", "17": "udp", "58": "icmp"}
    return numbers.get(ipv4_protocol or ipv6_next, native.lower()[:32])


def native_application(native: str, transport: str) -> str | None:
    application = native.lower()[:64]
    if application.startswith("tls") or application == "ssl":
        application = "tls"
    elif application in {"http2", "http3"}:
        application = "http"
    elif application == "mdns":
        application = "dns"
    elif application in {"icmpv6", "icmp6"}:
        application = "icmp"
    return None if application in {"", "data", transport} else application


def ek_value(layers: dict[str, Any], layer: str, field: str, default: str = "") -> str:
    values = layers.get(layer, {})
    value = values.get(field, default) if isinstance(values, dict) else default
    if isinstance(value, list):
        value = value[0] if value else default
    return str(value) if value is not None else default


def packet_ifindex(document: dict[str, Any]) -> str:
    return ek_value(document.get("layers", {}), "sll", "sll_sll_ifindex")


def packet_tree(layers: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    count = 0
    remaining = TREE_BYTES
    truncated = False

    def build(name: str, value: object, depth: int) -> dict[str, Any] | None:
        nonlocal count, remaining, truncated
        if count >= TREE_NODES or depth > TREE_DEPTH or remaining < 256:
            truncated = True
            return None
        name = name[:256] or "field"
        label = redact_text(name)[:256]
        if SENSITIVE.search(name):
            value = "[redacted]"
            label = f"{name}: [redacted]"
        scalar = None
        if not isinstance(value, (dict, list)):
            scalar = "[redacted]" if SENSITIVE.search(name) else redact_text(str(value))
            encoded = scalar.encode("utf-8")
            limit = min(2048, max(0, remaining - 256))
            truncated |= len(encoded) > limit
            scalar = encoded[:limit].decode("utf-8", "ignore")
        node: dict[str, Any] = {
            "name": name,
            "label": label,
            "value": scalar,
            "offset": None,
            "length": None,
            "data_source_id": 0,
            "filter_expr": None,
            "generated": False,
            "severity": None,
            "children": [],
        }
        size = len(json.dumps(node, ensure_ascii=False).encode())
        if size > remaining:
            truncated = True
            return None
        remaining -= size
        count += 1
        items = (
            value.items()
            if isinstance(value, dict)
            else enumerate(value)
            if isinstance(value, list)
            else []
        )
        for key, child in items:
            if str(key).endswith("_raw"):
                continue
            child_node = build(str(key), child, depth + 1)
            if child_node is None:
                break
            node["children"].append(child_node)
        return node

    result = []
    for key, value in layers.items():
        if key.endswith("_raw"):
            continue
        node = build(key, value, 1)
        if node is None:
            break
        result.append(node)
    return result, truncated


def packet_record(
    document: dict[str, Any],
    source_id: str,
    number: int,
    owner: str | None,
    interface: str | None = None,
) -> dict[str, Any]:
    layers = document.get("layers")
    if not isinstance(layers, dict):
        raise ValueError("Official packet layers unavailable")
    native = str(
        document.get("protocol") or ek_value(layers, "_ws_col", "_ws_col__ws_col_protocol")
    )
    stack = ek_value(layers, "frame", "frame_frame_protocols")
    protocol = raw_protocol(
        native,
        stack,
        ek_value(layers, "ip", "ip_ip_proto"),
        ek_value(layers, "ipv6", "ipv6_ipv6_nxt"),
    )
    application = native_application(native, protocol)
    info = redact_text(
        str(document.get("info") or ek_value(layers, "_ws_col", "_ws_col__ws_col_info"))
    )[:2048]
    timestamp = ek_value(layers, "frame", "frame_frame_time_epoch")
    if not timestamp:
        timestamp = datetime.fromtimestamp(float(document["timestamp"]) / 1000, UTC).isoformat()
    elif "T" not in timestamp:
        timestamp = datetime.fromtimestamp(float(timestamp), UTC).isoformat()
    frame_length = int(ek_value(layers, "frame", "frame_frame_len", "0"))
    captured_length = int(ek_value(layers, "frame", "frame_frame_cap_len", "0"))
    native_number = int(ek_value(layers, "frame", "frame_frame_number", "0")) or None
    packet_type = ek_value(layers, "sll", "sll_sll_pkttype")
    direction = (
        "loopback"
        if interface == "lo"
        else "outbound"
        if packet_type == "4"
        else ("inbound" if packet_type in {"0", "1", "2", "3"} else "unknown")
    )
    raw = layers.get("frame_raw", "")
    error = None
    if not isinstance(raw, str) or len(raw) % 2 or not re.fullmatch(r"[a-fA-F0-9]*", raw):
        raw, error = "", "Official packet bytes unavailable"
    elif not raw and captured_length:
        error = "Official packet bytes unavailable"
    tree, tree_truncated = packet_tree(layers)
    return {
        "record_kind": "packet",
        "timestamp": timestamp,
        "agent_id": owner,
        "attribution": "exact" if owner is not None else "shared",
        "protocol": protocol,
        "transport_protocol": protocol if protocol in {"tcp", "udp", "icmp"} else None,
        "application_protocol": application,
        "source_ip": ek_value(layers, "ip", "ip_ip_src")
        or ek_value(layers, "ipv6", "ipv6_ipv6_src"),
        "destination_ip": ek_value(layers, "ip", "ip_ip_dst")
        or ek_value(layers, "ipv6", "ipv6_ipv6_dst"),
        "source_port": int(value)
        if (
            value := ek_value(layers, "tcp", "tcp_tcp_srcport")
            or ek_value(layers, "udp", "udp_udp_srcport")
        )
        else None,
        "destination_port": int(value)
        if (
            value := ek_value(layers, "tcp", "tcp_tcp_dstport")
            or ek_value(layers, "udp", "udp_udp_dstport")
        )
        else None,
        "method": None,
        "url": None,
        "status_code": None,
        "summary": info,
        "request_bytes": frame_length,
        "response_bytes": 0,
        "plaintext": (
            "decoded"
            if "tls" in layers and "http" in layers
            else "encrypted"
            if application in {"tls", "quic"}
            else "not_applicable"
        ),
        "request": None,
        "response": None,
        "packet": {
            "source_id": source_id,
            "number": number,
            "native_frame_number": native_number,
            "frame_length": frame_length,
            "captured_length": captured_length,
            "direction": direction,
            "interface": interface,
            "info": info,
        },
        "packet_detail": {
            "layers": tree,
            "raw_hex": raw[: RAW_BYTES * 2],
            "raw_truncated": len(raw) > RAW_BYTES * 2,
            "tree_truncated": tree_truncated,
            "decode_error": error,
        },
    }
