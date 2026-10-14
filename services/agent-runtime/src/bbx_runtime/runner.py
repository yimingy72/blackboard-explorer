"""Run one registered agent, recording every terminal path on the blackboard."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from time import monotonic
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

from bbx_runtime.clients import BlackboardClient, EnvdClient, RemoteError
from bbx_runtime.clients.blackboard import is_control_transport_error
from bbx_runtime.context import CloseMode, RunContext, TaskType
from bbx_runtime.derive_history import DeriveHistoryProvider, start_derive_segment
from bbx_runtime.execenv import ExecEnvHandle, ExecEnvManager
from bbx_runtime.middleware import (
    BoardSyncMiddleware,
    GraceGateMiddleware,
    ToolArgumentFeedbackMiddleware,
    ToolLogMiddleware,
)
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
from bbx_runtime.recovery import (
    MAX_AGENT_RECOVERIES,
    RECOVERY_BACKOFF,
    RECOVERY_SECONDS,
    ModelRecoveryGate,
)
from bbx_runtime.scheduler.decision import _ever_claimed
from bbx_runtime.session import (
    CheckpointHistoryProvider,
    SessionCheckpoint,
    repair_unpaired_tool_calls,
)
from bbx_runtime.settings import Settings
from bbx_runtime.single_dispatch import SingleDispatchExecutionTool
from bbx_runtime.tool_failures import propagate_tool_failure
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
        self._recovery_gates: dict[tuple[str, str], ModelRecoveryGate] = {}

    def dispatch_paused(self, task_id: str, *, final: bool = False) -> bool:
        gates = [
            gate
            for (tid, _model), gate in self._recovery_gates.items()
            if tid == task_id and gate.paused
        ]
        return bool(gates) and (
            not final or any(gate.episode and gate.episode.final_dispatched for gate in gates)
        )

    def mark_final_dispatch(self, task_id: str) -> None:
        for (tid, _model), gate in self._recovery_gates.items():
            if tid == task_id and gate.paused and gate.episode:
                gate.episode.final_dispatched = True

    def forget_recovery(self, task_id: str) -> None:
        self._recovery_gates = {
            key: gate for key, gate in self._recovery_gates.items() if key[0] != task_id
        }

    async def _finish_state(self, task_id: str) -> dict[str, Any]:
        for attempt in range(3):
            try:
                return await self.service.state(task_id)
            except Exception as error:
                if not is_control_transport_error(error) or attempt == 2:
                    raise
                await asyncio.sleep(0.1 * 2**attempt)
        raise AssertionError("State read retry budget exhausted")

    @staticmethod
    def _same_finish_scope(state: dict[str, Any], proof: dict[str, Any]) -> bool:
        agent = state.get("agents", {}).get(proof.get("agent_id"))
        return bool(
            agent
            and str(state["task"].get("id", proof["task_id"])) == proof["task_id"]
            and state["task"].get("run_number") == proof.get("run_number")
            and str(agent.get("id", proof["agent_id"])) == proof.get("run_id")
            and agent.get("task_type") == proof.get("task_type")
            and (
                proof.get("derive_round") is None
                or int(agent.get("derive_round") or 1) == proof["derive_round"]
            )
        )

    async def _finish_receipt(
        self, task_id: str, agent_id: str, proof: dict[str, Any], *, compensate: bool = False
    ) -> bool:
        kwargs = (
            {"expected_derive_round": proof["derive_round"]}
            if proof["derive_round"] is not None
            else {}
        )
        fenced = proof["run_number"] is not None
        if fenced:
            kwargs.update(
                expected_run_number=proof["run_number"], expected_agent_run_id=proof["run_id"]
            )
        for attempt in range(3):
            try:
                if compensate:
                    state = await self._finish_state(task_id)
                    if not self._same_finish_scope(state, proof):
                        return False
                    agent = state["agents"][agent_id]
                    if agent["status"] not in {"running", "concluding"}:
                        if (
                            json.dumps(agent.get("receipt"), sort_keys=True, ensure_ascii=False)
                            != json.dumps(proof["receipt"], sort_keys=True, ensure_ascii=False)
                            or agent.get("end_reason") != proof["end_reason"]
                        ):
                            raise RuntimeError(
                                "Final receipt differs from the settled agent result"
                            )
                        return True
                    if not fenced:
                        return False
                await self.service.finish_agent(
                    task_id, agent_id, proof["receipt"], proof["end_reason"], **kwargs
                )
                return True
            except Exception as error:
                if not is_control_transport_error(error):
                    raise
                # Unknown writes are read first, then only the service's run-fenced
                # idempotent finish can compensate a still-current original run.
                compensate = True
                logger.warning(
                    "Agent final receipt deferred task_id=%s agent_id=%s run_number=%s "
                    "derive_round=%s attempt=%s error_type=%s",
                    task_id,
                    agent_id,
                    proof["run_number"],
                    proof["derive_round"],
                    attempt + 1,
                    type(error).__name__,
                )
                if attempt < 2:
                    await asyncio.sleep(0.1 * 2**attempt)
        return False

    async def reconcile_final_receipt(
        self, task_id: str, agent_id: str, agent: dict[str, Any], task: dict[str, Any]
    ) -> bool:
        """Prefer a confirmed final Session receipt over a generic sweeper timeout."""
        if not hasattr(self.service, "get_agent_session"):
            return False
        saved = None
        for attempt in range(3):
            try:
                saved = await self.service.get_agent_session(task_id, agent_id)
                break
            except RemoteError as error:
                if error.status == 404:
                    return False
                if not is_control_transport_error(error):
                    raise
                if attempt == 2:
                    # A failed lookup cannot prove absence of an already saved receipt.
                    logger.warning(
                        "Agent final receipt lookup deferred task_id=%s agent_id=%s error_type=%s",
                        task_id,
                        agent_id,
                        type(error).__name__,
                    )
                    return True
                await asyncio.sleep(0.1 * 2**attempt)
            except Exception as error:
                if not is_control_transport_error(error):
                    raise
                if attempt == 2:
                    logger.warning(
                        "Agent final receipt lookup deferred task_id=%s agent_id=%s error_type=%s",
                        task_id,
                        agent_id,
                        type(error).__name__,
                    )
                    return True
                await asyncio.sleep(0.1 * 2**attempt)
        assert saved is not None
        proof = saved["session"].get("state", {}).get("bbx_final_receipt")
        if (
            not isinstance(proof, dict)
            or proof.get("task_id") != task_id
            or proof.get("agent_id") != agent_id
        ):
            return False
        if not self._same_finish_scope({"task": task, "agents": {agent_id: agent}}, proof):
            return False
        try:
            await self._finish_receipt(task_id, agent_id, proof, compensate=True)
        except Exception as error:
            if not is_control_transport_error(error):
                logger.warning(
                    "Agent final receipt blocked task_id=%s agent_id=%s error_type=%s",
                    task_id,
                    agent_id,
                    type(error).__name__,
                )
        return True

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
        cancellation_error: asyncio.CancelledError | None = None
        checkpoint: SessionCheckpoint | None = None
        round_fence = expected_derive_round
        run_number = None
        run_id = agent_id
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
            run_number = task.get("run_number")
            run_id = str(run.get("id", agent_id))
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
                    SingleDispatchExecutionTool(
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
            recovery = (
                self._recovery_gates.setdefault(
                    (task_id, model.model_dump_json()), ModelRecoveryGate()
                )
                if checkpoint is not None
                else None
            )
            sync = BoardSyncMiddleware(ctx, recovery)
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
                    ToolArgumentFeedbackMiddleware(),
                    sync,
                ],
                require_per_service_call_history_persistence=checkpoint is not None,
            ) as agent:
                async with asyncio.timeout(run_limit):
                    session = checkpoint.session if checkpoint else agent.create_session()
                    prompt: str | Message | list[Message] = Message(
                        role="user",
                        message_id=f"bbx-start-{agent_id}",
                        contents=[Content.from_text("开始。")],
                    )
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
                    recoveries = 0
                    while True:
                        confirmed_before = (
                            set(checkpoint.delivered_message_ids) if checkpoint else set()
                        )
                        try:
                            with propagate_tool_failure():
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
                        except Exception as error:
                            failure = sync.take_failure()
                            if (
                                checkpoint is None
                                or failure is None
                                or recoveries >= MAX_AGENT_RECOVERIES
                            ):
                                raise
                            recoveries += 1
                            await record_trace(
                                ctx,
                                "board_update",
                                failure.step,
                                "[连接恢复] 模型连接暂断，等待有界恢复；"
                                "沿用同一 Agent、Intent 与已保存会话。"
                                f"本次运行累计恢复 {recoveries}/{MAX_AGENT_RECOVERIES}，"
                                "等待仍计入任务活动时间。",
                            )
                            await sync.check_recovery_control()
                            restored = await SessionCheckpoint.load(self.service, task_id, agent_id)
                            if (
                                restored is None
                                or restored.revision != checkpoint.revision
                                or restored.session.session_id != checkpoint.session.session_id
                            ):
                                raise RemoteError(409, "Recovery checkpoint changed") from None
                            # Providers keep the checkpoint object; replace only its
                            # session with the confirmed snapshot. Unconfirmed delivery
                            # ids and claim tokens remain on that same checkpoint.
                            checkpoint.session = session = restored.session
                            checkpoint.opening_instructions = restored.opening_instructions
                            repair_unpaired_tool_calls(session, include_interrupted=True)
                            sync.reset_failed_injections()
                            sync.recovery_metadata = failure.metadata
                            sync.recovery_deadline = (
                                failure.episode.deadline
                                if failure.episode is not None
                                else monotonic() + RECOVERY_SECONDS
                            )
                            if failure.episode is None:
                                await asyncio.sleep(RECOVERY_BACKOFF[recoveries - 1])
                            await sync.check_recovery_control()
                            persisted = {
                                message.message_id
                                for message in session.state.get("in_memory", {}).get(
                                    "messages", []
                                )
                            }
                            prompt = [
                                *(
                                    message
                                    for message in failure.inputs
                                    if message.message_id not in persisted
                                ),
                                Message(
                                    role="user",
                                    message_id=f"bbx-recovery-{agent_id}-{recoveries}",
                                    contents=[
                                        Content.from_text(
                                            "此前模型连接中断。请沿用已保存会话继续当前任务，"
                                            "遵守最新结束指令与原回执格式。"
                                            "已执行或结果未知的旧工具调用不得重新执行。"
                                        )
                                    ],
                                ),
                            ]
                            # A new MAF run owns a fresh stream aggregator. Never
                            # retry inside a stream whose fragments have been yielded.
                            continue
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
            cancellation_error = error
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
                    "Model call failed",
                    extra={"fields": {"task_id": task_id, "agent_id": agent_id, **metadata}},
                )
                reason = f"模型请求失败：{metadata['category']}"
            else:
                safe_type = model_error_metadata(error)["exception_type"]
                logger.warning(
                    "Agent run failed",
                    extra={
                        "fields": {
                            "task_id": task_id,
                            "agent_id": agent_id,
                            "category": safe_type,
                        }
                    },
                )
                reason = f"运行失败：{safe_type}"
            receipt = {"accepted": False, "reason": reason}
            if isinstance(metadata, dict):
                receipt["error"] = metadata
            end_reason = "runtime_error"

        async def finalize() -> None:
            async def close_resource(name, callback, *args) -> None:
                try:
                    await callback(*args)
                except Exception as error:
                    # These callbacks close local pools; they do not stop or delete task work.
                    logger.warning(
                        "Agent resource cleanup failed task_id=%s agent_id=%s "
                        "resource=%s error_type=%s",
                        task_id,
                        agent_id,
                        name,
                        type(error).__name__,
                    )

            async with AsyncExitStack() as resources:
                if envd is not None:
                    resources.push_async_callback(close_resource, "envd_client", envd.close)
                if owned_client is not None:
                    resources.push_async_callback(
                        close_resource, "model_client", close_model_client, owned_client
                    )
                if mcp_http is not None and envd_http_client is None:
                    resources.push_async_callback(close_resource, "mcp_http", mcp_http.aclose)
                if external_mcp_http is not None:
                    resources.push_async_callback(
                        close_resource, "external_mcp_http", external_mcp_http.aclose
                    )
                save_error: Exception | None = None
                proof = {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "run_id": run_id,
                    "run_number": run_number,
                    "task_type": task_type,
                    "derive_round": round_fence,
                    "receipt": receipt,
                    "end_reason": end_reason,
                }
                if checkpoint is not None:
                    try:
                        repair_unpaired_tool_calls(checkpoint.session, include_interrupted=False)
                        checkpoint.session.state["bbx_final_receipt"] = proof
                        await checkpoint.save()
                    except Exception as error:
                        save_error = error
                if save_error is not None:
                    proof = {
                        **proof,
                        "receipt": {"accepted": False, "reason": "会话持久化失败"},
                        "end_reason": "runtime_error",
                    }
                await self._finish_receipt(task_id, agent_id, proof)
                if save_error is not None:
                    raise save_error

        # Finish and resource cleanup must complete even if a sweeper cancels again.
        cleanup = asyncio.create_task(finalize())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                cancelled = True
                if cancellation_error is None:
                    cancellation_error = error
        cleanup.result()
        if cancelled:
            raise cancellation_error or asyncio.CancelledError()
        return RunResult(receipt, end_reason)
