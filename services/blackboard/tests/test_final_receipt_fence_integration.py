"""Final receipt compensation uses real service HTTP and task-lock writer identity."""

from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_agent_token
from bbx_blackboard.settings import Settings
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from pydantic import SecretStr
from test_store_integration import FakeObjects, task
from test_store_integration import board_service as board_service_fixture
from test_store_integration import database_url as database_url_fixture

board_service = board_service_fixture
database_url = database_url_fixture
pytestmark = pytest.mark.integration


@pytest.fixture
async def finish_http(board_service):
    settings = Settings.model_construct(
        postgres_password=SecretStr("isolated-test"),
        minio_root_password=SecretStr("unused-test"),
        service_token=SecretStr("final-receipt-service"),
        agent_token_secret=SecretStr("final-receipt-isolated-test-signing-key"),
        admin_users=SecretStr("admin:test"),
    )
    app = create_app(settings, engine=board_service.repo.engine, objects=FakeObjects())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://board", trust_env=False
        ) as http:
            board = BlackboardClient("http://board", "final-receipt-service", http)
            yield board_service, board, http, settings
    finally:
        await app.state.workspace_cache.close()


async def test_committed_finish_lost_response_confirms_exact_receipt_without_repeating_effects(
    finish_http, monkeypatch
):
    service, board, http, _ = finish_http
    tid = await task(service)
    aid = await service.register_agent(tid, "explore")
    receipt = {"accepted": True, "data": {"result": "Saved arithmetic"}}
    identity = {"expected_run_number": 1, "expected_agent_run_id": aid}
    send = http._transport.handle_async_request
    dropped = False

    async def lose_finish_response(request):
        nonlocal dropped
        response = await send(request)
        if request.url.path.endswith("/finish") and not dropped:
            assert response.status_code == 200
            await response.aclose()
            dropped = True
            raise httpx.RemoteProtocolError("Scripted final receipt response loss", request=request)
        return response

    monkeypatch.setattr(http._transport, "handle_async_request", lose_finish_response)
    with pytest.raises(httpx.RemoteProtocolError):
        await board.finish_agent(tid, aid, receipt, "normal", **identity)
    stored = await board.state(tid)
    assert stored["agents"][aid]["receipt"] == receipt
    assert await board.finish_agent(tid, aid, receipt, "normal", **identity) == []
    assert await board.state(tid) == stored
    for altered_receipt, reason in (({**receipt, "accepted": 1}, "normal"), (receipt, "heartbeat")):
        with pytest.raises(RemoteError) as conflict:
            await board.finish_agent(tid, aid, altered_receipt, reason, **identity)
        assert conflict.value.status == 422 and conflict.value.code == "final_receipt_conflict"
        assert await board.state(tid) == stored
    # Existing callers without the recovery proof keep their original inactive rejection.
    with pytest.raises(RemoteError) as inactive:
        await board.finish_agent(tid, aid, receipt, "normal")
    assert inactive.value.status == 422 and inactive.value.code == "agent_inactive"


async def test_old_task_run_receipt_cannot_confirm_or_finish_after_resume(finish_http):
    service, board, _, _ = finish_http
    tid = await task(service)
    old = await service.register_agent(tid, "explore")
    receipt = {"accepted": True}
    await board.finish_agent(
        tid, old, receipt, "normal", expected_run_number=1, expected_agent_run_id=old
    )
    await service.transition(tid, "failed", reason="Synthetic restart")
    await service.record_archive(tid, f"workspace/{tid}.tar.zst", 1, "none")
    await service.record_cleanup(tid)
    await service.resume(tid, uuid4(), 0, 1)
    await service.transition(tid, "running")
    current = await service.register_agent(tid, "explore")
    before = await board.state(tid)
    assert before["task"]["run_number"] == 2
    for aid, expected_id, run_number, code in (
        (old, old, 1, "stale_task_run"),
        (current, old, 1, "stale_task_run"),
        (current, old, 2, "stale_agent_run"),
    ):
        with pytest.raises(RemoteError) as stale:
            await board.finish_agent(
                tid,
                aid,
                receipt,
                "normal",
                expected_run_number=run_number,
                expected_agent_run_id=expected_id,
            )
        assert stale.value.status == 422 and stale.value.code == code
        assert await board.state(tid) == before
    await board.finish_agent(
        tid, current, receipt, "normal", expected_run_number=2, expected_agent_run_id=current
    )
    assert (await board.state(tid))["agents"][current]["receipt"] == receipt


async def test_old_derive_receipt_cannot_cross_same_agent_reactivation(finish_http):
    service, board, _, _ = finish_http
    tid = await task(service)
    aid = await service.register_agent(tid, "derive")
    receipt = {"accepted": True, "data": {"posted": [], "excluded": []}}
    identity = {"expected_run_number": 1, "expected_agent_run_id": aid}
    await board.finish_agent(tid, aid, receipt, "normal", expected_derive_round=1, **identity)
    assert await service.register_agent(tid, "derive") == aid
    before = await board.state(tid)
    assert before["agents"][aid]["derive_round"] == 2
    with pytest.raises(RemoteError) as stale:
        await board.finish_agent(tid, aid, receipt, "normal", expected_derive_round=1, **identity)
    assert stale.value.status == 422 and stale.value.code == "stale_agent_round"
    assert await board.state(tid) == before
    await board.finish_agent(tid, aid, receipt, "normal", expected_derive_round=2, **identity)
    stored = await board.state(tid)
    assert (
        await board.finish_agent(tid, aid, receipt, "normal", expected_derive_round=2, **identity)
        == []
    )
    assert await board.state(tid) == stored


async def test_finish_recovery_proof_does_not_expand_agent_authority_or_allow_partial_identity(
    finish_http,
):
    service, board, http, settings = finish_http
    tid = await task(service)
    aid = await service.register_agent(tid, "explore")
    before = await board.state(tid)
    response = await http.post(
        f"/api/tasks/{tid}/agents/{aid}/finish",
        headers={"Authorization": f"Bearer {issue_agent_token(settings, tid, aid)}"},
        json={
            "receipt": {"accepted": True},
            "end_reason": "normal",
            "expected_run_number": 1,
            "expected_agent_run_id": aid,
        },
    )
    assert response.status_code == 403
    for identity in ({"expected_run_number": 1}, {"expected_agent_run_id": aid}):
        with pytest.raises(RemoteError) as invalid:
            await board.finish_agent(tid, aid, {"accepted": True}, "normal", **identity)
        assert invalid.value.status == 422 and invalid.value.code == "invalid_finish_identity"
        assert await board.state(tid) == before
