"""Startup control loss preserves the confirmed environment and rejoins its loop."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.scheduler.loop import SchedulerLoop
from test_supervisor import Manager, Service, supervisor, task


@pytest.mark.parametrize("stage", ["list", "state", "finish", "finish_committed"])
async def test_startup_recovery_retries_control_loss_without_finishing_new_instance_agents(
    stage, monkeypatch
):
    class Recovering(Service):
        failed = False

        def failure(self, phase):
            if phase == stage and not self.failed:
                self.failed = True
                raise httpx.ReadError("synthetic private startup control loss")

        async def list_tasks(self):
            self.failure("list")
            return await super().list_tasks()

        async def state(self, tid):
            if tid == "second":
                self.failure("state")
            return await super().state(tid)

        async def finish_agent(self, tid, aid, receipt, reason):
            if tid == "second":
                if stage == "finish_committed" and not self.failed:
                    result = await super().finish_agent(tid, aid, receipt, reason)
                    self.failure("finish_committed")
                    return result
                self.failure("finish")
            return await super().finish_agent(tid, aid, receipt, reason)

    service = Recovering([task("first", "running"), task("second", "running")])
    for tid in ("first", "second"):
        service.states[tid]["agents"]["old"] = {"status": "running", "task_type": "explore"}
    manager = Manager()
    manager.handles = {tid: SimpleNamespace(task_id=tid) for tid in ("first", "second")}
    instance = supervisor(service, manager)

    async def start(state, handle):
        tid = state["task"]["id"]
        instance.loops[tid] = Mock(spec=SchedulerLoop)
        service.states[tid]["agents"]["new"] = {"status": "running", "task_type": "explore"}

    async def tick():
        assert set(instance.loops) == {"first", "second"}
        instance.stopping.set()

    original_wait = asyncio.wait_for

    async def no_backoff(awaitable, timeout):
        if timeout != 2 or not instance.stopping.is_set():
            awaitable.close()
            raise TimeoutError
        return await original_wait(awaitable, timeout=timeout)

    monkeypatch.setattr(instance, "_start", start)
    monkeypatch.setattr(instance, "tick", tick)
    monkeypatch.setattr("bbx_runtime.scheduler.supervisor.asyncio.wait_for", no_backoff)
    await instance.run()
    assert service.failed
    assert not any(
        call[:3] in {("finish", "first", "new"), ("finish", "second", "new")}
        for call in service.calls
    )
    assert len([call for call in service.calls if call[:3] == ("finish", "first", "old")]) == 1


async def test_startup_permanent_control_loss_stops_promptly_without_business_failure(monkeypatch):
    service, manager = Service([task("ordinary", "running")]), Manager()
    service.state = AsyncMock(side_effect=RemoteError(503, "synthetic unavailable board"))
    instance = supervisor(service, manager)
    original_wait = asyncio.wait_for

    async def request_stop(awaitable, timeout):
        instance.stopping.set()
        return await original_wait(awaitable, timeout=timeout)

    monkeypatch.setattr("bbx_runtime.scheduler.supervisor.asyncio.wait_for", request_stop)
    await instance.run()
    assert not instance.loops
    assert not any(call[0] == "transition" for call in service.calls)


async def test_startup_unknown_or_authentication_failure_is_not_retried():
    service = Service([task("ordinary", "running")])
    original = RemoteError(403, "synthetic forbidden recovery")
    service.state = AsyncMock(side_effect=original)
    instance = supervisor(service, Manager())
    with pytest.raises(RemoteError) as caught:
        await instance.run()
    assert caught.value is original
    service.state.assert_awaited_once()


class StartupService(Service):
    def __init__(self, *, stage="state", committed=False, error=None, permanent=False):
        super().__init__([task("starting", "provisioning")])
        self.stage, self.committed, self.permanent = stage, committed, permanent
        self.error = error or httpx.RemoteProtocolError("synthetic private startup failure")
        self.failed = False
        self.start_posts = 0

    def failure(self, stage):
        if stage == self.stage and (not self.failed or self.permanent):
            self.failed = True
            raise self.error

    async def get_profile(self, name, version):
        self.failure("profile")
        return await super().get_profile(name, version)

    async def state(self, tid):
        self.failure("state")
        return await super().state(tid)

    async def transition(self, tid, status, reason=None):
        if status == "running":
            self.start_posts += 1
            if self.stage == "transition" and not self.failed:
                if self.committed:
                    await super().transition(tid, status, reason)
                self.failure("transition")
        return await super().transition(tid, status, reason)


def owner(service, manager, monkeypatch):
    instance = supervisor(service, manager)
    clock = [0.0]
    monkeypatch.setattr("bbx_runtime.scheduler.supervisor.monotonic", lambda: clock[0])

    async def launch(state, handle):
        assert state["task"]["status"] in {"running", "closing"}
        assert handle is manager.handles[state["task"]["id"]]
        # Record the established loop without dispatching any model or tool.
        instance.loops[state["task"]["id"]] = Mock(spec=SchedulerLoop)

    started = AsyncMock(side_effect=launch)
    monkeypatch.setattr(instance, "_start", started)
    return instance, started, clock


@pytest.mark.parametrize("stage", ["profile", "state"])
@pytest.mark.parametrize(
    "error", [httpx.RemoteProtocolError("private"), RemoteError(503, "private")]
)
async def test_control_reads_defer_start_without_business_failure(
    stage, error, monkeypatch, caplog
):
    service = StartupService(stage=stage, error=error)
    manager = Manager()
    instance, started, clock = owner(service, manager, monkeypatch)
    await instance.tick()
    assert service.states["starting"]["task"]["status"] == "provisioning"
    assert not any(call[:3] == ("transition", "starting", "failed") for call in service.calls)
    started.assert_not_awaited()
    clock[0] = instance.start_retries["starting"][1]
    await instance.tick()
    assert service.states["starting"]["task"]["status"] == "running"
    started.assert_awaited_once()
    assert manager.calls.count(("provision", "starting")) == 1
    assert ("destroy", "starting") not in manager.calls
    assert not instance.pending_starts and not instance.start_retries
    assert "private" not in caplog.text


@pytest.mark.parametrize("committed", [False, True])
async def test_running_transition_loss_rejoins_without_recreating_environment(
    committed, monkeypatch
):
    service = StartupService(stage="transition", committed=committed)
    manager = Manager()
    instance, started, clock = owner(service, manager, monkeypatch)
    await instance.tick()
    handle = manager.handles["starting"]
    assert instance.pending_starts["starting"] is handle
    assert service.states["starting"]["task"]["status"] == (
        "running" if committed else "provisioning"
    )
    clock[0] = instance.start_retries["starting"][1]
    await instance.tick()
    assert service.states["starting"]["task"]["status"] == "running"
    assert service.start_posts == (1 if committed else 2)
    assert manager.calls.count(("provision", "starting")) == 1
    assert ("destroy", "starting") not in manager.calls
    assert started.call_args.args[1] is handle
    assert "starting" in instance.loops
    assert not instance.pending_starts


async def test_late_running_commit_coalesces_guarded_transition_conflict(monkeypatch):
    class LateCommit(StartupService):
        def __init__(self):
            super().__init__(stage="transition")
            self.commit_during_read = False

        async def state(self, tid):
            snapshot = await super().state(tid)
            if self.commit_during_read:
                self.commit_during_read = False
                await Service.transition(self, tid, "running")
            return snapshot

        async def transition(self, tid, status, reason=None):
            if status == "running" and self.states[tid]["task"]["status"] == "running":
                self.start_posts += 1
                raise RemoteError(422, "synthetic late commit", "invalid_transition")
            return await super().transition(tid, status, reason)

    service, manager = LateCommit(), Manager()
    instance, started, clock = owner(service, manager, monkeypatch)
    await instance.tick()
    service.commit_during_read = True
    clock[0] = instance.start_retries["starting"][1]
    await instance.tick()
    assert service.states["starting"]["task"]["status"] == "running"
    assert service.calls.count(("transition", "starting", "running", None)) == 1
    assert manager.calls.count(("provision", "starting")) == 1
    started.assert_awaited_once()


async def test_persistent_start_read_outage_keeps_handle_and_backs_off_without_posts(monkeypatch):
    service, manager = StartupService(permanent=True), Manager()
    instance, started, clock = owner(service, manager, monkeypatch)
    await instance.tick()
    retained = instance.pending_starts["starting"]
    for _ in range(5):
        await instance.tick()
    assert instance.start_retries["starting"][0] == 1
    for _ in range(8):
        clock[0] = instance.start_retries["starting"][1]
        await instance.tick()
    assert service.states["starting"]["task"]["status"] == "provisioning"
    assert instance.pending_starts["starting"] is retained
    assert instance.start_retries["starting"][0] == 5
    assert instance.start_retries["starting"][1] - clock[0] == 30
    assert manager.calls.count(("provision", "starting")) == 1
    assert not any(call[0] == "transition" for call in service.calls)
    started.assert_not_awaited()


async def test_retained_start_owns_capacity_but_can_retry_its_own_slot(monkeypatch):
    service, manager = StartupService(), Manager()
    later = task("later", "provisioning", 1)
    service.tasks.append(later)
    service.states["later"] = {"task": deepcopy(later), "agents": {}}
    instance, started, clock = owner(service, manager, monkeypatch)
    await instance.tick()
    assert ("provision", "later") not in manager.calls
    clock[0] = instance.start_retries["starting"][1]
    await instance.tick()
    assert service.states["starting"]["task"]["status"] == "running"
    assert service.states["later"]["task"]["status"] == "provisioning"
    assert ("provision", "later") not in manager.calls
    started.assert_awaited_once()


@pytest.mark.parametrize(
    "error",
    [
        ValueError("synthetic bad setting"),
        httpx.LocalProtocolError("synthetic invalid header"),
        httpx.UnsupportedProtocol("synthetic invalid URL"),
        RemoteError(403, "synthetic forbidden"),
        RemoteError(422, "synthetic bad request"),
    ],
)
async def test_deterministic_start_errors_keep_existing_failure_semantics(error, monkeypatch):
    service, manager = StartupService(error=error), Manager()
    instance, started, _ = owner(service, manager, monkeypatch)
    await instance.tick()
    assert service.states["starting"]["task"]["status"] == "failed"
    assert not instance.start_retries
    started.assert_not_awaited()


async def test_purge_response_loss_cannot_keep_destroyed_start_handle_occupying_capacity(
    monkeypatch,
):
    service, manager = StartupService(), Manager()
    instance, _, _ = owner(service, manager, monkeypatch)
    await instance.tick()
    assert instance.pending_starts
    await Service.transition(service, "starting", "stopped")
    monkeypatch.setattr(
        service,
        "purge_task",
        AsyncMock(side_effect=httpx.RemoteProtocolError("synthetic purge response")),
        raising=False,
    )
    with pytest.raises(httpx.RemoteProtocolError):
        await instance._purge_deleted("starting")
    assert ("destroy", "starting") in manager.calls
    assert not instance.pending_starts and not instance.start_retries
    assert "starting" not in manager.handles
