"""Exercise stale material rejection and the same Runner's immediate resume publication."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from bbx_runtime.native_runner import NativeRunnerController


async def test_pause_discards_inflight_full_material_and_mounts_new_lead_immediately(monkeypatch):
    observation = {
        "runner": {"pod_uid": "original-runner-pod"},
        "sandbox": {"id": "original-sandbox"},
    }
    bootstrap = {"fence": {"task_id": "task", "pod_uid": "original-runner-pod", "epoch": 3}}
    state = {
        "state": "ready",
        "desired": "active",
        "hard_deadline": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "observation": observation,
        "data": {"binding_id": "old-lead", "credentials_issued": True},
    }
    publications = []
    stale_material_issued = False
    idle_calls = 0

    async def observe(value):
        assert value == observation
        return value

    async def publish_mount(original, config, credentials, *, control_only):
        assert original == observation and config == bootstrap
        publications.append((control_only, set(credentials)))

    controller = NativeRunnerController(
        SimpleNamespace(
            runner_blackboard_url="https://blackboard.invalid",
            runner_model_proxy_url="https://proxy.invalid",
        ),
        None,
        SimpleNamespace(config={}, observe=observe, publish_mount=publish_mount),
        SimpleNamespace(
            fence=1, backend_pid=1, backend_started_at=datetime.now(UTC), lost=asyncio.Event()
        ),
    )

    async def view(_tid):
        return deepcopy(state)

    async def action(_tid, _operation, name, **_values):
        nonlocal stale_material_issued
        phase = state["data"].get("pause_state")
        if name == "observation":
            return deepcopy(state)
        if name == "renew":
            status = deepcopy(state)
            if phase is None:
                assert not stale_material_issued
                stale_material_issued = True
                state["state"] = "held"
                state["data"]["pause_state"] = "draining"
                return {
                    "status": status,
                    "bootstrap": bootstrap,
                    "credentials": {"control": {}, "native-model-lead": {}},
                }
            if phase in {"draining", "paused"}:
                return {
                    "status": status,
                    "bootstrap": bootstrap,
                    "credentials": {"control": {}},
                    "control_only": True,
                }
            assert phase == "resuming"
            return {
                "status": status,
                "bootstrap": bootstrap,
                "credentials": {
                    "control": {},
                    "native-model-new-lead": {},
                    "native-mcp-new-lead": {},
                },
            }
        assert name == "activate"
        if phase == "draining":
            assert publications and all(control for control, _ in publications)
            state["data"]["pause_state"] = "paused"
        else:
            assert phase == "resuming"
            assert publications[-1] == (
                False,
                {"control", "native-model-new-lead", "native-mcp-new-lead"},
            )
            state["data"]["pause_state"] = "resumed"
            controller.closed = True
        return deepcopy(state)

    async def idle():
        nonlocal idle_calls
        assert state["data"]["pause_state"] == "paused"
        assert idle_calls == 0
        idle_calls += 1
        state["data"].update(binding_id="new-lead", pause_state="resuming")

    monkeypatch.setattr(controller, "view", view)
    monkeypatch.setattr(controller, "action", action)
    await asyncio.wait_for(controller._drive("task", deepcopy(state), "tenant", idle), timeout=2)
    assert stale_material_issued and idle_calls == 1
    assert all("native-model-lead" not in keys for _, keys in publications)
    assert sum(not control for control, _ in publications) == 1
