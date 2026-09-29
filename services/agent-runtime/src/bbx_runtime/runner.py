"""Run one registered agent, recording every terminal path on the blackboard."""

from __future__ import annotations

import asyncio
import logging
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
from bbx_runtime.derive_history import DeriveHistoryProvider, start_derive_segment
from bbx_runtime.execenv import ExecEnvHandle, ExecEnvManager
from bbx_runtime.middleware import BoardSyncMiddleware, GraceGateMiddleware, ToolLogMiddleware
from bbx_runtime.model_errors import OPENAI_PROVIDERS, model_error_metadata
from bbx_runtime.models import (
    close_model_client,
    load_runtime_profile,
    make_client,
    model_run_options,
    resolve_model_credentials,
)
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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    receipt: dict[str, Any]
    end_reason: str


async def external_mcp_tools(
    service: BlackboardClient, bindings: list[Any], http_client: httpx.AsyncClient
) -> list[MCPStreamableHTTPTool]:
    """Build Explore-only tools from immutable platform server versions."""
    result: list[MCPStreamableHTTPTool] = []
    for index, binding in enumerate(bindings):
        if binding.allowed_tools == []:
            continue
        server = await service.get_mcp_server(binding.name, binding.version)
        if (
            server.get("name") != binding.name
            or server.get("version") != binding.version
            or not server.get("enabled")
        ):
            raise ValueError("Pinned MCP server is unavailable")
        headers: dict[str, str] = {}
        if server.get("has_secret"):
            credentials = await service.get_mcp_credentials(binding.name, binding.version)
            secret = credentials.get("secret")
            if not isinstance(secret, str) or not secret:
                raise ValueError("MCP server credential is unavailable")
            headers[server["auth_header"]] = f"{server['auth_scheme']} {secret}".strip()
        result.append(
            MCPStreamableHTTPTool(
                name=f"platform_{index}",
                url=server["url"],
                static_headers=headers,
                tool_name_prefix=f"mcp_{index}_{binding.name}",
                allowed_tools=binding.allowed_tools,
                load_prompts=False,
                sampling_max_requests=0,
                http_client=http_client,
            )
        )
    return result


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
        expected_derive_round: int | None = None,
    ) -> RunResult:
        owned_client = None
        envd = None
        mcp_http = envd_http_client
        external_mcp_http: httpx.AsyncClient | None = None
        cancelled = False
        checkpoint: SessionCheckpoint | None = None
        round_fence = expected_derive_round
        try:
            state = await self.service.state(task_id)
            run = state["agents"][agent_id]
            if run["task_type"] != task_type or run["status"] not in {"running", "concluding"}:
                raise ValueError("Agent registration does not match a runnable agent")
            if (
                task_type == "derive"
                and expected_derive_round is not None
                and (int(run.get("derive_round") or 1) != expected_derive_round)
            ):
                raise ValueError("Derive registration round changed before launch")
            if task_type == "derive" and round_fence is None:
                round_fence = int(run.get("derive_round") or 1)
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
                expected_derive_round=round_fence,
            )
            if hasattr(self.service, "get_agent_session"):
                checkpoint = await SessionCheckpoint.load(self.service, task_id, agent_id)
                if checkpoint is None:
                    checkpoint = SessionCheckpoint(self.service, task_id, agent_id, AgentSession())
                ctx.checkpoint = checkpoint
                checkpoint.expected_derive_round = ctx.expected_derive_round
            if (
                task_type == "derive"
                and checkpoint is not None
                and int(run.get("derive_round") or 1) > 1
                and int(run.get("context_tokens") or 0) >= ctx.params.context_threshold
                and int(checkpoint.session.state.get("bbx_derive_segment_round") or 0)
                != int(run["derive_round"])
            ):
                start = start_derive_segment(checkpoint, int(run["derive_round"]))
                source = checkpoint.session.state.get(
                    "bbx_prompt_source", profile.prompt_templates.derive
                )
                checkpoint.opening_instructions = await OpeningContextProvider(
                    ctx, checkpoint
                ).render(source, state)
                await checkpoint.save()
                await record_trace(
                    ctx,
                    "board_update",
                    int(run.get("steps") or 0) + 1,
                    f"[推导会话分段] 第 {run['derive_round']} 轮从消息 {start} 开始读取。"
                    "完整历史仍保存在同一 Agent Session；本段使用更新的黑板上下文。",
                )
            model = getattr(profile.models, task_type)
            # The scheduler stops exploration at the budget deadline; this is only a hard guard.
            run_limit = (task["budget"]["max_minutes"] + ctx.params.grace_timeout + 1) * 60
            if client is None:
                credentials = await resolve_model_credentials(
                    self.service, model, self.settings.deepseek_api_key.get_secret_value()
                )
                owned_client = make_client(
                    model,
                    credentials=credentials,
                    explore_max_steps=ctx.params.explore_max_steps,
                    conclude_grace_calls=ctx.params.conclude_grace_calls,
                    max_duration_seconds=run_limit,
                )
                client = owned_client
            tools: list[FunctionTool | MCPStreamableHTTPTool] = list(make_board_tools(ctx))
            worker = profile.worker_tools.get(task_type)
            if envd is not None and (worker is None or "execute_command" in worker.builtin):
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
            if task_type == "explore" and worker is not None and worker.mcp_servers:
                external_mcp_http = httpx.AsyncClient(
                    trust_env=False, timeout=httpx.Timeout(600, connect=10)
                )
                tools.extend(
                    await external_mcp_tools(self.service, worker.mcp_servers, external_mcp_http)
                )
            async with Agent(
                client=client,
                name=agent_id,
                tools=tools,
                context_providers=[
                    OpeningContextProvider(ctx, checkpoint),
                    *(
                        [DeriveHistoryProvider(checkpoint)]
                        if checkpoint and task_type == "derive"
                        else [CheckpointHistoryProvider(checkpoint)]
                        if checkpoint
                        else []
                    ),
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
                    if task_type == "derive":
                        round_number = int(run.get("derive_round") or 1)
                        mode_name = (
                            "完成复核"
                            if run.get("derive_review")
                            else "并行推导"
                            if run.get("derive_parallel")
                            else "静止推导"
                        )
                        previous = run.get("previous_receipt") or {}
                        detail = previous.get("data") if isinstance(previous, dict) else None
                        excluded = detail.get("excluded") if isinstance(detail, dict) else None
                        note = (
                            f"开始第 {round_number} 轮推导。本轮模式：{mode_name}。"
                            f"当前黑板版本：{task.get('version', 0)}；"
                            f"本轮起点版本：{run.get('round_start_version', 0)}。\n"
                            f"最新验收状态：{task.get('acceptance_state', {})}。\n"
                            "沿用当前会话已知的目标、证据和先前分析。"
                            "黑板变化将附加到本次未发送的消息。"
                            "旧轮次回执和结束指令仅属历史，不代表本轮已完成。"
                            "请针对新变化与上轮排除方向分析，"
                            "仅将本轮实际提交的意图写入本轮回执。"
                        )
                        if run.get("derive_review"):
                            note += (
                                "\n请复核目标范围、完成证据和未验证事项。"
                                "若无必要方向，posted 留空且 excluded 写具体排除理由。"
                            )
                        if excluded:
                            note += f"\n上轮排除理由：{excluded}"
                        prompt = Message(
                            role="user",
                            message_id=f"bbx-derive-round-{round_number}",
                            contents=[Content.from_text(note)],
                        )
                    while True:
                        confirmed_before = (
                            set(checkpoint.delivered_message_ids) if checkpoint else set()
                        )
                        if model.provider in OPENAI_PROVIDERS:
                            stream = agent.run(
                                prompt,
                                stream=True,
                                session=session,
                                options=model_run_options(model),
                            )
                            response = await stream.get_final_response()
                        else:
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
            metadata = getattr(error, "bbx_model_error", None)
            if not isinstance(metadata, dict):
                classified = model_error_metadata(error)
                metadata = classified if classified["category"] != "unknown" else None
            if isinstance(metadata, dict):
                logger.warning(
                    "Model request failed after retries",
                    extra={"fields": {"task_id": task_id, "agent_id": agent_id, **metadata}},
                )
                reason = f"模型请求失败：{metadata['category']}"
            else:
                logger.warning(
                    "Agent run failed",
                    extra={
                        "fields": {
                            "task_id": task_id,
                            "agent_id": agent_id,
                            "category": type(error).__name__,
                        }
                    },
                )
                reason = f"运行失败：{type(error).__name__}"
            receipt = {"accepted": False, "reason": reason}
            if isinstance(metadata, dict):
                receipt["error"] = metadata
            end_reason = "runtime_error"

        async def finalize() -> None:
            async with AsyncExitStack() as resources:
                if envd is not None:
                    resources.push_async_callback(envd.close)
                if owned_client is not None:
                    resources.push_async_callback(close_model_client, owned_client)
                if mcp_http is not None and envd_http_client is None:
                    resources.push_async_callback(mcp_http.aclose)
                if external_mcp_http is not None:
                    resources.push_async_callback(external_mcp_http.aclose)
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
                    **({"expected_derive_round": round_fence} if round_fence is not None else {}),
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
