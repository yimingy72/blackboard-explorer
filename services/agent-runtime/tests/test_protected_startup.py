"""A slow archive/attachment/capture write cannot consume the first forwarding lease."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bbx_contracts.legacy_network import LEGACY_NETWORK_VERSION, LegacyNetworkPolicy, snapshot
from bbx_runtime.execenv.manager import ExecEnvManager
from bbx_runtime.execenv.protected import ProtectedExecution, platform_subnets


def test_default_deny_covers_platform_bridges_but_not_unrelated_lan_overlap():
    def network(name, prefix, *, protected=False):
        return SimpleNamespace(
            id=name,
            name=name,
            attrs={
                "IPAM": {"Config": [{"Subnet": prefix}]},
                "Labels": {"bbx.managed": "agent-runtime-legacy-network"} if protected else {},
            },
        )

    current = network("owned-task-exec", "10.222.1.0/24")
    control = network("trusted-runtime-control", "10.222.2.0/24")
    other_task = network("another-protected-task", "10.222.3.0/24", protected=True)
    old_exec = network("configured-legacy-exec", "10.222.4.0/24")
    unrelated = network("unrelated-docker", "192.168.96.0/20")
    candidates = (other_task, old_exec, unrelated)
    expected = {f"10.222.{i}.0/24" for i in range(1, 5)}
    assert (
        platform_subnets(candidates, current, control, legacy_names={old_exec.name}, forced=False)
        == expected
    )
    assert platform_subnets(
        candidates, current, control, legacy_names={old_exec.name}, forced=True
    ) == expected | {"192.168.96.0/20"}


@pytest.mark.parametrize("stop_during_prepare", (False, True))
async def test_protected_slow_inputs_and_capture_do_not_open_lease(
    monkeypatch, stop_during_prepare
):
    task = {
        "id": str(uuid4()),
        "status": "provisioning",
        "run_number": 1,
        "legacy_network": snapshot(LegacyNetworkPolicy()).model_dump(),
        "resume_workspace_uri": "fixture://workspace",
        "initial_attachments": ["fixture input"],
    }
    materializing, capture_started = asyncio.Event(), asyncio.Event()
    allow_inputs, allow_capture = asyncio.Event(), asyncio.Event()
    actions = []
    handle = SimpleNamespace(base_url="http://isolated", token="fixture")

    async def inputs(*_):
        materializing.set()
        await allow_inputs.wait()

    async def capture(*_):
        capture_started.set()
        await allow_capture.wait()

    manager = SimpleNamespace(
        docker=None,
        settings=SimpleNamespace(legacy_network_policy_file=None, exec_egress_mode="direct"),
        wait_healthy=AsyncMock(),
        restore=AsyncMock(),
        ensure_initial_inputs=inputs,
        start_traffic=capture,
    )
    protected = ProtectedExecution(cast(ExecEnvManager, manager))
    monkeypatch.setattr(protected, "setup", AsyncMock(return_value=(object(), [])))
    monkeypatch.setattr(protected, "handle", AsyncMock(return_value=handle))
    monkeypatch.setattr(protected, "receipt", lambda _resources, phase, **_options: phase)

    class Env:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def health(self):
            return {
                "protected_network": LEGACY_NETWORK_VERSION,
                "task_id": task["id"],
                "network_mode": "default",
                "traffic": True,
            }

        async def protected_network(self, action, **_):
            actions.append(action)
            return {"capture_ready": True, "probe_ready": False}

    monkeypatch.setattr("bbx_runtime.execenv.protected.EnvdClient", Env)
    service = SimpleNamespace(
        legacy_execution_network=AsyncMock(), state=AsyncMock(return_value={"task": task})
    )
    work = asyncio.create_task(protected.provision(task, object(), service))
    try:
        async with asyncio.timeout(5):
            await materializing.wait()
            assert not actions
            allow_inputs.set()
            await capture_started.wait()
            assert not actions
            if stop_during_prepare:
                task["status"] = "closing"
            allow_capture.set()
            if stop_during_prepare:
                with pytest.raises(RuntimeError, match="no longer authorized"):
                    await work
                assert not actions
            else:
                assert await work is handle
                assert actions == ["renew", "probe"]
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
