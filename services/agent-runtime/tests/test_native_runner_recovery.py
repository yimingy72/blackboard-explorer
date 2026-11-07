"""Frozen recovery only observes original resources until normal stop takes over."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from bbx_contracts.runner import runner_json_text, runner_payload_digest
from bbx_runtime.native_runner import NativeRunnerController
from bbx_runtime.runner_resources import ResourceUnknown


@pytest.mark.parametrize("freeze_after", ["observation", "renew"])
async def test_recovery_reobserves_original_without_execution_and_preserves_stop(
    freeze_after, monkeypatch
):
    original = {
        "runner": {"pod_uid": "original-runner", "container_id": "runner-container"},
        "sandbox": {"id": "original-sandbox", "pod_uid": "original-sandbox-pod"},
    }
    state = {
        "state": "ready",
        "desired": "active",
        "hard_deadline": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "observation": original,
        "data": {
            "binding_id": "original-lead",
            "credentials_issued": True,
            "sandbox_host_ready": True,
        },
    }
    actions = []
    observed = []
    frozen_idle = 0

    async def observe(value):
        assert value == original
        observed.append(state["state"])
        if observed == ["ready", "frozen"]:
            raise ResourceUnknown("Original object temporarily unavailable")
        return value

    async def recover(operation, tenant_key):
        assert operation["state"] == "stopping" and operation["observation"] == original
        assert tenant_key == "tenant"
        return {**original, "pvcs_retained": True}

    async def stop(value, tenant_key):
        assert value == original and tenant_key == "tenant"
        return {"runner_terminated": True, "sandbox_terminated": True, "pvcs_retained": True}

    controller = NativeRunnerController(
        SimpleNamespace(
            runner_blackboard_url="https://blackboard.invalid",
            runner_model_proxy_url="https://proxy.invalid",
        ),
        None,
        SimpleNamespace(
            config={"runner_profile": "node-sandbox-host-v1"},
            observe=observe,
            recover=recover,
            stop=stop,
        ),
        SimpleNamespace(
            fence=2, backend_pid=1, backend_started_at=datetime.now(UTC), lost=asyncio.Event()
        ),
    )

    async def view(_tid):
        return deepcopy(state)

    async def action(_tid, _operation, name, **values):
        actions.append(name)
        if name == freeze_after and state["state"] == "ready":
            stale = deepcopy(state)
            state["state"] = "frozen"
            state["data"]["recovery"] = {"reason": "original_execution_unknown"}
            if name == "renew":
                return {"status": stale, "bootstrap": {}, "credentials": {"old-lead": {}}}
        if name == "observation":
            assert values == {"observation": original}
        elif name == "stop":
            assert state["desired"] == "stopped"
            state["state"] = "stopping"
        elif name == "cleanup":
            assert values["proof"] == {
                "runner_terminated": True,
                "sandbox_terminated": True,
                "pvcs_retained": True,
                "observation_digest": runner_payload_digest(runner_json_text(original)),
            }
            state["state"] = "stopped"
        else:
            pytest.fail(f"Unexpected action: {name}")
        return deepcopy(state)

    async def idle():
        nonlocal frozen_idle
        assert state["state"] == "frozen"
        assert state["data"]["recovery"] == {"reason": "original_execution_unknown"}
        frozen_idle += 1
        if frozen_idle == 2:
            state["desired"] = "stopped"

    monkeypatch.setattr(controller, "view", view)
    monkeypatch.setattr(controller, "action", action)
    await asyncio.wait_for(controller._drive("task", deepcopy(state), "tenant", idle), timeout=4)
    assert observed == ["ready", "frozen", "frozen"]
    assert actions == [
        "observation",
        *(["renew"] if freeze_after == "renew" else []),
        "observation",
        "stop",
        "observation",
        "cleanup",
    ]
