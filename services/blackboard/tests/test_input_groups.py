"""Streaming request boundaries before any object or transaction write."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import TaskCreateBody, create_app
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_blackboard.input_groups import check_filename, request_hash
from bbx_blackboard.settings import Settings
from bbx_contracts.models import InitialAttachment, TaskSpec
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError


def settings():
    return Settings.model_construct(
        postgres_password=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        service_token=SecretStr("input-service"),
        agent_token_secret=SecretStr("input-signing-key-at-least-thirty-two"),
        admin_users=SecretStr("alice:pass,bob:pass"),
    )


def group():
    return {
        "id": uuid4(),
        "owner": "user:alice",
        "expires_at": datetime.now(UTC) + timedelta(hours=24),
        "files": [],
        "deleting": False,
        "bound_task_id": None,
    }


@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", "../data", "a/b", "a\\b", "a\0b", "a\nb", "a\u0085b", "\ud800", "中" * 86],
)
def test_filename_cannot_escape_or_exceed_filesystem_limit(filename):
    with pytest.raises(HTTPException) as error:
        check_filename(filename)
    assert error.value.status_code == 422


def test_unicode_filename_and_normalized_request_hash():
    check_filename("中" * 85)
    gid = uuid4()
    assert request_hash({"id": gid, "cost": Decimal("2.00")}) == request_hash(
        {"cost": Decimal("2"), "id": gid}
    )
    assert request_hash({"goal": "first"}) != request_hash({"goal": "second"})


def test_request_hash_preserves_legal_decimal_precision_beyond_default_context():
    body = {
        "goal": "Precise request",
        "acceptance": [{"id": "A1", "desc": "Done"}],
        "budget": {"max_cost": "2.0000000000000000000000000001", "max_minutes": 10},
        "agent_profile": "default",
    }
    first = TaskCreateBody.model_validate(body)
    second = TaskCreateBody.model_validate(
        {**body, "budget": {**body["budget"], "max_cost": "2.0000000000000000000000000002"}}
    )
    assert first.budget.max_cost != second.budget.max_cost
    assert request_hash(first.model_dump()) != request_hash(second.model_dump())
    assert request_hash({"value": Decimal("1000")}) == request_hash({"value": Decimal("1000.00")})
    assert request_hash({"value": Decimal("1000")}) != request_hash({"value": Decimal("10000")})


def test_task_name_is_optional_and_client_cannot_supply_initial_metadata():
    body = {
        "goal": "complete goal",
        "acceptance": [{"id": "A1", "desc": "Done"}],
        "budget": {"max_cost": 2, "max_minutes": 10},
        "agent_profile": "default",
    }
    assert TaskSpec.model_validate(body).name is None
    assert TaskSpec.model_validate({**body, "name": "n" * 100}).goal == "complete goal"
    for override in (
        {"name": "n" * 101},
        {"name": ""},
        {"name": "a\x00b"},
        {"initial_attachments": []},
    ):
        with pytest.raises(ValidationError):
            TaskSpec.model_validate({**body, **override})


def test_explicit_input_selection_normalizes_ids_and_requires_group():
    first, second = uuid4(), uuid4()
    body = {
        "goal": "Check selection",
        "acceptance": [{"id": "A1", "desc": "Done"}],
        "budget": {"max_cost": 2, "max_minutes": 10},
        "agent_profile": "default",
        "input_group_id": uuid4(),
    }
    selected = TaskCreateBody.model_validate({**body, "input_file_ids": [first, second, first]})
    reordered = TaskCreateBody.model_validate({**body, "input_file_ids": [second, first]})
    assert selected.input_file_ids is not None and len(selected.input_file_ids) == 2
    assert request_hash(selected.model_dump()) == request_hash(reordered.model_dump())
    assert TaskCreateBody.model_validate(body).input_file_ids is None
    assert TaskCreateBody.model_validate({**body, "input_file_ids": []}).input_file_ids == []
    assert request_hash(selected.model_dump()) != request_hash(
        TaskCreateBody.model_validate({**body, "input_file_ids": []}).model_dump()
    )
    with pytest.raises(ValidationError):
        TaskCreateBody.model_validate({**body, "input_group_id": None, "input_file_ids": []})


@pytest.mark.parametrize("actor", ["bob", "service", "agent", None])
async def test_unbound_group_is_owner_scoped(actor):
    config = settings()
    app = create_app(config)
    row = group()
    app.state.input_groups = AsyncMock()
    app.state.input_groups.get.return_value = row
    token = (
        issue_user_token(config, actor)
        if actor == "bob"
        else "input-service"
        if actor == "service"
        else issue_agent_token(config, row["id"], "agent-1")
        if actor == "agent"
        else None
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/task-input-groups/{row['id']}",
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
    assert response.status_code == (403 if actor else 401)
    assert row["owner"] not in response.text


async def test_upload_spools_and_hashes_before_store_write(monkeypatch):
    config = settings()
    app = create_app(config)
    row = group()
    store = AsyncMock()
    store.get.return_value = row
    app.state.input_groups = store
    content = b"first\x00second"
    fid = uuid4()

    async def upload(gid, owner, filename, spooled, size, digest):
        assert spooled.read() == content
        assert owner == "user:alice" and size == len(content)
        assert digest == hashlib.sha256(content).hexdigest()
        return InitialAttachment(
            id=fid,
            filename=filename,
            size=size,
            sha256=digest,
            path=f"/workspace/shared/inputs/{fid}/{filename}",
            uri=f"inputs/{gid}/{fid}/{filename}",
        )

    store.upload.side_effect = upload

    async def chunks():
        for chunk in (content[:5], content[5:]):
            store.upload.assert_not_awaited()
            yield chunk

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/task-input-groups/{row['id']}/files",
            params={"filename": "数据.bin"},
            content=chunks(),
            headers={"Authorization": f"Bearer {issue_user_token(config, 'alice')}"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["sha256"] == hashlib.sha256(content).hexdigest()
    store.upload.assert_awaited_once()


@pytest.mark.parametrize("failure", ["size", "timeout", "cancel"])
async def test_failed_body_reception_never_reaches_object_write(monkeypatch, failure):
    config = settings()
    app = create_app(config)
    row = group()
    app.state.input_groups = AsyncMock()
    app.state.input_groups.get.return_value = row
    monkeypatch.setattr("bbx_blackboard.input_groups.FILE_LIMIT", 4)
    monkeypatch.setattr("bbx_blackboard.input_groups.UPLOAD_SECONDS", 0.01)

    async def chunks():
        if failure == "timeout":
            await asyncio.sleep(1)
        if failure == "cancel":
            raise asyncio.CancelledError()
        yield b"large"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await client.post(
                    f"/api/task-input-groups/{row['id']}/files?filename=data.bin",
                    content=chunks(),
                    headers={"Authorization": f"Bearer {issue_user_token(config, 'alice')}"},
                )
        else:
            response = await client.post(
                f"/api/task-input-groups/{row['id']}/files?filename=data.bin",
                content=chunks(),
                headers={"Authorization": f"Bearer {issue_user_token(config, 'alice')}"},
            )
            assert response.status_code == (413 if failure == "size" else 408)
    app.state.input_groups.upload.assert_not_awaited()


async def test_creation_retry_returns_original_profile_before_current_settings_reads():
    config = settings()
    app = create_app(config)
    gid = uuid4()
    app.state.input_groups = AsyncMock()
    app.state.input_groups.prior_task.return_value = {
        "id": gid,
        "agent_profile": "task-settings",
        "agent_profile_version": 7,
    }
    app.state.profile_store = AsyncMock()
    app.state.board_service = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/tasks",
            headers={"Authorization": f"Bearer {issue_user_token(config, 'alice')}"},
            json={
                "input_group_id": str(gid),
                "name": "Name",
                "goal": "Full goal",
                "acceptance": [{"id": "A1", "desc": "Done"}],
                "budget": {"max_cost": 2, "max_minutes": 10},
                "agent_profile": "default",
            },
        )
    assert response.status_code == 200
    assert response.json() == {
        "id": str(gid),
        "agent_profile": "task-settings",
        "agent_profile_version": 7,
    }
    app.state.profile_store.get.assert_not_awaited()
    app.state.profile_store.create.assert_not_awaited()
    app.state.board_service.create_task.assert_not_awaited()
