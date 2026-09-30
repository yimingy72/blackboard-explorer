"""Supervisor keeps task capacity and archive state durable across restarts."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from bbx_contracts.profile import load_profile
from bbx_runtime.clients import BlackboardClient, RemoteError
from bbx_runtime.execenv import ArchiveResult, ExecEnvHandle, ExecEnvManager
from bbx_runtime.execenv.archive import ArchiveCapacityError
from bbx_runtime.scheduler.loop import SchedulerLoop
from bbx_runtime.scheduler.supervisor import TaskSupervisor
from bbx_runtime.settings import Settings

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


def task(tid: str, status: str, minutes: int = 0) -> dict:
    return {
        "id": tid,
        "status": status,
        "created_at": (NOW + timedelta(minutes=minutes)).isoformat(),
        "agent_profile": "default",
        "agent_profile_version": 1,
    }


class Service:
    def __init__(self, tasks: list[dict]) -> None:
        self.tasks = tasks
        self.states = {
            row["id"]: {
                "task": {**row, "params": {}, "workspace_uri": None},
                "agents": {},
                "intents": {},
            }
            for row in tasks
        }
        self.calls: list[tuple] = []
        self.record_error = False

    async def list_tasks(self) -> list[dict]:
        return deepcopy(self.tasks)

    async def state(self, tid: str) -> dict:
        return deepcopy(self.states[tid])

    async def archive_data(self, tid: str) -> dict:
        self.calls.append(("archive_data", tid))
        return {"task_id": tid, "run_number": self.states[tid]["task"].get("run_number", 1)}

    async def get_profile(self, name: str, version: int) -> dict:
        self.calls.append(("profile", name, version))
        profile, _ = load_profile(ROOT / "profiles/default")
        return {"profile": profile.model_dump(mode="json")}

    async def transition(self, tid: str, status: str, reason: str | None = None) -> list:
        self.calls.append(("transition", tid, status, reason))
        self.states[tid]["task"]["status"] = status
        next(row for row in self.tasks if row["id"] == tid)["status"] = status
        return []

    async def finish_agent(self, tid: str, aid: str, receipt: dict, reason: str) -> list:
        self.calls.append(("finish", tid, aid, reason))
        self.states[tid]["agents"][aid]["status"] = "finished"
        self.states[tid]["agents"][aid]["end_reason"] = reason
        return []

    async def conclude(self, tid: str, aid: str, reason: str) -> list:
        self.calls.append(("conclude", tid, aid, reason))
        self.states[tid]["agents"][aid]["status"] = "concluding"
        self.states[tid]["agents"][aid]["conclude_requested_at"] = NOW.isoformat()
        return []

    async def record_archive(self, tid: str, uri: str, size: int, fallback: str) -> list:
        self.calls.append(("record", tid, uri, size, fallback))
        if self.record_error:
            raise RuntimeError("record failed")
        self.states[tid]["task"]["workspace_uri"] = uri
        return []

    async def record_cleanup(self, tid: str) -> list:
        self.states[tid]["task"]["cleanup_ready"] = True
        return []


class Manager:
    def __init__(self) -> None:
        self.handles: dict[str, SimpleNamespace] = {}
        self.calls: list[tuple] = []
        self.archive_gate: asyncio.Event | None = None
        self.archive_errors: list[Exception] = []
        self.ensure_initial_inputs = AsyncMock()

    async def find(self, tid: str) -> SimpleNamespace | None:
        self.calls.append(("find", tid))
        return self.handles.get(tid)

    async def wait_healthy(self, handle: SimpleNamespace) -> None:
        self.calls.append(("healthy", handle.task_id))

    async def provision(self, tid: str, _profile) -> SimpleNamespace:
        self.calls.append(("provision", tid))
        handle = SimpleNamespace(task_id=tid)
        self.handles[tid] = handle
        return handle

    async def archive_task(self, tid: str, data: dict) -> ArchiveResult:
        self.calls.append(("archive", tid))
        if self.archive_gate is not None:
            await self.archive_gate.wait()
        if self.archive_errors:
            raise self.archive_errors.pop(0)
        run_number = data["run_number"]
        uri = (
            f"workspace/{tid}.tar.zst"
            if run_number == 1
            else f"workspace/{tid}/run-{run_number}.tar.zst"
        )
        return ArchiveResult(uri, 123, "none")

    async def destroy(self, tid: str) -> None:
        self.calls.append(("destroy", tid))
        self.handles.pop(tid, None)


def supervisor(service: Service, manager: Manager, *, limit: int = 1) -> TaskSupervisor:
    return TaskSupervisor(
        Settings.model_construct(max_running_tasks=limit),
        cast(BlackboardClient, service),
        cast(ExecEnvManager, manager),
        Mock(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_deletion_failure_retries_without_restarting_archive_cleanup() -> None:
    class DeletionService(Service):
        def __init__(self, tasks: list[dict]) -> None:
            super().__init__(tasks)
            self.purges = 0

        async def pending_deletions(self) -> list[str]:
            return ["old"]

        async def purge_task(self, _tid: str) -> dict[str, bool]:
            self.purges += 1
            if self.purges == 1:
                raise RuntimeError("storage offline")
            return {"purged": True}

    deleting = task("old", "finished")
    deleting["deleting"] = True
    service = DeletionService([deleting])
    manager = Manager()
    owner = supervisor(service, manager)
    await owner.tick()
    assert owner.cleanups == {}
    assert ("archive", "old") not in manager.calls
    await owner.tick()
    assert service.purges == 2
    assert ("archive", "old") not in manager.calls


@pytest.mark.asyncio
async def test_fifo_queue_respects_max_running_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Service([task("new", "provisioning", 2), task("old", "provisioning", 0)])
    manager = Manager()
    owner = supervisor(service, manager)
    started = AsyncMock()
    monkeypatch.setattr(owner, "_start", started)
    await owner.tick()
    assert [call for call in manager.calls if call[0] == "provision"] == [("provision", "old")]
    assert service.states["old"]["task"]["status"] == "running"
    assert service.states["new"]["task"]["status"] == "provisioning"
    started.assert_awaited_once()
    await owner.tick()
    assert [call for call in manager.calls if call[0] == "provision"] == [("provision", "old")]


@pytest.mark.parametrize("status", ["provisioning", "running"])
async def test_initial_inputs_are_ready_before_running_or_agent_start(monkeypatch, status):
    service = Service([task("with-inputs", status)])
    service.states["with-inputs"]["task"]["initial_attachments"] = [{"id": "source"}]
    manager = Manager()
    handle = SimpleNamespace(task_id="with-inputs")
    manager.handles["with-inputs"] = handle
    owner = supervisor(service, manager)
    order = []

    async def ready(*_args):
        order.append("inputs")
        expected = status if len(order) == 1 else "running"
        assert service.states["with-inputs"]["task"]["status"] == expected

    async def launch():
        order.append("agents")
        assert order[0] == "inputs"
        assert service.states["with-inputs"]["task"]["status"] == "running"

    manager.ensure_initial_inputs = AsyncMock(side_effect=ready)
    monkeypatch.setattr(
        "bbx_runtime.scheduler.supervisor.SchedulerLoop.run", lambda _self: launch()
    )
    if status == "provisioning":
        # _start performs a second, idempotent readiness check after transition.
        await owner.tick()
    else:
        await owner.recover()
    await owner.loop_tasks["with-inputs"]
    assert order[-1] == "agents" and order[0] == "inputs"
    await owner.stop()


async def test_failed_initial_materialization_cannot_start_agents_or_change_old_archive(
    monkeypatch,
):
    service = Service([task("with-inputs", "provisioning")])
    service.states["with-inputs"]["task"].update(
        initial_attachments=[{"id": "source"}], workspace_uri="old-archive"
    )
    manager = Manager()
    manager.ensure_initial_inputs = AsyncMock(side_effect=ValueError("input hash mismatch"))
    owner = supervisor(service, manager)
    started = AsyncMock()
    monkeypatch.setattr(owner, "_start", started)
    await owner.tick()
    assert service.states["with-inputs"]["task"]["status"] == "failed"
    assert service.states["with-inputs"]["task"]["workspace_uri"] == "old-archive"
    assert not any(call[0] == "transition" and call[2] == "running" for call in service.calls)
    started.assert_not_awaited()


async def test_shutdown_during_input_restore_does_not_start_scheduler():
    service = Service([task("with-inputs", "running")])
    state = service.states["with-inputs"]
    state["task"]["initial_attachments"] = [{"id": "source"}]
    manager = Manager()
    entered, release = asyncio.Event(), asyncio.Event()

    async def restoring(*_args):
        entered.set()
        await release.wait()

    manager.ensure_initial_inputs.side_effect = restoring
    owner = supervisor(service, manager)
    starting = asyncio.create_task(
        owner._start(state, cast(ExecEnvHandle, SimpleNamespace(task_id="with-inputs")))
    )
    await asyncio.wait_for(entered.wait(), 1)
    owner.stopping.set()
    release.set()
    await starting
    assert owner.loops == {} and owner.loop_tasks == {}


@pytest.mark.asyncio
async def test_recovery_keeps_unprovisioned_queue_and_fails_missing_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("queued", "provisioning"), task("running", "running")])
    service.states["running"]["agents"]["agent-1"] = {"status": "running"}
    service.states["running"]["intents"]["I1"] = {"attempts": 2, "holder": "agent-1"}
    manager = Manager()
    owner = supervisor(service, manager)
    monkeypatch.setattr(owner, "_start", AsyncMock())
    await owner.recover()
    assert service.states["queued"]["task"]["status"] == "provisioning"
    assert service.states["running"]["task"]["status"] == "failed"
    assert ("finish", "running", "agent-1", "runtime_restart") in service.calls
    assert service.states["running"]["intents"]["I1"]["attempts"] == 2


@pytest.mark.asyncio
async def test_recovery_rejoins_running_and_existing_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("queued", "provisioning"), task("running", "running")])
    manager = Manager()
    manager.handles = {
        "queued": SimpleNamespace(task_id="queued"),
        "running": SimpleNamespace(task_id="running"),
    }
    owner = supervisor(service, manager)
    started = AsyncMock()
    monkeypatch.setattr(owner, "_start", started)
    await owner.recover()
    assert ("healthy", "queued") in manager.calls
    assert ("healthy", "running") in manager.calls
    started.assert_awaited_once()
    assert started.call_args.args[0]["task"]["id"] == "running"
    assert service.states["queued"]["task"]["status"] == "provisioning"


@pytest.mark.asyncio
async def test_archive_record_precedes_destroy_and_failure_retains_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("done", "finished")])
    manager = Manager()
    manager.handles["done"] = SimpleNamespace(task_id="done")
    owner = supervisor(service, manager)
    original_destroy = manager.destroy

    async def destroy_only_after_record(tid: str) -> None:
        assert service.states[tid]["task"]["workspace_uri"] == f"workspace/{tid}.tar.zst"
        await original_destroy(tid)

    monkeypatch.setattr(manager, "destroy", destroy_only_after_record)
    service.record_error = True
    with pytest.raises(RuntimeError, match="record failed"):
        await owner._cleanup("done")
    assert "done" in manager.handles
    assert ("destroy", "done") not in manager.calls
    service.record_error = False
    await owner._cleanup("done")
    assert service.states["done"]["task"]["workspace_uri"] == "workspace/done.tar.zst"
    assert [call[0] for call in manager.calls if call[0] in {"archive", "destroy"}] == [
        "archive",
        "archive",
        "destroy",
    ]
    assert [call[0] for call in service.calls if call[0] == "record"] == ["record", "record"]


@pytest.mark.asyncio
async def test_missing_source_container_still_archives_from_persisted_data() -> None:
    service = Service([task("gone", "failed")])
    manager = Manager()
    owner = supervisor(service, manager)
    await owner._cleanup("gone")
    assert ("archive", "gone") in manager.calls
    assert ("destroy", "gone") in manager.calls
    assert service.states["gone"]["task"]["cleanup_ready"] is True


@pytest.mark.asyncio
async def test_archive_upload_failure_retains_container_and_does_not_record_cleanup() -> None:
    service = Service([task("kept", "finished")])
    manager = Manager()
    manager.handles["kept"] = SimpleNamespace(task_id="kept")
    manager.archive_errors.append(OSError("object store upload failed"))
    owner = supervisor(service, manager)
    with pytest.raises(OSError):
        await owner._cleanup("kept")
    assert "kept" in manager.handles
    assert ("destroy", "kept") not in manager.calls
    assert not service.states["kept"]["task"].get("cleanup_ready", False)


@pytest.mark.asyncio
async def test_cleanup_occupies_slot_until_archive_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("done", "finished"), task("waiting", "provisioning", 1)])
    manager = Manager()
    manager.handles["done"] = SimpleNamespace(task_id="done")
    manager.archive_gate = asyncio.Event()
    owner = supervisor(service, manager)
    monkeypatch.setattr(owner, "_start", AsyncMock())
    await owner.tick()
    assert "done" in owner.cleanups
    assert ("provision", "waiting") not in manager.calls
    manager.archive_gate.set()
    await asyncio.wait_for(owner.cleanups["done"], 2)
    await owner.tick()
    assert ("provision", "waiting") in manager.calls


@pytest.mark.asyncio
async def test_archive_413_retains_workspace_without_retries_or_occupying_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("done", "finished"), task("waiting", "provisioning", 1)])
    manager = Manager()
    manager.handles["done"] = SimpleNamespace(task_id="done")
    manager.archive_errors.append(RemoteError(413, "archive too large"))
    owner = supervisor(service, manager)
    monkeypatch.setattr(owner, "_start", AsyncMock())

    await owner.tick()
    assert ("provision", "waiting") not in manager.calls
    await asyncio.wait_for(owner.cleanups["done"], 2)
    await owner.tick()
    await owner.tick()

    assert owner.archive_blocked == {"done"}
    assert "done" in manager.handles
    assert "done" not in owner.cleaned
    assert service.states["done"]["task"]["workspace_uri"] is None
    assert not service.states["done"]["task"].get("cleanup_ready", False)
    assert not any(call[0] == "record" for call in service.calls)
    assert ("destroy", "done") not in manager.calls
    assert [call for call in manager.calls if call[0] == "archive"] == [("archive", "done")]
    assert ("provision", "waiting") in manager.calls


@pytest.mark.asyncio
async def test_local_archive_capacity_error_retains_container() -> None:
    service = Service([task("oversized", "finished")])
    manager = Manager()
    manager.handles["oversized"] = SimpleNamespace(task_id="oversized")
    manager.archive_errors.append(ArchiveCapacityError("restore limit"))
    owner = supervisor(service, manager)
    await owner._cleanup("oversized")
    assert owner.archive_blocked == {"oversized"}
    assert "oversized" in manager.handles
    assert ("destroy", "oversized") not in manager.calls
    assert not service.states["oversized"]["task"].get("cleanup_ready", False)


@pytest.mark.asyncio
async def test_archive_transient_error_still_retries() -> None:
    service = Service([task("retry", "finished")])
    manager = Manager()
    manager.handles["retry"] = SimpleNamespace(task_id="retry")
    manager.archive_errors.append(RemoteError(503, "temporary"))
    owner = supervisor(service, manager)

    await owner.tick()
    await asyncio.gather(*owner.cleanups.values(), return_exceptions=True)
    assert "retry" not in owner.archive_blocked
    assert "retry" in manager.handles
    await owner.tick()
    await asyncio.wait_for(owner.cleanups["retry"], 2)
    assert [call for call in manager.calls if call == ("archive", "retry")] == [
        ("archive", "retry"),
        ("archive", "retry"),
    ]
    assert ("destroy", "retry") in manager.calls
    assert service.states["retry"]["task"]["cleanup_ready"] is True


@pytest.mark.asyncio
async def test_record_archive_413_does_not_block_archive_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("retry", "finished")])
    manager = Manager()
    manager.handles["retry"] = SimpleNamespace(task_id="retry")
    owner = supervisor(service, manager)
    record_archive = service.record_archive
    monkeypatch.setattr(
        service, "record_archive", AsyncMock(side_effect=RemoteError(413, "storage rejected"))
    )

    with pytest.raises(RemoteError):
        await owner._cleanup("retry")
    assert "retry" not in owner.archive_blocked
    assert "retry" in manager.handles

    monkeypatch.setattr(service, "record_archive", record_archive)
    await owner._cleanup("retry")
    assert [call for call in manager.calls if call == ("archive", "retry")] == [
        ("archive", "retry"),
        ("archive", "retry"),
    ]


@pytest.mark.asyncio
async def test_deleted_archive_block_is_purged(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Service([task("blocked", "finished")])
    manager = Manager()
    manager.handles["blocked"] = SimpleNamespace(task_id="blocked")
    manager.archive_errors.append(RemoteError(413, "too large"))
    owner = supervisor(service, manager)
    await owner.tick()
    await asyncio.wait_for(owner.cleanups["blocked"], 2)
    assert "blocked" in owner.archive_blocked

    service.tasks[0]["deleting"] = True
    pending_deletions = AsyncMock(return_value=["blocked"])
    purge_task = AsyncMock(return_value={"purged": True})
    monkeypatch.setattr(service, "pending_deletions", pending_deletions, raising=False)
    monkeypatch.setattr(service, "purge_task", purge_task, raising=False)
    await owner.tick()
    assert ("destroy", "blocked") in manager.calls
    purge_task.assert_awaited_once_with("blocked")
    assert "blocked" not in owner.archive_blocked


@pytest.mark.asyncio
async def test_new_provisioning_clears_archive_block() -> None:
    service = Service([task("reused", "provisioning")])
    owner = supervisor(service, Manager(), limit=0)
    owner.archive_blocked.add("reused")
    await owner.tick()
    assert "reused" not in owner.archive_blocked


@pytest.mark.asyncio
async def test_stop_does_not_destroy_running_task() -> None:
    service = Service([task("active", "running")])
    manager = Manager()
    manager.handles["active"] = SimpleNamespace(task_id="active")
    owner = supervisor(service, manager)
    loop = SimpleNamespace(stop=AsyncMock(), request_stop=Mock())
    owner.loops["active"] = cast(SchedulerLoop, loop)
    owner.loop_tasks["active"] = asyncio.create_task(asyncio.sleep(0))
    await owner.stop()
    loop.request_stop.assert_called_once_with("runtime_restart")
    loop.stop.assert_awaited_once_with(reason="runtime_restart")
    assert "active" in manager.handles
    assert not any(call[0] in {"archive", "destroy"} for call in manager.calls)


@pytest.mark.asyncio
async def test_shutdown_does_not_mark_intentionally_stopped_scheduler_as_failed() -> None:
    service = Service([task("active", "running")])
    owner = supervisor(service, Manager())
    owner.loop_tasks["active"] = asyncio.create_task(asyncio.sleep(0))
    await owner.loop_tasks["active"]
    owner.stopping.set()
    await owner._tick()
    assert service.states["active"]["task"]["status"] == "running"


@pytest.mark.asyncio
async def test_stop_waits_for_inflight_provision(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Service([task("first", "provisioning"), task("second", "provisioning", 1)])
    manager = Manager()
    owner = supervisor(service, manager)
    monkeypatch.setattr(owner, "_start", AsyncMock())
    entered = asyncio.Event()
    release = asyncio.Event()
    original = manager.provision

    async def slow_provision(tid: str, profile):
        entered.set()
        await release.wait()
        return await original(tid, profile)

    monkeypatch.setattr(manager, "provision", slow_provision)
    ticking = asyncio.create_task(owner.tick())
    await asyncio.wait_for(entered.wait(), 2)
    stopping = asyncio.create_task(owner.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(ticking, stopping), 2)
    assert ("provision", "first") in manager.calls
    assert ("provision", "second") not in manager.calls
    assert "first" in manager.handles
    assert ("destroy", "first") not in manager.calls


@pytest.mark.asyncio
async def test_terminal_during_provision_keeps_slot_until_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("first", "provisioning"), task("second", "provisioning", 1)])
    manager = Manager()
    owner = supervisor(service, manager)
    monkeypatch.setattr(owner, "_start", AsyncMock())
    original = manager.provision

    async def provision_then_stop(tid: str, profile):
        handle = await original(tid, profile)
        await service.transition(tid, "stopped")
        return handle

    monkeypatch.setattr(manager, "provision", provision_then_stop)
    await owner.tick()
    assert service.states["first"]["task"]["status"] == "stopped"
    assert service.states["second"]["task"]["status"] == "provisioning"
    assert ("provision", "second") not in manager.calls
    assert "first" in manager.handles
