"""Archive registration is a terminal, task-scoped event."""

from typing import Any
from uuid import UUID, uuid4

import bbx_blackboard.api as api
import httpx
import pytest
from bbx_blackboard.domain import BoardState, RuleViolation, decide
from bbx_blackboard.service import BoardService
from bbx_blackboard.settings import Settings
from pydantic import SecretStr


def archive_state(task_id: UUID, *, status: str = "finished", agent_status: str = "finished"):
    return BoardState(
        task={"id": task_id, "status": status, "workspace_uri": None},
        agents={"agent-1": {"status": agent_status}},
    )


def archive_data(task_id: UUID) -> dict[str, Any]:
    return {"uri": f"workspace/{task_id}.tar.zst", "size": 27, "fallback": "agents-only"}


def test_archive_rule_and_idempotency() -> None:
    task_id = uuid4()
    state = archive_state(task_id)
    data = archive_data(task_id)
    assert decide(state, "record_archive", "scheduler", data) == [
        {
            "type": "task.archived",
            "actor": "scheduler",
            "payload": data,
            "object_id": None,
            "addressed_to": None,
        }
    ]
    state.task["workspace_uri"] = data["uri"]
    assert decide(state, "record_archive", "scheduler", data) == []
    with pytest.raises(RuleViolation) as exc:
        decide(state, "record_archive", "scheduler", {**data, "uri": "workspace/other.tar.zst"})
    assert exc.value.code == "archive_invalid_uri"


@pytest.mark.parametrize(
    "status, agent_status, code",
    [
        ("running", "finished", "archive_not_terminal"),
        ("closing", "finished", "archive_not_terminal"),
        ("finished", "running", "archive_agents_active"),
        ("failed", "concluding", "archive_agents_active"),
    ],
)
def test_archive_rejects_nonterminal_or_active_agent(
    status: str, agent_status: str, code: str
) -> None:
    task_id = uuid4()
    with pytest.raises(RuleViolation) as exc:
        decide(
            archive_state(task_id, status=status, agent_status=agent_status),
            "record_archive",
            "scheduler",
            archive_data(task_id),
        )
    assert exc.value.code == code


def test_archive_rejects_bad_metadata() -> None:
    task_id = uuid4()
    for changes in ({"size": -1}, {"fallback": "other"}):
        with pytest.raises(RuleViolation) as exc:
            decide(
                archive_state(task_id),
                "record_archive",
                "scheduler",
                {**archive_data(task_id), **changes},
            )
        assert exc.value.code == "archive_invalid_metadata"


@pytest.mark.asyncio
async def test_service_requires_exact_existing_object(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = uuid4()

    class Objects:
        found = False

        async def exists(self, uri: str) -> bool:
            return self.found

    objects = Objects()
    service = BoardService(engine=None, objects=objects)  # type: ignore[arg-type]
    calls = []

    async def write(tid, command, actor, data):
        calls.append((tid, command, actor, data))
        return []

    monkeypatch.setattr(service, "_write", write)
    data = archive_data(task_id)
    with pytest.raises(RuleViolation) as exc:
        await service.record_archive(task_id, "workspace/wrong.tar.zst", 1, "none")
    assert exc.value.code == "archive_invalid_uri"
    with pytest.raises(RuleViolation) as exc:
        await service.record_archive(task_id, **data)
    assert exc.value.code == "archive_missing"
    objects.found = True
    assert await service.record_archive(task_id, **data) == []
    assert calls == [(task_id, "record_archive", "scheduler", data)]


@pytest.mark.asyncio
async def test_archive_api_service_only_and_validates_body(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = uuid4()
    settings = Settings.model_construct(
        postgres_password=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        service_token=SecretStr("service-test"),
        agent_token_secret=SecretStr("agent-test"),
        admin_users=SecretStr("admin:test"),
    )
    app = api.create_app(settings)
    seen = []

    class Service:
        async def record_archive(self, tid, uri, size, fallback):
            seen.append((tid, uri, size, fallback))
            return []

    app.state.board_service = Service()

    async def existing(_request, _task_id):
        return {"id": _task_id}

    monkeypatch.setattr(api, "_task", existing)
    path = f"/api/tasks/{task_id}/archive"
    data = archive_data(task_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(path, json=data)).status_code == 401
        assert (
            await client.post(path, json=data, headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        headers = {"Authorization": "Bearer service-test"}
        assert (
            await client.post(path, json={**data, "fallback": "bad"}, headers=headers)
        ).status_code == 422
        response = await client.post(path, json=data, headers=headers)
    assert response.status_code == 200 and response.json() == []
    assert seen == [(task_id, data["uri"], data["size"], data["fallback"])]
