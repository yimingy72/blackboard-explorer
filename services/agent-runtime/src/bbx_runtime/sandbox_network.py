"""Private control of a per-Assignment network in the original official Sandbox."""

import asyncio
import base64
import hashlib
import json
import re
from datetime import timedelta

from opensandbox import Sandbox
from opensandbox.models.execd import ExecutionHandlers, ExecutionInit, RunCommandOpts

CONTROL = "/opt/bbx/control"
NETWORK_ROOT = "/var/lib/bbx/control/networks"
SOURCE = re.compile(r"[a-z0-9-]{1,80}\Z")
PCAP_LIMIT = 4 * 1024 * 1024
IDENTITY_FIELDS = ("netns_inode", "host_ifindex", "capture_pid", "capture_start_ticks")
SEAL_FIELDS = IDENTITY_FIELDS + (
    "size_bytes",
    "packet_count",
    "sha256",
    "complete",
    "error",
    "dropped_packets",
    "truncated_packets",
    "discarded_tail_bytes",
)


async def root_network(sandbox: Sandbox, mode: str, source: str, data=None):
    if not SOURCE.fullmatch(source) or mode not in {
        "prepare",
        "read",
        "renew",
        "fence",
        "seal",
        "ack",
    }:
        raise ValueError("Original Sandbox network control is unavailable")
    argv = ["/usr/bin/python3", "-I", CONTROL + "/assignment-network.py", mode, source]
    if data is not None:
        argv.append(
            base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
        )
    execution_id = None

    async def initialized(event: ExecutionInit):
        nonlocal execution_id
        if not event.id or execution_id not in {None, event.id}:
            raise ValueError("Original network control execution ID changed")
        execution_id = event.id

    async with asyncio.timeout(8):
        result = await sandbox.commands.run(
            argv,
            opts=RunCommandOpts(working_directory=CONTROL, timeout=timedelta(seconds=7)),
            handlers=ExecutionHandlers(on_init=initialized),
        )
    text = "".join(item.text for item in result.logs.stdout)
    if (
        not execution_id
        or execution_id != result.id
        or result.complete is None
        or result.exit_code != 0
        or result.error is not None
        or len(text) > 4096
    ):
        raise ValueError("Original network control result is unconfirmed")
    value = json.loads(text)
    if not isinstance(value, dict) or value.get("source_id") != source:
        raise ValueError("Original network control source changed")
    return value


def ready_identity(value):
    return {
        "netns_inode": str(value["netns_inode"]),
        "host_ifindex": value["veth_ifindex"],
        "capture_pid": value["capture_pid"],
        "capture_start_ticks": str(value["capture_start_ticks"]),
    }


async def sealed_bytes(sandbox: Sandbox, source, seal):
    size = seal["size_bytes"]
    if not SOURCE.fullmatch(source) or type(size) is not int or not 24 <= size <= PCAP_LIMIT:
        raise ValueError("Original capture length is not bounded")
    data = bytearray()
    async with asyncio.timeout(8):
        stream = await sandbox.files.read_bytes_stream(
            f"{NETWORK_ROOT}/{source}/sealed.pcap", chunk_size=8192
        )
        try:
            async for chunk in stream:
                if len(data) + len(chunk) > size:
                    raise ValueError("Original sealed capture length changed")
                data.extend(chunk)
        finally:
            await stream.aclose()
    if len(data) != size or hashlib.sha256(data).hexdigest() != seal["sha256"]:
        raise ValueError("Original sealed capture digest changed")
    return bytes(data)
