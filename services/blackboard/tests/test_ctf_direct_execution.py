"""Direct CTF dispatch has no network allocation or capture authority dependency."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from bbx_blackboard.sandbox_tools import SandboxTools


async def test_command_dispatch_skips_network_and_records_at_host_admission():
    service: Any = object.__new__(SandboxTools)
    binding = {"role": "worker", "execution_profile": "ctf-direct"}
    service.binding = AsyncMock(return_value=binding)
    service.traffic = SimpleNamespace(allocate=AsyncMock(side_effect=AssertionError("network")))
    service._record_command = AsyncMock(side_effect=AssertionError("premature admission"))
    conn = SimpleNamespace(execute=AsyncMock())
    call = {"id": "tool:fixture", "data": {"tool_name": "exec_command", "invocation_id": "fixture"}}
    result = await service.dispatch_claim(
        conn,
        "task",
        call,
        {"hard_deadline": datetime.now(UTC) + timedelta(minutes=5)},
        {"id": "worker"},
        {"command": "printf direct"},
    )
    assert result["binding"] == binding and "network" not in result
    assert result["arguments"]["command_id"] == "fixture"
    service.traffic.allocate.assert_not_awaited()
    service._record_command.assert_not_awaited()
    conn.execute.assert_awaited_once()
