"""Opt-in trusted native bootstrap; no Python Agent loop or legacy Docker execution."""

import asyncio
import json
import logging
import signal
import ssl
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import uvicorn
from bbx_contracts.runner import runner_json_text, runner_payload_digest
from bbx_contracts.runner_bootstrap import retirement_manifest_digest

from bbx_runtime.clients import BlackboardClient
from bbx_runtime.clients.blackboard import RemoteError, is_control_transport_error
from bbx_runtime.runner_proxy import RunnerModelProxy
from bbx_runtime.runner_resources import ResourceUnknown, RunnerResources
from bbx_runtime.scheduler.lock import RuntimeLock
from bbx_runtime.settings import NativeRunnerSettings


class BootstrapStopped(RuntimeError):
    pass


class NativeRunnerController:
    def __init__(self, settings, service, resources, instance):
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
        self.retirement_authority = resources.config.get("retirement_single_apiserver", {})
        self.config = {
            **{
                key: value
                for key, value in resources.config.items()
                if key != "retirement_single_apiserver"
            },
            "runner_blackboard_url": settings.runner_blackboard_url.rstrip("/"),
            "runner_model_proxy_url": settings.runner_model_proxy_url.rstrip("/"),
        }
        self.jobs = {}
        self.closed = False

    @staticmethod
    def path(tid):
        return f"/tasks/{tid}/ctf/runtime/runner-bootstrap"

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
        context = await self.guarded(
            tid, operation, self.resources.sandbox_host_context(operation, tenant_key)
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
        await self.guarded(
            tid, operation, self.resources.publish_sandbox_host(operation, configuration)
        )
        await self.guarded(
            tid, operation, self.resources.wait_sandbox_host_ready(operation, configuration)
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
                        "resource_config": self.config,
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
            try:
                await asyncio.wait_for(wake.wait(), timeout=3)
            except TimeoutError:
                pass
            finally:
                wake.clear()

        try:
            await self._drive(tid, operation, tenant_key, idle)
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    @staticmethod
    def _credential_scope(operation):
        data = operation["data"]
        return (
            data.get("binding_id"),
            data.get("pause_state"),
            tuple(sorted(data.get("worker_binding_ids", ()))),
        )

    @staticmethod
    def _material_changed(scope, published, control_only):
        return (
            published is None
            or scope[:2] != published[:2]
            or (not control_only and scope[2] != published[2])
        )

    async def _recover_original(self, tid, operation, tenant_key):
        incident = operation["data"]["recovery"]
        identity = {"incident_id": incident["incident_id"]}
        blocker = "journal_writer_unconfirmed"
        try:
            node = await self.guarded(tid, operation, self.resources.recovery_volumes(operation))
            if not incident.get("exit_proof"):
                blocker = "original_exit_unconfirmed"
                original = operation["observation"]
                proof = await self.guarded(
                    tid, operation, self.resources.stop(original, tenant_key)
                )
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
            await self.guarded(tid, operation, self.resources.release_recovery(operation))
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
                self.resources.verify_recovery_journal(
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

    async def _drive(self, tid, operation, tenant_key, idle):
        last_rotation = 0.0
        published_scope = None
        control_only = operation["state"] == "held"
        while not self.closed:
            self.current()
            try:
                state = await self.view(tid)
                if state["state"] == "stopped":
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
                            self.resources.retire(
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
                if state["state"] == "exited":
                    await self.action(tid, operation, "cleanup")
                    continue
                if self.stopping(operation):
                    operation.update(await self.action(tid, operation, "stop"))
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
                            self.resources.release_recovery(
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
                                    "observation_digest": runner_payload_digest(
                                        runner_json_text(operation["observation"])
                                    ),
                                },
                            )
                        )
                        continue
                    recovered = await self.guarded(
                        tid, operation, self.resources.recover(operation, tenant_key), cleanup=True
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
                            self.resources.stop(observation, tenant_key),
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
                            tid, operation, self.resources.observe(operation["observation"])
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
                        self.resources.ensure_task_resources(
                            operation, tenant_key, admit_sandbox_create=admit, checkpoint=checkpoint
                        ),
                    )
                    operation.update(
                        await self.action(tid, operation, "observation", observation=observed)
                    )
                else:
                    observed = await self.guarded(
                        tid, operation, self.resources.observe(operation["observation"])
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
                if self.config.get("runner_profile") == "node-sandbox-host-v1" and not operation[
                    "data"
                ].get("sandbox_host_ready"):
                    await self._prepare_sandbox_host(tid, operation, tenant_key)
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
                        self.resources.publish_mount(
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
        ):
            controller = NativeRunnerController(settings, service, resources, instance)
            proxy = RunnerModelProxy(settings, service, http=control_http)
            server = uvicorn.Server(
                uvicorn.Config(
                    proxy.app,
                    host=settings.runner_proxy_host,
                    port=settings.runner_proxy_port,
                    access_log=False,
                )
            )
            work = asyncio.create_task(controller.run())
            http = asyncio.create_task(server.serve())
            shutdown = asyncio.create_task(stopped.wait())
            lost = asyncio.create_task(instance.lost.wait())
            try:
                done, _ = await asyncio.wait(
                    [work, http, shutdown, lost], return_when=asyncio.FIRST_COMPLETED
                )
                if lost in done:
                    raise RuntimeError("Native Runtime instance lock was lost")
                if work in done:
                    await work
                if http in done:
                    await http
                    raise RuntimeError("Native Runner proxy stopped unexpectedly")
            finally:
                await controller.close()
                work.cancel()
                server.should_exit = True
                shutdown.cancel()
                lost.cancel()
                await asyncio.gather(work, shutdown, lost, return_exceptions=True)
                try:
                    await asyncio.wait_for(http, 5)
                except TimeoutError:
                    http.cancel()
                    await asyncio.gather(http, return_exceptions=True)
                await proxy.close()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
