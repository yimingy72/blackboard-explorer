"""Real Task transactions and stale scheduler fences for legacy Agent networks."""

from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_contracts.legacy_network import (
    LEGACY_NETWORK_HEADER,
    LEGACY_NETWORK_VERSION,
    LegacyNetworkPolicy,
    snapshot,
)
from bbx_contracts.profile import load_profile
from test_board_flags_integration import Objects
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url

pytestmark = pytest.mark.integration


async def test_protected_blackboard_policy_and_original_execution(ctf, ctf_url):
    options = settings()
    app = create_app(options, engine=ctf.repo.engine, objects=Objects())
    base = Path(__file__).resolve().parents[3]
    profile, _ = load_profile(base / "profiles/default")
    saved = await app.state.profile_store.create("protected-fixture", profile, "test")
    body = {
        "name": "Protected legacy Task",
        "goal": "Verify original execution and capture",
        "agent_profile": saved["name"],
        "profile_version": saved["version"],
        "acceptance": [{"id": "A1", "desc": "Capture an actual source"}],
        "budget": {"max_cost": "2", "max_minutes": 5, "max_concurrent_agents": 1},
    }
    old = await app.state.board_service.create_task(
        {key: value for key, value in body.items() if key != "profile_version"},
        profile_version=saved["version"],
    )
    user = {"Authorization": f"Bearer {issue_user_token(options, 'alice')}"}
    old_service = {"Authorization": f"Bearer {options.service_token.get_secret_value()}"}
    versioned = {**old_service, LEGACY_NETWORK_HEADER: LEGACY_NETWORK_VERSION}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:

        async def create(*, extra=None):
            result = await http.post("/api/tasks", headers=user, json={**body, **(extra or {})})
            assert result.status_code == 200, result.text
            return result.json()["id"]

        async def observe(tid, proof, phase, **changes):
            return await http.post(
                f"/api/tasks/{tid}/execution-network",
                headers=versioned,
                json={**proof, "phase": phase, **changes},
            )

        def identities(policy, *, forced=False):
            return {
                "run_number": 1,
                "policy_digest": policy.policy_digest,
                "envd_id": uuid4().hex + uuid4().hex,
                "envd_started_at": "2026-11-09T03:00:00.123456789Z",
                "network_id": uuid4().hex + uuid4().hex,
                "relay_id": uuid4().hex + uuid4().hex,
                "relay_started_at": "2026-11-09T03:00:01Z",
                "relay_network_id": uuid4().hex + uuid4().hex,
                **(
                    {
                        "gateway_id": uuid4().hex + uuid4().hex,
                        "gateway_started_at": "2026-11-09T03:00:02Z",
                        "gateway_network_id": uuid4().hex + uuid4().hex,
                    }
                    if forced
                    else {}
                ),
            }

        tid = await create()
        path = f"/api/tasks/{tid}"
        listing = await http.get("/api/tasks?limit=1", headers=old_service)
        assert [item["id"] for item in listing.json()] == [str(old)]
        assert (await http.get("/api/tasks?limit=1", headers=versioned)).json()[0]["id"] == tid
        assert (await http.post(path + "/start", headers=old_service)).status_code == 409
        assert (await http.get(path + "/state", headers=old_service)).status_code == 409
        default = snapshot(LegacyNetworkPolicy())
        public = (await http.get(path, headers=user)).json()["legacy_network"]
        assert public == {"mode": "default", "state": "pending", "capture_required": True}
        internal = (await http.get(path + "/state", headers=versioned)).json()["task"]
        assert internal["legacy_network"] == default.model_dump()
        assert internal["legacy_network_receipt"] is None
        assert (await http.post(path + "/start", headers=user)).status_code == 200

        async def running(headers=versioned):
            return await http.post(path + "/status", headers=headers, json={"status": "running"})

        assert (await running()).status_code == 409
        proof = identities(default)
        assert (await observe(tid, proof, "ready", capture_ready=True)).status_code == 409
        assert (await observe(tid, proof, "prepared")).status_code == 200
        assert (await observe(tid, proof, "starting")).status_code == 409
        assert (await observe(tid, proof, "ready")).status_code == 409
        assert (
            await observe(tid, proof, "ready", capture_ready=True, probe_ready=True)
        ).status_code == 409
        assert (await observe(tid, proof, "ready", capture_ready=True)).status_code == 200
        assert (await running(old_service)).status_code == 409
        assert (await running()).status_code == 200
        assert (await http.post(path + "/stop", headers=user)).status_code == 200
        assert (await observe(tid, proof, "fenced")).status_code == 200
        solver = await http.post(path + "/agents", headers=versioned, json={"task_type": "explore"})
        assert solver.status_code == 409
        close = await http.post(
            path + "/agents",
            headers=versioned,
            json={"task_type": "close", "close_mode": "final"},
        )
        assert close.status_code == 200, close.text
        await app.state.board_service.finish_agent(
            UUID(tid), close.json()["agent_id"], {"accepted": False, "reason": "fixture"}, "normal"
        )

        assert (await observe(tid, proof, "ready", capture_ready=True)).status_code == 409
        assert (await observe(tid, proof, "exited", envd_id="a" * 64)).status_code == 409
        assert (await observe(tid, proof, "exited")).status_code == 200
        stopped = await http.post(path + "/status", headers=versioned, json={"status": "stopped"})
        assert stopped.status_code == 200, stopped.text
        deleted = await http.delete(path, headers=user)
        assert deleted.status_code == 202, deleted.text
        assert tid not in (await http.get("/api/tasks/deletions", headers=old_service)).json()
        assert tid in (await http.get("/api/tasks/deletions", headers=versioned)).json()
        assert (await http.post(path + "/purge", headers=old_service)).status_code == 409
        assert (await http.post(path + "/purge", headers=versioned)).status_code == 409
        assert (await observe(tid, proof, "exited", capture_closed=True)).status_code == 200

        vpn = LegacyNetworkPolicy.model_validate(
            {
                "mode": "vpn",
                "gateway": {
                    "image": "fixture-egress@sha256:" + "a" * 64,
                    "uplink_network": "fixture-private",
                    "upstream": {"ipv4": "10.80.0.2", "port": 1194, "server_name": "fixture-vpn"},
                    "allow_cidrs": ["10.90.0.0/24"],
                    "deny_cidrs": ["10.91.0.0/24"],
                    "probe": {"ipv4": "10.90.0.2", "port": 18888, "path": "/health", "status": 204},
                    "credential_file": "/trusted/fixture-private.json",
                    "credential_sha256": "b" * 64,
                },
            }
        )
        selected = snapshot(vpn)
        app.state.legacy_network_publication = selected
        assert (await http.post(f"/api/tasks/{old}/start", headers=user)).status_code == 409
        first, second = await create(), await create()
        one, two = identities(selected, forced=True), identities(selected, forced=True)
        for task_id in (first, second):
            assert (await http.post(f"/api/tasks/{task_id}/start", headers=user)).status_code == 200
        assert (await observe(first, one, "prepared")).status_code == 200
        assert (await observe(second, two, "prepared")).status_code == 409
        launch = await observe(first, one, "starting")
        assert launch.status_code == 200 and launch.json()["launch_allowed"]
        duplicate = await observe(first, one, "starting")
        assert duplicate.status_code == 200 and not duplicate.json()["launch_allowed"]
        assert (await observe(first, one, "ready", capture_ready=True)).status_code == 409
        assert (await observe(first, one, "unknown")).status_code == 200
        assert (await observe(second, two, "prepared")).status_code == 409
        assert (await observe(first, one, "exited")).status_code == 200
        assert (await observe(second, two, "prepared")).status_code == 200

        app.state.legacy_network_publication = default
        assert (await observe(second, two, "starting")).status_code == 409
        historical = (await http.get(f"/api/tasks/{second}/state", headers=versioned)).json()[
            "task"
        ]
        assert historical["legacy_network"] == selected.model_dump()
        assert "fixture-private.json" not in str(historical)

        app.state.legacy_network_publication = None
        assert (await http.post("/api/tasks", headers=user, json=body)).status_code == 503
        public = (await http.get(f"/api/tasks/{second}", headers=user)).json()
        assert public["legacy_network"] == {
            "mode": "vpn",
            "state": "prepared",
            "capture_required": True,
        }
