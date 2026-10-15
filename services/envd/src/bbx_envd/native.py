"""Byte-preserving PCAPNG framing and private immutable native-analysis parts.

Only the capture container framing is inspected; Wireshark owns protocol decoding.
"""

import hashlib
import json
import os
import re
import stat
import struct
import uuid
from pathlib import Path
from typing import Any

PART_BYTES = 1024 * 1024
MAX_BLOCK_BYTES = 4 * 1024 * 1024
KEYLOG_BYTES = 16 * 1024 * 1024
SAFE = re.compile(r"[a-zA-Z0-9_-]{1,80}\Z")
SHB = b"\x0a\x0d\x0d\x0a"


def complete_blocks(data: bytes, endian: str | None) -> tuple[int, str | None, int]:
    """Return the complete validated prefix, byte order and packet-block count."""
    offset, packets = 0, 0
    while len(data) - offset >= 12:
        header = data[offset : offset + 12]
        order = endian
        if header[:4] == SHB:
            if header[8:12] == b"\x4d\x3c\x2b\x1a":
                order = "<"
            elif header[8:12] == b"\x1a\x2b\x3c\x4d":
                order = ">"
            else:
                raise ValueError("Invalid PCAPNG section byte order")
        if order is None:
            raise ValueError("PCAPNG source does not begin with a section")
        kind, size = struct.unpack(order + "II", header[:8])
        minimum = 28 if header[:4] == SHB else 12
        if size < minimum or size % 4 or size > MAX_BLOCK_BYTES:
            raise ValueError("Invalid or oversized PCAPNG block")
        if offset + size > len(data):
            break
        if struct.unpack_from(order + "I", data, offset + size - 4)[0] != size:
            raise ValueError("PCAPNG block trailer does not match")
        if offset and offset + size > PART_BYTES:
            break
        endian = order
        offset += size
        packets += kind in {2, 3, 6}
        if offset >= PART_BYTES:
            break
    return offset, endian, packets


class NativeParts:
    def __init__(self, directory: Path, task_id: str, run: int, max_bytes: int) -> None:
        self.directory = directory
        self.task_id, self.run, self.max_bytes = task_id, run, max_bytes
        self.sources: dict[str, dict[str, Any]] = {}
        self.segments: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.keylog_parts: list[dict[str, Any]] = []
        self.keylog_offset = 0
        self.error: str | None = None
        self.path = directory / "native-index.json"
        if self.path.exists():
            with os.fdopen(self.open_file(self.path.name)) as source:
                saved = json.load(source)
            for key in ("sources", "segments", "pending", "keylog_parts", "keylog_offset", "error"):
                setattr(self, key, saved[key])

    def save(self) -> None:
        content = {
            key: getattr(self, key)
            for key in ("sources", "segments", "pending", "keylog_parts", "keylog_offset", "error")
        }
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as target:
            json.dump(content, target, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(self.path)

    def open_file(self, name: str) -> int:
        if Path(name).name != name:
            raise ValueError("Invalid native filename")
        fd = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("Native input is not a regular file")
        return fd

    def add_segment(self, source_id: str, owner: str | None, name: str) -> None:
        if not SAFE.fullmatch(source_id) or Path(name).name != name:
            raise ValueError("Invalid native capture identity")
        self.sources.setdefault(
            source_id,
            {
                "schema_version": 1,
                "task_id": self.task_id,
                "run": self.run,
                "source_id": source_id,
                "agent_id": owner,
                "complete": False,
                "error": None,
                "packet_count": 0,
                "last_packet_at": None,
                "parts": [],
            },
        )
        if name not in self.segments:
            self.segments[name] = {
                "id": "s-" + uuid.uuid4().hex,
                "source_id": source_id,
                "offset": 0,
                "endian": None,
                "closed": False,
            }
            self.save()

    def publish(
        self, content: bytes, kind: str, source_id: str | None, descriptor: dict[str, Any]
    ) -> None:
        pending_bytes = sum(item["descriptor"]["size_bytes"] for item in self.pending.values())
        if len(self.pending) >= 4096:
            raise ValueError("Native staging part limit reached; archive acknowledgment required")
        if pending_bytes + len(content) > self.max_bytes:
            raise ValueError("Native staging capacity reached; archive acknowledgment required")
        identifier = "n-" + uuid.uuid4().hex
        descriptor.update(
            id=identifier, size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest()
        )
        fd = os.open(
            self.directory / (identifier + ".bin"),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        self.pending[identifier] = {
            "id": identifier,
            "kind": kind,
            "source_id": source_id,
            "descriptor": descriptor,
        }

    def drain_segment(self, name: str, *, final: bool = False) -> None:
        segment = self.segments[name]
        source = self.sources[segment["source_id"]]
        try:
            fd = self.open_file(name)
        except FileNotFoundError:
            # Closed, fully published source files may already have been ACKed.
            if segment["closed"]:
                return
            raise
        with os.fdopen(fd, "rb") as raw:
            raw.seek(segment["offset"])
            # One part per source per tick; final drain consumes the bounded file fully.
            while True:
                content = raw.read(MAX_BLOCK_BYTES)
                size, endian, count = complete_blocks(content, segment["endian"])
                if not size:
                    if final and content:
                        raise ValueError("Interrupted PCAPNG block; native prefix is incomplete")
                    break
                first = source["packet_count"] + 1 if count else None
                if len(source["parts"]) >= 65536:
                    raise ValueError("Native source part capacity reached")
                descriptor = {
                    "segment_id": segment["id"],
                    "sequence": len(source["parts"]) + 1,
                    "offset": segment["offset"],
                    "first_packet": first,
                    "last_packet": source["packet_count"] + count if count else None,
                }
                self.publish(content[:size], "pcapng", source["source_id"], descriptor)
                source["parts"].append(descriptor)
                source["packet_count"] += count
                segment["offset"] += size
                segment["endian"] = endian
                self.save()
                if not final:
                    break
                raw.seek(segment["offset"])
        if final:
            segment["closed"] = True
            self.save()

    def drain_keylog(self) -> None:
        try:
            fd = self.open_file("tls.keys")
        except FileNotFoundError:
            return
        with os.fdopen(fd, "rb") as source:
            if os.fstat(fd).st_size > KEYLOG_BYTES:
                raise ValueError("Private TLS keylog capacity reached")
            source.seek(self.keylog_offset)
            content = source.read(PART_BYTES)
        end = content.rfind(b"\n") + 1
        if not end:
            return
        content = content[:end]
        descriptor = {"sequence": len(self.keylog_parts) + 1, "offset": self.keylog_offset}
        if len(self.keylog_parts) >= 65536:
            raise ValueError("Private TLS keylog part capacity reached")
        self.publish(content, "keylog", None, descriptor)
        self.keylog_parts.append(descriptor)
        self.keylog_offset += len(content)
        self.save()

    def drain(self, *, final: bool = False, error: str | None = None) -> None:
        try:
            self.drain_keylog()
            for name in self.segments:
                if not self.segments[name]["closed"]:
                    self.drain_segment(name, final=final)
            if final:
                while self.keylog_offset < self.keylog_size():
                    previous = self.keylog_offset
                    self.drain_keylog()
                    if previous == self.keylog_offset:
                        raise ValueError("Interrupted private TLS keylog line")
        except (ValueError, OSError) as exc:
            self.error = str(exc)[:512]
        if error:
            self.error = error
        if self.error:
            for source in self.sources.values():
                source["error"] = self.error
        if final:
            for source in self.sources.values():
                source.update(complete=self.error is None, error=self.error)
        self.save()

    def keylog_size(self) -> int:
        try:
            return (self.directory / "tls.keys").stat().st_size
        except FileNotFoundError:
            return 0

    def listing(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "sources": [
                {**source, "keylog_parts": self.keylog_parts} for source in self.sources.values()
            ],
            "pending": list(self.pending.values()),
            "error": self.error,
        }

    def open_part(self, identifier: str) -> int:
        if not SAFE.fullmatch(identifier) or identifier not in self.pending:
            raise FileNotFoundError("Native part unavailable")
        return self.open_file(identifier + ".bin")

    def ack(self, identifiers: list[str]) -> None:
        if any(not isinstance(value, str) or not SAFE.fullmatch(value) for value in identifiers):
            raise ValueError("Invalid native part ID")
        for identifier in identifiers:
            if identifier not in self.pending:
                continue
            (self.directory / (identifier + ".bin")).unlink(missing_ok=True)
            self.pending.pop(identifier, None)
        self.save()
