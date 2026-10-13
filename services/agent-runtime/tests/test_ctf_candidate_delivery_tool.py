"""The native MAF candidate tool exposes explicit work delivery."""

import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.tools import build_tools


@pytest.mark.parametrize("work_status", [None, "completed", "blocked"])
async def test_candidate_tool_forwards_explicit_work_status(work_status):
    service = SimpleNamespace(tool=AsyncMock(return_value={"id": "record"}))
    candidate = next(
        tool
        for tool in build_tools(cast(CtfClient, service), "task", {"token": "token"}, "teammate")
        if tool.name == "record_candidate"
    )
    result = await candidate.invoke(
        arguments={
            "challenge_id": "challenge",
            "expected_revision": 3,
            "status": "unknown",
            "summary": "Delivered",
            "work_status": work_status,
        }
    )
    assert json.loads(result[0].text)["id"] == "record"
    assert service.tool.call_args.kwargs["work_status"] == work_status
    assert service.tool.call_args.kwargs["status"] == "unknown"
