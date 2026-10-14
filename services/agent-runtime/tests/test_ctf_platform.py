"""Platform calls use native MAF tools without automatic write retries."""

import asyncio
import copy
import json
from types import SimpleNamespace as NS
from typing import Any, cast

import httpx
import pytest
from agent_framework import Agent, Content, FunctionInvocationContext
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.failures import propagate_tool_failure
from bbx_runtime.ctf.platform import PlatformAdapter
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.ctf.telemetry import ToolArgumentFeedbackMiddleware, ToolObservationMiddleware
from bbx_runtime.session import CheckpointHistoryProvider, ToolResultCheckpointMiddleware
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_runtime import MemoryCtf


class Checkpoint:
    def __init__(self):
        self.session = NS(state={})
        self.saved = {}

    async def save(self):
        self.saved = copy.deepcopy(self.session.state)


class Store:
    def __init__(self):
        self.data = {}

    async def put(self, uri, source, length):
        self.data[uri] = source.read()
        assert len(self.data[uri]) == length


class Service:
    def __init__(self, store):
        self.client = self
        self.store = store
        self.records = []
        self.registry = []
        self.denied = False
        self.begun = {}

    async def get_mcp_server(self, name, version):
        self.registry.append((name, version))
        return dict(name=name, version=version, enabled=True, url="http://fake", has_secret=False)

    async def runtime(self, task_id, operation, **body):
        if operation in {"authorize_platform_call", "begin_platform_call"}:
            if self.denied:
                raise ValueError("denied")
            if operation == "begin_platform_call":
                self.begun[body["call_id"]] = body
            return {}
        assert operation == "record_platform_result"
        assert body["call_id"] in self.begun
        result = json.loads(self.store.data[body["response_uri"]])
        self.records.append((body, result))
        return {"id": body["request_id"], "simulated": True}


def binding(name, purpose, read_only=False):
    return NS(
        server_name="fake",
        server_version=3,
        tool_name=name,
        purpose=purpose,
        result_adapter="fake_ctf_v1",
        read_only=read_only,
    )


def profile():
    bindings = [
        binding("start", "management"),
        binding("submit", "submit"),
        binding("status", "status", True),
        binding("ambiguous", "unknown"),
    ]
    grant = NS(name="fake", version=3, allowed_tools=[b.tool_name for b in bindings])
    return NS(
        platform_tools=bindings,
        worker_tools={role: NS(mcp_servers=[grant]) for role in ("lead", "teammate")},
    )


class FakeMcp:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.calls = []
        self.fail = False

    async def __call__(self, server, headers, name, arguments):
        assert server["version"] == 3 and not headers
        if name is None:
            return {
                "tools": [
                    {"name": b.tool_name, "inputSchema": {"type": "object"}}
                    for b in profile().platform_tools
                ]
            }
        assert self.checkpoint.saved["ctf_platform_calls"]
        self.calls.append((name, arguments))
        if self.fail and name == "submit":
            raise httpx.ReadError("response lost after write")
        return {
            "structuredContent": {
                "status": "accepted"
                if arguments.get("flag") == "correct" or name == "status"
                else "rejected",
                "target_id": "fake-target",
            }
        }


async def setup(role="teammate"):
    checkpoint, store = Checkpoint(), Store()
    service = Service(store)
    remote = FakeMcp(checkpoint)
    adapter = PlatformAdapter(service, lambda: store, remote)
    tools = await adapter.build(
        profile(), role, "task", "member-1", {"id": "turn", "generation": 1}, lambda: checkpoint
    )
    return tools, checkpoint, service, remote


async def run(tools, calls):
    client = ScriptedChatClient(
        [
            *[
                ScriptStep(
                    calls=(
                        ScriptToolCall(
                            name,
                            dict(challenge_id="challenge", expected_revision=1, arguments=args),
                        ),
                    )
                )
                for name, args in calls
            ],
            ScriptStep(text="done"),
        ]
    )
    async with Agent(
        client=client, instructions="Use configured fake platform", tools=tools
    ) as agent:
        result = await agent.run("perform scripted actions")
    assert result.text == "done"


async def test_permissions_and_fake_rejected_then_accepted():
    tools, _, service, remote = await setup()
    assert {t.name for t in tools} == {"platform_fake_submit", "platform_fake_status"}
    await run(
        tools,
        [
            ("platform_fake_submit", {"flag": "wrong"}),
            ("platform_fake_submit", {"flag": "correct"}),
        ],
    )
    assert [r[1]["structuredContent"]["status"] for r in service.records] == [
        "rejected",
        "accepted",
    ]
    assert len(remote.calls) == 2
    lead, *_ = await setup("lead")
    assert {t.name for t in lead} == {
        "platform_fake_start",
        "platform_fake_submit",
        "platform_fake_status",
    }


async def test_unknown_write_not_repeated_and_status_query_remains_available():
    tools, checkpoint, service, remote = await setup()
    remote.fail = True
    await run(
        tools,
        [
            ("platform_fake_submit", {"flag": "correct"}),
            ("platform_fake_submit", {"flag": "correct"}),
            ("platform_fake_status", {}),
        ],
    )
    assert [c[0] for c in remote.calls] == ["submit", "status"]
    assert service.records[0][1]["status"] == "unknown"
    assert any(e["state"] == "unknown" for e in checkpoint.saved["ctf_platform_calls"].values())


async def test_no_platform_needs_no_registry_or_credentials():
    store = Store()
    service = Service(store)
    p = NS(platform_tools=[], worker_tools={"lead": NS(mcp_servers=[])})
    assert (
        await PlatformAdapter(service, lambda: store).build(
            p, "lead", "task", "lead", {}, lambda: None
        )
        == []
    )
    assert not service.registry


async def test_preflight_denies_before_remote_write():
    tools, _, service, remote = await setup()
    service.denied = True
    await run(tools, [("platform_fake_submit", {"flag": "correct"})])
    assert not remote.calls and not service.records


async def test_native_preflight_revision_conflict_does_not_block_corrected_submission():
    tools, checkpoint, service, remote = await setup()
    original = service.runtime
    rejected = False

    async def conflict(task_id, operation, **body):
        nonlocal rejected
        if operation == "begin_platform_call" and not rejected:
            rejected = True
            raise RemoteError(409, "revision conflict", "revision_conflict")
        return await original(task_id, operation, **body)

    service.runtime = conflict
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "platform_fake_submit",
                        dict(challenge_id="challenge", expected_revision=revision, arguments={}),
                    ),
                )
            )
            for revision in (1, 2)
        ]
        + [ScriptStep(text="done")]
    )
    async with Agent(client=client, tools=tools) as agent:
        await agent.run("synthetic submission")
    assert len(remote.calls) == len(service.records) == 1
    assert [entry["state"] for entry in checkpoint.saved["ctf_platform_calls"].values()] == [
        "rejected",
        "received",
    ]


def invocation_context(item, occurrence):
    context = FunctionInvocationContext(function=item, arguments={})
    context.metadata["function_call_occurrence_id"] = occurrence
    return context


async def test_restored_explicit_preflight_rejection_reuses_call_id():
    tools, checkpoint, service, remote = await setup()
    item = next(item for item in tools if item.name == "platform_fake_submit")
    original = service.runtime

    async def conflict(task_id, operation, **body):
        if operation == "begin_platform_call":
            raise RemoteError(409, "revision conflict", "revision_conflict")
        return await original(task_id, operation, **body)

    service.runtime = conflict
    arguments = dict(challenge_id="challenge", expected_revision=1, arguments={})
    feedback = await item.invoke(
        arguments=arguments, context=invocation_context(item, "same"), skip_parsing=True
    )
    assert isinstance(feedback, Content)
    assert feedback.additional_properties["ctf_tool_error"] == "revision_conflict"
    call_id = checkpoint.saved["ctf_platform_calls"]["turn:same"]["call_id"]
    checkpoint.session.state = copy.deepcopy(checkpoint.saved)
    service.runtime = original
    await item.invoke(
        arguments={**arguments, "expected_revision": 2},
        context=invocation_context(item, "same"),
    )
    assert len(remote.calls) == len(service.records) == 1
    assert service.records[0][0]["call_id"] == call_id


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadError("synthetic unknown begin"),
        RemoteError(503, "synthetic unknown begin"),
        asyncio.CancelledError("synthetic cancelled begin"),
    ],
)
@pytest.mark.parametrize("restored_occurrence", ["same", "new"])
async def test_unconfirmed_preflight_never_replays_on_restore(failure, restored_occurrence):
    tools, checkpoint, service, remote = await setup()
    item = next(item for item in tools if item.name == "platform_fake_submit")
    original = service.runtime

    async def unknown_begin(task_id, operation, **body):
        if operation == "begin_platform_call":
            await original(task_id, operation, **body)
            raise failure
        return await original(task_id, operation, **body)

    service.runtime = unknown_begin
    arguments = dict(challenge_id="challenge", expected_revision=1, arguments={})
    with pytest.raises(type(failure)):
        await item.invoke(arguments=arguments, context=invocation_context(item, "same"))
    checkpoint.session.state = copy.deepcopy(checkpoint.saved)
    service.runtime = original
    result = await item.invoke(
        arguments=arguments,
        context=invocation_context(item, restored_occurrence),
        skip_parsing=True,
    )
    assert json.loads(result)["status"] == "unknown"
    assert not remote.calls and not service.records


async def test_dispatched_checkpoint_failure_does_not_send_or_allow_replay():
    tools, checkpoint, service, remote = await setup()
    item = next(item for item in tools if item.name == "platform_fake_submit")
    original_save = checkpoint.save

    async def lose_dispatch_save():
        await original_save()
        if any(
            entry["state"] == "dispatched"
            for entry in checkpoint.saved["ctf_platform_calls"].values()
        ):
            raise httpx.ReadError("synthetic lost dispatch checkpoint response")

    checkpoint.save = lose_dispatch_save
    arguments = dict(challenge_id="challenge", expected_revision=1, arguments={})
    with pytest.raises(httpx.ReadError):
        await item.invoke(arguments=arguments, context=invocation_context(item, "first"))
    checkpoint.save = original_save
    checkpoint.session.state = copy.deepcopy(checkpoint.saved)
    result = await item.invoke(
        arguments=arguments, context=invocation_context(item, "second"), skip_parsing=True
    )
    assert json.loads(result)["status"] == "unknown"
    assert len(service.begun) == 1
    assert not remote.calls and not service.records


async def native_platform_setup(failure, stage):
    store = Store()

    class NativeService(MemoryCtf):
        def __init__(self):
            super().__init__()
            self.client = self
            self.begun = {}
            self.records = []
            self.rejected = False

        async def get_mcp_server(self, name, version):
            return dict(
                name=name, version=version, enabled=True, url="http://fake", has_secret=False
            )

        async def runtime(self, task_id, operation, **body):
            if operation == stage and not self.rejected:
                self.rejected = True
                raise failure
            if operation == "authorize_platform_call":
                return {}
            if operation == "begin_platform_call":
                self.begun[body["call_id"]] = body
                return {}
            if operation == "record_platform_result":
                assert body["call_id"] in self.begun
                self.records.append(body)
                return {"id": body["request_id"], "simulated": True}
            return await super().runtime(task_id, operation, **body)

    service = NativeService()
    turn = {"id": "turn", "generation": 1}
    checkpoint = await load_checkpoint(
        cast(Any, service), "task", "member-1", turn, "Synthetic platform"
    )
    requests = []

    async def remote(server, headers, name, arguments):
        if name is None:
            return {
                "tools": [
                    {"name": b.tool_name, "inputSchema": {"type": "object"}}
                    for b in profile().platform_tools
                ]
            }
        assert service.begun
        assert service.saved is not None
        assert any(
            item["state"] == "dispatched"
            for item in service.saved["session"]["state"]["ctf_platform_calls"].values()
        )
        requests.append(arguments)
        return {"structuredContent": {"status": "accepted"}}

    tools = await PlatformAdapter(service, lambda: store, remote).build(
        profile(), "teammate", "task", "member-1", turn, lambda: checkpoint
    )
    return service, checkpoint, requests, tools


def native_platform_agent(service, checkpoint, tools, client):
    return Agent(
        client=client,
        tools=tools,
        context_providers=[CheckpointHistoryProvider(checkpoint)],
        middleware=[
            ToolObservationMiddleware(service, "task", "member-1", {"id": "turn", "generation": 1}),
            ToolResultCheckpointMiddleware(checkpoint),
            ToolArgumentFeedbackMiddleware(),
        ],
        require_per_service_call_history_persistence=True,
    )


@pytest.mark.parametrize("stage", ["authorize_platform_call", "begin_platform_call"])
async def test_production_native_preflight_feedback_is_saved_and_corrects_before_single_mcp(stage):
    error = RemoteError(
        409, "synthetic private conflict", "revision_conflict", detail={"current_revision": 2}
    )
    service, checkpoint, requests, tools = await native_platform_setup(error, stage)
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "platform_fake_submit",
                        dict(challenge_id="challenge", expected_revision=1, arguments={}),
                        call_id="conflict",
                    ),
                )
            ),
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "platform_fake_submit",
                        dict(challenge_id="challenge", expected_revision=2, arguments={}),
                    ),
                ),
                expect_contains='"current_revision": 2',
            ),
            ScriptStep(text="done"),
        ]
    )
    async with native_platform_agent(service, checkpoint, tools, client) as agent:
        with propagate_tool_failure():
            result = await agent.run("Synthetic CAS correction", session=checkpoint.session)
    assert result.text == "done"
    assert len(requests) == len(service.records) == 1
    assert all(option.get("tool_choice") != "none" for option in client.received_options)
    assert service.saved is not None
    saved = service.saved["session"]["state"]
    result_item = next(
        content
        for message in saved["in_memory"]["messages"]
        for content in message["contents"]
        if content.get("type") == "function_result" and content.get("call_id") == "conflict"
    )["items"][0]
    assert result_item["additional_properties"]["ctf_tool_error"] == "revision_conflict"
    assert "synthetic private conflict" not in result_item["text"]
    assert service.observations[1]["body"]["tool_error_code"] == "revision_conflict"
    assert [item["state"] for item in saved["ctf_platform_calls"].values()] == (
        ["received"] if stage == "authorize_platform_call" else ["rejected", "received"]
    )


@pytest.mark.parametrize("stage", ["authorize_platform_call", "begin_platform_call"])
@pytest.mark.parametrize(
    "error",
    [
        RemoteError(401, "synthetic private authentication"),
        RemoteError(403, "synthetic private forbidden", "permission_revoked"),
        RemoteError(409, "synthetic private fence", "generation_revoked"),
        RemoteError(409, "Task is not running"),
        RemoteError(409, "synthetic private budget", "ctf_budget_exhausted"),
        RemoteError(422, "synthetic private unknown arguments"),
        RemoteError(503, "synthetic private service unavailable"),
    ],
)
async def test_production_native_preflight_control_refusal_never_reaches_model_or_mcp(error, stage):
    service, checkpoint, requests, tools = await native_platform_setup(error, stage)
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "platform_fake_submit",
                        dict(challenge_id="challenge", expected_revision=1, arguments={}),
                    ),
                )
            ),
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "platform_fake_submit",
                        dict(challenge_id="challenge", expected_revision=2, arguments={}),
                    ),
                )
            ),
            ScriptStep(text="must not execute"),
        ]
    )
    async with native_platform_agent(service, checkpoint, tools, client) as agent:
        with pytest.raises(RemoteError) as caught, propagate_tool_failure():
            await agent.run("Synthetic denied call", session=checkpoint.session)
    assert caught.value is error
    assert not requests and not service.records
    assert all(option.get("tool_choice") == "none" for option in client.received_options[1:])
    assert "synthetic private" not in json.dumps(
        [
            content.to_dict()
            for messages in client.received_messages
            for message in messages
            for content in message.contents
        ]
    )
    entries = checkpoint.session.state.get("ctf_platform_calls", {})
    if stage == "authorize_platform_call":
        assert not entries
    else:
        assert len(entries) == 1


async def test_preflight_rejection_checkpoint_failure_is_fatal_before_feedback_or_mcp():
    error = RemoteError(409, "synthetic private conflict", "revision_conflict")
    service, checkpoint, requests, tools = await native_platform_setup(error, "begin_platform_call")
    original_save = checkpoint.save
    original = httpx.ReadError("synthetic private rejection checkpoint failure")

    async def failed_rejection_save():
        if any(
            item["state"] == "rejected"
            for item in checkpoint.session.state.get("ctf_platform_calls", {}).values()
        ):
            raise original
        await original_save()

    checkpoint.save = failed_rejection_save
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "platform_fake_submit",
                        dict(challenge_id="challenge", expected_revision=1, arguments={}),
                    ),
                )
            ),
            ScriptStep(text="must not continue"),
        ]
    )
    async with native_platform_agent(service, checkpoint, tools, client) as agent:
        with pytest.raises(httpx.ReadError) as caught, propagate_tool_failure():
            await agent.run("Synthetic rejection save failure", session=checkpoint.session)
    assert caught.value is original
    assert not requests and not service.records


async def test_native_zero_revision_is_corrected_locally_before_single_authorized_dispatch():
    service, checkpoint, requests, tools = await native_platform_setup(None, None)
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "platform_fake_submit",
                        dict(challenge_id="challenge", expected_revision=0, arguments={}),
                        call_id="zero-revision",
                    ),
                )
            ),
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "platform_fake_submit",
                        dict(challenge_id="challenge", expected_revision=1, arguments={}),
                    ),
                ),
                expect_contains="greater_than_equal",
            ),
            ScriptStep(text="done"),
        ]
    )
    async with native_platform_agent(service, checkpoint, tools, client) as agent:
        result = await agent.run("Synthetic revision correction", session=checkpoint.session)
    assert result.text == "done"
    assert len(requests) == len(service.records) == len(service.begun) == 1
    assert next(iter(service.begun.values()))["expected_revision"] == 1
    assert len(checkpoint.session.state["ctf_platform_calls"]) == 1
    assert service.observations[1]["body"]["tool_error_code"] == "invalid_arguments"
    assert service.saved is not None
    feedback = next(
        content
        for message in service.saved["session"]["state"]["in_memory"]["messages"]
        for content in message["contents"]
        if content.get("type") == "function_result" and content.get("call_id") == "zero-revision"
    )["items"][0]
    assert json.loads(feedback["text"])["error"]["fields"] == [
        {"path": ["expected_revision"], "type": "greater_than_equal"}
    ]


async def test_cancelled_call_records_unknown_and_restart_does_not_repeat():
    import asyncio

    import pytest

    tools, checkpoint, service, remote = await setup()
    original = remote.__call__

    async def cancelled(server, headers, name, arguments):
        if name is not None:
            remote.calls.append((name, arguments))
            raise asyncio.CancelledError
        return await original(server, headers, name, arguments)

    adapter = PlatformAdapter(service, lambda: service.store, cancelled)
    tools = await adapter.build(
        profile(),
        "teammate",
        "task",
        "member-1",
        {"id": "turn", "generation": 1},
        lambda: checkpoint,
    )
    with pytest.raises(asyncio.CancelledError):
        await run(tools, [("platform_fake_submit", {"flag": "correct"})])
    assert service.records[0][1]["status"] == "unknown"
    checkpoint.session.state = copy.deepcopy(checkpoint.saved)
    restored = PlatformAdapter(service, lambda: service.store, remote)
    tools = await restored.build(
        profile(),
        "teammate",
        "task",
        "member-1",
        {"id": "new-turn", "generation": 2},
        lambda: checkpoint,
    )
    await run(tools, [("platform_fake_submit", {"flag": "correct"})])
    assert len(remote.calls) == 1


async def test_inflight_response_is_recorded_after_stop_disallows_new_calls():
    tools, checkpoint, service, remote = await setup("lead")
    original = remote.__call__

    async def stopping(server, headers, name, arguments):
        if name is not None:
            assert service.begun
            service.denied = True
        return await original(server, headers, name, arguments)

    adapter = PlatformAdapter(service, lambda: service.store, stopping)
    tools = await adapter.build(
        profile(), "lead", "task", "lead", {"id": "turn", "generation": 1}, lambda: checkpoint
    )
    await run(tools, [("platform_fake_start", {}), ("platform_fake_start", {"second": True})])
    assert len(remote.calls) == 1
    assert len(service.records) == 1
    assert service.records[0][1]["structuredContent"]["target_id"] == "fake-target"
    assert service.records[0][0]["call_id"] in service.begun


async def test_native_maf_full_fake_platform_lifecycle_and_role_filtering():
    checkpoint, store = Checkpoint(), Store()
    service = Service(store)
    configured = profile()
    configured.platform_tools.extend(
        [binding("connect", "connect"), binding("close", "management")]
    )
    for worker in configured.worker_tools.values():
        worker.mcp_servers = [
            NS(
                name="fake",
                version=3,
                allowed_tools=[b.tool_name for b in configured.platform_tools],
            )
        ]
    actions = []
    target_state = "absent"

    async def lifecycle(server, headers, name, arguments):
        nonlocal target_state
        if name is None:
            return {
                "tools": [
                    {"name": b.tool_name, "inputSchema": {"type": "object"}}
                    for b in configured.platform_tools
                ]
            }
        assert service.begun and checkpoint.saved["ctf_platform_calls"]
        actions.append(name)
        if name == "start":
            assert target_state == "absent"
            target_state = "running"
        elif name == "close":
            assert target_state == "running"
            target_state = "closed"
        else:
            assert target_state == "running"
        return {
            "structuredContent": {
                "target_id": "fake-lifecycle-target",
                "target_status": target_state,
                "status": "accepted" if name == "submit" else "unknown",
                **({"connection": "fake://target"} if name == "connect" else {}),
            }
        }

    adapter = PlatformAdapter(service, lambda: store, lifecycle)
    turn = {"id": "lifecycle-turn", "generation": 1}
    lead = await adapter.build(configured, "lead", "task", "lead", turn, lambda: checkpoint)
    teammate = await adapter.build(
        configured, "teammate", "task", "member-1", turn, lambda: checkpoint
    )
    assert {item.name for item in lead} == {
        f"platform_fake_{name}" for name in ("start", "connect", "close", "submit", "status")
    }
    assert {item.name for item in teammate} == {"platform_fake_submit", "platform_fake_status"}
    await run(
        lead,
        [
            (f"platform_fake_{name}", {"flag": "correct"} if name == "submit" else {})
            for name in ("start", "connect", "submit", "status", "close")
        ],
    )
    assert actions == ["start", "connect", "submit", "status", "close"]
    assert target_state == "closed"
    results = [result["structuredContent"] for _, result in service.records]
    assert results[1]["connection"] == "fake://target"
    assert results[2]["status"] == "accepted"
    assert results[-1]["target_status"] == "closed"
    assert all(result["target_id"] == "fake-lifecycle-target" for result in results)
