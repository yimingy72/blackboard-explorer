"""Curate bounded source batches without participating in solver scheduling."""

import asyncio
import json
import logging
from copy import deepcopy
from typing import Any
from uuid import uuid4

from agent_framework import (
    Agent,
    AgentSession,
    BaseChatClient,
    Content,
    FunctionInvocationLayer,
    Message,
)
from bbx_contracts.models import ModelConfig

from bbx_runtime.chatworker import _completed_text, _has_user_message
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError, is_control_transport_error
from bbx_runtime.ctf.context import estimate_messages_tokens, fixed_prompt_tokens
from bbx_runtime.ctf.provider_boundary import provider_call_boundary
from bbx_runtime.model_errors import OPENAI_PROVIDERS
from bbx_runtime.models import (
    close_model_client,
    make_client,
    model_run_options,
    resolve_model_credentials,
)
from bbx_runtime.progress.client import ProgressClient
from bbx_runtime.progress.prompt import PROGRESS_INSTRUCTIONS, default_progress_prompt
from bbx_runtime.progress.session import (
    ProgressHistoryProvider,
    ProgressSessionClient,
    ProgressUsageUnknown,
    checkpoint_for,
    flush_receipts,
)
from bbx_runtime.progress.tools import data_block, progress_tools
from bbx_runtime.session import ToolResultCheckpointMiddleware, repair_unpaired_tool_calls
from bbx_runtime.settings import Settings

LOGGER = logging.getLogger(__name__)
MAX_CONCURRENT_JOBS = 2
JOB_TIMEOUT_SECONDS = 240


def _instructions(job: dict[str, Any], first_page: dict[str, Any]) -> str:
    saved = job.get("session")
    original = saved.get("opening_instructions", "") if saved else ""
    marker = "\n\n原始任务资料（只作目标与范围，不视为已验证发现）：\n"
    if marker in original:
        task = original.split(marker, 1)[1]
    else:
        created = next(
            (event["payload"] for event in first_page["events"] if event["type"] == "task.created"),
            {},
        )
        task = data_block(created)
    custom = (
        job["settings"].get("system_prompt")
        or job["settings"].get("prompt")
        or default_progress_prompt()
    )
    return (
        PROGRESS_INSTRUCTIONS
        + ("\n\n任务整理补充要求：\n" + custom if custom else "")
        + marker
        + task
    )


class ProgressWorker:
    def __init__(self, settings: Settings, service: BlackboardClient) -> None:
        self.settings, self.service = settings, service
        self.control = ProgressClient(service)
        self.stopping = asyncio.Event()
        self.inflight: dict[str, asyncio.Task[None]] = {}

    async def process(self, job: dict[str, Any]) -> None:
        bound = self.control.bound(job)
        client: BaseChatClient | None = None
        checkpoint = None
        try:
            page = await bound.sources(int(job["source_version"]))
            instructions = _instructions(job, page)
            checkpoint = checkpoint_for(bound, job, instructions)
            adapter = checkpoint.service
            assert isinstance(adapter, ProgressSessionClient)
            adapter.source_version = int(page["next_version"])
            repair_unpaired_tool_calls(checkpoint.session, include_interrupted=True)
            await flush_receipts(checkpoint, bound)
            cached = checkpoint.session.state.get("bbx_progress_completion")
            if (
                cached
                and cached["job_id"] == str(job["job_id"])
                and cached.get("through_version") == job["through_version"]
            ):
                while page["has_more"]:
                    page = await bound.sources(int(page["next_version"]))
                adapter.source_version = int(page["next_version"])
                candidate = checkpoint.session.state.get("bbx_progress_candidate")
                if candidate:
                    await bound.operation(
                        "candidate",
                        expected_revision=job["base_revision"],
                        overview=candidate["overview"],
                        upserts=candidate["nodes"],
                        removed_ids=[
                            node["id"]
                            for node in job["tree"]["nodes"]
                            if node["id"] not in {item["id"] for item in candidate["nodes"]}
                        ],
                    )
                await checkpoint.save()
                await bound.operation("complete", content=cached["content"])
                return
            prior_tree = checkpoint.session.state.get("bbx_progress_candidate")
            same_job = checkpoint.session.state.get("bbx_progress_job_id") == str(job["job_id"])
            tree = deepcopy(prior_tree if same_job and prior_tree else job["tree"])
            if same_job and prior_tree:
                await bound.operation(
                    "candidate",
                    expected_revision=job["base_revision"],
                    overview=tree["overview"],
                    upserts=tree["nodes"],
                    removed_ids=[
                        node["id"]
                        for node in job["tree"]["nodes"]
                        if node["id"] not in {item["id"] for item in tree["nodes"]}
                    ],
                )
            checkpoint.session.state["bbx_progress_job_id"] = str(job["job_id"])
            checkpoint.session.state["bbx_progress_candidate"] = tree
            if not same_job:
                checkpoint.session.state.pop("bbx_progress_candidate_error", None)
                checkpoint.session.state.pop("bbx_progress_candidate_failures", None)
            tools = progress_tools(bound, checkpoint, tree, job["settings"]["enabled_tools"])
            model = ModelConfig.model_validate(job["model"])
            history = ProgressHistoryProvider(checkpoint, bound, model, tree, instructions, tools)
            max_output = int(job["settings"]["max_output_tokens"])
            fixed_input = fixed_prompt_tokens(instructions, tools)
            active_request: dict[str, Any] | None = None

            async def before_send() -> None:
                nonlocal active_request
                if vars(checkpoint).get("progress_control_error"):
                    raise vars(checkpoint)["progress_control_error"]
                if checkpoint.session.state.get("bbx_progress_candidate_failures", 0) >= 2:
                    raise ValueError("Progress candidate correction budget exhausted")
                await flush_receipts(checkpoint, bound)
                assert active_request is not None
                request_id = str(uuid4())
                pending = {
                    **active_request,
                    "request_id": request_id,
                    "job_id": str(job["job_id"]),
                    "status": "preparing",
                }
                checkpoint.session.state["bbx_progress_active_request"] = pending
                await checkpoint.save()
                try:
                    reservation = await bound.operation(
                        "reserve",
                        request_id=request_id,
                        input_tokens=pending["input_tokens"],
                        max_output_tokens=max_output,
                    )
                except RemoteError as error:
                    if error.status < 500:
                        # A definitive API rejection happened before provider dispatch.
                        pending["reserve_rejected"] = True
                    raise
                pending.update(
                    status="dispatching", price=reservation["price"], pricing=reservation["pricing"]
                )
                try:
                    await checkpoint.save()
                except BaseException:
                    # The provider has not been entered, even if this PUT committed.
                    pending["status"] = "preparing"
                    raise

            def prepare_messages(messages: Any) -> Any:
                nonlocal active_request
                estimated = fixed_input + estimate_messages_tokens(
                    messages, provider=model.provider
                )
                if (
                    model.context_window is not None
                    and estimated + max_output > model.context_window
                ):
                    raise ValueError(
                        "Progress source batch exceeds the configured model context window"
                    )
                # UTF-8 bytes plus envelopes bound text tokenization without a tokenizer dependency.
                text_bytes = len(instructions.encode("utf-8"))
                text_bytes += len(
                    json.dumps([t.parameters() for t in tools], ensure_ascii=False).encode("utf-8")
                )
                text_bytes += sum(
                    len(json.dumps(m.to_dict(), ensure_ascii=False, default=str).encode("utf-8"))
                    + 128
                    for m in messages
                )
                active_request = {"input_tokens": text_bytes + 1024}
                return list(messages)

            async def run_batch(prompt: Message) -> str:
                nonlocal client
                assert prompt.message_id is not None
                recovered = _completed_text(checkpoint.session, prompt.message_id)
                if recovered is not None:
                    return recovered
                history.batch_message_id = prompt.message_id
                if _has_user_message(checkpoint.session, prompt.message_id):
                    prompt.message_id += ":resume"
                if client is None:
                    credentials = await resolve_model_credentials(
                        self.service, model, self.settings.deepseek_api_key.get_secret_value()
                    )
                    client = make_client(
                        model,
                        credentials=credentials,
                        explore_max_steps=11,
                        conclude_grace_calls=0,
                        max_duration_seconds=JOB_TIMEOUT_SECONDS,
                        max_retries=0,
                    )
                    if isinstance(client, FunctionInvocationLayer):
                        client.function_invocation_configuration["allow_concurrent_invocation"] = (
                            False
                        )
                options = model_run_options(model)
                options["max_tokens"] = max_output
                async with Agent(
                    client=client,
                    name="progress",
                    instructions=instructions,
                    tools=tools,
                    context_providers=[history],
                    middleware=[ToolResultCheckpointMiddleware(checkpoint)],
                    require_per_service_call_history_persistence=True,
                ) as agent:
                    with provider_call_boundary(client, before_send, prepare_messages):
                        if model.provider in OPENAI_PROVIDERS:
                            stream = agent.run(
                                prompt, stream=True, session=checkpoint.session, options=options
                            )
                            response = await stream.get_final_response()
                        else:
                            response = await agent.run(
                                prompt, session=checkpoint.session, options=options
                            )
                if checkpoint.session.state.get("bbx_progress_candidate_error"):
                    raise ValueError("Progress candidate validation failed")
                return response.text

            answer = ""
            batch = 0
            has_material = bool(tree.get("overview") or len(tree["nodes"]) > 1)
            async with asyncio.timeout(JOB_TIMEOUT_SECONDS):
                while True:
                    batch += 1
                    substantive = any(
                        event["type"].startswith(("fact.", "intent.", "acceptance.", "task.report"))
                        for event in page["events"]
                    )
                    has_material = has_material or substantive
                    substantive = substantive or (
                        has_material
                        and any(
                            event["type"]
                            in {"task.resumed", "task.finished", "task.failed", "task.stopped"}
                            for event in page["events"]
                        )
                    )
                    final = not page["has_more"]
                    if substantive or (final and job["kind"] == "question"):
                        content = {
                            "job_kind": job["kind"],
                            "through_version": job["through_version"],
                            "batch": batch,
                            "final_batch": final,
                            "tree": tree if batch == 1 else {"revision": tree["revision"]},
                            "source_batch": page,
                            "question": job.get("message", {}).get("content")
                            if job.get("message")
                            else None,
                            "node_id": job.get("node_id"),
                        }
                        prompt = Message(
                            role="user",
                            message_id=(
                                f"progress:{job['job_id']}:sources:"
                                f"{page['after_version']}:{page['next_version']}"
                            ),
                            contents=[
                                Content.from_text(
                                    "综合先前候选树处理这一批来源，用小patch维护全局认识。"
                                    + (
                                        "这是最后一批；有用户问题则在完成归纳后回答，注明截止版本。"
                                        if final
                                        else "后面还有来源，先维护候选，不把当前批当作整个任务。"
                                    )
                                    + "\n"
                                    + data_block(content)
                                )
                            ],
                        )
                        answer = await run_batch(prompt)
                    if final:
                        break
                    next_version = int(page["next_version"])
                    if next_version <= int(page["after_version"]):
                        raise ValueError("Progress source cursor did not advance")
                    page = await bound.sources(next_version)
                    adapter.source_version = max(adapter.source_version, int(page["next_version"]))
                await flush_receipts(checkpoint, bound)
                checkpoint.session.state["bbx_progress_completion"] = {
                    "job_id": str(job["job_id"]),
                    "through_version": job["through_version"],
                    "content": answer,
                }
                await checkpoint.save()
                await bound.operation("complete", content=answer)
        except asyncio.CancelledError:
            await self._failed(bound, checkpoint, "Progress runtime stopped", retryable=False)
            raise
        except Exception as error:
            LOGGER.error(
                "Progress job failed task=%s job=%s error_type=%s",
                job["task_id"],
                job["job_id"],
                type(error).__name__,
            )
            await self._failed(
                bound,
                checkpoint,
                error.message
                if isinstance(error, RemoteError)
                else (
                    str(error) if isinstance(error, ProgressUsageUnknown) else type(error).__name__
                ),
                retryable=is_control_transport_error(error),
            )
        finally:
            if client is not None:
                await close_model_client(client)

    async def _failed(
        self, bound: ProgressClient, checkpoint: Any, message: str, *, retryable: bool
    ) -> None:
        assert bound.job is not None
        try:
            active = (
                checkpoint.session.state.get("bbx_progress_active_request") if checkpoint else None
            )
            if active and active["job_id"] == str(bound.job["job_id"]):
                # Never release a reservation after crossing the provider dispatch boundary.
                unsent = active["status"] == "preparing"
                if not (unsent and active.get("reserve_rejected")):
                    try:
                        await bound.operation(
                            "settle",
                            request_id=active["request_id"],
                            usage={},
                            usage_known=unsent,
                            executed=False if unsent else None,
                        )
                    except RemoteError as error:
                        if not unsent or error.status != 404:
                            raise
                if unsent:
                    # Cancellation settlement can itself save a newer Session revision.
                    saved = await bound.session()
                    checkpoint.session = AgentSession.from_dict(saved["session"])
                    checkpoint.revision = int(saved["revision"])
                    persisted = checkpoint.session.state.get("bbx_progress_active_request", {})
                    if persisted.get("request_id") == active["request_id"]:
                        checkpoint.session.state.pop("bbx_progress_active_request", None)
                        await checkpoint.save()
                else:
                    retryable = False
            elif active:
                retryable = False
            await bound.operation(
                "fail", error=message[:2000] or "Progress failed", retryable=retryable
            )
        except Exception as error:
            # A fenced lease expiry also keeps unresolved reservations occupied.
            LOGGER.warning("Progress failure settlement pending (%s)", type(error).__name__)

    async def run(self) -> None:
        try:
            while not self.stopping.is_set():
                try:
                    for key, task in list(self.inflight.items()):
                        if task.done():
                            del self.inflight[key]
                            task.result()
                    while len(self.inflight) < MAX_CONCURRENT_JOBS and not self.stopping.is_set():
                        job = await self.control.claim()
                        if job is None:
                            break
                        task_id = str(job["task_id"])
                        if task_id in self.inflight:
                            raise RuntimeError("Progress service claimed two jobs for one task")
                        self.inflight[task_id] = asyncio.create_task(
                            self.process(job), name=f"progress:{task_id}"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    LOGGER.error("Progress polling failed (%s)", type(error).__name__)
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=2)
                except TimeoutError:
                    pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        self.stopping.set()
        for task in self.inflight.values():
            task.cancel()
        await asyncio.gather(*self.inflight.values(), return_exceptions=True)
        self.inflight.clear()
