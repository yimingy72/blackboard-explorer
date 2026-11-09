import logging

import admin_ui_proxy
import httpx
import pytest
from admin_ui_proxy import AdminUI
from starlette.applications import Starlette
from starlette.routing import Route

PUBLIC = "http://browser.test"
NATIVE = "https://native.test"
PATH = "/api/config/resources/llm.provider"
SYNTHETIC_SECRET = "fixture-key-must-not-appear-in-log"
STORED = {
    "id": "fixture",
    "revision": 1,
    "value": {"name": "fixture", "apiKey": SYNTHETIC_SECRET},
    "createdAt": "2026-11-09T00:00:00Z",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "status", "reason"),
    [
        ("temporary", 200, None),
        ("down", 503, "runtime_unavailable"),
        ("auth", 503, "runtime_rejected"),
        ("invalid", 503, "invalid_readback"),
        ("invalid_write_receipt", 503, "invalid_write_receipt"),
        ("invalid_read_receipt", 503, "invalid_readback"),
        ("conflict", 503, "resource_conflict"),
        ("unknown_write", 503, "unconfirmed_response"),
        ("unknown_rollout", 503, "unconfirmed_same_store"),
    ],
)
async def test_provider_confirmation_only_retries_reads(monkeypatch, caplog, fault, status, reason):
    monkeypatch.setattr(admin_ui_proxy, "READBACK_SECONDS", 0.1)
    monkeypatch.setattr(admin_ui_proxy, "READBACK_INTERVAL", 0.005)
    caplog.set_level(logging.WARNING, logger=admin_ui_proxy.__name__)
    writes, restarts, readbacks, preflight = 0, 0, 0, 0
    fences = []

    async def restart(_http, _request_id):
        nonlocal restarts
        restarts += 1
        if fault == "unknown_rollout":
            raise httpx.ReadTimeout(SYNTHETIC_SECRET)

    async def native(request):
        nonlocal writes, readbacks, preflight
        if request.url.host != "native.test":
            body = admin_ui_proxy.GatewayMaintenanceFence.model_validate_json(request.content)
            fences.append((body.site_id, body.request_id, body.action))
            return httpx.Response(
                200,
                json={
                    "site_id": body.site_id,
                    "request_id": str(body.request_id),
                    "state": "idle" if body.action == "prepare" else "released",
                },
            )
        if request.method == "PUT":
            writes += 1
            if fault == "unknown_write":
                raise httpx.ReadTimeout(SYNTHETIC_SECRET, request=request)
            item = {**STORED, "revision": None} if fault == "invalid_write_receipt" else STORED
            return httpx.Response(200, json={"resources": [item]})
        assert request.method == "GET"
        if request.url.path == "/api/runtime":
            if restarts:
                assert "if-none-match" not in request.headers
                readbacks += 1
                if fault == "down":
                    return httpx.Response(503)
                if fault == "auth":
                    return httpx.Response(401)
                if fault == "invalid":
                    return httpx.Response(200, json={"bad": SYNTHETIC_SECRET})
                if fault == "temporary" and readbacks == 1:
                    raise httpx.ConnectError(SYNTHETIC_SECRET, request=request)
                if fault == "temporary" and readbacks == 2:
                    return httpx.Response(200, json={"configReload": {"synchronized": False}})
            else:
                preflight += 1
            return httpx.Response(200, json={"configReload": {"synchronized": True}})
        assert request.url.path == PATH
        if not writes or fault == "temporary" and readbacks == 3:
            return httpx.Response(200, json={"resources": []})
        value = (
            {**STORED, "revision": None}
            if fault == "invalid_read_receipt"
            else {**STORED, "revision": 2}
            if fault == "conflict"
            else STORED
        )
        return httpx.Response(200, json={"resources": [value]})

    proxy = AdminUI(
        NATIVE,
        PUBLIC,
        {"legacy": "http://legacy.test", "native": "http://secondary.test"},
        "x" * 32,
        restart,
    )
    app = Starlette(routes=[Route("/{path:path}", proxy.handle, methods=["PUT"])])
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(native)) as upstream,
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=PUBLIC) as browser,
    ):
        app.state.http = app.state.kube = upstream
        response = await browser.put(
            PATH,
            headers={"origin": PUBLIC, "if-none-match": '"browser-etag"'},
            json={"resources": [STORED]},
        )

    assert response.status_code == status
    assert writes == 1 and preflight == 1
    assert restarts == (0 if fault == "unknown_write" else 1)
    assert len({request_id for _, request_id, _ in fences}) == 1
    assert {site for site, _, action in fences if action == "prepare"} == {"legacy", "native"}
    assert SYNTHETIC_SECRET not in caplog.text
    if fault == "temporary":
        assert readbacks == 4
        assert response.json() == {"resources": [STORED]}
        assert {site for site, _, action in fences if action == "release"} == {"legacy", "native"}
    else:
        assert not any(action == "release" for _, _, action in fences)
        assert response.headers.get("x-bbx-maintenance-id")
        assert f"reason={reason}" in caplog.text
        assert SYNTHETIC_SECRET not in response.text
        if fault in {"auth", "invalid", "invalid_read_receipt", "conflict"}:
            assert readbacks == 1
        if fault == "down":
            assert readbacks > 1
