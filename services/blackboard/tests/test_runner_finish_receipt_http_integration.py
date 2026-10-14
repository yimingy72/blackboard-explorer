"""Ordinary receipt compensation crosses real PG/HTTP without generic timeout replacement."""

from unittest.mock import Mock

import httpx
import pytest
from agent_framework import AgentSession
from bbx_runtime.runner import AgentRunner
from bbx_runtime.settings import Settings
from test_final_receipt_fence_integration import finish_http as finish_http
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url
from test_store_integration import task

pytestmark = pytest.mark.integration


async def test_saved_receipt_compensates_uncommitted_finish_and_reads_committed_loss_once(
    finish_http, monkeypatch
):
    service, board, http, _ = finish_http
    tid = await task(service)
    aid = await service.register_agent(tid, "explore")
    receipt = {"accepted": True, "data": {"result": "Synthetic saved result"}}
    proof = {
        "task_id": str(tid),
        "agent_id": aid,
        "run_id": aid,
        "run_number": 1,
        "derive_round": None,
        "task_type": "explore",
        "receipt": receipt,
        "end_reason": "normal",
    }
    session = AgentSession()
    session.state["bbx_final_receipt"] = proof
    await board.put_agent_session(
        tid,
        aid,
        session=session.to_dict(),
        opening_instructions="synthetic",
        origin="native",
        expected_revision=0,
    )
    send = http._transport.handle_async_request
    writes = 0

    async def interrupted_finish(request):
        nonlocal writes
        if request.url.path.endswith("/finish"):
            writes += 1
            if writes == 1:
                raise httpx.RemoteProtocolError(
                    "synthetic finish did not reach server", request=request
                )
            response = await send(request)
            assert response.status_code == 200
            await response.aclose()
            raise httpx.RemoteProtocolError(
                "synthetic committed finish lost response", request=request
            )
        return await send(request)

    monkeypatch.setattr(http._transport, "handle_async_request", interrupted_finish)
    runner = AgentRunner(Settings.model_construct(), board, Mock(), Mock())
    assert await runner._finish_receipt(str(tid), aid, proof)
    assert writes == 2
    stored = await board.state(tid)
    assert stored["agents"][aid]["receipt"] == receipt
    assert stored["agents"][aid]["end_reason"] == "normal"
    assert await runner.reconcile_final_receipt(
        str(tid), aid, stored["agents"][aid], stored["task"]
    )
    assert writes == 2
    assert (await board.get_agent_session(tid, aid))["session"]["state"]["bbx_final_receipt"][
        "receipt"
    ] == receipt
