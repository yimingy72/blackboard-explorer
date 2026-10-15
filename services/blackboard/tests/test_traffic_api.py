"""Traffic reads stay task/run scoped and never turn missing archive data into success."""

import gzip
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_blackboard.conversations import Conversations
from bbx_contracts.traffic import TrafficMessage, TrafficRecordDetail, TrafficRecordSummary
from fastapi import HTTPException
from test_api_auth import settings


class Objects:
    def __init__(self):
        self.data = {}
        self.reads = []
        self.fail = False

    async def exists(self, uri: str) -> bool:
        if self.fail:
            raise RuntimeError("password=do-not-expose")
        return uri in self.data

    async def stream(self, key):
        self.reads.append(key)
        data = self.data[key]
        for offset in range(0, len(data), 1024):
            yield data[offset : offset + 1024]

    def publish(self, task_id, run=1, *, sequences=(1, 2, 3)):
        root = f"traffic/{task_id}/run-{run}/"
        stamp = datetime.now(UTC)
        now = stamp.isoformat()
        records = []
        for seq in sequences:
            detail = TrafficRecordDetail(
                id=f"r-{seq}",
                seq=seq,
                timestamp=stamp,
                protocol="https" if seq % 2 else "tool",
                agent_id="a" if seq % 2 else "b",
                attribution="exact",
                summary=f"request {seq}",
                plaintext="decoded",
                url=f"https://test.local/{seq}?token=hidden",
                request=TrafficMessage(body_preview="password=hidden", body_encoding="utf-8"),
            )
            self.data[root + f"records/r-{seq}.json.gz"] = gzip.compress(
                detail.model_dump_json().encode()
            )
            records.append(
                detail.model_dump(mode="json", include=set(TrafficRecordSummary.model_fields))
            )
        chunks = []
        for index in range(0, len(records), 2):
            part = records[index : index + 2]
            name = f"chunk-{index}"
            self.data[root + f"chunks/{name}.json.gz"] = gzip.compress(json.dumps(part).encode())
            chunks.append(
                dict(id=name, first_seq=part[0]["seq"], last_seq=part[-1]["seq"], count=len(part))
            )
        manifest = dict(
            schema_version=1,
            task_id=str(task_id),
            run=run,
            status="archived",
            https="enabled",
            started_at=now,
            updated_at=now,
            record_count=len(records),
            chunks=chunks,
            files=[dict(id="raw", name="任务.pcap", kind="pcap", size_bytes=8)],
        )
        self.data[root + "manifest.json"] = json.dumps(manifest).encode()
        self.data[root + "pcap/raw"] = b"raw-pcap"
        return root


@pytest.fixture
async def traffic(monkeypatch):
    tid = uuid4()
    config = settings()
    objects = Objects()
    root = objects.publish(tid)
    app = api.create_app(config, objects=objects)

    async def task(_request, task_id):
        if task_id != tid:
            raise HTTPException(404, "Task not found")
        return {"id": tid, "mode": "blackboard", "run_number": 2}

    monkeypatch.setattr(api, "_task", task)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test", trust_env=False
    ) as client:
        yield SimpleNamespace(
            tid=tid,
            root=root,
            config=config,
            objects=objects,
            app=app,
            client=client,
            path=f"/api/tasks/{tid}/traffic",
            headers={"Authorization": f"Bearer {issue_user_token(config, 'alice')}"},
        )


async def test_pagination_filters_detail_and_streamed_raw_file(traffic):
    t = traffic
    result = await t.client.get(t.path, params={"run": 1, "limit": 2}, headers=t.headers)
    assert result.status_code == 200
    data = result.json()
    assert [r["seq"] for r in data["records"]] == [3, 2]
    assert data["has_more"] and data["next_before"] == 2
    assert data["capture"]["status"] == "archived"
    page = await t.client.get(t.path, params={"run": 1, "before": 2}, headers=t.headers)
    assert [r["seq"] for r in page.json()["records"]] == [1]
    assert page.json()["next_before"] is None
    for query in ({"agent_id": "a"}, {"protocol": "HTTPS"}, {"q": "/3"}):
        response = await t.client.get(t.path, params={"run": 1, **query}, headers=t.headers)
        assert response.status_code == 200
        assert all(
            r["seq"] in ({3} if "q" in query else {1, 3}) for r in response.json()["records"]
        )
    detail = await t.client.get(t.path + "/records/r-3", params={"run": 1}, headers=t.headers)
    assert detail.status_code == 200
    assert "hidden" not in detail.text
    raw = await t.client.get(t.path + "/files/raw", params={"run": 1}, headers=t.headers)
    assert raw.status_code == 200 and raw.content == b"raw-pcap"
    assert raw.headers["x-content-type-options"] == "nosniff"
    assert raw.headers["cache-control"] == "no-store"


async def test_protocol_filter_and_search_cover_transport_and_application_without_inference(
    traffic,
):
    t = traffic
    layers = {
        1: {"protocol": "udp", "transport_protocol": "udp", "application_protocol": "dns"},
        2: {"protocol": "tcp", "transport_protocol": "tcp", "application_protocol": "tls"},
        3: {"protocol": "model"},
    }
    for chunk_name in ("chunk-0", "chunk-2"):
        key = t.root + f"chunks/{chunk_name}.json.gz"
        records = json.loads(gzip.decompress(t.objects.data[key]))
        for record in records:
            record.update(layers[record["seq"]])
            if record["seq"] == 3:
                record.pop("transport_protocol", None)
                record.pop("application_protocol", None)
        t.objects.data[key] = gzip.compress(json.dumps(records).encode())
    for query, expected in (
        ({"protocol": "UDP"}, [1]),
        ({"protocol": "dNs"}, [1]),
        ({"protocol": "TCP"}, [2]),
        ({"protocol": "TLS"}, [2]),
        ({"q": "DNS"}, [1]),
        ({"q": "UDP"}, [1]),
        ({"q": "TLS"}, [2]),
        ({"q": "TCP"}, [2]),
        ({"protocol": "quic"}, []),
        ({"protocol": "None"}, []),
        ({"protocol": "model"}, [3]),
    ):
        response = await t.client.get(t.path, params={"run": 1, **query}, headers=t.headers)
        assert response.status_code == 200
        assert [record["seq"] for record in response.json()["records"]] == expected
    response = await t.client.get(t.path + "?run=1&protocol=model", headers=t.headers)
    record = response.json()["records"][0]
    assert record["transport_protocol"] is None and record["application_protocol"] is None


async def test_agent_filter_returns_only_its_namespace_original_fragments(traffic):
    t = traffic
    owners = ["lead", "member-2"]
    data = json.loads(t.objects.data[t.root + "manifest.json"])
    files = [
        dict(id="agent-a", agent_id=owners[0], source_id="netns-a", name="a.pcapng", size_bytes=3),
        dict(
            id="agent-a-next",
            agent_id=owners[0],
            source_id="netns-a",
            name="a-next.pcapng",
            size_bytes=3,
        ),
        dict(id="agent-b", agent_id=owners[1], source_id="netns-b", name="b.pcapng", size_bytes=3),
        dict(
            id="gateway", agent_id=None, source_id="task-root", name="gateway.pcapng", size_bytes=3
        ),
    ]
    data["files"] = files
    t.objects.data[t.root + "manifest.json"] = json.dumps(data).encode()
    for item in files:
        t.objects.data[t.root + "pcap/" + item["id"]] = b"raw"
    response = await t.client.get(t.path + "?run=1", headers=t.headers)
    assert {item["id"] for item in response.json()["files"]} == {item["id"] for item in files}
    for owner, expected in (
        (owners[0], {"agent-a", "agent-a-next"}),
        (owners[1], {"agent-b"}),
        (str(uuid4()), set()),
    ):
        response = await t.client.get(
            t.path, params={"run": 1, "agent_id": owner}, headers=t.headers
        )
        assert response.status_code == 200
        assert {item["id"] for item in response.json()["files"]} == expected
        assert all(item["agent_id"] == owner for item in response.json()["files"])
    raw = await t.client.get(t.path + "/files/agent-a?run=1", headers=t.headers)
    assert raw.status_code == 200 and raw.content == b"raw"
    assert (
        await t.client.get(t.path + "/files/agent-a?run=2", headers=t.headers)
    ).status_code == 404


async def test_shared_gateway_records_are_visible_only_without_an_agent_filter(traffic):
    t = traffic
    chunk_key = t.root + "chunks/chunk-2.json.gz"
    records = json.loads(gzip.decompress(t.objects.data[chunk_key]))
    records[0].update(agent_id=None, attribution="shared", summary="Shared task gateway")
    t.objects.data[chunk_key] = gzip.compress(json.dumps(records).encode())
    detail_key = t.root + "records/r-3.json.gz"
    detail = json.loads(gzip.decompress(t.objects.data[detail_key]))
    detail.update(agent_id=None, attribution="shared", summary="Shared task gateway")
    t.objects.data[detail_key] = gzip.compress(json.dumps(detail).encode())
    response = await t.client.get(t.path + "?run=1", headers=t.headers)
    assert response.status_code == 200
    assert response.json()["records"][0]["attribution"] == "shared"
    response = await t.client.get(t.path + "/records/r-3?run=1", headers=t.headers)
    assert response.status_code == 200 and response.json()["agent_id"] is None
    for agent in ("a", "b"):
        response = await t.client.get(
            t.path, params={"run": 1, "agent_id": agent}, headers=t.headers
        )
        assert response.status_code == 200
        assert all(record["attribution"] == "exact" for record in response.json()["records"])
        assert all(record["seq"] != 3 for record in response.json()["records"])


async def test_authentication_and_both_task_modes(traffic, monkeypatch):
    t = traffic
    for suffix in ("", "/records/r-1", "/files/raw"):
        path = t.path + suffix + "?run=1"
        assert (await t.client.get(path)).status_code == 401
        agent = issue_agent_token(t.config, t.tid, "a")
        assert (
            await t.client.get(path, headers={"Authorization": f"Bearer {agent}"})
        ).status_code == 403
        assert (
            await t.client.get(path, headers={"Authorization": "Bearer service-test"})
        ).status_code == 200
    t.app.state.ctf_service = SimpleNamespace()
    monkeypatch.setattr(
        api, "_task", AsyncMock(return_value={"id": t.tid, "mode": "ctf", "run_number": 2})
    )
    assert (await t.client.get(t.path + "?run=1", headers=t.headers)).status_code == 200


async def test_historical_missing_current_and_cross_run_are_explicit(traffic):
    t = traffic
    response = await t.client.get(t.path, headers=t.headers)
    assert response.status_code == 200 and response.json()["availability"] == "not_recorded"
    for suffix in ("/records/r-1", "/files/raw"):
        assert (await t.client.get(t.path + suffix, headers=t.headers)).status_code == 404
    assert (await t.client.get(t.path + "?run=3", headers=t.headers)).status_code == 404
    other = str(uuid4())
    assert (
        await t.client.get(t.path.replace(str(t.tid), other), headers=t.headers)
    ).status_code == 404
    t.objects.publish(t.tid, 2, sequences=(5,))
    assert (await t.client.get(t.path + "/records/r-1", headers=t.headers)).status_code == 404


@pytest.mark.parametrize(
    "target", ["manifest", "chunk", "detail", "file", "gzip", "store", "mismatch", "bomb"]
)
async def test_broken_archive_returns_safe_503(traffic, target):
    t = traffic
    suffix = ""
    if target == "manifest":
        data = json.loads(t.objects.data[t.root + "manifest.json"])
        data["task_id"] = str(uuid4())
        t.objects.data[t.root + "manifest.json"] = json.dumps(data).encode()
    elif target == "chunk":
        del t.objects.data[t.root + "chunks/chunk-2.json.gz"]
    elif target == "detail":
        del t.objects.data[t.root + "records/r-1.json.gz"]
        suffix = "/records/r-1"
    elif target == "file":
        del t.objects.data[t.root + "pcap/raw"]
        suffix = "/files/raw"
    elif target == "gzip":
        t.objects.data[t.root + "chunks/chunk-2.json.gz"] = b"bad"
    elif target == "store":
        t.objects.fail = True
    elif target == "mismatch":
        key = t.root + "records/r-1.json.gz"
        data = json.loads(gzip.decompress(t.objects.data[key]))
        data["agent_id"] = "different"
        t.objects.data[key] = gzip.compress(json.dumps(data).encode())
        suffix = "/records/r-1"
    elif target == "bomb":
        t.objects.data[t.root + "chunks/chunk-2.json.gz"] = gzip.compress(
            b" " * (8 * 1024 * 1024 + 1)
        )
    response = await t.client.get(t.path + suffix, params={"run": 1}, headers=t.headers)
    assert response.status_code == 503
    assert "password" not in response.text and "do-not-expose" not in response.text


@pytest.mark.parametrize("query", [{"run": 0}, {"limit": 201}, {"before": 0}, {"q": "a" * 301}])
async def test_invalid_queries_are_rejected(traffic, query):
    assert (
        await traffic.client.get(traffic.path, params=query, headers=traffic.headers)
    ).status_code == 422


async def test_manifest_cannot_inject_an_object_path(traffic):
    t = traffic
    data = json.loads(t.objects.data[t.root + "manifest.json"])
    data["files"][0]["id"] = "../../../secret"
    t.objects.data[t.root + "manifest.json"] = json.dumps(data).encode()
    assert (await t.client.get(t.path + "?run=1", headers=t.headers)).status_code == 503
    assert all(".." not in key for key in t.objects.reads)


async def test_empty_and_truncated_raw_files_never_finish_as_success(traffic):
    t = traffic
    key = t.root + "pcap/raw"
    t.objects.data[key] = b""
    response = await t.client.get(t.path + "/files/raw?run=1", headers=t.headers)
    assert response.status_code == 503
    t.objects.data[key] = b"raw"
    with pytest.raises(RuntimeError, match="Traffic file is incomplete"):
        await t.client.get(t.path + "/files/raw?run=1", headers=t.headers)


async def test_purge_deletes_all_traffic_runs_and_retries_before_database_delete():
    tid, other = uuid4(), uuid4()
    conn = SimpleNamespace(execute=AsyncMock())
    result = MagicMock()
    result.mappings.return_value.one.return_value = {"deleting": True}
    conn.execute.return_value = result
    engine = MagicMock()
    engine.begin.return_value.__aenter__.return_value = conn
    conversations = Conversations(engine)
    conversations.repo.lock = AsyncMock()
    keys = {
        f"traffic/{tid}/run-1/manifest.json",
        f"traffic/{tid}/run-2/pcap/raw",
        f"traffic/{tid}/run-2/checkpoint.json",
        f"traffic/{other}/run-1/pcap/keep",
    }
    objects = SimpleNamespace(
        list=AsyncMock(side_effect=lambda prefix: [k for k in keys if k.startswith(prefix)]),
        remove=AsyncMock(side_effect=RuntimeError("storage unavailable")),
    )
    with pytest.raises(RuntimeError):
        await conversations.purge(tid, objects)
    assert engine.begin.call_count == 1
    objects.remove = AsyncMock(side_effect=lambda key: keys.discard(key))
    assert await conversations.purge(tid, objects) == {"purged": True}
    assert keys == {f"traffic/{other}/run-1/pcap/keep"}


def publish_packet_bundles(t):
    t.objects.publish(t.tid, 1, sequences=(1, 2, 3, 4, 5, 6))
    manifest_key = t.root + "manifest.json"
    manifest = json.loads(t.objects.data[manifest_key])
    manifest.update(
        last_seq=1000,
        preview_dropped=7,
        pending_chunks=2,
        last_packet_at=datetime.now(UTC).isoformat(),
    )
    for chunk in manifest["chunks"]:
        chunk["detail_storage"] = "chunk"
        details = []
        summaries = []
        for seq in range(chunk["first_seq"], chunk["last_seq"] + 1):
            record_key = t.root + f"records/r-{seq}.json.gz"
            detail = json.loads(gzip.decompress(t.objects.data.pop(record_key)))
            if seq in {1, 2, 4, 5}:
                detail.update(
                    record_kind="packet",
                    request=None,
                    response=None,
                    packet=dict(
                        source_id="root-source" if seq == 5 else "agent-source",
                        number=100 + seq,
                        native_frame_number=1,
                        frame_length=4,
                        captured_length=4,
                        direction="loopback" if seq == 4 else "outbound",
                        interface="lo" if seq == 4 else "eth0",
                        info="TCP packet",
                    ),
                    packet_detail=dict(
                        raw_hex="01020304",
                        layers=[
                            dict(
                                name="tcp",
                                label="Transmission Control Protocol",
                                children=[dict(name="tcp.port", value="443")],
                            )
                        ],
                    ),
                )
                if seq == 5:
                    detail.update(agent_id=None, attribution="shared")
            else:
                detail["record_kind"] = "http" if seq == 3 else "logical"
            normalized = TrafficRecordDetail.model_validate(detail)
            details.append(normalized.model_dump(mode="json"))
            summaries.append(
                normalized.model_dump(mode="json", include=set(TrafficRecordSummary.model_fields))
            )
        t.objects.data[t.root + f"chunks/{chunk['id']}.json.gz"] = gzip.compress(
            json.dumps(summaries).encode()
        )
        t.objects.data[t.root + f"detail-chunks/{chunk['id']}.json.gz"] = gzip.compress(
            json.dumps(details).encode()
        )
    t.objects.data[manifest_key] = json.dumps(manifest).encode()


async def test_packet_list_uses_only_summaries_and_bundle_details_preserve_sources(traffic):
    t = traffic
    publish_packet_bundles(t)
    response = await t.client.get(t.path + "?run=1&kind=packet", headers=t.headers)
    assert response.status_code == 200
    data = response.json()
    assert [record["seq"] for record in data["records"]] == [5, 4, 2, 1]
    assert response.headers["cache-control"] == "no-store"
    assert data["snapshot_seq"] == 6
    assert data["capture"]["preview_dropped"] == 7
    assert data["capture"]["pending_chunks"] == 2
    assert data["capture"]["dropped_packets"] == 0
    assert not any("detail-chunks" in key or "/records/" in key for key in t.objects.reads)
    for seq, expected_kind in ((4, "packet"), (3, "http"), (6, "logical")):
        response = await t.client.get(t.path + f"/records/r-{seq}?run=1", headers=t.headers)
        assert response.status_code == 200 and response.json()["record_kind"] == expected_kind
        assert response.headers["cache-control"] == "no-store"
    detail = (await t.client.get(t.path + "/records/r-4?run=1", headers=t.headers)).json()
    assert detail["packet"]["number"] == 104 and detail["packet"]["native_frame_number"] == 1
    assert detail["packet"]["interface"] == "lo"
    assert detail["packet_detail"]["raw_hex"] == "01020304"
    filtered = await t.client.get(t.path + "?run=1&kind=packet&agent_id=a", headers=t.headers)
    assert [record["seq"] for record in filtered.json()["records"]] == [1]
    loopback = await t.client.get(t.path + "?run=1&direction=loopback", headers=t.headers)
    assert [record["seq"] for record in loopback.json()["records"]] == [4]


async def test_after_pages_are_ascending_exclusive_and_advance_over_unmatched_records(traffic):
    t = traffic
    publish_packet_bundles(t)
    first = (
        await t.client.get(t.path + "?run=1&kind=packet&after=0&limit=2", headers=t.headers)
    ).json()
    assert [record["seq"] for record in first["records"]] == [1, 2]
    assert first["has_more"] and first["next_after"] == 2 and first["next_before"] is None
    second = (
        await t.client.get(t.path + "?run=1&kind=packet&after=2&limit=2", headers=t.headers)
    ).json()
    assert [record["seq"] for record in second["records"]] == [4, 5]
    assert not second["has_more"] and second["next_after"] == second["snapshot_seq"] == 6
    empty = (await t.client.get(t.path + "?run=1&kind=packet&after=5", headers=t.headers)).json()
    assert empty["records"] == [] and empty["next_after"] == 6
    future = (await t.client.get(t.path + "?run=1&after=100", headers=t.headers)).json()
    assert future["records"] == [] and future["next_after"] == 100 and future["snapshot_seq"] == 6
    historical = await t.client.get(t.path + "?run=1&kind=packet&before=4", headers=t.headers)
    assert [record["seq"] for record in historical.json()["records"]] == [2, 1]


async def test_live_after_cursor_sees_newly_published_chunks_without_replaying_old_packets(traffic):
    t = traffic
    publish_packet_bundles(t)
    empty = await t.client.get(t.path + "?run=1&kind=packet&after=6", headers=t.headers)
    assert empty.json()["next_after"] == 6 and empty.json()["records"] == []
    detail = json.loads(gzip.decompress(t.objects.data[t.root + "detail-chunks/chunk-0.json.gz"]))[
        0
    ]
    detail.update(id="r-7", seq=7)
    detail["packet"].update(number=202, native_frame_number=1)
    record = TrafficRecordDetail.model_validate(detail)
    t.objects.data[t.root + "detail-chunks/live-7.json.gz"] = gzip.compress(
        json.dumps([record.model_dump(mode="json")]).encode()
    )
    t.objects.data[t.root + "chunks/live-7.json.gz"] = gzip.compress(
        json.dumps(
            [record.model_dump(mode="json", include=set(TrafficRecordSummary.model_fields))]
        ).encode()
    )
    manifest_key = t.root + "manifest.json"
    manifest = json.loads(t.objects.data[manifest_key])
    manifest["chunks"].append(
        dict(id="live-7", first_seq=7, last_seq=7, count=1, detail_storage="chunk")
    )
    manifest["record_count"] += 1
    t.objects.data[manifest_key] = json.dumps(manifest).encode()
    response = await t.client.get(t.path + "?run=1&kind=packet&after=6", headers=t.headers)
    assert [item["seq"] for item in response.json()["records"]] == [7]
    assert response.json()["next_after"] == response.json()["snapshot_seq"] == 7


@pytest.mark.parametrize(
    "query", ["before=3&after=0", "after=-1", "kind=session", "direction=guessed"]
)
async def test_packet_invalid_cursor_and_filters_are_rejected(traffic, query):
    response = await traffic.client.get(traffic.path + "?run=1&" + query, headers=traffic.headers)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "damage", ["missing", "wrong_summary", "duplicate", "oversized", "wrong_storage"]
)
async def test_missing_or_corrupt_packet_bundles_fail_without_fallback_to_arbitrary_objects(
    traffic, damage
):
    t = traffic
    publish_packet_bundles(t)
    key = t.root + "detail-chunks/chunk-0.json.gz"
    details = json.loads(gzip.decompress(t.objects.data[key]))
    if damage == "missing":
        del t.objects.data[key]
    elif damage == "wrong_summary":
        details[0]["packet"]["source_id"] = "another-source"
        t.objects.data[key] = gzip.compress(json.dumps(details).encode())
    elif damage == "duplicate":
        t.objects.data[key] = gzip.compress(json.dumps([details[0], details[0]]).encode())
    elif damage == "oversized":
        t.objects.data[key] = gzip.compress(b" " * (1024 * 1024 + 1))
    elif damage == "wrong_storage":
        manifest_key = t.root + "manifest.json"
        manifest = json.loads(t.objects.data[manifest_key])
        manifest["chunks"][0]["detail_storage"] = "record"
        t.objects.data[manifest_key] = json.dumps(manifest).encode()
    response = await t.client.get(t.path + "/records/r-1?run=1", headers=t.headers)
    assert response.status_code == 503
    assert not any("/records/" in path for path in t.objects.reads)


async def test_legacy_records_do_not_become_packets_and_after_not_recorded_stays_explicit(traffic):
    t = traffic
    response = await t.client.get(t.path + "?run=1&kind=packet&after=0", headers=t.headers)
    assert response.status_code == 200
    assert response.json()["records"] == [] and response.json()["next_after"] == 3
    response = await t.client.get(t.path + "?run=2&kind=packet&after=0", headers=t.headers)
    assert response.json()["availability"] == "not_recorded" and response.json()["next_after"] == 0
