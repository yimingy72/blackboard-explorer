"""Legacy supervision cannot adopt a frozen new business contract."""

from unittest.mock import AsyncMock

import pytest
from bbx_contracts.challenge_board import BOARD_CONTRACT
from test_ctf_supervisor import supervisor


async def test_legacy_supervision_skips_v1_before_recovery_environment_and_model():
    rows = [
        {
            "id": "new-task",
            "mode": "ctf",
            "status": "provisioning",
            "created_at": "2026-11-06",
            "business_contract": BOARD_CONTRACT,
        }
    ]
    instance, service, manager, coordinator = supervisor(rows)
    await instance.recover()
    await instance.tick()
    assert not instance.ctf_tasks
    coordinator.run.assert_not_awaited()
    coordinator.recover.assert_not_awaited()
    coordinator.review_tick.assert_not_awaited()
    manager.find.assert_not_awaited()
    manager.provision.assert_not_called()
    service.state = AsyncMock(
        return_value={
            "task": {
                "mode": "ctf",
                "status": "provisioning",
                "ctf_control": {"business_contract": BOARD_CONTRACT},
            }
        }
    )
    with pytest.raises(RuntimeError, match="registered Runner"):
        await instance._ctf_envd("new-task")
    manager.find.assert_not_awaited()
    manager.provision.assert_not_called()
