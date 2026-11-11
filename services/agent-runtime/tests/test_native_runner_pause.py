"""Exercise stale material rejection and the same Runner's immediate resume publication."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from bbx_runtime.native_runner import NativeRunnerController


async def test_one_controller_keeps_concurrent_mode_resource_views_independent(monkeypatch):
    profile = "openvpn-task-ipv4-tcp-v1"
    shared = {"source_id": "owned", "runner_profile": "node-sandbox-host-v1"}
    ctf_resources = SimpleNamespace(
        config={**shared, "vpn": {"profile": profile, "default_cidrs": ["192.0.2.0/24"]}}
    )
    board_resources = SimpleNamespace(
        config={**shared, "vpn": {"profile": profile, "default_cidrs": ["198.51.100.0/24"]}}
    )
    originals = deepcopy((ctf_resources.config, board_resources.config))
    claims = {}
    arrived = set()
    both = asyncio.Event()

    def state(tid):
        return {
            "state": "ready",
            "data": {
                "execution_contract": "exploration-board-v1"
                if tid == "board"
                else "challenge-board-v1",
                "vpn_profile": profile,
                "native_network": "default",
            },
        }

    async def request(method, path, *, json):
        tid = path.split("/")[2]
        assert method == "POST" and path == f"/tasks/{tid}/runtime/runner-bootstrap/claim"
        claims[tid] = json
        return {**state(tid), "tenant_key": tid}

    controller = NativeRunnerController(
        SimpleNamespace(
            runner_blackboard_url="https://blackboard.invalid",
            runner_model_proxy_url="https://proxy.invalid",
        ),
        SimpleNamespace(_json=request),
        ctf_resources,
        SimpleNamespace(
            fence=1, backend_pid=1, backend_started_at=datetime.now(UTC), lost=asyncio.Event()
        ),
        board_resources=board_resources,
    )

    async def view(tid):
        return state(tid)

    async def drive(tid, operation, _tenant, _idle):
        selected, config = controller._operation_resources(operation)
        assert selected is (board_resources if tid == "board" else ctf_resources)
        assert config["vpn"] == selected.config["vpn"]
        arrived.add(tid)
        if len(arrived) == 2:
            both.set()
        await both.wait()

    monkeypatch.setattr(controller, "view", view)
    monkeypatch.setattr(controller, "_drive", drive)
    await asyncio.wait_for(asyncio.gather(controller._task("board"), controller._task("ctf")), 1)
    assert claims["board"]["controller_fence"] == claims["ctf"]["controller_fence"] == 1
    assert claims["board"]["resource_config"]["vpn"] == board_resources.config["vpn"]
    assert claims["ctf"]["resource_config"]["vpn"] == ctf_resources.config["vpn"]
    assert originals == (ctf_resources.config, board_resources.config)


async def test_forced_board_network_does_not_publish_solver_material_before_its_lease(monkeypatch):
    now = datetime.now(UTC)
    state = {
        "state": "ready",
        "desired": "active",
        "hard_deadline": (now + timedelta(minutes=1)).isoformat(),
        "observation": {"runner": {"pod_uid": "original"}, "sandbox": {"id": "original"}},
        "data": {
            "execution_contract": "exploration-board-v1",
            "native_network": "vpn",
            "vpn_profile": "openvpn-task-ipv4-tcp-v1",
            "vpn": {"state": "idle"},
            "credentials_issued": True,
            "sandbox_host_ready": True,
        },
    }
    actions = []

    async def observe(observation):
        return observation

    async def mount(_operation, material, _blackboard):
        assert material == {"control": "original-only"}
        actions.append("network-mount")

    async def policy(operation):
        assert operation["data"]["vpn"]["state"] == "awaiting_policy"
        return {"observed": True}

    async def mount_solver(_observation, _bootstrap, credentials, **_kwargs):
        assert credentials == {"solver": "after-original-proof"}
        actions.append("solver-mount")

    resources = SimpleNamespace(
        config={"board_network": {"mode": "socks5"}},
        observe=observe,
        publish_vpn_mount=mount,
        ensure_vpn_peer_policy=policy,
        publish_mount=mount_solver,
    )
    controller = NativeRunnerController(
        SimpleNamespace(
            runner_blackboard_url="https://blackboard.invalid",
            runner_model_proxy_url="https://proxy.invalid",
        ),
        None,
        resources,
        SimpleNamespace(fence=1, backend_pid=1, backend_started_at=now, lost=asyncio.Event()),
    )

    async def view(_tid):
        return deepcopy(state)

    async def action(_tid, _operation, name, **_kwargs):
        if name != "observation":
            actions.append(name)
        if name == "vpn_material":
            state["data"]["vpn"]["state"] = "awaiting_policy"
            return {"control": "original-only"}
        if name == "vpn_peer_ready":
            state["data"]["vpn"]["state"] = "preflight"
        if name == "renew":
            assert state["data"]["vpn"]["applied"]["mode"] == "solver"
            return {
                "status": deepcopy(state),
                "bootstrap": {},
                "credentials": {"solver": "after-original-proof"},
            }
        if name == "activate":
            controller.closed = True
        return deepcopy(state)

    waits = 0

    async def idle():
        nonlocal waits
        if controller.closed:
            return
        waits += 1
        assert "solver-mount" not in actions
        state["data"]["vpn"] = {
            "state": "ready",
            "applied": {
                "mode": "preflight" if waits == 1 else "solver",
                "applied_until_ms": int(now.timestamp() * 1000) + 30000,
            },
        }

    monkeypatch.setattr(controller, "view", view)
    monkeypatch.setattr(controller, "action", action)
    await asyncio.wait_for(controller._drive("task", deepcopy(state), "tenant", idle), 2)
    assert waits == 2
    assert actions == [
        "vpn_material",
        "network-mount",
        "vpn_peer_ready",
        "renew",
        "solver-mount",
        "activate",
    ]


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
