"""CTF task supervision branches before blackboard profiles and environments."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from bbx_runtime.scheduler.supervisor import TaskSupervisor
from bbx_runtime.settings import Settings


def supervisor(rows):
    service = Mock()
    service.list_tasks = AsyncMock(return_value=rows)
    service.pending_deletions = AsyncMock(return_value=[])
    manager = Mock()
    instance = TaskSupervisor(
        Settings.model_construct(max_running_tasks=1), service, manager, Mock()
    )
    coordinator = Mock()
    coordinator.run = AsyncMock()
    coordinator.recover = AsyncMock()
    coordinator.close = AsyncMock()
    coordinator.detach = AsyncMock()
    coordinator.service = Mock(runtime=AsyncMock())
    coordinator.review_tick = AsyncMock()
    instance.ctf = coordinator
    return instance, service, manager, coordinator


@pytest.mark.asyncio
async def test_ctf_queue_does_not_load_blackboard_profile_or_provision_unfenced_envd():
    instance, service, manager, coordinator = supervisor(
        [{"id": "ctf-1", "mode": "ctf", "status": "provisioning", "created_at": "2026-10-08"}]
    )
    await instance.tick()
    await instance.ctf_tasks["ctf-1"]
    coordinator.run.assert_awaited_once_with("ctf-1")
    service.get_profile.assert_not_called()
    manager.provision.assert_not_called()
    await instance.stop()
    coordinator.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_ctf_recovery_preserves_mode_and_uses_its_coordinator():
    instance, service, manager, coordinator = supervisor(
        [{"id": "ctf-1", "mode": "ctf", "status": "running"}]
    )
    await instance.recover()
    await instance.ctf_tasks["ctf-1"]
    coordinator.recover.assert_awaited_once_with("ctf-1")
    service.finish_agent.assert_not_called()
    service.state.assert_not_called()
    manager.find.assert_not_called()
    await instance.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "upload", "register", "destroy", "cleanup"])
async def test_ctf_terminal_archive_registration_precedes_destroy_and_retries(failure):
    instance, service, manager, coordinator = supervisor(
        [{"id": "ctf-1", "mode": "ctf", "status": "finished"}]
    )
    task = {"id": "ctf-1", "mode": "ctf", "status": "finished", "workspace_uri": None}
    calls = []
    service.state = AsyncMock(side_effect=lambda _: {"task": task})
    service.archive_data = AsyncMock(return_value={"format": "bbx.task-archive.v1"})
    archive = Mock(uri="workspace/ctf-1.tar.zst", size=123, fallback="none")

    async def upload(*_):
        calls.append("upload")
        if failure == "upload":
            raise ConnectionError("object store unavailable")
        return archive

    async def register(*_):
        calls.append("register")
        if failure == "register":
            # Commit succeeded, but the response was lost.
            task["workspace_uri"] = archive.uri
            raise ConnectionError("response lost")
        task["workspace_uri"] = archive.uri

    async def destroy(*_):
        assert task["workspace_uri"] == archive.uri
        calls.append("destroy")
        if failure == "destroy":
            raise ConnectionError("container API unavailable")

    async def cleaned(*_):
        calls.append("cleanup")
        if failure == "cleanup":
            raise ConnectionError("database unavailable after destruction")

    manager.archive_task = AsyncMock(side_effect=upload)
    service.record_archive = AsyncMock(side_effect=register)
    manager.destroy = AsyncMock(side_effect=destroy)
    service.record_cleanup = AsyncMock(side_effect=cleaned)
    await instance.tick()
    result = await asyncio.gather(instance.cleanups["ctf-1"], return_exceptions=True)
    if failure:
        assert isinstance(result[0], ConnectionError)
        assert "ctf-1" not in instance.cleaned
        if failure in {"upload", "register"}:
            manager.destroy.assert_not_awaited()
        failure = None
        await instance.tick()
        await instance.cleanups["ctf-1"]
    assert "ctf-1" in instance.cleaned
    assert calls.index("register") < calls.index("destroy")
    assert calls[-1] == "cleanup"
    coordinator.detach.assert_awaited()
    service.conclude.assert_not_called()
    await instance.stop()


@pytest.mark.asyncio
async def test_running_ctf_task_is_recovered_after_coordinator_transport_failure():
    instance, _, _, coordinator = supervisor([{"id": "ctf-1", "mode": "ctf", "status": "running"}])
    import asyncio

    async def failed():
        raise ConnectionError("control channel disconnected")

    previous = asyncio.create_task(failed())
    await asyncio.gather(previous, return_exceptions=True)
    instance.ctf_tasks["ctf-1"] = previous
    await instance.tick()
    await instance.ctf_tasks["ctf-1"]
    coordinator.recover.assert_awaited_once_with("ctf-1")
    coordinator.run.assert_awaited_once_with("ctf-1")
    await instance.stop()


@pytest.mark.asyncio
async def test_ctf_factory_provisions_fixed_mode_and_restores_registered_inputs():
    from pathlib import Path

    from bbx_contracts.ctf import CtfAgentProfile, load_ctf_profile

    instance, service, manager, _ = supervisor([])
    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    attachments = [{"filename": "challenge.zip"}]
    service.state = AsyncMock(
        return_value={
            "task": {
                "mode": "ctf",
                "status": "provisioning",
                "agent_profile": "ctf",
                "agent_profile_version": 1,
                "initial_attachments": attachments,
                "resume_workspace_uri": "workspace/task-1.tar.zst",
            }
        }
    )
    service.get_profile = AsyncMock(return_value={"profile": profile.model_dump(mode="json")})
    manager.find = AsyncMock(return_value=None)
    handle = Mock(base_url="http://envd.invalid:8080", token="test-token")
    manager.provision = AsyncMock(return_value=handle)
    manager.wait_healthy = AsyncMock()
    manager.ensure_initial_inputs = AsyncMock()
    client = await instance._ctf_envd("task-1")
    try:
        assert isinstance(manager.provision.call_args.args[1], CtfAgentProfile)
        assert manager.provision.call_args.args[2] == "workspace/task-1.tar.zst"
        manager.ensure_initial_inputs.assert_awaited_once_with(handle, attachments)
        assert client.base_url == handle.base_url
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ctf_factory_does_not_silently_replace_missing_running_container():
    instance, service, manager, _ = supervisor([])
    service.state = AsyncMock(return_value={"task": {"mode": "ctf", "status": "running"}})
    manager.find = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="recovery required"):
        await instance._ctf_envd("task-1")
    manager.provision.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [None, "archive", "destroy", "restore"])
async def test_ctf_replacement_persists_progress_before_destroy_and_resumes(monkeypatch, fail_at):
    from pathlib import Path

    import bbx_runtime.scheduler.supervisor as module
    from bbx_contracts.ctf import load_ctf_profile

    instance, service, manager, coordinator = supervisor([])
    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    task = {"ctf_control": {}, "agent_profile": "ctf", "agent_profile_version": 1}
    control = task["ctf_control"]
    phases = []

    async def record(_tid, operation, **body):
        assert operation == "execution_replacement"
        phases.append(body["phase"])
        control["replacement"] = {**control.get("replacement", {}), **body}

    coordinator.service = Mock()
    coordinator.service.state = AsyncMock(
        return_value={
            "task": task,
            "members": [{"execution": {"boot_id": "registered-boot", "drained": False}}],
        }
    )
    coordinator.service.runtime = AsyncMock(side_effect=record)
    service.get_profile = AsyncMock(return_value={"profile": profile.model_dump(mode="json")})
    old = Mock(container_id="old-container", base_url="http://old", token="test")
    new = Mock(container_id="new-container", base_url="http://new", token="test")
    manager.find = AsyncMock(return_value=old)

    async def archive(*args, **kwargs):
        assert control["replacement"]["phase"] == "begin"
        if fail_at == "archive":
            raise OSError("archive unavailable")
        return Mock(uri="workspace/task/replacement-old-container.tar.zst")

    async def destroy(*args):
        assert control["replacement"]["phase"] == "archive_saved"
        if fail_at == "destroy":
            raise OSError("destroy unavailable")

    async def restore(*args):
        assert control["replacement"]["phase"] == "destroyed"
        assert args[2] == control["replacement"]["archive_uri"]
        if fail_at == "restore":
            raise OSError("restore unavailable")
        return new

    manager.archive_to_store = AsyncMock(side_effect=archive)
    manager.destroy_confirmed = AsyncMock(side_effect=destroy)
    manager.provision = AsyncMock(side_effect=restore)

    class Client:
        def __init__(self, url, token):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def close(self):
            pass

        async def ctf_status(self):
            return {
                "boot_id": "old-boot" if self.url == "http://old" else "new-boot",
                "state": "unknown" if self.url == "http://old" else "ready",
            }

    monkeypatch.setattr(module, "EnvdClient", Client)
    if fail_at:
        with pytest.raises(OSError):
            await instance._replace_ctf_envd("task")
        expected = {"archive": "begin", "destroy": "archive_saved", "restore": "destroyed"}
        assert control["replacement"]["phase"] == expected[fail_at]
        if fail_at == "archive":
            manager.destroy_confirmed.assert_not_called()
        fail_at = None
    await instance._replace_ctf_envd("task")
    assert control["replacement"]["phase"] == "ready"
    assert control["replacement"]["new_boot_id"] == "new-boot"
    assert control["replacement"]["old_boot_id"] == "registered-boot"
    assert phases[-3:] == ["archive_saved", "destroyed", "ready"]
    manager.destroy_confirmed.assert_awaited_with("task", "old-container")


async def test_terminal_review_waits_for_cleanup_ready_and_never_runs_execution():
    row = {"id": "ctf-1", "mode": "ctf", "status": "finished", "cleanup_ready": True}
    instance, service, manager, coordinator = supervisor([row])
    await instance.tick()
    coordinator.review_tick.assert_awaited_once_with("ctf-1")
    coordinator.run.assert_not_awaited()
    assert not instance.cleanups
    manager.provision.assert_not_called()
    manager.destroy.assert_not_called()
    await instance.stop()


async def test_purge_joins_ctf_writers_before_destroy_and_task_purge():
    instance, service, manager, coordinator = supervisor([])
    order = []

    async def detach(tid, *, deleting):
        assert deleting
        order.append("detach")

    async def destroy(tid):
        order.append("destroy")

    async def purge(tid):
        order.append("purge")

    coordinator.detach = AsyncMock(side_effect=detach)
    manager.destroy = AsyncMock(side_effect=destroy)
    service.purge_task = AsyncMock(side_effect=purge)
    await instance._purge_deleted("ctf-1")
    assert order == ["detach", "destroy", "purge"]
    await instance.stop()
