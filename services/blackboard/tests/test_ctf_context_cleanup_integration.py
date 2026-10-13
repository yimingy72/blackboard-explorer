"""Maintenance previews stay read-only and apply only to selected closed tasks."""

import json
from uuid import uuid4

import pytest
from bbx_blackboard.ctf_context_cleanup import cleanup
from bbx_blackboard.store import schema as s
from sqlalchemy import update
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_integration import team

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def legacy_marker(ctf, tid):
    task = (await ctf.state(tid))["task"]
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **task["ctf_control"],
                    "context_blocked_members": {
                        "lead": {
                            "turn_id": "legacy-turn",
                            "message_ids": [None, "saved-input"],
                            "private_text": "Private material never belongs in CLI output",
                        }
                    },
                }
            )
        )


async def closed_task(ctf):
    tid = await team(ctf)
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "partial", "summary": "Synthetic close"}
    )
    await ctf.finalize_close(tid, True)
    await legacy_marker(ctf, tid)
    return tid


async def test_cleanup_cli_defaults_to_metadata_only_read_only_preview(ctf):
    tid = await closed_task(ctf)
    before = await ctf.state(tid)
    result = await cleanup(ctf.repo.engine, task_ids=[tid])
    assert result == [{"task_id": str(tid), "cleared_members": 1}]
    assert "Private material" not in json.dumps(result)
    assert await ctf.state(tid) == before


async def test_cleanup_cli_honors_selection_and_never_touches_active_task(ctf):
    selected = await closed_task(ctf)
    other = await closed_task(ctf)
    active = await team(ctf)
    await legacy_marker(ctf, active)
    before_other = await ctf.state(other)
    before_active = await ctf.state(active)
    result = await cleanup(ctf.repo.engine, apply=True, task_ids=[selected, active])
    assert result == [{"task_id": str(selected), "changed": True, "cleared_members": 1}]
    assert await ctf.state(other) == before_other
    assert await ctf.state(active) == before_active
    control = (await ctf.state(selected))["task"]["ctf_control"]
    assert control["context_blocked_members"] == {}
    assert control["context_blocked_history"]["legacy-turn"]["message_ids"] == ["saved-input"]
    assert await cleanup(ctf.repo.engine, apply=True, task_ids=[selected]) == []
