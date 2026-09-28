"""Opening instructions come from the pinned profile, never local paths."""

from pathlib import Path
from typing import Any, cast

import pytest
from agent_framework import Agent, Message, SessionContext
from bbx_contracts.profile import load_profile
from bbx_runtime.context import RunContext
from bbx_runtime.opening import OpeningContextProvider
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
from jinja2 import UndefinedError
from jinja2.exceptions import SecurityError

ROOT = Path(__file__).resolve().parents[3]


class FakeBoard:
    def __init__(self) -> None:
        self.snapshots: list[tuple[str, int]] = []
        self.event_queries = 0

    async def snapshot(self, task_id: str, max_lines: int) -> str:
        self.snapshots.append((task_id, max_lines))
        return "facts:\n  - {id: F1, s: 连接池耗尽}\n"

    async def events(self, task_id: str, since: int = 0) -> list[dict[str, Any]]:
        self.event_queries += 1
        return [
            {
                "version": 9,
                "type": "acceptance.judged",
                "payload": {"verdicts": [{"id": "A1", "reason": "缺少复现", "verdict": "unmet"}]},
            }
        ]


def board_state() -> dict[str, Any]:
    return {
        "task": {
            "goal": "查明网关 502 的原因",
            "domain_context": "订单服务",
            "acceptance": [{"id": "A1", "desc": "能复现根因"}],
            "acceptance_state": {
                "A1": {
                    "status": "unmet",
                    "reason": "尚未证实",
                    "missing": "缺少复现",
                    "evidence_facts": [],
                }
            },
            "budget": {"max_cost": "10", "max_minutes": 60},
            "usage": {"cost": "2.5"},
            "params": {"seed_max_steps": 7, "conclude_grace_calls": 2, "snapshot_max_lines": 25},
        },
        "facts": {
            "F1": {
                "id": "F1",
                "kind": "observation",
                "status": "proposed",
                "statement": "连接池耗尽时返回 502",
                "provenance": "tool_backed",
                "relied_by": 1,
                "satisfies": ["A1"],
                "evidence": [{"type": "log", "summary": "连接池日志", "uri": "evidence/one"}],
            }
        },
        "intents": {
            "I1": {
                "id": "I1",
                "statement": "检查慢查询",
                "status": "claimed",
                "result": None,
                "based_on": ["F1"],
                "expected": "找到阻塞点",
                "method": "读取 trace",
                "relates_to": ["A1"],
                "retry_of": None,
                "notes": [{"text": "上轮已经排除网络重试"}],
            }
        },
        "agents": {
            "agent-older": {
                "task_type": "derive",
                "finished_at": "2026-09-23T00:00:00Z",
                "receipt": {"accepted": True, "data": {"excluded": ["旧排除"]}},
            },
            "agent-newer": {
                "task_type": "derive",
                "finished_at": "2026-09-24T00:00:00Z",
                "receipt": {"accepted": True, "data": {"excluded": ["网络重试已否定"]}},
            },
        },
    }


def run_context(task_type: str, *, intent_id: str | None = None, mode: str | None = None):
    profile, _ = load_profile(ROOT / "profiles/default")
    board = FakeBoard()
    run = RunContext(
        task_id="test-task",
        agent_id="agent-1",
        task_type=cast(Any, task_type),
        state=board_state(),
        profile=profile,
        service=cast(Any, board),
        board=cast(Any, board),
        objects=cast(Any, None),
        intent_id=intent_id,
        mode=cast(Any, mode),
    )
    return run, board


@pytest.mark.asyncio
async def test_seed_explore_has_only_l0_and_uses_pinned_template() -> None:
    run, board = run_context("explore")
    provider = OpeningContextProvider(run)
    rendered = await provider.render()
    assert "查明网关 502 的原因" in rendered
    assert "订单服务" in rendered
    assert "缺少复现" in rendered
    assert "剩余预算" not in rendered
    assert "7 步内仍未认领" in rendered
    assert "黑板为空，你是第一个探索者" in rendered
    assert "# 任务图快照" not in rendered
    assert "检查慢查询" not in rendered
    assert board.snapshots == []

    context = SessionContext(input_messages=[Message(role="user", contents=["开始。"])])
    await provider.before_run(agent=None, session=None, context=context, state={})
    assert any("查明网关 502 的原因" in value for value in context.instructions)

    client = ScriptedChatClient([ScriptStep(text="done")])
    agent = Agent(client=client, context_providers=[provider])
    await agent.run("开始。", session=agent.create_session())
    assert "查明网关 502 的原因" in client.received_options[0]["instructions"]
    assert client.received_messages[0][0].text == "开始。"


@pytest.mark.asyncio
async def test_explore_intent_includes_handoff_evidence_and_l2() -> None:
    run, board = run_context("explore", intent_id="I1")
    rendered = await OpeningContextProvider(run).render()
    for value in ("检查慢查询", "读取 trace", "上轮已经排除网络重试", "连接池日志", "id: F1"):
        assert value in rendered
    assert "黑板为空，你是第一个探索者" not in rendered
    assert board.snapshots == [("test-task", 25)]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_name", ["default", "single"])
async def test_explore_prompts_publish_intermediate_findings_and_independent_intents(
    profile_name: str,
) -> None:
    run, _ = run_context("explore")
    run.profile, _ = load_profile(ROOT / "profiles" / profile_name)
    rendered = await OpeningContextProvider(run).render()
    assert "可复核的中间发现" in rendered
    assert "可供他人独立使用的最小部分" in rendered
    assert "有事实支撑且可由他人独立推进" in rendered
    assert "不要为了增加并发伪造发现" in rendered
    assert "用 post_intent(claim=true) 认领它" in rendered


@pytest.mark.asyncio
async def test_derive_includes_all_objects_and_latest_excluded() -> None:
    run, board = run_context("derive")
    rendered = await OpeningContextProvider(run).render()
    assert "连接池耗尽时返回 502" in rendered
    assert "检查慢查询" in rendered
    assert "网络重试已否定" in rendered
    assert "旧排除" not in rendered
    assert "retry_of" in rendered
    assert board.snapshots == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, phrase", [("judge", "不生成终结报告"), ("final", "Markdown 最终报告")]
)
async def test_close_includes_claims_history_snapshot_and_mode(mode: str, phrase: str) -> None:
    run, board = run_context("close", mode=mode)
    rendered = await OpeningContextProvider(run).render()
    assert phrase in rendered
    assert "连接池耗尽时返回 502" in rendered
    assert "缺少复现" in rendered
    assert "id: F1" in rendered
    assert board.snapshots == [("test-task", 25)]
    assert board.event_queries == 1


@pytest.mark.asyncio
async def test_template_is_strict_and_receives_no_host_objects() -> None:
    run, _ = run_context("explore")
    run.profile.prompt_templates.explore = "{{ missing_host_object }}"
    with pytest.raises(UndefinedError):
        await OpeningContextProvider(run).render()
    run.profile.prompt_templates.explore = "{{ goal.__class__ }}"
    with pytest.raises(SecurityError):
        await OpeningContextProvider(run).render()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_name", ["default", "single"])
@pytest.mark.parametrize(
    "task_type, intent_id, mode",
    [
        ("explore", None, None),
        ("explore", "I1", None),
        ("derive", None, None),
        ("close", None, "judge"),
        ("close", None, "final"),
    ],
)
async def test_budget_does_not_change_context_or_break_legacy_templates(
    profile_name: str, task_type: str, intent_id: str | None, mode: str | None
) -> None:
    run, _ = run_context(task_type, intent_id=intent_id, mode=mode)
    run.profile, _ = load_profile(ROOT / "profiles" / profile_name)
    provider = OpeningContextProvider(run)
    original = await provider.render()
    assert "剩余预算" not in original
    task = run.state["task"]
    task.update(
        budget={"max_cost": "9876.54321", "max_minutes": 43210},
        usage={"cost": "1234.56789"},
        started_at="2000-01-01T00:00:00Z",
    )
    assert await provider.render() == original

    del task["budget"]
    del task["usage"]
    assert await provider.render() == original
    setattr(run.profile.prompt_templates, task_type, "{{ goal }}｜{{ budget_left }}")
    legacy = await provider.render()
    assert legacy.split("\n\n", 1)[0] == f"{task['goal']}｜由系统管理"
    if task_type == "close":
        assert "完成依据协议与最近复核" in legacy
        assert "completion_basis" in legacy


async def test_completion_review_mode_and_followup_judge_receive_execution_context():
    run, _ = run_context("derive")
    run.state["agents"][run.agent_id] = {
        "id": run.agent_id,
        "task_type": "derive",
        "derive_review": True,
        "status": "running",
        "derive_from_version": 10,
    }
    run.profile.prompt_templates.derive = "Custom derive {{ goal }}"
    rendered = await OpeningContextProvider(run).render()
    assert "完成前必要复核" in rendered
    assert "excluded" in rendered
    close, _ = run_context("close", mode="judge")
    close.state["agents"]["reviewer"] = {
        "id": "reviewer",
        "derive_review": True,
        "finished_version": 15,
        "derive_from_version": 10,
        "status": "finished",
        "end_reason": "normal",
        "receipt": {
            "accepted": True,
            "data": {"posted": [], "excluded": ["Scope independently checked"]},
        },
    }
    close.profile.prompt_templates.close = "Custom close {{ goal }}"
    context = await OpeningContextProvider(close).render()
    assert "Scope independently checked" in context
    assert "completion_basis" in context
