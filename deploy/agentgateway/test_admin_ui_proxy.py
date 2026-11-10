import hashlib
import json
import logging
from http.cookiejar import CookieJar, DefaultCookiePolicy

import admin_ui_proxy
import httpx
import pytest
from admin_ui_proxy import AdminUI, LocalizedUI
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
        ("policy_array", 200, None),
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
async def test_native_confirmation_only_retries_reads(monkeypatch, caplog, fault, status, reason):
    monkeypatch.setattr(admin_ui_proxy, "READBACK_SECONDS", 0.1)
    monkeypatch.setattr(admin_ui_proxy, "READBACK_INTERVAL", 0.005)
    caplog.set_level(logging.WARNING, logger=admin_ui_proxy.__name__)
    writes, restarts, readbacks, preflight = 0, 0, 0, 0
    fences = []
    read_path = PATH if fault != "policy_array" else "/api/config/resources/llm.policy"
    path = PATH if fault != "policy_array" else read_path + "/localRateLimit"
    stored = (
        STORED
        if fault != "policy_array"
        else {**STORED, "id": "localRateLimit", "value": [{"tokens": 10}]}
    )

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
            item = {**stored, "revision": None} if fault == "invalid_write_receipt" else stored
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
            return httpx.Response(
                200,
                json={"configReload": {"synchronized": True}, "ui": {"configStoreMode": "hybrid"}},
            )
        assert request.url.path == read_path
        if not writes or fault == "temporary" and readbacks == 3:
            return httpx.Response(200, json={"resources": []})
        value = (
            {**stored, "revision": None}
            if fault == "invalid_read_receipt"
            else {**stored, "revision": 2}
            if fault == "conflict"
            else stored
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
            path,
            headers={"origin": PUBLIC, "if-none-match": '"browser-etag"'},
            json={"resources": [stored]},
        )

    assert response.status_code == status
    assert writes == 1 and preflight == 1
    assert restarts == (0 if fault == "unknown_write" else 1)
    assert len({request_id for _, request_id, _ in fences}) == 1
    assert {site for site, _, action in fences if action == "prepare"} == {"legacy", "native"}
    assert SYNTHETIC_SECRET not in caplog.text
    if fault in {"temporary", "policy_array"}:
        assert readbacks == (4 if fault == "temporary" else 1)
        assert response.json() == {"resources": [stored]}
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


@pytest.mark.asyncio
async def test_playground_relays_only_dedicated_same_origin_chat(tmp_path, monkeypatch):
    key = "synthetic-playground-key"
    path = admin_ui_proxy.PLAYGROUND_PATH.decode()
    marker = admin_ui_proxy.PLAYGROUND_MARKER
    digest = hashlib.sha256(key.encode()).hexdigest()
    key_id = "10aaf4e2-a9ef-400b-8879-4f145210b215"
    platform_id = "97c46625-8ace-4471-8e11-e9df54af6d3d"
    platform_key = "synthetic-review-key"
    rows = [
        {
            "id": key_id,
            "value": {
                "keyHash": "sha256:" + digest,
                "metadata": {
                    "name": "operator",
                    admin_ui_proxy.KEY_ID: key_id,
                    admin_ui_proxy.KEY_OWNER: True,
                },
            },
        },
        {"id": platform_id, "value": {"key": platform_key, "metadata": {"name": "bbx-review"}}},
    ]
    data_origin = "http://native.test:4000"
    calls = []
    mode = "ok"
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/index.html").write_bytes(marker)
    (tmp_path / "assets-manifest.json").write_text(
        json.dumps(
            {
                "upstream_commit": admin_ui_proxy.UPSTREAM_UI_COMMIT,
                "files": {"index.html": hashlib.sha256(marker).hexdigest()},
                "spa_paths": ["/ui", "/ui/llm/playground"],
            }
        )
    )

    def native(request):
        if str(request.url) == NATIVE + admin_ui_proxy.KEY_PATH:
            assert admin_ui_proxy.PLAYGROUND_KEY_HEADER not in request.headers
            if request.headers.get("authorization") != "Basic fixture":
                return httpx.Response(401)
            return httpx.Response(200, json={"resources": rows})
        if str(request.url) == NATIVE + "/api/runtime":
            assert "data=session" not in request.headers.get("cookie", "")
            if request.headers.get("authorization") != "Basic fixture":
                return httpx.Response(401)
            return httpx.Response(
                200,
                json={"build": {"gitRevision": admin_ui_proxy.UPSTREAM_UI_COMMIT}},
                headers={"set-cookie": "admin=session; Path=/"},
            )
        calls.append(request)
        assert str(request.url) == data_origin + admin_ui_proxy.CHAT_PATH
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {key}"
        assert request.headers["content-type"] == "application/json"
        for header in (
            "origin",
            "cookie",
            "x-forwarded-for",
            "x-bbx-task-id",
            admin_ui_proxy.PLAYGROUND_KEY_HEADER,
        ):
            assert header not in request.headers
        assert request.content == b'{"model":"fixture"}'
        if mode == "timeout":
            raise httpx.ReadTimeout("fixture timeout")
        if mode == "redirect":
            return httpx.Response(307, headers={"location": "https://unrelated.test"})
        return httpx.Response(
            200,
            json={"result": "synthetic"},
            headers={"set-cookie": "data=session; Path=/", "www-authenticate": "Basic ignored"},
        )

    peers = {"legacy": "http://legacy.test", "native": "http://secondary.test"}
    enabled_ui = LocalizedUI(tmp_path, playground=True)
    disabled_ui = LocalizedUI(tmp_path)
    assert b'content="disabled"' in disabled_ui.index[0]
    with pytest.raises(ValueError, match="requires a verified UI"):
        AdminUI(NATIVE, PUBLIC, peers, "x" * 32, None, playground_data_url=data_origin)
    proxy = AdminUI(
        NATIVE,
        PUBLIC,
        peers,
        "x" * 32,
        None,
        localized=enabled_ui,
        playground_data_url=data_origin,
    )
    app = Starlette(routes=[Route("/{path:path}", proxy.handle, methods=["GET", "POST", "PUT"])])
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(native)) as upstream,
        httpx.AsyncClient(
            transport=httpx.MockTransport(native),
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        ) as playground_http,
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=PUBLIC) as browser,
    ):
        app.state.http, app.state.playground_http = upstream, playground_http
        assert (await browser.get("/ui/llm/playground")).status_code == 401
        page = await browser.get("/ui/llm/playground", headers={"authorization": "Basic fixture"})
        assert page.status_code == 200 and b'content="enabled"' in page.content
        calls.clear()
        key_header = admin_ui_proxy.PLAYGROUND_KEY_HEADER
        headers = {"origin": PUBLIC, "authorization": "Basic fixture", key_header: key}
        for target, additional, status in (
            (path, {"origin": "http://foreign.test"}, 403),
            (path, {"authorization": "Bearer fixture"}, 401),
            (path, {key_header: platform_key}, 401),
            (path, {key_header: "synthetic-task-key"}, 401),
            (path, {key_header: "synthetic-catalog-key"}, 401),
            (path, {"content-type": "text/plain"}, 415),
            (path + "?query=yes", {}, 403),
            (path + "/", {}, 403),
            ("/v1/chat/%63ompletions", {}, 405),
            ("/v1/responses", {}, 405),
        ):
            response = await browser.post(
                target, headers={**headers, **additional}, json={"model": "fixture"}
            )
            assert response.status_code == status
        assert (await browser.get(path, headers=headers)).status_code == 405
        assert (
            await browser.put(
                "/api/config/resources/llm.policy/authorization", headers=headers, json={}
            )
        ).status_code == 403
        assert (
            await browser.put("/api/config/resources/traffic.gateway", headers=headers, json={})
        ).status_code == 403
        async with proxy.lock:
            assert (
                await browser.post(path, headers=headers, json={"model": "fixture"})
            ).status_code == 409
        with monkeypatch.context() as small:
            small.setattr(admin_ui_proxy, "LIMIT", 1)
            assert (
                await browser.post(path, headers=headers, json={"model": "fixture"})
            ).status_code == 413
        assert calls == []
        response = await browser.post(
            path,
            headers={
                **headers,
                "cookie": "ignored",
                "x-forwarded-for": "ignored",
                "x-bbx-task-id": "ignored",
            },
            json={"model": "fixture"},
        )
        assert response.json() == {"result": "synthetic"}
        assert response.headers["cache-control"] == "no-store"
        assert "www-authenticate" not in response.headers and "set-cookie" not in response.headers
        for fault, status in (("timeout", 504), ("redirect", 502)):
            mode = fault
            response = await browser.post(path, headers=headers, json={"model": "fixture"})
            assert response.status_code == status
            assert "location" not in response.headers
        assert len(calls) == 3
        assert list(playground_http.cookies.jar) == []
        assert (
            await browser.get("/ui/llm/playground", headers={"authorization": "Basic fixture"})
        ).status_code == 200

        proxy.playground_data = None
        response = await browser.post(path, headers=headers, json={"model": "fixture"})
        assert response.status_code == 405 and len(calls) == 3


@pytest.mark.asyncio
async def test_key_guard_rejects_platform_mutation_and_credential_collision():
    native_key = "fixture-catalog-key"
    resource_id = "4208b51e-7136-4fae-b2c1-afc4d97c9b4f"
    key_path = admin_ui_proxy.KEY_PATH

    def native(request):
        assert request.method == "GET"
        if request.url.path == "/api/runtime":
            return httpx.Response(
                200,
                json={"ui": {"configStoreMode": "hybrid"}, "configReload": {"synchronized": True}},
            )
        if request.url.path == key_path:
            return httpx.Response(
                200,
                json={
                    "resources": [
                        {
                            "id": resource_id,
                            "value": {
                                "metadata": {"name": "bbx-platform-terminal-review-v1"},
                                "key": "synthetic-review-key",
                            },
                        }
                    ]
                },
            )
        assert request.url.path == "/api/config"
        return httpx.Response(
            200,
            json={
                "llm": {
                    "policies": {
                        "apiKey": {
                            "mode": "strict",
                            "keys": [{"key": native_key}],
                        }
                    }
                }
            },
        )

    proxy = AdminUI(
        NATIVE,
        PUBLIC,
        {"legacy": "http://legacy.test", "native": "http://secondary.test"},
        "x" * 32,
        None,
    )
    app = Starlette(routes=[Route("/{path:path}", proxy.handle, methods=["PUT", "DELETE"])])
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(native)) as upstream,
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=PUBLIC) as browser,
    ):
        app.state.http = upstream
        headers = {"origin": PUBLIC}
        protected = key_path + "/" + resource_id
        assert (await browser.delete(protected, headers=headers)).status_code == 403
        assert (await browser.put(protected, headers=headers, json={})).status_code == 403
        for value, status in (
            ({"key": "other", "metadata": {"name": "BBX-task-fake"}}, 422),
            ({"key": "other", "metadata": {"name": "user", admin_ui_proxy.KEY_OWNER: True}}, 422),
            ({"key": "other", "metadata": {"name": "user", "bbx_catalog_reader": True}}, 422),
            ({"key": "synthetic-review-key", "metadata": {"name": "user"}}, 409),
            (
                {
                    "keyHash": "sha256:" + hashlib.sha256(native_key.encode()).hexdigest(),
                    "metadata": {"name": "user"},
                },
                409,
            ),
        ):
            response = await browser.put(
                key_path, headers=headers, json={"resources": [{"value": value}]}
            )
            assert response.status_code == status
        native_key = "${FILE_OWNED_KEY}"
        response = await browser.put(
            key_path,
            headers=headers,
            json={"resources": [{"value": {"key": "fixture-other", "metadata": {"name": "user"}}}]},
        )
        assert response.status_code == 503
