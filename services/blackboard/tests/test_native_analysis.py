"""Native archive trust boundaries and query revisions without invoking a real decoder."""

import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.auth import issue_agent_token
from bbx_blackboard.native import (
    NativeAnalysisCache,
    NativeFilterError,
    NativeUnavailable,
    NativeWorker,
    create_cache_instance,
    native_detail,
    process_birth,
)
from bbx_contracts.traffic import TrafficNativeSourceIndex, TrafficNativeSourceRef
from test_api_auth import settings


class Objects:
    def __init__(self):
        self.data = {}
        self.reads = []

    async def exists(self, uri: str) -> bool:
        return uri in self.data

    async def stream(self, uri: str):
        self.reads.append(uri)
        yield self.data[uri]


class FakeWorker:
    def __init__(self):
        self.process = SimpleNamespace(returncode=None)
        self.version = "fake-for-unit-tests"
        self.rows = []

    async def rpc(self, method, params, timeout=10):
        if method == "load":
            self.rows = json.loads(Path(params["file"]).read_bytes())
            return {"status": "OK"}
        if method == "status":
            return {"frames": len(self.rows)}
        if method == "frame":
            return {"bytes": base64.b64encode(b"original").decode(), "tree": []}
        return {}

    async def check(self, expression):
        if expression == "invalid!":
            raise NativeFilterError("Native syntax error")

    async def frames(self, expression, skip, limit):
        return self.rows[skip : skip + limit]

    async def close(self):
        self.process.returncode = 0


async def worker_factory(_home):
    return cast(NativeWorker, FakeWorker())


def publish(objects, tid, source="source-a", agent="lead", numbers=(1, 2)):
    rows = [
        {
            "num": number,
            "c": [str(number), str(100 + number), "192.0.2.1", "192.0.2.2", "UDP", "42", "test"],
        }
        for number in numbers
    ]
    content = json.dumps(rows).encode()
    digest = hashlib.sha256(content).hexdigest()
    index = TrafficNativeSourceIndex.model_validate(
        dict(
            task_id=str(tid),
            run=1,
            source_id=source,
            agent_id=agent,
            complete=True,
            packet_count=len(rows),
            parts=[
                dict(
                    id="part-" + digest[:12],
                    segment_id="segment-1",
                    sequence=1,
                    offset=0,
                    size_bytes=len(content),
                    sha256=digest,
                    first_packet=1,
                    last_packet=len(rows),
                )
            ],
        )
    )
    body = index.model_dump_json().encode()
    revision = hashlib.sha256(body).hexdigest()
    prefix = f"traffic/{tid}/run-1/native/{source}/"
    objects.data[prefix + f"indexes/{revision}.json"] = body
    objects.data[prefix + f"parts/{index.parts[0].id}.bin"] = content
    return TrafficNativeSourceRef(
        source_id=source,
        agent_id=agent,
        revision=revision,
        available_bytes=len(content),
        packet_count=len(rows),
        complete=True,
    )


@pytest.fixture
async def native_app(monkeypatch):
    tid = uuid4()
    objects = Objects()
    ref = publish(objects, tid)
    stamp = "2026-10-15T00:00:00Z"
    manifest = dict(
        task_id=str(tid),
        run=1,
        status="complete",
        https="enabled",
        started_at=stamp,
        updated_at=stamp,
        native=[ref.model_dump(mode="json")],
    )
    objects.data[f"traffic/{tid}/run-1/manifest.json"] = json.dumps(manifest).encode()
    app = api.create_app(settings(), objects=objects)
    app.state.native_cache = NativeAnalysisCache(worker_factory)
    monkeypatch.setattr(api, "_task", lambda *_args: None)

    async def task(_request, requested):
        if requested != tid:
            from fastapi import HTTPException

            raise HTTPException(404, "Task not found")
        return {"id": tid, "run_number": 2}

    monkeypatch.setattr(api, "_task", task)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test", trust_env=False
    ) as client:
        yield SimpleNamespace(
            tid=tid,
            ref=ref,
            objects=objects,
            app=app,
            client=client,
            base=f"/api/tasks/{tid}/traffic/native",
            headers={"Authorization": "Bearer service-test"},
        )
    await app.state.native_cache.close()


async def wait_ready(native, query_id):
    query = native.app.state.native_cache.query(query_id, native.tid)
    await query.future
    return query


async def test_auth_native_check_and_queries_are_task_scoped(native_app):
    n = native_app
    check = n.base + "/check"
    assert (await n.client.post(check, json={"display_filter": "udp"})).status_code == 401
    token = issue_agent_token(settings(), n.tid, "lead")
    assert (
        await n.client.post(
            check, json={"display_filter": "udp"}, headers={"Authorization": "Bearer " + token}
        )
    ).status_code == 403
    assert (await n.client.get(check, headers=n.headers)).status_code in {404, 405}
    good = await n.client.post(check, json={"display_filter": "udp"}, headers=n.headers)
    assert good.status_code == 200 and good.json()["valid"]
    bad = await n.client.post(check, json={"display_filter": "invalid!"}, headers=n.headers)
    assert bad.status_code == 422 and "Native syntax error" in bad.text
    result = await n.client.post(n.base + "/queries", json={"run": 1}, headers=n.headers)
    assert result.status_code == 202
    from uuid import UUID

    query = await wait_ready(n, UUID(result.json()["id"]))
    assert query.status == "ready" and len(query.records) == 2
    page = await n.client.get(
        n.base + f"/queries/{query.id}/packets",
        params={"revision": query.revision},
        headers=n.headers,
    )
    assert page.status_code == 200 and page.json()["records"][0]["source_number"] == 1
    assert page.headers["cache-control"] == "no-store"
    stale = await n.client.get(
        n.base + f"/queries/{query.id}/packets", params={"revision": "0" * 64}, headers=n.headers
    )
    assert stale.status_code == 409
    other = n.base.replace(str(n.tid), str(uuid4()))
    assert (
        await n.client.get(other + f"/queries/{query.id}", headers=n.headers)
    ).status_code == 404
    assert (await n.client.get(n.base + f"/queries/{query.id}")).status_code == 401
    frame = await n.client.get(
        n.base + f"/queries/{query.id}/sources/source-a/frames/1",
        params={"revision": query.revision},
        headers=n.headers,
    )
    assert frame.status_code == 200
    assert frame.json()["data_sources"][0]["raw_hex"] == b"original".hex()
    assert (
        await n.client.get(
            n.base + f"/queries/{query.id}/sources/foreign/frames/1",
            params={"revision": query.revision},
            headers=n.headers,
        )
    ).status_code == 404


async def test_index_pin_survives_gc_and_revisions_reuse_immutable_parts(native_app):
    n = native_app
    cache = n.app.state.native_cache
    query = cache.create(n.tid, 1, "", None, n.objects)
    await cache.refresh(query, [n.ref])
    old_index = f"traffic/{n.tid}/run-1/native/source-a/indexes/{n.ref.revision}.json"
    del n.objects.data[old_index]
    await query.future
    assert query.status == "ready"
    before = sum("/parts/" in key for key in n.objects.reads)
    await cache.invalidate_task(n.tid)
    assert not cache.sessions and not cache.parts
    # Recreating a query requires the authoritative index again, not an assumed empty capture.
    fresh = cache.create(n.tid, 1, "", None, n.objects)
    with pytest.raises(NativeUnavailable):
        await cache.refresh(fresh, [n.ref])
    assert before == 1


async def test_new_revision_keeps_committed_result_until_ready_and_reuses_downloaded_parts(
    native_app,
):
    n = native_app
    cache = n.app.state.native_cache
    query = cache.create(n.tid, 1, "", None, n.objects)
    await cache.refresh(query, [n.ref])
    await query.future
    old_revision = query.revision
    updated = query.indices["source-a"].model_dump(mode="json")
    updated["complete"] = False
    body = json.dumps(updated).encode()
    digest = hashlib.sha256(body).hexdigest()
    n.objects.data[f"traffic/{n.tid}/run-1/native/source-a/indexes/{digest}.json"] = body
    ref = n.ref.model_copy(update={"revision": digest, "complete": False})
    query.last_build = 0
    await cache.refresh(query, [ref])
    assert query.status == "building" and query.revision == old_revision
    assert len(query.records) == 2 and not query.view(old_revision).reset_required
    await query.future
    assert query.status == "ready" and query.revision != old_revision
    assert query.view(old_revision).reset_required
    assert sum("/parts/" in key for key in n.objects.reads) == 1


async def test_source_isolation_order_and_filter_scope_do_not_use_preview_sequences(native_app):
    n = native_app
    second = publish(n.objects, n.tid, "source-b", "member-2")
    cache = n.app.state.native_cache
    query = cache.create(n.tid, 1, "", None, n.objects)
    await cache.refresh(query, [second, n.ref])
    await query.future
    assert [(row.source_id, row.source_number) for row in query.records] == [
        ("source-a", 1),
        ("source-b", 1),
        ("source-a", 2),
        ("source-b", 2),
    ]
    scoped = cache.create(n.tid, 1, "", "member-2", n.objects)
    await cache.refresh(scoped, [second, n.ref])
    await scoped.future
    assert {row.agent_id for row in scoped.records} == {"member-2"}
    assert query.view("0" * 64).reset_required


async def test_missing_native_part_is_error_and_never_successful_empty_result(native_app):
    n = native_app
    for key in list(n.objects.data):
        if "/parts/" in key:
            del n.objects.data[key]
    query = n.app.state.native_cache.create(n.tid, 1, "", None, n.objects)
    await n.app.state.native_cache.refresh(query, [n.ref])
    await query.future
    assert query.status == "error" and query.revision is None
    assert "resource limit" in query.error


def test_native_tree_has_real_data_sources_and_never_exposes_secret_match_expressions():
    ref = TrafficNativeSourceRef(
        source_id="source-a", revision="a" * 64, available_bytes=1, packet_count=1, complete=True
    )
    result = native_detail(
        dict(
            bytes=base64.b64encode(b"wire").decode(),
            ds=[dict(name="Decrypted TLS", bytes=base64.b64encode(b"clear").decode())],
            tree=[
                dict(
                    fn="http.authorization",
                    l="Authorization: private",
                    f='http.authorization == "private"',
                    ds=1,
                    h=[0, 5],
                ),
                dict(
                    fn="http.request.method",
                    l="Request Method: GET",
                    ds=1,
                    h=[0, 3],
                    f='http.request.method == "GET"',
                ),
            ],
        ),
        ["1", "100", "a", "b", "HTTP", "4", "test"],
        ref,
        1,
        "r",
    )
    assert result.layers[0].filter_expr is None and "private" not in result.model_dump_json()
    assert result.layers[1].data_source_id == 1
    assert result.layers[1].filter_expr == 'http.request.method == "GET"'
    assert result.data_sources[0].raw_hex == b"wire".hex()
    assert result.data_sources[1].raw_hex == b"clear".hex()
    for field, label in (
        ("http.authorization", "Authorization: synthetic-private"),
        ("http.cookie", "Cookie: synthetic-private"),
        ("http.proxy_authorization", "Proxy-Authorization: synthetic-private"),
    ):
        missing_name = native_detail(
            dict(bytes="", tree=[dict(l=label, h=[0, 1], f=f'{field} == "synthetic-private"')]),
            [],
            ref,
            1,
            "r",
        )
        assert missing_name.layers[0].filter_expr is None
        assert "synthetic-private" not in missing_name.model_dump_json()


async def test_wrong_digest_or_discontinuous_index_is_rejected_before_worker(native_app):
    n = native_app
    key = f"traffic/{n.tid}/run-1/native/source-a/indexes/{n.ref.revision}.json"
    n.objects.data[key] = b"{}"
    query = n.app.state.native_cache.create(n.tid, 1, "", None, n.objects)
    with pytest.raises(NativeUnavailable, match="digest"):
        await n.app.state.native_cache.refresh(query, [n.ref])


def test_startup_reaps_only_dead_service_cache_instances(tmp_path, monkeypatch):
    import bbx_blackboard.native as module

    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))
    base = tmp_path / f"bbx-native-cache-{os.geteuid()}"
    base.mkdir()
    dead = base / "instance-dead"
    alive = base / "instance-alive"
    dead.mkdir()
    alive.mkdir()
    (dead / "owner.json").write_text(json.dumps({"pid": 2**30, "birth": "old"}))
    (dead / "private-keylog").write_text("synthetic secret")
    (alive / "owner.json").write_text(
        json.dumps({"pid": os.getpid(), "birth": process_birth(os.getpid())})
    )
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (base / "instance-link").symlink_to(outside, target_is_directory=True)
    current = create_cache_instance()
    assert current.parent == base and current.exists()
    assert not dead.exists() and alive.exists() and outside.exists()
