"""Execute one member turn with a fixed native MAF session."""

import asyncio
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
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
from bbx_contracts.ctf import (
    CtfAgentProfile,
    execution_agent_id,
    render_ctf_prompt,
    resolve_ctf_options,
)
from bbx_contracts.models import ModelConfig

from bbx_runtime.clients.blackboard import RemoteError, is_control_transport_error
from bbx_runtime.clients.objects import object_store
from bbx_runtime.ctf.artifacts import ArtifactRegistrar
from bbx_runtime.ctf.budget import ctf_call_reservation
from bbx_runtime.ctf.client import CtfClient, CtfControlTransportExhausted
from bbx_runtime.ctf.context import fixed_prompt_tokens
from bbx_runtime.ctf.execution import DrainPending
from bbx_runtime.ctf.failures import propagate_tool_failure
from bbx_runtime.ctf.middleware import (
    CtfHistoryProvider,
    MailboxMiddleware,
    TurnBoundary,
    flush_usage_outbox,
)
from bbx_runtime.ctf.platform import PlatformAdapter, PlatformCatalogCache
from bbx_runtime.ctf.provider_boundary import provider_call_boundary
from bbx_runtime.ctf.review import build_review_tools
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.ctf.telemetry import (
    ToolArgumentFeedbackMiddleware,
    ToolObservationMiddleware,
    observe,
)
from bbx_runtime.ctf.tools import build_tools
from bbx_runtime.model_errors import (
    is_transient_model_error,
    model_attempt_limit,
    model_error_metadata,
)
from bbx_runtime.models import (
    close_model_client,
    ctf_model_run_options,
    make_client,
    resolve_model_credentials,
)
from bbx_runtime.session import ToolResultCheckpointMiddleware
from bbx_runtime.settings import Settings
from bbx_runtime.traffic.observation import observation_context, runtime_secrets

logger = logging.getLogger(__name__)


class _SettlementUnconfirmed(RuntimeError):
    """A successful read still cannot establish the uncertain write's outcome."""


@dataclass
class SettlementRetry:
    turn_id: str
    attempts: int = 0
    next_retry: float = 0
    stage: str = "read_member"
    finish_uncertain: bool = False
    finish_confirmed: bool = False
    continuation_uncertain: bool = False


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
            if recorded or isinstance(error, (TurnBoundary, httpx.TransportError, RemoteError)):
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
        self.platform_catalog = PlatformCatalogCache()
        self.envds: dict[str, Any] = {}
        self.pending_settlements: dict[tuple[str, str], tuple[Any, Any, str, str]] = {}
        self.pending_checkpoints: dict[tuple[str, str], tuple[Any, Any, Any, str]] = {}
        self.settlement_retries: dict[tuple[str, str], SettlementRetry] = {}

    async def run(self, task_id: str, member: dict[str, Any], turn: dict[str, Any]) -> None:
        platform = PlatformAdapter(
            self.service,
            lambda: self.objects if self.objects is not None else object_store(self.settings),
            self.platform_transport,
            self.platform_catalog,
        )
        try:
            await self._run(task_id, member, turn, platform)
        finally:
            primary = sys.exception()
            try:
                await platform.close()
            except asyncio.CancelledError:
                if primary is None:
                    raise

    def clear_platform_catalog(self, task_id: str) -> None:
        self.platform_catalog.clear(task_id)

    async def _run(
        self,
        task_id: str,
        member: dict[str, Any],
        turn: dict[str, Any],
        platform: PlatformAdapter,
    ) -> None:
        task = (await self.service.runtime_state(task_id))["task"]
        review = turn.get("purpose") == "review"
        envd: Any = None if review else self.envds.get(task_id, self.envd)
        checkpoint = None
        if hasattr(envd, "prepare"):
            await envd.prepare(task_id, member, turn)
        document = await self.service.client.get_profile(
            task["agent_profile"], task["agent_profile_version"]
        )
        profile = CtfAgentProfile.model_validate(document.get("profile", document))
        profile = profile.model_copy(
            update={
                "options": resolve_ctf_options(profile, task.get("ctf_options")),
            }
        )
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
        credentials: dict[str, str] = {}
        answer, reason = "", "completed"
        response = None
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
            instructions = (
                checkpoint.opening_instructions + "\n当前为终态只读复盘。"
                "上面的执行职责仅为历史上下文。仅回答用户历史问题并读取已保存题目、记录及附件；"
                "不执行命令、发送团队消息、变更任务或连接平台。成员身份及历史保持不变。"
                if review
                else checkpoint.opening_instructions
            )
            history = CtfHistoryProvider(
                self.service,
                checkpoint,
                turn,
                profile.model,
                profile.options.context_threshold,
                fixed_input_tokens=fixed_prompt_tokens(instructions, tools),
            )
            await history.flush_usage()
            with (
                observation_context(
                    task_id,
                    int(task.get("run_number") or 1),
                    member["id"],
                    turn_id=turn["id"],
                    secrets=runtime_secrets(
                        self.settings,
                        *credentials.values(),
                        *platform.observation_secrets,
                        getattr(getattr(envd, "envd", None), "token", ""),
                    ),
                ),
                propagate_tool_failure(),
                provider_call_boundary(
                    client, mailbox.enter_provider, history.prepare_provider_messages
                ),
            ):
                async with Agent(
                    client=client,
                    name=member["id"],
                    instructions=instructions,
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
                        ToolArgumentFeedbackMiddleware(),
                    ],
                    require_per_service_call_history_persistence=True,
                ) as agent:
                    stream = agent.run(
                        [],
                        session=checkpoint.session,
                        stream=True,
                        options=ctf_model_run_options(profile.model),
                    )
                    async for update in stream:
                        answer += update.text
                    response = await stream.get_final_response()
                    answer = response.text
            await checkpoint.save()
        except TurnBoundary as error:
            reason = str(error)
            if checkpoint is not None:
                if reason == "context_blocked":
                    blocked = checkpoint.session.state.get("ctf_context_blocked", {})
                    answer = (
                        "Context blocked before provider dispatch. Full history and evidence "
                        "remain saved. "
                        f"Compaction stopped at {blocked.get('branch', 'unknown')}, "
                        f"eligible {blocked.get('eligible', 0)}, cut {blocked.get('cut', 0)}: "
                        f"{blocked.get('estimated_input_tokens', 0)} "
                        f"estimated input tokens, limit {blocked.get('limit_tokens', 0)}. "
                        "Lead action is required."
                    )
                    try:
                        await observe(
                            self.service,
                            task_id,
                            member["id"],
                            turn,
                            "turn_context",
                            {"boundary": "context_blocked", "diagnostics": blocked},
                            f"{turn['id']}:context_blocked",
                        )
                    except Exception as diagnostic_error:
                        logger.warning(
                            "CTF blocked observation failed turn_id=%s error_type=%s",
                            turn["id"],
                            type(diagnostic_error).__name__,
                        )
                try:
                    await checkpoint.save()
                except Exception:
                    # Exceptions raised inside this handler bypass sibling handlers.
                    checkpoint.pending_end_reason = reason
                    self.pending_checkpoints[(task_id, member["id"])] = (
                        checkpoint,
                        member,
                        turn,
                        answer,
                    )
                    raise
        except asyncio.CancelledError:
            if checkpoint is not None:
                checkpoint.pending_end_reason = "interrupted"
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
                await asyncio.shield(self.settle(task_id, member, turn, "stopped", answer))
            raise
        except (httpx.TransportError, ConnectionError) as error:
            if not is_control_transport_error(error):
                raise
            if checkpoint is None:
                raise
            if getattr(checkpoint, "failed", False):
                checkpoint.pending_end_reason = reason if response is not None else "interrupted"
                self.pending_checkpoints[(task_id, member["id"])] = (
                    checkpoint,
                    member,
                    turn,
                    answer,
                )
                raise
            # Platform transport failure never replays the dispatched native stream or tools.
            reason = "interrupted"
            checkpoint.pending_end_reason = reason
            try:
                await checkpoint.save()
                await flush_usage_outbox(self.service, checkpoint, turn)
            except Exception:
                self.pending_checkpoints[(task_id, member["id"])] = (
                    checkpoint,
                    member,
                    turn,
                    answer,
                )
                raise
            logger.warning(
                "CTF turn control transport interrupted task_id=%s member_id=%s "
                "turn_id=%s generation=%s error_type=%s",
                task_id,
                member["id"],
                turn["id"],
                turn["generation"],
                type(error).__name__,
            )
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
            logger.warning(
                "CTF turn interrupted turn_id=%s generation=%s status=%s "
                "answer_length=%s checkpoint_failed=%s",
                turn["id"],
                turn["generation"],
                error.status,
                len(answer),
                getattr(checkpoint, "failed", False),
            )
        except Exception as error:
            if checkpoint is not None and getattr(checkpoint, "failed", False):
                self.pending_checkpoints[(task_id, member["id"])] = (
                    checkpoint,
                    member,
                    turn,
                    answer,
                )
                raise
            if (
                review
                or checkpoint is None
                or not isinstance(getattr(error, "bbx_model_error", None), dict)
                or not is_transient_model_error(error)
            ):
                raise
            # A new turn owns a fresh reservation; the uncertain dispatched call
            # remains charged conservatively by the existing settlement path.
            reason = "provider_transient"
            answer = (
                "A transient model service error interrupted this turn. Confirmed history "
                "and tool results remain saved; automatic recovery is bounded by task controls."
            )
            try:
                await checkpoint.save()
            except Exception:
                checkpoint.pending_end_reason = reason
                self.pending_checkpoints[(task_id, member["id"])] = (
                    checkpoint,
                    member,
                    turn,
                    answer,
                )
                raise
        finally:
            if client is not None and self.client_factory is None:
                try:
                    await close_model_client(client)
                except Exception as cleanup_error:
                    # Closing the owned connection pool cannot invalidate durable turn work.
                    logger.warning(
                        "CTF model client cleanup failed task_id=%s member_id=%s "
                        "turn_id=%s generation=%s error_type=%s",
                        task_id,
                        member["id"],
                        turn["id"],
                        turn["generation"],
                        type(cleanup_error).__name__,
                    )
        await self.settle(task_id, member, turn, reason, answer)

    async def retry_checkpoints(self, task_id: str, *, settle: bool = True) -> None:
        for key, (checkpoint, member, turn, answer) in list(self.pending_checkpoints.items()):
            if key[0] != task_id:
                continue
            try:
                await checkpoint.save()
                await flush_usage_outbox(self.service, checkpoint, turn)
            except RemoteError as error:
                if error.status >= 500:
                    raise
                snapshot = await self.service.runtime_state(task_id)
                current_turn = next(
                    (item for item in snapshot.get("turns", []) if item["id"] == turn["id"]),
                    None,
                )
                current_member = next(
                    (item for item in snapshot.get("members", []) if item["id"] == member["id"]),
                    None,
                )
                terminal = (
                    current_turn is not None and current_turn.get("status") == "finished"
                ) or (
                    current_member is not None
                    and (
                        current_member.get("generation", turn["generation"]) != turn["generation"]
                        or (
                            "current_turn_id" in current_member
                            and current_member["current_turn_id"] != turn["id"]
                        )
                    )
                )
                if not terminal:
                    raise
                control = snapshot.get("task", {}).get("ctf_control", {})
                if getattr(checkpoint, "failed", False) and (
                    turn["id"] not in control.get("unresolved_checkpoints", [])
                    and turn["id"] not in control.get("checkpoint_failures", {})
                ):
                    # A failed failure-marker must remain visible to bounded close escalation.
                    raise
                usage_key = (
                    "ctf_review_usage_outbox"
                    if turn.get("purpose") == "review"
                    else "ctf_usage_outbox"
                )
                entries = checkpoint.session.state.get(usage_key, [])
                if entries:
                    confirmed = await self.service.runtime(
                        task_id, "reconcile_usage_outbox", agent_id=member["id"]
                    )
                    if any(
                        entry["request_id"] not in confirmed.get("request_ids", [])
                        for entry in entries
                    ):
                        # Retain unsaved usage; closing escalation keeps sent reservations unknown.
                        raise
                del self.pending_checkpoints[key]
                logger.warning(
                    "CTF checkpoint retry discarded turn_id=%s generation=%s status=%s "
                    "terminal=%s answer_length=%s",
                    turn["id"],
                    turn["generation"],
                    error.status,
                    terminal,
                    len(answer),
                )
                continue
            del self.pending_checkpoints[key]
            if settle:
                reason = getattr(checkpoint, "pending_end_reason", None) or (
                    "completed" if answer else "interrupted"
                )
                await self.settle(task_id, member, turn, reason, answer)

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

    async def _confirm_finish(self, task_id, member, turn, reason, answer) -> bool:
        state = await self.service.state(task_id)
        saved = next((item for item in state.get("turns", []) if item["id"] == turn["id"]), None)
        if saved is None:
            return False
        if saved.get("agent_id") != member["id"] or saved.get("generation") != turn["generation"]:
            raise RuntimeError("CTF settlement read-back has another writer identity")
        if saved.get("status") != "finished":
            return False
        current = next(
            (item for item in state.get("members", []) if item["id"] == member["id"]), None
        )
        phase = state.get("task", {}).get("ctf_control", {}).get("phase")
        if (
            saved.get("purpose") == "execution"
            and saved.get("end_reason") == "stopped"
            and current is not None
            and (
                current.get("generation") != turn["generation"]
                or current.get("current_turn_id") != turn["id"]
            )
            and (
                current.get("lifecycle") in {"stopped", "removed"}
                or any(
                    operation.get("kind") in {"stop", "remove"}
                    and (
                        (
                            operation.get("settled_turn_id") == turn["id"]
                            and operation.get("settled_turn_generation") == turn["generation"]
                        )
                        or (
                            "settled_turn_id" not in operation
                            and "settled_turn_generation" not in operation
                            and operation.get("target_generation") == turn["generation"]
                        )
                    )
                    for operation in current.get("operation_history", [])
                )
                or phase in {"closing", "closed"}
            )
        ):
            # A matching old turn plus explicit stop authority supersedes its local finish.
            # Pending checkpoints, Session evidence and unknown fees remain untouched.
            key = (task_id, member["id"])
            self.pending_settlements.pop(key, None)
            self.settlement_retries.pop(key, None)
            logger.warning(
                "CTF settlement superseded by authoritative stop task_id=%s member_id=%s "
                "turn_id=%s generation=%s",
                task_id,
                member["id"],
                turn["id"],
                turn["generation"],
            )
            return True
        self._validate_finished(saved, member, turn, reason, answer)
        return True

    @staticmethod
    def _validate_finished(saved, member, turn, reason, answer) -> None:
        if (
            saved.get("status") != "finished"
            or saved.get("agent_id") != member["id"]
            or saved.get("generation") != turn["generation"]
            or saved.get("end_reason") != reason
            or saved.get("final_answer") != answer
        ):
            raise RuntimeError("CTF settlement read-back does not match the requested turn result")

    async def settle(
        self, task_id: str, member: dict[str, Any], turn: dict[str, Any], reason: str, answer: str
    ) -> bool:
        key = (task_id, member["id"])
        original = self.pending_settlements.get(key)
        if original is not None:
            if original[1]["id"] != turn["id"]:
                raise RuntimeError("Another turn settlement is still pending for this member")
            member, turn, reason, answer = original
        self.pending_settlements[key] = (member, turn, reason, answer)
        retry = self.settlement_retries.get(key)
        if retry is None or retry.turn_id != str(turn["id"]):
            retry = SettlementRetry(str(turn["id"]))
            self.settlement_retries[key] = retry
        if monotonic() < retry.next_retry:
            return False
        if retry.attempts >= 3:
            raise CtfControlTransportExhausted(
                task_id, member["id"], str(turn["id"]), turn["generation"], retry.stage
            )
        stage = "confirm_finish" if retry.finish_uncertain else "read_member"
        try:
            if retry.finish_uncertain:
                if not await self._confirm_finish(task_id, member, turn, reason, answer):
                    raise _SettlementUnconfirmed("Settlement is not yet confirmed")
                retry.finish_confirmed = True
            if not retry.finish_confirmed:
                envd: Any = (
                    None if turn.get("purpose") == "review" else self.envds.get(task_id, self.envd)
                )
                if hasattr(envd, "stop_member"):
                    current = next(
                        m
                        for m in (await self.service.runtime_state(task_id))["members"]
                        if m["id"] == member["id"]
                    )
                    if current.get("pending_operation"):
                        return False
                    if (
                        current.get("generation", turn["generation"]) != turn["generation"]
                        or current.get("current_turn_id", turn["id"]) != turn["id"]
                    ):
                        stage = "confirm_finish"
                        if not await self._confirm_finish(task_id, member, turn, reason, answer):
                            raise RuntimeError("CTF settlement writer no longer owns the member")
                        retry.finish_confirmed = True
                    else:
                        binding = current.get("execution") or {}
                        if not (
                            binding.get("drained")
                            and binding.get("generation") == turn["generation"]
                        ):
                            try:
                                stage = "drain_member"
                                await envd.stop_member(task_id, current)
                            except DrainPending:
                                return False
            if not retry.finish_confirmed:
                stage = "finish_turn"
                try:
                    result = await self.service.runtime(
                        task_id,
                        "finish_turn",
                        agent_id=member["id"],
                        turn_id=turn["id"],
                        generation=turn["generation"],
                        end_reason=reason,
                        answer=answer,
                    )
                    if isinstance(result, dict) and "status" in result:
                        self._validate_finished(result, member, turn, reason, answer)
                except (httpx.TransportError, ConnectionError) as error:
                    if not is_control_transport_error(error):
                        raise
                    retry.finish_uncertain = True
                    stage = "confirm_finish"
                    if not await self._confirm_finish(task_id, member, turn, reason, answer):
                        raise
                except RemoteError as error:
                    if is_control_transport_error(error):
                        retry.finish_uncertain = True
                        stage = "confirm_finish"
                        if not await self._confirm_finish(task_id, member, turn, reason, answer):
                            raise
                    elif error.status != 409:
                        raise
                    else:
                        stage = "read_member"
                        current = next(
                            m
                            for m in (await self.service.runtime_state(task_id))["members"]
                            if m["id"] == member["id"]
                        )
                        if current.get("pending_operation"):
                            return False
                        raise
                retry.finish_confirmed = True
            if key not in self.pending_settlements:
                return True
            if reason == "context_limit":
                stage = "continuation"
                if retry.continuation_uncertain:
                    state = await self.service.state(task_id)
                    present = any(
                        message.get("source_turn_id") == turn["id"]
                        and message.get("notification_purpose") == "context_continuation"
                        for message in state.get("messages", [])
                    )
                    if not present and state["task"]["ctf_control"]["phase"] == "running":
                        raise _SettlementUnconfirmed("Continuation is not yet confirmed")
                else:
                    try:
                        await self.enqueue_context_continuation(task_id, member, turn)
                    except (httpx.TransportError, RemoteError, ConnectionError) as error:
                        if not is_control_transport_error(error):
                            raise
                        retry.continuation_uncertain = True
                        raise
        except (
            httpx.TransportError,
            RemoteError,
            ConnectionError,
            _SettlementUnconfirmed,
        ) as error:
            if not (is_control_transport_error(error) or isinstance(error, _SettlementUnconfirmed)):
                raise
            retry.attempts += 1
            retry.stage = stage
            retry.next_retry = monotonic() + 2 ** (retry.attempts - 1)
            logger.warning(
                "CTF control settlement deferred task_id=%s member_id=%s turn_id=%s "
                "generation=%s stage=%s attempt=%s error_type=%s",
                task_id,
                member["id"],
                turn["id"],
                turn["generation"],
                stage,
                retry.attempts,
                type(error).__name__,
            )
            if retry.attempts >= 3:
                raise CtfControlTransportExhausted(
                    task_id, member["id"], str(turn["id"]), turn["generation"], stage
                ) from error
            return False
        self.pending_settlements.pop(key, None)
        self.settlement_retries.pop(key, None)
        return True
