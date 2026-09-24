"""M2b middleware through actual MAF tool and chat loops, without network calls."""

from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from agent_framework import Agent, Content, Message, tool
from bbx_contracts.models import Price, Usage
from bbx_objects import ObjectStore
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.context import RunContext
from bbx_runtime.middleware import (
    BoardSyncMiddleware,
    GraceGateMiddleware,
    ToolLogMiddleware,
    _append_call_id,
    _render_events,
    append_board_update,
)
from bbx_runtime.models import load_runtime_profile
from bbx_runtime.testing.scripted_client import (
    ScriptedChatClient,
    ScriptStep,
    ScriptToolCall,
    ScriptUsage,
)

PROFILE_DIR = Path(__file__).resolve().parents[3] / "profiles" / "default"


class FakeService:
    def __init__(self) -> None:
        self.status = "running"
        self.last_seen = 0
        self.conclude_requested_at: str | None = None
        self.conclude_reason: str | None = None
        self.grace: list[int | RemoteError] = []
        self.logged: list[dict[str, Any]] = []
        self.beats: list[dict[str, Any]] = []

    async def state(self, _task_id: str) -> dict[str, Any]:
        return {
            "agents": {
                "agent-1": {
                    "status": self.status,
                    "last_seen_version": self.last_seen,
                    "conclude_requested_at": self.conclude_requested_at,
                    "conclude_reason": self.conclude_reason,
                }
            }
        }

    async def take_grace(self, _task_id: str, _agent_id: str) -> int:
        value = self.grace.pop(0)
        if isinstance(value, RemoteError):
            raise value
        return value

    async def record_tool_call(self, _task_id: str, call: dict[str, Any]) -> list:
        self.logged.append(call)
        return []

    async def heartbeat(self, _task_id: str, _agent_id: str, **kwargs: Any) -> dict:
        self.beats.append(kwargs)
        self.last_seen = kwargs["last_seen_version"]
        return {"warning": None}


class FakeBoard:
    def __init__(self) -> None:
        self.log: list[dict[str, Any]] = []
        self.requests: list[tuple[int, str | None]] = []

    async def events(
        self, _task_id: str, since: int = 0, for_agent: str | None = None
    ) -> list[dict[str, Any]]:
        self.requests.append((since, for_agent))
        return [event for event in self.log if event["version"] > since]


class FakeObjects:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def put(self, uri: str, data: bytes, **_kwargs: Any) -> None:
        self.files[uri] = data


def run_context(
    service: FakeService, board: FakeBoard | None = None, objects: FakeObjects | None = None
) -> RunContext:
    profile = load_runtime_profile(PROFILE_DIR)
    return RunContext(
        task_id="00000000-0000-0000-0000-000000000001",
        agent_id="agent-1",
        task_type="explore",
        state={"task": {"params": {"delta_max_lines": 1, "conclude_grace_calls": 3}}},
        profile=profile,
        service=cast(BlackboardClient, service),
        board=cast(BlackboardClient, board or FakeBoard()),
        objects=cast(ObjectStore, objects or FakeObjects()),
    )


def message_text(messages) -> str:
    return "\n".join(
        part
        for message in messages
        for part in [
            message.text,
            *(str(item.result) for item in message.contents if item.type == "function_result"),
        ]
    )


async def test_tool_log_records_complete_result_and_appends_same_call_id():
    service, objects = FakeService(), FakeObjects()
    ctx = run_context(service, objects=objects)

    @tool
    async def execute_command() -> str:
        return "完整命令输出：" + "x" * 5000

    client = ScriptedChatClient(
        [ScriptStep(calls=(ScriptToolCall("execute_command"),)), ScriptStep(text="done")]
    )
    assert (
        await Agent(
            client=client, tools=[execute_command], middleware=[ToolLogMiddleware(ctx)]
        ).run("start")
    ).text == "done"
    assert len(service.logged) == 1
    record = service.logged[0]
    assert record["agent_id"] == "agent-1"
    assert record["tool"] == "execute_command"
    assert len(record["id"]) == 14 and record["id"].startswith("c_")
    assert len(record["result_head"].encode()) <= 4096
    assert record["result_uri"] in objects.files
    assert b"x" * 5000 in objects.files[record["result_uri"]]
    assert f"[call_id: {record['id']}]" in message_text(client.received_messages[1])


async def test_tool_log_records_failed_local_call_without_exposing_exception_details():
    service, objects = FakeService(), FakeObjects()
    ctx = run_context(service, objects=objects)

    @tool
    async def failing_tool() -> str:
        raise ValueError("internal-only detail")

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("failing_tool"),)),
            ScriptStep(text="retry later", expect_contains="工具调用失败"),
        ]
    )
    response = await Agent(
        client=client, tools=[failing_tool], middleware=[ToolLogMiddleware(ctx)]
    ).run("start")
    assert response.text == "retry later"
    assert len(service.logged) == 1
    record = service.logged[0]
    assert record["tool"] == "failing_tool"
    assert "ValueError" in record["result_head"]
    assert "internal-only detail" not in record["result_head"]
    assert f"[call_id: {record['id']}]" in message_text(client.received_messages[1])
    assert record["result_uri"] in objects.files


def test_mcp_content_list_keeps_original_items_when_call_id_is_appended():
    original = Content.from_text("<command_output>full MCP output</command_output>")
    updated = _append_call_id([original], "c_abcdefghijkl")
    assert isinstance(updated, list)
    assert updated[0] is original
    assert updated[0].text == "<command_output>full MCP output</command_output>"
    assert updated[1].text == "[call_id: c_abcdefghijkl]"


def test_board_update_serializes_mcp_content_and_has_explicit_boundary():
    message = Message(
        role="tool",
        contents=[
            Content.from_function_result("call-1", result=[Content.from_text("MCP full result")])
        ],
    )
    append_board_update([message], "[黑板更新]\n新增事实 F2")
    result = message.contents[0].result
    assert isinstance(result, str)
    assert "MCP full result" in result
    assert "<agent_framework" not in result
    assert result.endswith("[黑板更新结束]")


async def test_grace_zero_is_last_allowed_call_and_409_rejects_without_terminating():
    service = FakeService()
    service.status = "concluding"
    service.grace = [0, RemoteError(409, "exhausted")]
    executed: list[str] = []
    ctx = run_context(service)

    @tool
    async def execute_command() -> str:
        executed.append("execute_command")
        return "should not run"

    @tool
    async def post_fact() -> str:
        executed.append("post_fact")
        return "saved fact"

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("execute_command"),)),
            ScriptStep(calls=(ScriptToolCall("post_fact"),), expect_contains="只允许"),
            ScriptStep(calls=(ScriptToolCall("post_fact"),), expect_contains="saved fact"),
            ScriptStep(text="handoff done", expect_contains="次数已用完"),
        ]
    )
    result = await Agent(
        client=client,
        tools=[execute_command, post_fact],
        middleware=[GraceGateMiddleware(ctx)],
    ).run("start")
    assert result.text == "handoff done"
    assert executed == ["post_fact"]
    assert service.grace == []


def test_event_rendering_preserves_targeted_and_verdict_fulltext_but_counts_digest():
    events = [
        {
            "version": 1,
            "type": "fact.posted",
            "actor": "agent-1",
            "payload": {"statement": "my own"},
        },
        {
            "version": 2,
            "type": "fact.posted",
            "actor": "agent-2",
            "payload": {"statement": "other A"},
        },
        {
            "version": 3,
            "type": "intent.posted",
            "actor": "agent-2",
            "payload": {"statement": "other B"},
        },
        {
            "version": 4,
            "type": "fact.disputed",
            "actor": "system",
            "addressed_to": ["agent-1"],
            "payload": {"fact_id": "F1", "detail": "full reason"},
        },
        {
            "version": 5,
            "type": "acceptance.judged",
            "actor": "agent-3",
            "payload": {"verdicts": [{"id": "A1", "missing": "fresh trace"}]},
        },
    ]
    lines = _render_events(events, "agent-1", max_lines=1)
    text = "\n".join(lines)
    assert "full reason" in text and "fresh trace" in text
    assert "新增事实 1 / 意图 1" in text
    assert "my own" not in text and "other A" not in text
    lines = _render_events(events, "agent-1", max_lines=2)
    assert any("other A" in line for line in lines)


async def test_board_sync_updates_persist_conclude_once_and_heartbeat_usage():
    service, board = FakeService(), FakeBoard()
    board.log = [
        {
            "version": 1,
            "type": "fact.posted",
            "actor": "agent-2",
            "object_id": "F1",
            "payload": {"statement": "other fact"},
        },
        {
            "version": 2,
            "type": "fact.posted",
            "actor": "agent-1",
            "object_id": "F2",
            "payload": {"statement": "own fact"},
        },
    ]
    ctx = run_context(service, board=board)
    price = Price(
        currency="CNY",
        cache_hit_per_m=Decimal("1"),
        cache_miss_per_m=Decimal("2"),
        output_per_m=Decimal("3"),
        off_peak=False,
    )
    explore = ctx.profile.models.explore.model_copy(update={"price": price})
    ctx.profile = ctx.profile.model_copy(
        update={"models": ctx.profile.models.model_copy(update={"explore": explore})}
    )

    @tool
    async def step() -> str:
        service.status = "concluding"
        service.conclude_requested_at = "now"
        service.conclude_reason = "closing"
        board.log.append(
            {
                "version": 3,
                "type": "agent.conclude_requested",
                "actor": "scheduler",
                "addressed_to": ["agent-1"],
                "payload": {"reason": "closing"},
            }
        )
        return "step done"

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("step"),), usage=ScriptUsage(11, 13, 5, 2)),
            ScriptStep(calls=(ScriptToolCall("step"),), expect_contains="[结束指令]"),
            ScriptStep(text="finished", expect_contains="[结束指令]"),
        ]
    )
    middleware = BoardSyncMiddleware(ctx)
    agent = Agent(client=client, tools=[step], middleware=[middleware])
    assert (await agent.run("start", session=agent.create_session())).text == "finished"
    assert len(service.beats) == 3
    assert all(beat["steps"] == 1 for beat in service.beats)
    assert service.beats[0]["context_tokens"] == 24
    usage = service.beats[0]["usage"]
    assert isinstance(usage, Usage)
    assert usage.cache_hit_tokens == 11 and usage.cache_miss_tokens == 13
    assert usage.reasoning_tokens == 2
    assert usage.cost == Decimal("0.000052")
    assert middleware.price_warning is None
    assert board.requests == [(0, "agent-1"), (2, "agent-1"), (3, "agent-1")]
    first = message_text(client.received_messages[0])
    third = message_text(client.received_messages[2])
    assert "other fact" in first and "own fact" not in first
    assert third.count("[结束指令]") == 1
    assert third.count("other fact") == 1


async def test_board_sync_missing_price_costs_zero_and_derive_skips_events(caplog):
    service, board = FakeService(), FakeBoard()
    ctx = run_context(service, board=board)
    ctx.task_type = "derive"
    derive = ctx.profile.models.derive.model_copy(update={"price": Price()})
    ctx.profile = ctx.profile.model_copy(
        update={"models": ctx.profile.models.model_copy(update={"derive": derive})}
    )

    @tool
    async def step() -> str:
        return "progress"

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("step"),), usage=ScriptUsage(10, 20, 30, 4)),
            ScriptStep(text="done"),
        ]
    )
    middleware = BoardSyncMiddleware(ctx)
    agent = Agent(client=client, tools=[step], middleware=[middleware])
    assert (await agent.run("start")).text == "done"
    assert board.requests == []
    assert middleware.price_warning == "价格未配置"
    assert service.beats[0]["usage"].cost == 0
    assert (
        len(
            [
                record
                for record in caplog.records
                if "Model price is not configured" in record.message
            ]
        )
        == 1
    )
