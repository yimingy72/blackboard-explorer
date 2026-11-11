"""One trusted OpenSandbox controller and its ordinary blackboard scheduler."""

import asyncio
import json
import logging
import signal
import ssl
import tempfile
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from bbx_contracts.models import Params
from bbx_contracts.runner import CommandReceipt, runner_json_text, runner_payload_digest
from bbx_contracts.runner_bootstrap import retirement_manifest_digest
from bbx_contracts.runner_control import BoardRunIdentity
from bbx_contracts.runner_vpn import VPN_DYNAMIC_PROFILE, VPN_PROFILE

from bbx_runtime.board_workspace import (
    archive_board_workspace,
    read_retained_board_journal,
    restore_board_workspace,
)
from bbx_runtime.chatworker import ChatWorker
from bbx_runtime.clients import BlackboardClient, object_store
from bbx_runtime.clients.blackboard import RemoteError, is_control_transport_error
from bbx_runtime.context import execution_timestamp
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.models import load_runtime_profile
from bbx_runtime.opening import BoardOpening
from bbx_runtime.progress.worker import ProgressWorker
from bbx_runtime.receipts import parse_receipt
from bbx_runtime.runner_proxy import model_proxy_server
from bbx_runtime.runner_resources import (
    TRAFFIC_CONFIG_FIELDS,
    TRAFFIC_PROFILE,
    ResourceUnknown,
    RunnerResources,
)
from bbx_runtime.scheduler.executor import ActionExecutor
from bbx_runtime.scheduler.lock import RuntimeLock
from bbx_runtime.scheduler.loop import SchedulerLoop
from bbx_runtime.settings import NativeRunnerSettings


class BootstrapStopped(RuntimeError):
    pass


BOARD_CONTRACT = "exploration-board-v1"


class NativeRunnerController:
    def __init__(
        self, settings, service, resources, instance, *, board_resources=None, objects=None
    ):
        self.settings, self.service, self.resources, self.instance = (
            settings,
            service,
            resources,
            instance,
        )
        if (
            instance.fence is None
            or instance.backend_pid is None
            or instance.backend_started_at is None
        ):
            raise RuntimeError("Native Runner control requires the fenced Runtime instance lock")
        self.controller_id = str(uuid4())
        self.board_resources = board_resources
        self.objects = objects
        self.retirement_authority = resources.config.get("retirement_single_apiserver", {})
        self.config = self._resource_config(resources)
        self.board_config = (
            self._resource_config(board_resources) if board_resources else self.config
        )
        self.jobs = {}
        self.board_schedulers: dict[str, tuple[SchedulerLoop, asyncio.Task[None]]] = {}
        self.closed = False

    def _resource_config(self, resources):
        return {
            **{
                key: value
                for key, value in resources.config.items()
                if key != "retirement_single_apiserver"
            },
            "runner_blackboard_url": self.settings.runner_blackboard_url.rstrip("/"),
            "runner_model_proxy_url": self.settings.runner_model_proxy_url.rstrip("/"),
        }

    def _operation_resources(self, operation):
        if operation["data"].get("execution_contract") == BOARD_CONTRACT:
            if self.board_resources:
                return self.board_resources, self.board_config
            if getattr(self.settings, "board_resource_config_file", None) is not None:
                raise ResourceUnknown("The published Blackboard resource view is unavailable")
        return self.resources, self.config

    def _claim_config(self, operation):
        _, config = self._operation_resources(operation)
        data = operation["data"]
        with_vpn = data.get("vpn_profile") in {VPN_PROFILE, VPN_DYNAMIC_PROFILE}
        if data.get("execution_contract") != BOARD_CONTRACT:
            with_vpn |= data.get("native_network") is not None
        else:
            with_vpn |= self._forced_board_policy(operation, config)
        return {
            key: value
            for key, value in config.items()
            if (key not in TRAFFIC_CONFIG_FIELDS or data.get("traffic_profile") == TRAFFIC_PROFILE)
            and (key != "vpn" or with_vpn)
        }

    @staticmethod
    def _forced_board_policy(operation, config):
        return operation["data"].get("execution_contract") == BOARD_CONTRACT and config.get(
            "board_network", {}
        ).get("mode") in {"vpn", "socks5"}

    @staticmethod
    def _forced_board_ready(operation):
        vpn = operation["data"].get("vpn", {})
        applied = vpn.get("applied", {})
        return (
            vpn.get("state") == "ready"
            and applied.get("mode") == "solver"
            and applied.get("applied_until_ms", 0) > int(datetime.now(UTC).timestamp() * 1000)
        )

    @staticmethod
    def path(tid):
        return f"/tasks/{tid}/runtime/runner-bootstrap"

    def current(self):
        if self.closed or self.instance.lost.is_set():
            raise asyncio.CancelledError

    async def view(self, tid):
        self.current()
        return await self.service._json("GET", self.path(tid))

    async def action(self, tid, operation, action, **values):
        self.current()
        return await self.service._json(
            "POST",
            self.path(tid) + "/actions",
            json={
                "operation_id": operation["operation_id"],
                "controller_id": self.controller_id,
                "controller_generation": self.instance.fence,
                "resource_generation": operation["data"].get("resource_generation", 1),
                "action": action,
                **values,
            },
        )

    async def _board_run(
        self, tid: str, aid: str, started_at: str, derive_round: int | None
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        while True:
            self.current()
            try:
                state = await self.service.state(tid)
                break
            except Exception as error:
                if not is_control_transport_error(error):
                    raise
                await asyncio.sleep(1)
        task = state["task"]
        agent = state["agents"].get(aid)
        if (
            task.get("mode") != "blackboard"
            or task["status"] not in {"running", "closing"}
            or agent is None
            or agent["status"] not in {"running", "concluding"}
            or execution_timestamp(agent.get("started_at")) != execution_timestamp(started_at)
            or (agent.get("derive_round") if agent["task_type"] == "derive" else None)
            != derive_round
        ):
            return None
        identity = BoardRunIdentity.model_validate(
            {
                "run_number": task["run_number"],
                "started_at": started_at,
                "derive_round": derive_round,
            }
        )
        return (
            state,
            agent,
            {"agent_id": aid, **identity.model_dump(mode="json", exclude_none=True)},
        )

    async def _board_action(
        self, tid: str, action: str, proof: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        while True:
            try:
                operation = await self.view(tid)
                if operation["data"].get("execution_contract") != BOARD_CONTRACT:
                    raise ResourceUnknown("Original ordinary Runner resources are unavailable")
                if action == "board_dispatch" and operation["data"].get("board_recovery_blocked"):
                    raise ResourceUnknown("The original Blackboard execution awaits reconciliation")
                return operation, await self.action(tid, operation, action, proof=proof)
            except Exception as error:
                if not is_control_transport_error(error):
                    raise
                self.current()
                await asyncio.sleep(1)

    async def run_agent(
        self, task_id: str, agent_id: str, *, started_at: str, derive_round: int | None
    ) -> None:
        original = await self._board_run(task_id, agent_id, started_at, derive_round)
        if original is None:
            return
        state, agent, proof = original
        task = state["task"]
        profile = load_runtime_profile(
            await self.service.get_profile(task["agent_profile"], task["agent_profile_version"])
        )
        instructions = await BoardOpening(
            task_id=task_id,
            agent_id=agent_id,
            task_type=agent["task_type"],
            state=state,
            profile=profile,
            board=self.service,
            params=Params.model_validate(task["params"]),
            intent_id=agent.get("intent_id"),
            mode=agent.get("close_mode"),
        ).render()
        message = "开始。"
        if derive_round is not None:
            mode = (
                "完成复核"
                if agent.get("derive_review")
                else "并行推导"
                if agent.get("derive_parallel")
                else "静止推导"
            )
            message = (
                f"开始第 {derive_round} 轮推导。本轮模式：{mode}。"
                f"当前黑板版本：{task.get('version', 0)}；"
                f"本轮起点版本：{agent.get('round_start_version', 0)}。"
                "只将本轮实际提交的意图写入本轮回执；旧轮次回执仅属历史。"
            )
        dispatch = {**proof, "instructions": instructions, "input": message}
        while (
            current := await self._board_run(task_id, agent_id, started_at, derive_round)
        ) is not None:
            operation, status = await self._board_action(task_id, "board_dispatch", dispatch)
            if status["input_status"] == "completed":
                terminal = CommandReceipt.model_validate(status["terminal"])
                if (
                    terminal.status != "completed"
                    or terminal.board_terminal is None
                    or terminal.request_id != status["input_request_id"]
                    or terminal.input_id != status["input_id"]
                    or terminal.binding_id != status["binding_id"]
                ):
                    raise ResourceUnknown("Original Blackboard terminal receipt changed")
                resources, _ = self._operation_resources(operation)
                text = await self.guarded(
                    task_id,
                    operation,
                    resources.read_board_terminal(operation, agent_id, status["terminal"]),
                )
                await self._record_board_terminal_trace(
                    task_id, agent_id, current[1], terminal, proof["run_number"], derive_round, text
                )
                await self._record_board_journal(
                    task_id, agent_id, operation, status["terminal"], proof["run_number"]
                )
                normal = terminal.board_terminal.outcome == "completed" and bool(text.strip())
                receipt = (
                    parse_receipt(text, agent["task_type"])
                    if normal
                    else {"accepted": False, "reason": "原 Pi 会话未提供正常终态"}
                )
                try:
                    await self.service.finish_agent(
                        task_id,
                        agent_id,
                        receipt,
                        "normal" if normal else "runtime_error",
                        expected_run_number=proof["run_number"],
                        expected_agent_run_id=str(agent.get("id", agent_id)),
                        expected_started_at=started_at,
                        expected_derive_round=derive_round,
                        terminal_request_id=terminal.request_id,
                    )
                    return
                except Exception as error:
                    if not is_control_transport_error(error):
                        raise
            await asyncio.sleep(2)

    async def _record_board_journal(
        self, tid, aid, operation, receipt, run_number, *, cleaning=False, offline=False
    ) -> bool:
        if self.objects is None:
            return False
        resources, _ = self._operation_resources(operation)
        cursor = receipt["cursor"]
        uri = f"traces/{tid}/{aid}/{receipt['binding_id']}/{cursor['hash']}.jsonl"
        try:
            with tempfile.TemporaryFile() as output:
                reader = (
                    read_retained_board_journal(resources, operation, aid, receipt, output)
                    if offline
                    else resources.read_board_journal(operation, aid, receipt, output)
                )
                size, sha = await self.guarded(tid, operation, reader, cleanup=cleaning)
                self.current()
                output.seek(0)
                if not await self.objects.exists(uri):
                    self.current()
                    await self.objects.put(
                        uri, output, length=size, content_type="application/x-ndjson"
                    )
            self.current()
            await self.service.record_board_journal(
                tid,
                aid,
                {
                    "format": "pi-journal-v1",
                    "binding_id": receipt["binding_id"],
                    "sdk_session_id": receipt["sdk_session_id"],
                    "cursor": cursor,
                    "run_number": run_number,
                    "transcript_uri": uri,
                    "sha256": sha,
                    "size": size,
                },
            )
            return True
        except Exception as error:
            logging.getLogger(__name__).warning(
                "The original Blackboard journal could not be projected (%s)", type(error).__name__
            )
            return False

    async def _read_stopping_board_journals(self, tid, operation, *, offline=False):
        if not operation["observation"].get("runner", {}).get("pod_uid"):
            return
        try:
            async with asyncio.timeout(180 if offline else 20):
                status = await self.service._json(
                    "GET", self.path(tid), params={"journals": "true"}
                )
                if offline and "board_journals" not in status:
                    raise ResourceUnknown(
                        "The original Blackboard journal inventory is unavailable"
                    )
                for item in status.get("board_journals", ()):
                    if item.get("projected") is True:
                        continue
                    receipt = {
                        **item,
                        "fence": {
                            "task_id": tid,
                            "pod_uid": item["pod_uid"],
                            "runner_id": item["runner_id"],
                            "epoch": item["owner_epoch"],
                        },
                    }
                    saved = await self._record_board_journal(
                        tid,
                        item["agent_id"],
                        operation,
                        receipt,
                        item["run_number"],
                        cleaning=True,
                        offline=offline,
                    )
                    if offline and not saved:
                        raise ResourceUnknown("A confirmed Blackboard journal remains unarchived")
        except (RemoteError, TimeoutError):
            if offline:
                raise ResourceUnknown(
                    "Retained Blackboard journal export is not confirmed"
                ) from None
            logging.getLogger(__name__).warning(
                "The last confirmed Blackboard journals remain on their original volume"
            )

    async def _record_board_terminal_trace(
        self, task_id, agent_id, agent, terminal, run_number, derive_round, text
    ):
        if self.objects is None or not text or not terminal.board_terminal.assistant_entry_id:
            return
        entry = terminal.board_terminal.assistant_entry_id
        uri = f"traces/{task_id}/{agent_id}/{terminal.binding_id}-{entry}.json"
        value = json.dumps({"text": text}, ensure_ascii=False).encode()
        try:
            if not await self.objects.exists(uri):
                await self.objects.put(uri, value, content_type="application/json")
            elif await self.objects.get(uri) != value:
                raise ResourceUnknown("Original Blackboard trace object changed")
            await self.service.record_agent_trace(
                task_id,
                agent_id,
                {
                    "kind": "model_output",
                    "step": max(1, int(agent.get("steps") or 0)),
                    "uri": uri,
                    "summary": text[:240],
                    "runner_trace": {
                        "binding_id": terminal.binding_id,
                        "sdk_session_id": terminal.sdk_session_id,
                        "cursor": terminal.cursor.model_dump(mode="json"),
                        "entry_id": entry,
                        "run_number": run_number,
                    },
                },
                expected_derive_round=derive_round,
            )
        except Exception as error:
            logging.getLogger(__name__).warning(
                "The original Blackboard trace could not be projected (%s)", type(error).__name__
            )

    async def stop_agent(
        self, task_id: str, agent_id: str, reason: str, *, started_at: str, derive_round: int | None
    ) -> None:
        while (
            original := await self._board_run(task_id, agent_id, started_at, derive_round)
        ) is not None:
            operation, status = await self._board_action(
                task_id, "board_abort", {**original[2], "reason": reason}
            )
            if status["status"] == "aborted":
                terminal = CommandReceipt.model_validate(status["terminal"])
                if (
                    terminal.status not in {"aborted", "closed"}
                    or terminal.request_id != status["request_id"]
                    or terminal.binding_id != status["binding_id"]
                    or terminal.sdk_idle is not True
                    or terminal.journal_confirmed is not True
                    or terminal.cursor is None
                    or terminal.pending_confirmation is not None
                    or terminal.unresolved_attempts
                    or terminal.unresolved_invocations
                ):
                    raise ResourceUnknown("Original Blackboard abort is not confirmed idle")
                await self._record_board_journal(
                    task_id, agent_id, operation, status["terminal"], original[2]["run_number"]
                )
                try:
                    await self.service.finish_agent(
                        task_id,
                        agent_id,
                        {"accepted": False, "reason": f"Agent {reason}"},
                        reason if reason in {"heartbeat", "grace_timeout"} else "runtime_error",
                        expected_run_number=original[2]["run_number"],
                        expected_agent_run_id=str(original[1].get("id", agent_id)),
                        expected_started_at=started_at,
                        expected_derive_round=derive_round,
                        terminal_request_id=terminal.request_id,
                    )
                    return
                except Exception as error:
                    if not is_control_transport_error(error):
                        raise
            await asyncio.sleep(1)

    async def _archive_stopped_board(self, tid, operation, resources):
        if not operation["observation"].get("workspace_pvc"):
            return
        task = (await self.service.state(tid))["task"]
        if task.get("deleting") or task.get("cleanup_ready"):
            return
        await self._read_stopping_board_journals(tid, operation, offline=True)
        if operation["data"].get("board_archive"):
            await self.service.record_cleanup(tid)
            return
        if self.objects is None:
            raise ResourceUnknown("The trusted Blackboard archive store is unavailable")
        data = await self.service.archive_data(tid)
        if data.get("run_number") != task["run_number"]:
            raise ResourceUnknown("The original Blackboard archive snapshot changed")
        uri, size, proof = await self.guarded(
            tid,
            operation,
            archive_board_workspace(resources, operation, data, self.objects),
            cleanup=True,
        )
        self.current()
        await self.service.record_archive(tid, uri, size, "none", runner_archive=proof)
        await self.service.record_cleanup(tid)

    @staticmethod
    def stopping(operation):
        return operation["desired"] == "stopped" or datetime.now(UTC) >= datetime.fromisoformat(
            operation["hard_deadline"]
        )

    async def guarded(self, tid, operation, awaitable, *, cleanup=False):
        work = asyncio.create_task(awaitable)
        try:
            while not work.done():
                self.current()
                done, _ = await asyncio.wait([work], timeout=3)
                if done:
                    break
                state = await self.view(tid)
                if self.stopping(state) and not cleanup:
                    raise BootstrapStopped("Native bootstrap stop was requested")
            return await work
        finally:
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)

    async def _prepare_sandbox_host(self, tid, operation, tenant_key):
        resources, _ = self._operation_resources(operation)
        context = await self.guarded(
            tid, operation, resources.sandbox_host_context(operation, tenant_key)
        )
        private = {key: context.pop(key) for key in ("execd_token", "execd_headers")}
        material = await self.action(
            tid, operation, "sandbox_host_material", proof={"context": context}
        )
        digest = material.pop("context_digest")
        material.pop("host_url")
        configuration = {
            **material,
            **private,
            "version": 1,
            "ca_file": "/var/run/bbx-sandbox-host/ca.pem",
        }
        await self.guarded(tid, operation, resources.publish_sandbox_host(operation, configuration))
        await self.guarded(
            tid, operation, resources.wait_sandbox_host_ready(operation, configuration)
        )
        operation.update(
            await self.action(
                tid, operation, "sandbox_host_ready", proof={"context_digest": digest}
            )
        )

    async def _watch_worker_events(self, tid, wake):
        since = 0
        while not self.closed:
            try:
                async for event in self.service.stream(tid, since):
                    since = max(since, event["version"])
                    if event["type"] in {
                        "ctf.challenge.created",
                        "ctf.challenge.updated",
                        "board.flag.receipt",
                        "ctf.message.posted",
                        "ctf.native_worker.assigned",
                        "ctf.native_worker.released",
                        "ctf.native_worker.stopped",
                        "ctf.native_worker.cleanup_changed",
                    }:
                        wake.set()
            except RemoteError as error:
                if error.status in {401, 403, 404}:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(1)

    async def _task(self, tid):
        self.current()
        initial = await self.view(tid)
        if (
            initial.get("never_claimed")
            and initial.get("data", {}).get("retirement", {}).get("policy") == "user_delete"
        ):
            await self.service._json("POST", f"/tasks/{tid}/purge")
            return
        while True:
            try:
                operation = await self.service._json(
                    "POST",
                    self.path(tid) + "/claim",
                    json={
                        "controller_id": self.controller_id,
                        "controller_fence": self.instance.fence,
                        "controller_pid": self.instance.backend_pid,
                        "controller_started_at": self.instance.backend_started_at.isoformat(),
                        "resource_config": self._claim_config(initial),
                        "retirement_single_apiserver": self.retirement_authority,
                    },
                )
                break
            except RemoteError as error:
                if error.status == 404:
                    return
                self.current()
                await asyncio.sleep(10)
            except Exception as error:
                if not is_control_transport_error(error):
                    raise
                self.current()
                await asyncio.sleep(5)
        tenant_key = operation.pop("tenant_key")
        wake = asyncio.Event()
        watcher = None

        async def idle():
            nonlocal watcher
            if operation["data"].get("worker_spec") is not None and watcher is None:
                watcher = asyncio.create_task(self._watch_worker_events(tid, wake))
            _, config = self._operation_resources(operation)
            board_ready = (
                operation["data"].get("execution_contract") == BOARD_CONTRACT
                and operation["state"] == "ready"
                and not operation["data"].get("recovery")
                and not operation["data"].get("board_recovery_blocked")
                and not self.stopping(operation)
                and (
                    not self._forced_board_policy(operation, config)
                    or self._forced_board_ready(operation)
                )
            )
            existing = self.board_schedulers.get(tid)
            if board_ready and existing is None:
                state = await self.service.state(tid)
                task = state["task"]
                if task.get("mode") != "blackboard":
                    raise ResourceUnknown("The ordinary scheduler cannot own a CTF Task")
                if task["status"] in {"running", "closing"}:
                    executor = ActionExecutor(tid, self.service, board_runner=self)
                    executor.recover_board(state)
                    board_loop = SchedulerLoop(
                        executor, self.service, Params.model_validate(task["params"])
                    )
                    self.board_schedulers[tid] = (
                        board_loop,
                        asyncio.create_task(board_loop.run(), name=f"board:{tid}"),
                    )
            elif existing is not None and (not board_ready or existing[1].done()):
                board_loop, board_work = self.board_schedulers.pop(tid)
                await board_loop.stop()
                await board_work
            try:
                await asyncio.wait_for(wake.wait(), timeout=3)
            except TimeoutError:
                pass
            finally:
                wake.clear()

        try:
            await self._drive(tid, operation, tenant_key, idle)
        finally:
            if existing := self.board_schedulers.pop(tid, None):
                board_loop, board_work = existing
                await board_loop.stop()
                await asyncio.gather(board_work, return_exceptions=True)
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    @staticmethod
    def _credential_scope(operation):
        data = operation["data"]
        bindings = (
            data.get("board_binding_ids", {}).values()
            if data.get("execution_contract") == BOARD_CONTRACT
            else data.get("worker_binding_ids", ())
        )
        return (
            data.get("binding_id"),
            data.get("pause_state"),
            tuple(sorted(bindings)),
        )

    @staticmethod
    def _material_changed(scope, published, control_only):
        return (
            published is None
            or scope[:2] != published[:2]
            or (not control_only and scope[2] != published[2])
        )

    @staticmethod
    def _traffic_pending(operation):
        data = operation["data"]
        original = operation["observation"].get("sandbox", {}).get("pod_uid")
        return data.get("traffic_profile") == TRAFFIC_PROFILE and any(
            entry["state"] != "archived" and entry["scope"]["sandbox_pod_uid"] == original
            for entry in data.get("network_scopes", {}).values()
        )

    async def _recover_original(self, tid, operation, tenant_key):
        resources, _ = self._operation_resources(operation)
        incident = operation["data"]["recovery"]
        identity = {"incident_id": incident["incident_id"]}
        blocker = "journal_writer_unconfirmed"
        try:
            if self._traffic_pending(operation):
                blocker = "original_traffic_unconfirmed"
                raise ResourceUnknown("The original capture archive is unresolved")
            node = await self.guarded(tid, operation, resources.recovery_volumes(operation))
            if not incident.get("exit_proof"):
                blocker = "original_exit_unconfirmed"
                original = operation["observation"]
                proof = await self.guarded(tid, operation, resources.stop(original, tenant_key))
                if not all(
                    proof.get(key) is True
                    for key in ("runner_terminated", "sandbox_terminated", "pvcs_retained")
                ):
                    raise ResourceUnknown("Original native exits remain unconfirmed")
                result = await self.action(
                    tid,
                    operation,
                    "recovery_exit",
                    proof={
                        **identity,
                        **proof,
                        "observation_digest": runner_payload_digest(runner_json_text(original)),
                    },
                )
                operation.update(result)
                return
            prepared = await self.action(tid, operation, "recovery_prepare", proof=identity)
            if prepared.get("blocker"):
                return
            blocker = "journal_writer_unconfirmed"
            await self.guarded(tid, operation, resources.release_recovery(operation))
            blocker = "original_journal_unconfirmed"
            verifier = {**identity, "manifest_digest": prepared["manifest_digest"]}

            async def admit():
                result = await self.action(
                    tid, operation, "recovery_verifier_admit", proof=verifier
                )
                return result.get("create_allowed") is True

            async def checkpoint(receipt):
                operation.update(
                    await self.action(
                        tid, operation, "recovery_verifier_observe", proof={**verifier, **receipt}
                    )
                )

            proof = await self.guarded(
                tid,
                operation,
                resources.verify_recovery_journal(
                    operation, prepared, node, admit=admit, checkpoint=checkpoint
                ),
            )
            result = await self.action(
                tid, operation, "recovery_replace", proof={**identity, **proof}
            )
            if not result.get("blocker"):
                operation.update(result)
        except ResourceUnknown:
            await self.action(
                tid, operation, "recovery_blocked", proof={**identity, "blocker": blocker}
            )

    async def _prepare_network(
        self,
        tid,
        operation,
        resources,
        config,
        control_only,
        published_vpn_generation,
        last_vpn_control,
    ):
        data = operation["data"]
        if (
            data.get("execution_contract") == BOARD_CONTRACT
            and data.get("native_network") == "default"
            and data.get("network_digest")
            and not data.get("network_policy")
        ):
            receipt = await self.guarded(
                tid, operation, resources.ensure_default_network_policy(operation)
            )
            operation.update(
                await self.action(tid, operation, "network_default_ready", proof=receipt)
            )
        generation = operation["data"].get("resource_generation", 1)
        forced_board = self._forced_board_policy(operation, config)
        new_vpn_mount = published_vpn_generation != generation
        now = asyncio.get_running_loop().time()
        if operation["data"].get("vpn_profile") in {VPN_PROFILE, VPN_DYNAMIC_PROFILE} and (
            new_vpn_mount
            or (
                forced_board
                and not self._forced_board_ready(operation)
                and now - last_vpn_control >= 20
            )
        ):
            material = await self.action(tid, operation, "vpn_material")
            if new_vpn_mount:
                await self.guarded(
                    tid,
                    operation,
                    resources.publish_vpn_mount(
                        operation, material, config["runner_blackboard_url"]
                    ),
                )
                published_vpn_generation = generation
            if forced_board:
                last_vpn_control = now
                operation.update(await self.view(tid))
        if (
            operation["data"].get("vpn_profile") == VPN_DYNAMIC_PROFILE
            and (not control_only or forced_board)
            and operation["data"].get("pause_state") not in {"draining", "paused", "resuming"}
        ):
            data = operation["data"]
            vpn = data["vpn"]
            if data.get("native_network") == "default" and not data.get("network_policy"):
                receipt = await self.guarded(
                    tid, operation, resources.ensure_default_network_policy(operation)
                )
                operation.update(
                    await self.action(tid, operation, "network_default_ready", proof=receipt)
                )
            elif not forced_board and vpn["state"] in {"source_pending", "acquiring"}:
                if vpn["state"] == "source_pending":
                    await self.guarded(
                        tid, operation, resources.verify_vpn_source_boundary(operation)
                    )
                operation.update(
                    await self.action(
                        tid,
                        operation,
                        "vpn_prepare_source",
                        proof={"vpn_operation_id": vpn["operation_id"]},
                    )
                )
            elif vpn["state"] == "awaiting_policy":
                receipt = await self.guarded(
                    tid, operation, resources.ensure_vpn_peer_policy(operation)
                )
                operation.update(await self.action(tid, operation, "vpn_peer_ready", proof=receipt))
        ready = not forced_board or self._forced_board_ready(operation)
        return published_vpn_generation, last_vpn_control, ready

    async def _drive(self, tid, operation, tenant_key, idle):
        resources, config = self._operation_resources(operation)
        last_rotation = 0.0
        published_scope = None
        published_vpn_generation = None
        last_vpn_control = 0.0
        stopping_journals_read = False
        control_only = operation["state"] == "held"
        while not self.closed:
            self.current()
            try:
                state = await self.view(tid)
                if state["state"] == "stopped":
                    operation.update(state)
                    if state["data"].get("execution_contract") == BOARD_CONTRACT:
                        try:
                            await self._archive_stopped_board(tid, operation, resources)
                        except (ResourceUnknown, RemoteError, OSError, TimeoutError):
                            logging.getLogger(__name__).warning(
                                "The original Blackboard workspace archive awaits reconciliation"
                            )
                            return
                    retirement = state["data"].get("retirement", {})
                    if retirement and retirement.get("state") != "retired":
                        operation.update(state)
                        operation.update(await self.action(tid, operation, "retire_prepare"))

                        async def checkpoint_retirement(manifest):
                            operation.update(
                                await self.action(
                                    tid, operation, "retire_plan", proof={"manifest": manifest}
                                )
                            )

                        async def admit_retirement():
                            result = await self.action(
                                tid,
                                operation,
                                "retire_admit",
                                proof={
                                    "manifest_digest": retirement_manifest_digest(
                                        operation["retirement"]
                                    )
                                },
                            )
                            return result["admitted"]

                        proof = await self.guarded(
                            tid,
                            operation,
                            resources.retire(
                                operation,
                                checkpoint=checkpoint_retirement,
                                admit_retire=admit_retirement,
                            ),
                            cleanup=True,
                        )
                        proof["manifest_digest"] = retirement_manifest_digest(
                            operation["retirement"]
                        )
                        operation.update(
                            await self.action(tid, operation, "retire_complete", proof=proof)
                        )
                    if retirement.get("policy") == "user_delete":
                        await self.service._json("POST", f"/tasks/{tid}/purge")
                    return
                operation.update(state)
                if state["data"].get("board_recovery_blocked") and not self.stopping(operation):
                    await idle()
                    continue
                if state["state"] == "exited":
                    await self.action(tid, operation, "cleanup")
                    continue
                if self.stopping(operation):
                    cutoff = operation["data"].get("board_close_drain_until")
                    if cutoff and operation["data"].get("execution_contract") == BOARD_CONTRACT:
                        remaining = (
                            datetime.fromisoformat(cutoff) - datetime.now(UTC)
                        ).total_seconds()
                        if 0 < remaining <= 10:
                            # Durable stop intent already fenced all new sends. Let the original
                            # committed Close result and SDK journal confirmation finish.
                            await asyncio.sleep(min(remaining, 0.5))
                            continue
                    if (
                        not stopping_journals_read
                        and operation["data"].get("execution_contract") == BOARD_CONTRACT
                    ):
                        stopping_journals_read = True
                        await self._read_stopping_board_journals(tid, operation)
                    operation.update(await self.action(tid, operation, "stop"))
                    cutoff = operation["data"].get("traffic_preseal_until")
                    if cutoff and self._traffic_pending(operation):
                        remaining = (
                            datetime.fromisoformat(cutoff) - datetime.now(UTC)
                        ).total_seconds()
                        if remaining > 0:
                            # The independent original Host owns object I/O; stop polls state only.
                            await asyncio.sleep(min(remaining, 0.75))
                            continue
                    if operation["data"].get("recovery", {}).get("exit_proof"):
                        incident = operation["data"]["recovery"]

                        async def checkpoint_stopped_verifier(receipt, incident=incident):
                            operation.update(
                                await self.action(
                                    tid,
                                    operation,
                                    "recovery_verifier_observe",
                                    proof={
                                        "incident_id": incident["incident_id"],
                                        "manifest_digest": incident["verifier"]["manifest_digest"],
                                        **receipt,
                                    },
                                )
                            )

                        await self.guarded(
                            tid,
                            operation,
                            resources.release_recovery(
                                operation, stopping=True, checkpoint=checkpoint_stopped_verifier
                            ),
                            cleanup=True,
                        )
                        operation.update(
                            await self.action(
                                tid,
                                operation,
                                "cleanup",
                                proof={
                                    "runner_terminated": True,
                                    "sandbox_terminated": True,
                                    "pvcs_retained": True,
                                    **(
                                        {
                                            "egress_terminated": incident["exit_proof"].get(
                                                "egress_terminated"
                                            )
                                            is True,
                                            "egress_container_id": incident["exit_proof"].get(
                                                "egress_container_id"
                                            ),
                                        }
                                        if operation["data"].get("vpn_profile")
                                        in {VPN_PROFILE, VPN_DYNAMIC_PROFILE}
                                        else {}
                                    ),
                                    "observation_digest": runner_payload_digest(
                                        runner_json_text(operation["observation"])
                                    ),
                                },
                            )
                        )
                        continue
                    recovered = await self.guarded(
                        tid, operation, resources.recover(operation, tenant_key), cleanup=True
                    )
                    not_started = recovered.pop("not_started", False)
                    pvcs_retained = recovered.pop("pvcs_retained", False)
                    operation.update(
                        await self.action(tid, operation, "observation", observation=recovered)
                    )
                    observation = operation["observation"]
                    if not_started:
                        proof = {"not_started": True, "pvcs_retained": pvcs_retained}
                    else:
                        proof = await self.guarded(
                            tid,
                            operation,
                            resources.stop(observation, tenant_key),
                            cleanup=True,
                        )
                        proof["pvcs_retained"] = proof["pvcs_retained"] and pvcs_retained
                    proof["observation_digest"] = runner_payload_digest(
                        runner_json_text(observation)
                    )
                    operation.update(await self.action(tid, operation, "cleanup", proof=proof))
                    await asyncio.sleep(1)
                    continue
                if operation["data"].get("recovery"):
                    if operation["data"]["recovery"].get("request"):
                        await self._recover_original(tid, operation, tenant_key)
                        if operation["data"].get("recovery") and not self.stopping(operation):
                            await idle()
                        continue
                    try:
                        observed = await self.guarded(
                            tid, operation, resources.observe(operation["observation"])
                        )
                    except ResourceUnknown:
                        # The original recovery snapshot remains the authority.
                        pass
                    else:
                        operation.update(
                            await self.action(tid, operation, "observation", observation=observed)
                        )
                    if not self.stopping(operation):
                        await idle()
                    continue
                if not operation["data"].get("credentials_issued"):

                    async def admit():
                        value = await self.action(tid, operation, "sandbox_create")
                        return value["create_allowed"]

                    async def checkpoint(observed):
                        operation.update(
                            await self.action(tid, operation, "observation", observation=observed)
                        )

                    observed = await self.guarded(
                        tid,
                        operation,
                        resources.ensure_task_resources(
                            operation, tenant_key, admit_sandbox_create=admit, checkpoint=checkpoint
                        ),
                    )
                    operation.update(
                        await self.action(tid, operation, "observation", observation=observed)
                    )
                else:
                    observed = await self.guarded(
                        tid, operation, resources.observe(operation["observation"])
                    )
                    operation.update(
                        await self.action(tid, operation, "observation", observation=observed)
                    )
                    if operation["data"].get("recovery"):
                        continue
                    if any(
                        value.get("observation") == "unknown"
                        or value.get("terminated") is True
                        or value.get("host_terminated") is True
                        for value in (observed.get("runner", {}), observed.get("sandbox", {}))
                    ):
                        raise ResourceUnknown(
                            "The original native execution identity is unavailable"
                        )
                if operation["data"].get("board_workspace_source") and not operation["data"].get(
                    "board_workspace_restored"
                ):
                    if self.objects is None:
                        raise ResourceUnknown("The trusted Blackboard archive store is unavailable")
                    proof = await self.guarded(
                        tid, operation, restore_board_workspace(resources, operation, self.objects)
                    )
                    while True:
                        try:
                            operation.update(
                                await self.action(
                                    tid, operation, "board_workspace_restored", proof=proof
                                )
                            )
                            break
                        except Exception as error:
                            if not is_control_transport_error(error):
                                raise
                            self.current()
                            await asyncio.sleep(1)
                if config.get("runner_profile") == "node-sandbox-host-v1" and not operation[
                    "data"
                ].get("sandbox_host_ready"):
                    await self._prepare_sandbox_host(tid, operation, tenant_key)
                (
                    published_vpn_generation,
                    last_vpn_control,
                    network_ready,
                ) = await self._prepare_network(
                    tid,
                    operation,
                    resources,
                    config,
                    control_only,
                    published_vpn_generation,
                    last_vpn_control,
                )
                if not network_ready:
                    await idle()
                    continue
                now = asyncio.get_running_loop().time()
                if (
                    not operation["data"].get("credentials_issued")
                    or now - last_rotation >= 20
                    or self._material_changed(
                        self._credential_scope(operation), published_scope, control_only
                    )
                ):
                    material = await self.action(
                        tid,
                        operation,
                        "renew" if operation["data"].get("credentials_issued") else "material",
                    )
                    operation.update(material["status"])
                    # Recheck durable intent after issuing and before writing the UID-bound mount.
                    latest = await self.view(tid)
                    if self.stopping(latest):
                        raise BootstrapStopped("Native credential publication was stopped")
                    if latest["data"].get("recovery"):
                        continue
                    scope = self._credential_scope(operation)
                    if self._credential_scope(latest) != scope:
                        continue
                    control_only = material.get("control_only", False)
                    retained = material.get("retain_binding_ids", ())
                    await self.guarded(
                        tid,
                        operation,
                        resources.publish_mount(
                            operation["observation"],
                            material["bootstrap"],
                            material["credentials"],
                            control_only=control_only,
                            **({"retain_binding_ids": retained} if retained else {}),
                        ),
                    )
                    last_rotation = asyncio.get_running_loop().time()
                    published_scope = scope
                if (
                    not control_only
                    or operation["data"].get("worker_spec") is not None
                    or operation["data"].get("pause_state") in {"draining", "resuming"}
                ):
                    operation.update(await self.action(tid, operation, "activate"))
                if self._material_changed(
                    self._credential_scope(operation), published_scope, control_only
                ):
                    continue
                await idle()
            except BootstrapStopped:
                operation.update(await self.action(tid, operation, "stop"))
            except ResourceUnknown:
                await self.action(
                    tid,
                    operation,
                    "error",
                    error_code="native_resource_unknown",
                    proof={"revoke_owner": bool(operation["data"].get("credentials_issued"))},
                )
                await asyncio.sleep(10)
            except RemoteError as error:
                if error.status == 409:
                    latest = await self.view(tid)
                    if self.stopping(latest) or latest["state"] in {"exited", "stopped"}:
                        operation.update(latest)
                        continue
                if error.status in {401, 403, 404}:
                    raise
                if not operation["data"].get("recovery") or self.stopping(operation):
                    await self.action(
                        tid, operation, "error", error_code="native_control_unavailable"
                    )
                await asyncio.sleep(10)
            except Exception as error:
                if not is_control_transport_error(error):
                    raise
                self.current()
                await asyncio.sleep(5)

    async def _supervise_task(self, tid):
        while not self.closed:
            self.current()
            try:
                await self._task(tid)
                return
            except Exception as error:
                if not is_control_transport_error(error):
                    raise
                # Re-read the original PG operation; never repeat an uncertain SDK create.
                await asyncio.sleep(5)

    async def run(self):
        after = None
        try:
            while not self.closed:
                self.current()
                for tid, job in list(self.jobs.items()):
                    if job.done():
                        self.jobs.pop(tid)
                        try:
                            job.result()
                        except (RemoteError, ResourceUnknown):
                            logging.getLogger(__name__).warning(
                                "Native bootstrap awaits trusted reconciliation"
                            )
                try:
                    values = await self.service._json(
                        "GET", "/native-runner/bootstraps", params={"after": after} if after else {}
                    )
                except Exception as error:
                    if not is_control_transport_error(error):
                        raise
                    await asyncio.sleep(5)
                    continue
                tids = values["task_ids"]
                for tid in tids:
                    if tid not in self.jobs and len(self.jobs) < self.settings.max_running_tasks:
                        self.jobs[tid] = asyncio.create_task(
                            self._supervise_task(tid), name="native-task-" + tid
                        )
                after = tids[-1] if tids else None
                await asyncio.sleep(1)
        finally:
            await self.close()

    async def close(self):
        self.closed = True
        for job in self.jobs.values():
            job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        self.jobs.clear()
        # Loss/shutdown is not a durable Task stop and must never kill a new controller's resources.


async def serve_native(settings: NativeRunnerSettings):
    config = json.loads(settings.runner_resource_config_file.read_text())
    resources = RunnerResources(config)
    if resources.platform_namespace and (
        settings.blackboard_url.rstrip("/") != config["runner_blackboard_url"]
        or any(
            getattr(settings, key).rstrip("/") != config[key]
            for key in ("runner_blackboard_url", "runner_model_proxy_url")
        )
    ):
        raise ValueError("Runtime control addresses must match the published in-cluster services")
    board_resources = None
    if settings.board_resource_config_file:
        try:
            board_config = json.loads(settings.board_resource_config_file.read_text())
            shared_fields = {
                "source_id",
                "cluster_uid",
                "context",
                "kubeconfig",
                "system_namespace",
                "kubernetes_platform_namespace",
                "storage_class",
                "sandbox_server_url",
                "runner_image",
                "runner_profile",
                "sandbox_host_image",
                "runtime_artifact_sha256",
                "ca_file",
            }
            if any(board_config.get(key) != config.get(key) for key in shared_fields):
                raise ValueError("Blackboard deployment must use the same Runner and OpenSandbox")
            board_resources = RunnerResources(board_config)
        except (OSError, TypeError, ValueError, KeyError):
            logging.getLogger(__name__).error(
                "The published Blackboard resource view is unavailable; its Tasks remain blocked"
            )
    tls = ssl.create_default_context()
    if config.get("ca_file"):
        tls.load_verify_locations(cafile=config["ca_file"])
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    try:
        async with (
            RuntimeLock(settings, fenced=True) as instance,
            httpx.AsyncClient(
                verify=tls,
                trust_env=False,
                follow_redirects=False,
                timeout=30,
                transport=httpx.AsyncHTTPTransport(verify=tls, retries=0),
            ) as control_http,
            BlackboardClient(
                settings.blackboard_url,
                settings.service_token.get_secret_value(),
                http_client=control_http,
            ) as service,
            model_proxy_server(settings, service) as http,
        ):
            if http is None:
                raise RuntimeError("The common trusted Pi sender is not configured")
            controller = NativeRunnerController(
                settings,
                service,
                resources,
                instance,
                board_resources=board_resources,
                objects=object_store(settings) if settings.minio_root_password else None,
            )
            await service.recover_conversations()
            reviews = CtfCoordinator(service, settings)
            chat = ChatWorker(settings, service)
            progress = ProgressWorker(settings, service)
            workers = [
                asyncio.create_task(controller.run(), name="sandbox-controller"),
                asyncio.create_task(reviews.serve_readonly_reviews(), name="ctf-review"),
                asyncio.create_task(chat.run(), name="board-review"),
                asyncio.create_task(progress.run(), name="progress-curation"),
            ]
            shutdown = asyncio.create_task(stopped.wait())
            lost = asyncio.create_task(instance.lost.wait())
            managed: list[asyncio.Task[Any]] = [*workers, shutdown, lost]
            try:
                done, _ = await asyncio.wait([*managed, http], return_when=asyncio.FIRST_COMPLETED)
                if lost in done:
                    raise RuntimeError("Native Runtime instance lock was lost")
                if http in done:
                    await http
                    raise RuntimeError("The common trusted Pi listener stopped unexpectedly")
                for worker in workers:
                    if worker in done:
                        await worker
                        raise RuntimeError(f"{worker.get_name()} stopped unexpectedly")
            finally:
                await controller.close()
                await progress.stop()
                await chat.stop()
                for task in managed:
                    task.cancel()
                await asyncio.gather(*managed, return_exceptions=True)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
