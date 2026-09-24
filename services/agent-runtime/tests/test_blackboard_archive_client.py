"""Runtime archive command, task pagination, and one SSE connection."""

import json
from uuid import uuid4

import httpx
import pytest
from bbx_runtime.clients.blackboard import BlackboardClient


@pytest.mark.asyncio
async def test_archive_command_and_all_task_pages() -> None:
    task_id = uuid4()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer service-test"
        if request.url.path == "/api/tasks":
            offset = int(request.url.params["offset"])
            seen.append(("tasks", str(offset)))
            count = 500 if offset == 0 else 2
            return httpx.Response(200, json=[{"id": offset + index} for index in range(count)])
        assert request.url.path == f"/api/tasks/{task_id}/archive"
        assert request.method == "POST"
        seen.append(("archive", request.content.decode()))
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        board = BlackboardClient("http://test", "service-test", http)
        tasks = await board.list_tasks()
        assert len(tasks) == 502
        assert tasks[0]["id"] == 0 and tasks[-1]["id"] == 501
        assert (
            await board.record_archive(task_id, f"workspace/{task_id}.tar.zst", 1234, "agents-only")
            == []
        )
    assert seen[:2] == [("tasks", "0"), ("tasks", "500")]
    assert json.loads(seen[-1][1]) == {
        "uri": f"workspace/{task_id}.tar.zst",
        "size": 1234,
        "fallback": "agents-only",
    }


@pytest.mark.asyncio
async def test_sse_stream_yields_full_events_and_ignores_heartbeat() -> None:
    task_id = uuid4()
    first = {"version": 10, "type": "fact.posted", "payload": {"id": "F1"}}
    second = {"version": 11, "type": "intent.posted", "payload": {"id": "I1"}}
    body = (
        ": ping\n\n"
        f"id: 10\nevent: fact.posted\ndata: {json.dumps(first)}\n\n"
        f"id: 11\nevent: intent.posted\ndata: {json.dumps(second)}\n\n"
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        board = BlackboardClient("http://test", "service-test", http)
        events = [event async for event in board.stream(task_id, since=9)]
    assert events == [first, second]
    assert str(seen[0].url) == f"http://test/api/tasks/{task_id}/stream?since=9"
    assert seen[0].headers["authorization"] == "Bearer service-test"
    assert seen[0].extensions["timeout"]["read"] is None


@pytest.mark.asyncio
async def test_sse_rejects_disagreeing_frame_metadata() -> None:
    task_id = uuid4()
    body = 'id: 10\nevent: fact.posted\ndata: {"version": 9, "type": "fact.posted"}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        board = BlackboardClient("http://test", "service-test", http)
        with pytest.raises(ValueError, match="disagrees"):
            _ = [event async for event in board.stream(task_id)]
