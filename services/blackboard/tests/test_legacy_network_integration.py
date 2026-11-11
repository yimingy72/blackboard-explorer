"""Retired ordinary execution data remains readable without a live solver fallback."""

from pathlib import Path

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.profiles import ProfileStore
from bbx_contracts.legacy_network import LegacyNetworkPolicy, snapshot
from test_board_flags_integration import Objects
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url

pytestmark = pytest.mark.integration


async def test_historical_network_stays_readable_and_never_restarts(ctf):
    options = settings()
    app = create_app(options, engine=ctf.repo.engine, objects=Objects())
    root = Path(__file__).resolve().parents[3]
    await ProfileStore(ctf.repo.engine).ensure_default(root / "profiles/default")
    spec = {
        "goal": "Historical local task",
        "acceptance": [{"id": "A1", "desc": "Check a local note"}],
        "budget": {"max_cost": "1", "max_minutes": 5},
        "agent_profile": "default",
    }
    tid = await app.state.board_service.create_task(
        spec, legacy_network=snapshot(LegacyNetworkPolicy())
    )
    user = {"Authorization": "Bearer " + issue_user_token(options, "alice")}
    service = {"Authorization": "Bearer " + options.service_token.get_secret_value()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        historical = await http.get(f"/api/tasks/{tid}/state", headers=user)
        assert historical.status_code == 200, historical.text
        assert historical.json()["task"]["legacy_network"]["mode"] == "default"
        assert (await http.post(f"/api/tasks/{tid}/start", headers=user)).status_code == 409
        status = await http.post(
            f"/api/tasks/{tid}/status", headers=service, json={"status": "running"}
        )
        assert status.status_code == 409
        assert (
            await http.post(f"/api/tasks/{tid}/execution-network", headers=service)
        ).status_code == 404
        caps = (await http.get("/api/task-creation-capabilities", headers=user)).json()
        assert not caps["blackboard"]["available"]
