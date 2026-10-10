"""Execute one member turn with a fixed native MAF session."""

import asyncio
from collections.abc import Callable
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx
from agent_framework import (
    Agent,
    BaseChatClient,
    ChatContext,
    ChatMiddleware,
    MessageInjectionMiddleware,
    ResponseStream,
)
from bbx_contracts.ctf import CtfAgentProfile, execution_agent_id, render_ctf_prompt
from bbx_contracts.models import ModelConfig

from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.clients.objects import object_store
from bbx_runtime.ctf.artifacts import ArtifactRegistrar
from bbx_runtime.ctf.budget import ctf_call_reservation
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.execution import DrainPending
from bbx_runtime.ctf.middleware import CtfHistoryProvider, MailboxMiddleware, TurnBoundary
from bbx_runtime.ctf.platform import PlatformAdapter
from bbx_runtime.ctf.review import build_review_tools
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.ctf.telemetry import ToolObservationMiddleware, observe
from bbx_runtime.ctf.tools import build_tools
from bbx_runtime.model_errors import model_attempt_limit, model_error_metadata
from bbx_runtime.models import (
    close_model_client,
    ctf_model_run_options,
    make_client,
    resolve_model_credentials,
)
from bbx_runtime.session import ToolResultCheckpointMiddleware
from bbx_runtime.settings import Settings


class ModelErrorMiddleware(ChatMiddleware):
    """Record bounded diagnostics for each failed service call, without retrying it."""

    def __init__(
        self, service: CtfClient, task_id: str, member_id: str, turn: dict, model: ModelConfig
    ):
        self.service, self.task_id, self.member_id, self.turn = service, task_id, member_id, turn
        self.model = model

    async def process(self, context: ChatContext, call_next: Any) -> None:
        started = monotonic()
        key = f"{self.turn['id']}:model_error:{uuid4()}"
        recorded = False

        async def record_error(error: Exception) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            metadata = model_error_metadata(
                error,
                elapsed_ms=int((monotonic() - started) * 1000),
                messages=context.messages,
                attempt_limit=model_attempt_limit(self.model.provider),
            )
            vars(error)["bbx_model_error"] = metadata
            try:
                await observe(
                    self.service,
                    self.task_id,
                    self.member_id,
                    self.turn,
                    "model_error",
                    metadata,
                    key,
                )
            except Exception:
                # Diagnostic persistence must not replace the original model failure.
                pass

        try:
            await call_next()
        except Exception as error:
            await record_error(error)
            raise
        if isinstance(context.result, ResponseStream):
            inner = context.result

            async def updates():
                try:
                    async for update in inner:
                        yield update
                    await inner.get_final_response()
                except Exception as error:
                    await record_error(error)
                    raise

            context.result = ResponseStream(
                updates(), finalizer=lambda _: inner.get_final_response()
            )


class CtfRunner:
    def __init__(
        self,
        service: CtfClient,
        settings: Settings,
        client_factory: Callable[..., BaseChatClient] | None = None,
        envd: Any = None,
        objects: Any = None,
        platform_transport: Any = None,
    ) -> None:
        self.service, self.settings, self.client_factory = service, settings, client_factory
        self.envd = envd
        self.objects = objects
        self.platform_transport = platform_transport
        self.envds: dict[str, Any] = {}
        self.pending_settlements: dict[tuple[str, str], tuple[Any, Any, str, str]] = {}
        self.pending_checkpoints: dict[tuple[str, str], tuple[Any, Any, Any, str]] = {}

    async def run(self, task_id: str, member: dict[str, Any], turn: dict[str, Any]) -> None:
        task = (await self.service.state(task_id))["task"]
        review = turn.get("purpose") == "review"
        envd: Any = None if review else self.envds.get(task_id, self.envd)
        checkpoint = None
        if hasattr(envd, "prepare"):
            await envd.prepare(task_id, member, turn)
        document = await self.service.client.get_profile(
            task["agent_profile"], task["agent_profile_version"]
        )
        profile = CtfAgentProfile.model_validate(document.get("profile", document))
        reservation_cost = (
            None
            if review
            else ctf_call_reservation(
                profile.model, context_tokens=profile.options.context_threshold
            )
        )
        artifacts = (
            ArtifactRegistrar(
                self.service,
                envd.envd,
                lambda: self.objects if self.objects is not None else object_store(self.settings),
            )
            if hasattr(envd, "envd")
            else None
        )
        if review:
            tools = build_review_tools(self.service, task_id, member["id"], turn)
        else:
            tools = build_tools(
                self.service,
                task_id,
                turn,
                member["role"],
                envd=envd,
                member_id=member["id"],
                checkpoint_provider=lambda: checkpoint,
                artifacts=artifacts,
            )
            allowed = set(profile.worker_tools[member["role"]].builtin)
            tools = [item for item in tools if item.name in allowed]
            platform = PlatformAdapter(
                self.service,
                lambda: self.objects if self.objects is not None else object_store(self.settings),
                self.platform_transport,
            )
            tools.extend(
                await platform.build(
                    profile, member["role"], task_id, member["id"], turn, lambda: checkpoint
                )
            )
        instructions = render_ctf_prompt(
            profile,
            member["role"],
            member_name=member["display_name"],
            member_id=member["id"],
            task_id=task_id,
            agent_workspace=f"/workspace/agents/{execution_agent_id(member['id'])}",
            shared_workspace="/workspace/shared",
            allowed_tool_names=", ".join(t.name for t in tools),
        )
        if envd is None and not review:
            instructions += "\n执行容器尚未连接，execute_command不可用。"
        client = None
        answer, reason = "", "completed"
        checkpoint = None
        try:
            if self.client_factory:
                client = self.client_factory(member, profile)
            else:
                credentials = await resolve_model_credentials(
                    self.service.client,
                    profile.model,
                    self.settings.deepseek_api_key.get_secret_value(),
                )
                client = make_client(
                    profile.model,
                    credentials=credentials,
                    explore_max_steps=profile.options.max_steps,
                    conclude_grace_calls=0,
                    max_duration_seconds=task["budget"]["max_minutes"] * 60,
                )
            for attempt in range(2):
                checkpoint = await load_checkpoint(
                    self.service, task_id, member["id"], turn, instructions
                )
                if checkpoint.revision == 0:
                    await observe(
                        self.service,
                        task_id,
                        member["id"],
                        turn,
                        "initial_context",
                        {
                            "instructions": checkpoint.opening_instructions,
                            "tools": [item.name for item in tools],
                            "profile": task["agent_profile"],
                            "profile_version": task["agent_profile_version"],
                        },
                        "initial_context",
                    )
                await observe(
                    self.service,
                    task_id,
                    member["id"],
                    turn,
                    "turn_context",
                    {
                        "session_id": checkpoint.session.session_id,
                        "session_revision": checkpoint.revision,
                    },
                    f"{turn['id']}:context",
                )
                mailbox = MailboxMiddleware(
                    self.service,
                    checkpoint,
                    turn,
                    profile.options.max_steps,
                    reservation_cost,
                )
                history = CtfHistoryProvider(
                    self.service, checkpoint, turn, profile.model, profile.options.context_threshold
                )
                await history.flush_usage()
                try:
                    async with Agent(
                        client=client,
                        name=member["id"],
                        instructions=(
                            checkpoint.opening_instructions + "\n当前为终态只读复盘。"
                            "上面的执行职责仅为历史上下文。仅回答用户历史问题并读取已保存题目、记录及附件；"
                            "不执行命令、发送团队消息、变更任务或连接平台。成员身份及历史保持不变。"
                            if review
                            else checkpoint.opening_instructions
                        ),
                        tools=tools,
                        context_providers=[history],
                        middleware=[
                            mailbox,
                            MessageInjectionMiddleware(),
                            ModelErrorMiddleware(
                                self.service, task_id, member["id"], turn, profile.model
                            ),
                            ToolObservationMiddleware(self.service, task_id, member["id"], turn),
                            ToolResultCheckpointMiddleware(checkpoint),
                        ],
                        require_per_service_call_history_persistence=True,
                    ) as agent:
                        stream = agent.run(
                            [],
                            session=checkpoint.session,
                            stream=True,
                            options=ctf_model_run_options(profile.model),
                        )
                        async for _ in stream:
                            pass
                        response = await stream.get_final_response()
                        answer = response.text
                    await checkpoint.save()
                    break
                except (httpx.TransportError, ConnectionError):
                    if attempt:
                        raise
                    # The failed stream has exited; rebuild from confirmed state.
        except TurnBoundary as error:
            reason = str(error)
        except asyncio.CancelledError:
            if checkpoint is not None:
                try:
                    await asyncio.shield(checkpoint.save())
                except Exception:
                    self.pending_checkpoints[(task_id, member["id"])] = (
                        checkpoint,
                        member,
                        turn,
                        answer,
                    )
                    raise
            if not hasattr(envd, "stop_member"):
                await asyncio.shield(self.settle(task_id, member, turn, "stopped", ""))
            raise
        except RemoteError as error:
            if checkpoint is not None and getattr(checkpoint, "failed", False):
                self.pending_checkpoints[(task_id, member["id"])] = (
                    checkpoint,
                    member,
                    turn,
                    answer,
                )
                raise
            if error.status not in {403, 409}:
                raise
            reason = "interrupted"
        except Exception:
            if checkpoint is not None and getattr(checkpoint, "failed", False):
                self.pending_checkpoints[(task_id, member["id"])] = (
                    checkpoint,
                    member,
                    turn,
                    answer,
                )
            raise
        finally:
            if client is not None and self.client_factory is None:
                await close_model_client(client)
        await self.settle(task_id, member, turn, reason, answer)

    async def retry_checkpoints(self, task_id: str, *, settle: bool = True) -> None:
        for key, (checkpoint, member, turn, answer) in list(self.pending_checkpoints.items()):
            if key[0] != task_id:
                continue
            await checkpoint.save()
            entries = checkpoint.session.state.get(
                "ctf_review_usage_outbox"
                if turn.get("purpose") == "review"
                else "ctf_usage_outbox",
                [],
            )
            for entry in entries:
                await self.service.runtime(
                    task_id,
                    "bill_usage",
                    agent_id=member["id"],
                    turn_id=turn["id"],
                    generation=turn["generation"],
                    **entry,
                )
            if entries:
                entries.clear()
                await checkpoint.save()
            if settle:
                await self.settle(
                    task_id, member, turn, "completed" if answer else "interrupted", answer
                )
            del self.pending_checkpoints[key]

    async def retry_settlements(self, task_id: str) -> None:
        for key, (member, turn, reason, answer) in list(self.pending_settlements.items()):
            if key[0] == task_id:
                await self.settle(task_id, member, turn, reason, answer)

    async def enqueue_context_continuation(
        self, task_id: str, member: dict[str, Any], turn: dict[str, Any]
    ) -> None:
        if turn.get("purpose") == "review":
            return
        try:
            await self.service.runtime(
                task_id,
                "enqueue_continuation",
                agent_id=member["id"],
                turn_id=turn["id"],
                generation=turn["generation"],
            )
        except RemoteError as error:
            # Budget exhaustion and a concurrent user stop are authoritative; the
            # settled turn must remain terminal in either case.
            if error.status != 409:
                raise

    async def settle(
        self, task_id: str, member: dict[str, Any], turn: dict[str, Any], reason: str, answer: str
    ) -> bool:
        envd: Any = None if turn.get("purpose") == "review" else self.envds.get(task_id, self.envd)
        if hasattr(envd, "stop_member"):
            current = next(
                m for m in (await self.service.state(task_id))["members"] if m["id"] == member["id"]
            )
            if current.get("pending_operation"):
                self.pending_settlements.pop((task_id, member["id"]), None)
                return False
            try:
                await envd.stop_member(task_id, current)
            except DrainPending:
                self.pending_settlements[(task_id, member["id"])] = (member, turn, reason, answer)
                return False
        try:
            await self.service.runtime(
                task_id,
                "finish_turn",
                agent_id=member["id"],
                turn_id=turn["id"],
                generation=turn["generation"],
                end_reason=reason,
                answer=answer,
            )
        except RemoteError as error:
            if error.status != 409:
                raise
            current = next(
                m for m in (await self.service.state(task_id))["members"] if m["id"] == member["id"]
            )
            if not current.get("pending_operation"):
                raise
        self.pending_settlements.pop((task_id, member["id"]), None)
        if reason == "context_limit":
            await self.enqueue_context_continuation(task_id, member, turn)
        return True
