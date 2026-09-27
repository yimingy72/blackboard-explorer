"""Run one registered agent, recording every terminal path on the blackboard."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from agent_framework import (
    Agent,
    AgentSession,
    BaseChatClient,
    Content,
    FunctionTool,
    MCPStreamableHTTPTool,
    Message,
)
from bbx_objects import ObjectStore

from bbx_runtime.clients import BlackboardClient, EnvdClient
from bbx_runtime.context import CloseMode, RunContext, TaskType
from bbx_runtime.execenv import ExecEnvHandle, ExecEnvManager
from bbx_runtime.middleware import BoardSyncMiddleware, GraceGateMiddleware, ToolLogMiddleware
from bbx_runtime.models import load_runtime_profile, make_client, model_run_options
from bbx_runtime.opening import OpeningContextProvider
from bbx_runtime.receipts import parse_receipt
from bbx_runtime.scheduler.decision import _ever_claimed
from bbx_runtime.session import (
    CheckpointHistoryProvider,
    SessionCheckpoint,
    repair_unpaired_tool_calls,
)
from bbx_runtime.settings import Settings
from bbx_runtime.tools import make_board_tools
from bbx_runtime.trace import record_trace


@dataclass(frozen=True)
class RunResult:
    receipt: dict[str, Any]
    end_reason: str


class AgentRunner:
    def __init__(
        self,
        settings: Settings,
        service: BlackboardClient,
        objects: ObjectStore,
        manager: ExecEnvManager,
    ) -> None:
        self.settings = settings
        self.service = service
        self.objects = objects
        self.manager = manager

    async def run_agent(
        self,
        task_id: str,
        agent_id: str,
        task_type: TaskType,
        intent_id: str | None = None,
        mode: CloseMode | None = None,
        *,
        agent_token: str,
        client: BaseChatClient | None = None,
        handle: ExecEnvHandle | None = None,
        envd_http_client: httpx.AsyncClient | None = None,
    ) -> RunResult:
        owned_client = None
        envd = None
        mcp_http = envd_http_client
        cancelled = False
        checkpoint: SessionCheckpoint | None = None
        try:
            state = await self.service.state(task_id)
            run = state["agents"][agent_id]
            if run["task_type"] != task_type or run["status"] not in {"running", "concluding"}:
                raise ValueError("Agent registration does not match a runnable agent")
            task = state["task"]
            profile = load_runtime_profile(
                await self.service.get_profile(task["agent_profile"], task["agent_profile_version"])
            )
            if task_type == "explore":
                handle = handle or await self.manager.find(task_id)
                if handle is None:
                    raise RuntimeError("Task execution environment is not provisioned")
                envd = EnvdClient(handle.base_url, handle.token, envd_http_client)
            ctx = RunContext(
                task_id=task_id,
                agent_id=agent_id,
                task_type=task_type,
                state=state,
                profile=profile,
                service=self.service,
                board=self.service.with_token(agent_token),
                objects=self.objects,
                envd=envd,
                intent_id=intent_id or run.get("intent_id"),
                mode=mode or run.get("close_mode"),
            )
            if hasattr(self.service, "get_agent_session"):
                checkpoint = await SessionCheckpoint.load(self.service, task_id, agent_id)
                if checkpoint is None:
                    checkpoint = SessionCheckpoint(self.service, task_id, agent_id, AgentSession())
                ctx.checkpoint = checkpoint
            model = getattr(profile.models, task_type)
            # The scheduler stops exploration at the budget deadline; this is only a hard guard.
            run_limit = (task["budget"]["max_minutes"] + ctx.params.grace_timeout + 1) * 60
            if client is None:
                owned_client = make_client(
                    model,
                    api_key=self.settings.deepseek_api_key.get_secret_value(),
                    explore_max_steps=ctx.params.explore_max_steps,
                    conclude_grace_calls=ctx.params.conclude_grace_calls,
                    max_duration_seconds=run_limit,
                )
                client = owned_client
            tools: list[FunctionTool | MCPStreamableHTTPTool] = list(make_board_tools(ctx))
            if envd is not None:
                if mcp_http is None:
                    mcp_http = httpx.AsyncClient(
                        trust_env=False, timeout=httpx.Timeout(600, connect=10)
                    )
                tools.append(
                    MCPStreamableHTTPTool(
                        name="exec",
                        url=f"{envd.base_url}/mcp",
                        static_headers={**envd.headers, "X-Agent-Id": agent_id},
                        http_client=mcp_http,
                    )
                )
            async with Agent(
                client=client,
                name=agent_id,
                tools=tools,
                context_providers=[
                    OpeningContextProvider(ctx, checkpoint),
                    *([CheckpointHistoryProvider(checkpoint)] if checkpoint else []),
                ],
                middleware=[
                    ToolLogMiddleware(ctx),
                    GraceGateMiddleware(ctx),
                    BoardSyncMiddleware(ctx),
                ],
                require_per_service_call_history_persistence=checkpoint is not None,
            ) as agent:
                async with asyncio.timeout(run_limit):
                    session = checkpoint.session if checkpoint else agent.create_session()
                    prompt: str | Message = "开始。"
                    while True:
                        confirmed_before = (
                            set(checkpoint.delivered_message_ids) if checkpoint else set()
                        )
                        response = await agent.run(
                            prompt, session=session, options=model_run_options(model)
                        )
                        receipt = parse_receipt(response.text, task_type)
                        if not (
                            "raw_text" in receipt
                            and checkpoint
                            and checkpoint.delivered_message_ids - confirmed_before
                        ):
                            break
                        latest = await self.service.state(task_id)
                        current = latest["agents"][agent_id]
                        if current["status"] not in {"running", "concluding"}:
                            break
                        step_limit = (
                            ctx.params.seed_max_steps
                            if current.get("is_seed") and not _ever_claimed(latest, current)
                            else ctx.params.explore_max_steps
                        )
                        limit_reached = task_type == "explore" and (
                            int(current.get("steps") or 0) >= step_limit
                            or int(current.get("context_tokens") or 0)
                            >= ctx.params.context_threshold
                        )
                        handoff = (
                            current["status"] == "concluding"
                            or latest["task"]["status"] != "running"
                            or limit_reached
                        )
                        note = (
                            "刚才的自然语言答复已回应用户，但不是任务回执。"
                            "若用户要求暂停或结束，或者系统已进入结束阶段，请停止探索，"
                            "立即按原任务格式返回有效交接回执。"
                            if handoff
                            else "刚才的自然语言答复已回应用户，但不是任务回执。"
                            "若用户要求暂停或结束，请按原任务格式返回有效交接回执；"
                            "否则继续当前任务。不要因进度问答结束本次任务。"
                        )
                        await record_trace(
                            ctx,
                            "board_update",
                            int(current.get("steps") or 0) + 1,
                            "[运行控制]\n" + note,
                        )
                        prompt = Message(
                            role="user",
                            message_id=f"bbx-control-{uuid4()}",
                            contents=[Content.from_text(note)],
                        )
            end_reason = "normal" if receipt.get("accepted") else "refused"
        except asyncio.CancelledError as error:
            cancelled = True
            receipt = {"accepted": True, "data": {"note": "运行被取消，系统结束交接"}}
            reason = error.args[0] if error.args else None
            end_reason = (
                reason
                if reason in {"heartbeat", "grace_timeout", "runtime_restart"}
                else "grace_timeout"
            )
        except Exception as error:
            # SDK error strings can contain request details; record only the exception type.
            receipt = {"accepted": False, "reason": f"运行失败：{type(error).__name__}"}
            end_reason = "runtime_error"

        async def finalize() -> None:
            async with AsyncExitStack() as resources:
                if envd is not None:
                    resources.push_async_callback(envd.close)
                if owned_client is not None:
                    resources.push_async_callback(owned_client.client.close)
                if mcp_http is not None and envd_http_client is None:
                    resources.push_async_callback(mcp_http.aclose)
                save_error: Exception | None = None
                if checkpoint is not None:
                    try:
                        repair_unpaired_tool_calls(checkpoint.session, include_interrupted=False)
                        await checkpoint.save()
                    except Exception as error:
                        save_error = error
                await self.service.finish_agent(
                    task_id,
                    agent_id,
                    {"accepted": False, "reason": "会话持久化失败"} if save_error else receipt,
                    "runtime_error" if save_error else end_reason,
                )
                if save_error is not None:
                    raise save_error

        # Finish and resource cleanup must complete even if a sweeper cancels again.
        cleanup = asyncio.create_task(finalize())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
        return RunResult(receipt, end_reason)
