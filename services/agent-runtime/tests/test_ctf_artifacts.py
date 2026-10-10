"""Workspace artifacts are immutable snapshots, never model-provided trusted URIs."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.artifacts import ArtifactRegistrar, artifact_path


class Store:
    def __init__(self, barrier=None):
        self.data = {}
        self.removed = []
        self.barrier = barrier

    async def put(self, uri, source, length):
        content = source.read()
        assert len(content) == length
        self.data[uri] = content
        if self.barrier:
            await self.barrier.wait()

    async def remove(self, uri):
        self.removed.append(uri)
        self.data.pop(uri, None)


class Files:
    def __init__(self, content=b"script"):
        self.content = content
        self.scopes = []

    async def stat(self, path, *, scope_agent_id):
        self.scopes.append(("stat", scope_agent_id))
        return {"is_file": True, "size": len(self.content)}

    @asynccontextmanager
    async def file_stream(self, path, *, scope_agent_id):
        self.scopes.append(("stream", scope_agent_id))
        yield httpx.Response(200, content=self.content)


class Service:
    def __init__(self):
        self.records = {}
        self.failure: Exception | None = None
        self.lose_response = False

    async def runtime(self, task_id, operation, **body):
        if operation == "authorize_member":
            return {}
        if operation == "lookup_artifact":
            return self.records.get(body["request_id"])
        assert operation == "register_artifact"
        if self.failure:
            raise self.failure
        prior = self.records.get(body["request_id"])
        if prior:
            if prior["sha256"] != body["sha256"]:
                raise RemoteError(409, "request conflict")
            return prior
        self.records[body["request_id"]] = {**body, "id": str(uuid4())}
        if self.lose_response:
            self.lose_response = False
            raise httpx.ReadError("committed response lost")
        return self.records[body["request_id"]]


TURN = {"id": "turn", "generation": 1}
PATH = "/workspace/agents/agent-2/solve.py"


async def test_artifact_reads_scoped_snapshot_and_computes_metadata_then_reconciles_commit():
    files, store, service = Files(b"proof"), Store(), Service()
    service.lose_response = True
    registrar = ArtifactRegistrar(cast(Any, service), files, lambda: store)
    request = str(uuid4())
    result = await registrar.register("task", "member-1", TURN, PATH, request)
    assert result["sha256"] == hashlib.sha256(b"proof").hexdigest()
    assert result["size"] == 5 and result["filename"] == "solve.py"
    assert store.data[result["uri"]] == b"proof"
    assert files.scopes == [("stat", "agent-2"), ("stream", "agent-2")]
    assert not store.removed
    files.content = b"later content"
    assert await registrar.register("task", "member-1", TURN, PATH, request) == result
    assert len(store.data) == 1


async def test_failed_registration_reclaims_only_unregistered_unique_object():
    files, store, service = Files(), Store(), Service()
    service.failure = RemoteError(409, "generation revoked")
    registrar = ArtifactRegistrar(cast(Any, service), files, lambda: store)
    with pytest.raises(RemoteError):
        await registrar.register("task", "member-1", TURN, PATH, str(uuid4()))
    assert not store.data
    assert len(store.removed) == 1


async def test_uncertain_registration_never_deletes_a_possibly_late_commit():
    files, store, service = Files(), Store(), Service()
    service.failure = httpx.ReadError("response unknown")
    registrar = ArtifactRegistrar(cast(Any, service), files, lambda: store)
    with pytest.raises(httpx.ReadError):
        await registrar.register("task", "member-1", TURN, PATH, str(uuid4()))
    assert len(store.data) == 1
    assert not store.removed


async def test_concurrent_same_request_different_bytes_never_overwrites_registered_object():
    store, service = Store(asyncio.Barrier(2)), Service()
    request = str(uuid4())
    first = ArtifactRegistrar(cast(Any, service), Files(b"first"), lambda: store)
    second = ArtifactRegistrar(cast(Any, service), Files(b"second"), lambda: store)
    results = await asyncio.gather(
        first.register("task", "member-1", TURN, PATH, request),
        second.register("task", "member-1", TURN, PATH, request),
        return_exceptions=True,
    )
    winner = service.records[request]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert hashlib.sha256(store.data[winner["uri"]]).hexdigest() == winner["sha256"]
    assert winner["uri"] not in store.removed
    assert len(store.data) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/workspace/agents/agent-1/a",
        "/workspace/shared/../secret",
        "relative.py",
        "/workspace/shared",
    ],
)
def test_artifact_path_rejects_foreign_or_noncanonical_paths(path):
    with pytest.raises(ValueError):
        artifact_path(path, "member-1")


def test_shared_script_path_is_allowed():
    assert (
        artifact_path("/workspace/shared/ctf/solve.py", "member-1")
        == "/workspace/shared/ctf/solve.py"
    )


async def test_native_maf_board_tools_read_record_and_append_registered_artifact_reference():
    from agent_framework import Agent
    from bbx_runtime.ctf.tools import build_tools
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall

    calls = []

    class ToolService:
        async def tool(self, tid, token, operation, **body):
            calls.append((operation, body))
            assert token == "member-token"
            if operation == "list_records":
                return [{"body": "route A failed because nonce expires", "artifact_ids": []}]
            if operation == "append_record":
                from bbx_contracts.ctf import CtfRecordAppendRequest

                CtfRecordAppendRequest.model_validate(
                    {k: v for k, v in body.items() if k != "challenge_id"}
                )
            return {"id": str(uuid4()), **body}

    artifact_id = str(uuid4())
    tools = build_tools(
        cast(Any, ToolService()),
        "task",
        {**TURN, "token": "member-token"},
        "teammate",
        member_id="member-1",
    )
    by_name = {item.name: item for item in tools}
    assert set(by_name["register_artifact"].parameters()["properties"]) == {"path"}
    assert "post_fact" not in by_name and "post_intent" not in by_name
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("list_challenges", {"offset": 500, "limit": 50}),)),
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "list_records", {"challenge_id": "challenge", "offset": 100, "limit": 25}
                    ),
                )
            ),
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "append_record",
                        {
                            "challenge_id": "challenge",
                            "body": "copied the script; tested nonce refresh",
                            "artifact_ids": [artifact_id],
                        },
                    ),
                ),
                expect_contains="nonce expires",
            ),
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "send_message",
                        {
                            "recipient_id": "member-2",
                            "body": "refresh nonce first",
                            "reply_to": None,
                        },
                    ),
                )
            ),
            ScriptStep(text="shared the observation"),
        ]
    )
    async with Agent(
        client=client, instructions="Work with shared CTF records", tools=tools
    ) as agent:
        response = await agent.run("Read existing attempts and append your observation")
    assert response.text == "shared the observation"
    assert [operation for operation, _ in calls] == [
        "list_challenges",
        "list_records",
        "append_record",
        "post_message",
    ]
    assert calls[0][1] == {"offset": 500, "limit": 50}
    assert calls[1][1]["offset"] == 100 and calls[1][1]["limit"] == 25
    assert calls[2][1]["artifact_ids"] == [artifact_id]
    assert calls[2][1]["kind"] == "note"
    assert calls[3][1]["reply_to"] is None
    assert all("actor" not in body and "uri" not in body for _, body in calls)


async def test_cancelled_registration_preserves_object_when_server_commits_later():
    started, release = asyncio.Event(), asyncio.Event()

    class LateService(Service):
        pending: asyncio.Task | None = None

        async def runtime(self, task_id, operation, **body):
            if operation != "register_artifact":
                return await super().runtime(task_id, operation, **body)

            async def commit():
                started.set()
                await release.wait()
                self.records[body["request_id"]] = {**body, "id": str(uuid4())}
                return self.records[body["request_id"]]

            self.pending = asyncio.create_task(commit())
            return await asyncio.shield(self.pending)

    service, store = LateService(), Store()
    registrar = ArtifactRegistrar(cast(Any, service), Files(), lambda: store)
    request_id = str(uuid4())
    task = asyncio.create_task(registrar.register("task", "member-1", TURN, PATH, request_id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service.records
    assert not store.removed and len(store.data) == 1
    release.set()
    assert service.pending is not None
    saved = await service.pending
    assert store.data[saved["uri"]] == b"script"
