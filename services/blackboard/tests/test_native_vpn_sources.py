"""Keep untrusted OpenVPN key validation off the control process."""

import asyncio
import os
import time
from pathlib import Path

import pytest
from anyio import CapacityLimiter
from bbx_blackboard import native_vpn_sources


def _blocked_parser(raw):
    marker = Path(os.fsdecode(raw))
    marker.write_text(str(os.getpid()))
    time.sleep(60)


def _worker_pid(raw):
    del raw
    return os.getpid()


async def test_private_parser_isolated_and_killed_on_timeout(monkeypatch, tmp_path):
    marker = tmp_path / "worker-pid"
    limiter = CapacityLimiter(1)
    monkeypatch.setattr(native_vpn_sources, "parse_vpn_configuration", _blocked_parser)
    parse = native_vpn_sources.parse_private_configuration
    started = asyncio.create_task(parse(os.fsencode(marker), limiter, timeout=2))
    async with asyncio.timeout(1.8):
        while not marker.exists():
            await asyncio.sleep(0.02)
    pid = int(marker.read_text())
    assert pid != os.getpid() and limiter.borrowed_tokens == 1
    with pytest.raises(TimeoutError):
        await started
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    monkeypatch.setattr(native_vpn_sources, "parse_vpn_configuration", _worker_pid)
    assert await parse(b"", limiter) != pid
