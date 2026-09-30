from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import bbx_blackboard.api as api
import bbx_blackboard.auth as auth
import httpx
import jwt
import pytest
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.settings import Settings
from bbx_contracts.profile import load_profile
from pydantic import SecretStr


def settings() -> Settings:
    return Settings.model_construct(
        postgres_password=SecretStr("postgres-test"),
        minio_root_password=SecretStr("minio-test"),
        service_token=SecretStr("service-test"),
        agent_token_secret=SecretStr("jwt-test-secret-with-32-or-more-chars"),
        admin_users=SecretStr("alice:password-test"),
    )


async def test_lifespan_initializes_conversations_with_owned_engine(monkeypatch):
    engine = SimpleNamespace(dispose=AsyncMock())
    dispatcher = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(api, "create_async_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(api.ProfileStore, "ensure_bundled", AsyncMock())
    app = api.create_app(settings(), objects=FakeObjects(), dispatcher=dispatcher)
    assert app.state.conversations is None
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.conversations, api.Conversations)
        assert app.state.conversations.repo.engine is engine
    engine.dispose.assert_awaited_once()


class FakeObjects:
    async def exists(self, uri: str) -> bool:
        return True

    async def stream(self, _uri: str) -> AsyncIterator[bytes]:
        yield b"evidence"


class FakeService:
    def __init__(self) -> None:
        self.trace_calls: list[tuple[Any, str, dict[str, Any], int | None]] = []

    async def post_fact(self, _tid, _aid, _body, *, dry_run=False, expected_derive_round=None):
        return {"valid": True} if dry_run else {"id": "F1"}

    async def record_agent_trace(self, tid, aid, body, *, expected_derive_round=None):
        self.trace_calls.append((tid, aid, body, expected_derive_round))
        return []

    async def get_object(self, _tid, _oid, _depth):
        raise RuleViolation("bad_object", "修正对象引用")


@pytest.mark.asyncio
async def test_auth_and_task_scoped_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    config = settings()
    app = api.create_app(config)
    service = FakeService()
    app.state.board_service = service
    app.state.objects = FakeObjects()

    async def fake_task(_request, _task_id):
        return {"id": _task_id}

    monkeypatch.setattr(api, "_task", fake_task)
    task_id, other_task = uuid4(), uuid4()
    agent = {"Authorization": f"Bearer {issue_agent_token(config, task_id, 'agent-1')}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", trust_env=False
    ) as client:
        response = await client.post("/api/login", json={"username": "alice", "password": "bad"})
        assert response.status_code == 401
        response = await client.post(
            "/api/login", json={"username": "alice", "password": "password-test"}
        )
        assert response.status_code == 200
        assert "httponly" in response.headers["set-cookie"].lower()

        uri = f"toolcalls/{task_id}/call-1.txt"
        response = await client.get("/api/evidence", params={"uri": uri}, headers=agent)
        assert response.status_code == 200
        assert response.content == b"evidence"
        response = await client.get(
            "/api/evidence", params={"uri": f"toolcalls/{other_task}/call-1.txt"}, headers=agent
        )
        assert response.status_code == 403
        trace_uri = f"traces/{task_id}/agent-1/000001-model_output-abcd.json"
        response = await client.get("/api/evidence", params={"uri": trace_uri}, headers=agent)
        assert response.status_code == 200
        assert response.content == b"evidence"
        for invalid_uri, expected in (
            (f"traces/{other_task}/agent-1/output.json", 403),
            (f"traces/{task_id}/agent-1/../output.json", 422),
            (f"traces/{task_id}/agent-1/output.txt", 422),
            (f"traces/{task_id}/agent-1/", 422),
            (f"reports/{task_id}.md", 422),
        ):
            response = await client.get("/api/evidence", params={"uri": invalid_uri}, headers=agent)
            assert response.status_code == expected

        trace_body = {
            "kind": "model_output",
            "step": 1,
            "uri": trace_uri,
            "summary": "summary",
        }
        trace_path = f"/api/tasks/{task_id}/agents/agent-1/traces"
        response = await client.post(trace_path, json=trace_body, headers=agent)
        assert response.status_code == 403
        response = await client.post(
            trace_path,
            json=trace_body,
            headers={"Authorization": "Bearer service-test"},
        )
        assert response.status_code == 200
        assert service.trace_calls == [(task_id, "agent-1", trace_body, None)]
        model_error = {**trace_body, "kind": "model_error", "expected_derive_round": 2}
        response = await client.post(
            trace_path,
            json=model_error,
            headers={"Authorization": "Bearer service-test"},
        )
        assert response.status_code == 200
        assert service.trace_calls[-1] == (
            task_id,
            "agent-1",
            trace_body | {"kind": "model_error"},
            2,
        )
        response = await client.post(
            trace_path,
            json={**trace_body, "kind": "fabricated"},
            headers={"Authorization": "Bearer service-test"},
        )
        assert response.status_code == 422

        fact: dict[str, Any] = {
            "kind": "observation",
            "statement": "seen",
            "evidence": [{"type": "text", "summary": "trace", "uri": uri}],
        }
        response = await client.post(f"/api/tasks/{task_id}/facts", json=fact, headers=agent)
        assert response.status_code == 200
        fact["evidence"][0]["uri"] = f"evidence/{other_task}/agent-2/x.txt"
        response = await client.post(f"/api/tasks/{task_id}/facts", json=fact, headers=agent)
        assert response.status_code == 403

        response = await client.get(f"/api/tasks/{task_id}/objects/F1", headers=agent)
        assert response.status_code == 422
        assert response.json() == {"code": "bad_object", "message": "修正对象引用"}


@pytest.mark.asyncio
async def test_invalid_audience_and_expired_token() -> None:
    config = settings()
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    wrong_audience = jwt.encode(
        {"aud": "unknown", "exp": 4102444800},
        config.agent_token_secret.get_secret_value(),
        algorithm="HS256",
    )
    expired = jwt.encode(
        {"aud": "user", "sub": "alice", "exp": 1},
        config.agent_token_secret.get_secret_value(),
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", trust_env=False
    ) as client:
        for token in (wrong_audience, expired):
            response = await client.get(
                "/api/profiles", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_conversation_endpoints_keep_user_and_service_roles_separate() -> None:
    config = settings()
    app = api.create_app(config)
    tid, mid = uuid4(), uuid4()

    class FakeConversations:
        async def post_message(self, task_id, aid, message_id, content):
            assert (task_id, aid, message_id, content) == (tid, "agent-1", mid, "hello")
            return {"id": message_id, "content": content}

        async def get_session(self, task_id, aid):
            assert (task_id, aid) == (tid, "agent-1")
            return {
                "session": {},
                "opening_instructions": "saved",
                "origin": "native",
                "revision": 1,
            }

        async def request_delete(self, task_id):
            assert task_id == tid
            return {"task_id": tid, "deleting": True}

        async def export_archive(self, task_id):
            assert task_id == tid
            return {"format": "bbx.task-archive.v1", "task_id": str(tid)}

    app.state.conversations = FakeConversations()
    agent = {"Authorization": f"Bearer {issue_agent_token(config, tid, 'agent-1')}"}
    service = {"Authorization": "Bearer service-test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as client:
        path = f"/api/tasks/{tid}/agents/agent-1"
        assert (
            await client.post(
                f"{path}/messages", json={"id": str(mid), "content": "hello"}, headers=agent
            )
        ).status_code == 403
        assert (await client.get(f"{path}/session", headers=agent)).status_code == 403
        archive_path = f"/api/tasks/{tid}/archive-data"
        assert (await client.get(archive_path, headers=agent)).status_code == 403
        assert (
            await client.post(
                f"{path}/messages", json={"id": str(mid), "content": "hello"}, headers=service
            )
        ).status_code == 403
        assert (await client.delete(f"/api/tasks/{tid}", headers=service)).status_code == 403
        client.cookies.set("bbx_session", issue_user_token(config, "alice"), path="/api")
        assert (await client.get(f"{path}/session")).status_code == 403
        assert (await client.get(archive_path)).status_code == 403
        assert (
            await client.post(f"{path}/messages", json={"id": str(mid), "content": "hello"})
        ).status_code == 200
        assert (await client.delete(f"/api/tasks/{tid}")).status_code == 202
        assert (await client.get(f"{path}/session", headers=service)).json()["revision"] == 1
        assert (await client.get(archive_path, headers=service)).json() == {
            "format": "bbx.task-archive.v1",
            "task_id": str(tid),
        }
        response = await client.get("/api/profiles", headers=[(b"Authorization", b"Bearer \xff")])
        assert response.status_code == 401
        user = issue_user_token(config, "alice")
        response = await client.post(
            f"/api/tasks/{uuid4()}/uploads?key=x",
            headers={"Authorization": f"Bearer {user}"},
            content=b"x",
        )
        assert response.status_code == 403


def test_key_layout() -> None:
    task_id = uuid4()
    assert api._key_task(f"toolcalls/{task_id}/call-1.txt") == task_id
    assert api._key_task(f"toolcalls/{task_id}/runtime-audit-run-2.txt") == task_id
    assert api._key_task(f"evidence/{task_id}/agent-1/a.txt") == task_id
    assert api._key_task(f"evidence/{task_id}/agent-1/../a.txt") is None
    assert api._key_task(f"reports/{task_id}.md") is None
    assert api._upload_limit(task_id, f"evidence/{task_id}/agent-1/a.txt") == 50 * 1024 * 1024


def test_unicode_credentials() -> None:
    config = settings()
    config.admin_users = SecretStr("张三:密码")
    assert auth.check_user_password(config, "张三", "密码")
    assert not auth.check_user_password(config, "张三", "错")
    assert not auth._equals("\ud800", "token")


@pytest.mark.asyncio
async def test_task_inherits_selected_profile_params_with_task_override() -> None:
    class Profiles:
        async def get(self, name, version):
            assert (name, version) == ("custom", 4)
            return {
                "name": "custom",
                "version": 4,
                "params": {"explore_max_steps": 7, "seed_max_steps": 3},
                "models": {},
            }

    class Service:
        def __init__(self):
            self.spec = None
            self.profile_version = None

        async def create_task(self, spec, *, profile_version):
            self.spec = spec
            self.profile_version = profile_version
            return uuid4()

    app = api.create_app(settings())
    service = Service()
    app.state.profile_store = Profiles()
    app.state.board_service = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as client:
        response = await client.post(
            "/api/tasks",
            headers={"Authorization": "Bearer service-test"},
            json={
                "goal": "Check parameters",
                "acceptance": [{"id": "A1", "desc": "Done"}],
                "budget": {"max_cost": "2", "max_minutes": 10},
                "agent_profile": "custom",
                "profile_version": 4,
                "params": {"seed_max_steps": 5},
            },
        )
    assert response.status_code == 200
    assert response.json()["agent_profile_version"] == 4
    assert service.profile_version == 4
    assert service.spec is not None
    assert service.spec["params"] == {"explore_max_steps": 7, "seed_max_steps": 5}
    assert service.spec["budget"] == {
        "max_concurrent_agents": 5,
        "max_cost": Decimal("2"),
        "max_minutes": 10,
    }


async def test_task_uses_smallest_explicit_profile_context_window() -> None:
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    profile.models.explore.context_window = 300000
    profile.models.derive.context_window = 500000
    row = {"name": "custom", "version": 4, **profile.model_dump(mode="json")}

    class Profiles:
        async def get(self, name, version):
            assert (name, version) == ("custom", 4)
            return row

        async def create(self, name, snapshot, actor):
            assert (name, actor) == ("task-settings", "task")
            assert snapshot.params.context_threshold == 240000
            return {"name": name, "version": 1, **snapshot.model_dump(mode="json")}

    class Service:
        async def create_task(self, spec, *, profile_version):
            assert spec["params"]["context_threshold"] == 240000
            assert profile_version == 1
            return uuid4()

    app = api.create_app(settings())
    app.state.profile_store = Profiles()
    app.state.board_service = Service()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as client:
        response = await client.post(
            "/api/tasks",
            headers={"Authorization": "Bearer service-test"},
            json={
                "goal": "Check window",
                "acceptance": [{"id": "A1", "desc": "Done"}],
                "budget": {"max_cost": "2", "max_minutes": 10},
                "agent_profile": "custom",
                "profile_version": 4,
                "params": {"context_threshold": 128000},
            },
        )
    assert response.status_code == 200, response.text
    assert row["params"]["context_threshold"] == 128000


@pytest.mark.parametrize(
    "override",
    [{}, {"reasoning_effort": None}, {"reasoning_effort": "max"}, {"reasoning_effort": "none"}],
)
@pytest.mark.parametrize("source", ["custom", "selected", "default"])
async def test_task_reasoning_creates_isolated_snapshot_and_not_task_spec(override, source):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    for model in (profile.models.explore, profile.models.derive, profile.models.close):
        model.context_window = 300000
        model.reasoning_effort = "high"
    row = {"name": "custom", "version": 4, **profile.model_dump(mode="json")}
    original = deepcopy(row)
    profiles = AsyncMock()
    profiles.get.return_value = row
    profiles.create.side_effect = lambda name, snapshot, actor: {
        "name": name,
        "version": 9,
        **snapshot.model_dump(mode="json"),
    }
    service = AsyncMock()
    service.create_task.return_value = uuid4()
    app = api.create_app(settings())
    app.state.profile_store = profiles
    app.state.board_service = service
    platform = MagicMock(spec=api.PlatformStore)
    platform.get.return_value = {"enabled": True}
    platform.public.return_value = {"config": row["models"]["explore"]}
    platform.default_model_name.return_value = "selected-model"
    app.state.platform_store = platform
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as client:
        response = await client.post(
            "/api/tasks",
            headers={"Authorization": "Bearer service-test"},
            json={
                "goal": "Check task reasoning",
                "acceptance": [{"id": "A1", "desc": "Done"}],
                "budget": {"max_cost": "2", "max_minutes": 10},
                "agent_profile": "default" if source == "default" else "custom",
                **({"profile_version": 4} if source != "default" else {}),
                **(
                    {"model_id": "selected-model", "model_version": 2}
                    if source == "selected"
                    else {}
                ),
                **override,
            },
        )
    assert response.status_code == 200, response.text
    profiles.create.assert_awaited_once()
    name, snapshot, actor = profiles.create.call_args.args
    assert (name, actor) == ("task-settings", "task")
    expected = override.get("reasoning_effort") or "high"
    assert {
        model.reasoning_effort
        for model in (snapshot.models.explore, snapshot.models.derive, snapshot.models.close)
    } == {expected}
    assert snapshot.params.context_threshold == 240000
    spec = service.create_task.call_args.args[0]
    assert "reasoning_effort" not in spec
    assert spec["agent_profile"] == "task-settings"
    assert service.create_task.call_args.kwargs["profile_version"] == 9
    assert row == original


@pytest.mark.parametrize("effort", ["", "ultra", " ", True, 1])
async def test_task_reasoning_validation_rejects_invalid_requests(effort):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    app = api.create_app(settings())
    app.state.profile_store = AsyncMock()
    app.state.profile_store.get.return_value = {
        "name": "custom",
        "version": 1,
        **profile.model_dump(mode="json"),
    }
    app.state.board_service = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as client:
        response = await client.post(
            "/api/tasks",
            headers={"Authorization": "Bearer service-test"},
            json={
                "goal": "Reject invalid task reasoning",
                "acceptance": [{"id": "A1", "desc": "Done"}],
                "budget": {"max_cost": "2", "max_minutes": 10},
                "agent_profile": "custom",
                "profile_version": 1,
                "reasoning_effort": effort,
            },
        )
    assert response.status_code == 422, response.text
    app.state.profile_store.create.assert_not_awaited()
    app.state.board_service.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_matches_text_and_orders_by_version(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = uuid4()
    app = api.create_app(settings())

    class SearchService:
        async def state(self, _task_id):
            return {
                "facts": {
                    "F1": {"id": "F1", "statement": "SLOW gateway", "version": 2},
                    "F2": {"id": "F2", "statement": "Other cause", "version": 7},
                },
                "intents": {
                    "I1": {
                        "id": "I1",
                        "statement": "Inspect upstream",
                        "expected": "Find the slow query",
                        "method": "Trace calls",
                        "version": 5,
                    }
                },
            }

    async def fake_task(_request, _task_id):
        return {"id": _task_id}

    monkeypatch.setattr(api, "_task", fake_task)
    app.state.board_service = SearchService()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer service-test"},
        trust_env=False,
    ) as client:
        response = await client.get(f"/api/tasks/{task_id}/search", params={"q": "slow"})
        assert response.status_code == 200
        assert response.json() == [
            {"id": "I1", "type": "intent", "statement": "Inspect upstream"},
            {"id": "F1", "type": "fact", "statement": "SLOW gateway"},
        ]
        response = await client.get(f"/api/tasks/{task_id}/search", params={"k": 2})
        assert [item["id"] for item in response.json()] == ["F2", "I1"]
