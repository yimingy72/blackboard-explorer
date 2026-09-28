"""HTTP clients preserve identity, payloads, and readable remote errors."""

import json
from uuid import uuid4

import httpx
import pytest
from bbx_runtime.clients import BlackboardClient, EnvdClient, RemoteError
from bbx_runtime.testing.fake_envd import FakeEnvd


@pytest.mark.asyncio
async def test_blackboard_client_identity_and_error() -> None:
    task_id = uuid4()
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/facts"):
            return httpx.Response(422, json={"code": "evidence_missing", "message": "请先上传证据"})
        if request.url.path.endswith("/agents"):
            return httpx.Response(200, json={"agent_id": "agent-1", "token": "agent-token"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"version": 3}])
        if request.url.path.endswith("/evidence"):
            if request.url.params["uri"].startswith("evidence/other-task/"):
                return httpx.Response(403, json={"detail": "Task access denied"})
            return httpx.Response(200, content=b"proof")
        return httpx.Response(200, json={"task": {"status": "running"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as http:
        client = BlackboardClient("http://blackboard:8000", "service-token", http)
        assert (await client.state(task_id))["task"]["status"] == "running"
        assert (await client.events(task_id, 2, "agent-1"))[0]["version"] == 3
        registered = await client.register_agent(task_id, "explore", is_seed=True)
        assert registered == {"agent_id": "agent-1", "token": "agent-token"}
        assert "derive_parallel" not in json.loads(seen[-1].content)
        await client.register_agent(task_id, "derive", derive_parallel=True)
        assert json.loads(seen[-1].content)["derive_parallel"] is True
        await client.register_agent(task_id, "derive", derive_review=True)
        assert json.loads(seen[-1].content)["derive_review"] is True
        agent = client.with_token(registered["token"])
        with pytest.raises(RemoteError) as caught:
            await agent.post_fact(task_id, {"statement": "x"}, dry_run=True)
        assert (caught.value.status, caught.value.code, caught.value.message) == (
            422,
            "evidence_missing",
            "请先上传证据",
        )
        assert seen[-1].headers["Authorization"] == "Bearer agent-token"
        assert seen[-1].url.params["dry_run"] == "true"
        assert seen[1].url.params["for"] == "agent-1"
        assert await agent.read_evidence(f"evidence/{task_id}/agent-1/proof.txt") == b"proof"
        with pytest.raises(RemoteError) as denied:
            await agent.read_evidence("evidence/other-task/agent-2/proof.txt")
        assert denied.value.status == 403
        await agent.close()
        assert not http.is_closed


@pytest.mark.asyncio
async def test_envd_client_files_and_archive_stream() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/users":
            return httpx.Response(
                200, json={"agent_id": "agent-1", "home": "/workspace/agents/agent-1"}
            )
        if request.url.path == "/stat":
            return httpx.Response(
                200, json={"exists": True, "size": 4, "is_file": True, "is_dir": False}
            )
        if request.url.path == "/files":
            return httpx.Response(200, content=b"data")
        return httpx.Response(200, content=b"archive", headers={"X-Archive-Fallback": "none"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as http:
        client = EnvdClient("http://envd:8080", "task-token", http)
        assert (await client.health())["status"] == "ok"
        assert (await client.create_user("agent-1"))["home"].endswith("agent-1")
        assert (await client.stat("/workspace/agents/agent-1/a"))["size"] == 4
        assert await client.read_file("/workspace/agents/agent-1/a") == b"data"
        async with client.archive_stream() as response:
            assert response.headers["X-Archive-Fallback"] == "none"
            assert b"".join([part async for part in response.aiter_bytes()]) == b"archive"
        assert all(request.headers["Authorization"] == "Bearer task-token" for request in seen)
        await client.close()
        assert not http.is_closed


@pytest.mark.asyncio
async def test_envd_error_message() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"error": "file too large"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as http:
        client = EnvdClient("http://envd:8080", "task-token", http)
        with pytest.raises(RemoteError) as caught:
            await client.read_file("/workspace/large")
        assert (caught.value.status, caught.value.message) == (413, "file too large")


@pytest.mark.asyncio
async def test_envd_client_against_fake_envd(tmp_path) -> None:
    fake = FakeEnvd(tmp_path, token="test-token")
    async with fake:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake.app), trust_env=False
        ) as http:
            client = EnvdClient("http://envd.test", "test-token", http)
            assert await client.health() == {"status": "ok"}
            assert (await client.create_user("agent-1"))["home"] == "/workspace/agents/agent-1"
            fake.put_file("/workspace/agents/agent-1/evidence.txt", b"proof")
            path = "/workspace/agents/agent-1/evidence.txt"
            assert (await client.stat(path))["size"] == 5
            assert await client.read_file(path) == b"proof"
            async with client.archive_stream() as response:
                assert response.headers["X-Archive-Fallback"] == "none"
                assert len(b"".join([chunk async for chunk in response.aiter_bytes()])) > 0
