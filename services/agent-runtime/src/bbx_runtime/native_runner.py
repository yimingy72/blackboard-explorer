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
        last_rotation = 0.0
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
                if not operation["data"].get("credentials_issued") or now - last_rotation >= 20:
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
                    await self.guarded(
                        tid,
                        operation,
                        self.resources.publish_mount(
                            operation["observation"],
                            material["bootstrap"],
                            material["credentials"],
                            control_only=material.get("control_only", False),
                        ),
                    )
                    last_rotation = asyncio.get_running_loop().time()
                    control_only = material.get("control_only", False)
                if control_only:
                    await asyncio.sleep(3)
                    continue
                operation.update(await self.action(tid, operation, "activate"))
                await asyncio.sleep(3)
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
                try:
                    await self.action(
                        tid, operation, "error", error_code="native_control_unavailable"
                    )
                except RemoteError:
                    raise
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
