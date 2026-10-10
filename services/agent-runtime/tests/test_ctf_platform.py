"""Platform calls use native MAF tools without automatic write retries."""

import copy
import json
from types import SimpleNamespace as NS

import httpx
from agent_framework import Agent
from bbx_runtime.ctf.platform import PlatformAdapter
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall


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
