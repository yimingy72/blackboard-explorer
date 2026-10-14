"""Stopping a newly claimed writer tracks its confirmed remote registration safely."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_turn, team
from test_ctf_integration import ctf_url as ctf_url_fixture

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def next_writer(ctf, *, drained):
    tid = await team(ctf)
    aid = (await ctf.create_member(tid, "user", "Registration worker", str(uuid4())))["id"]
    old = await ctf_turn(ctf, tid, aid)
    await ctf.record_execution(tid, aid, old["id"], old["generation"], "old-boot")
    if drained:
        await ctf.record_execution_drained(
            tid, aid, {"boot_id": "old-boot", "generation": old["generation"], "drained": True}
        )
    await ctf.finish_turn(tid, aid, old["id"], old["generation"], "completed", "Saved arithmetic")
    current = await ctf_turn(ctf, tid, aid)
    request_id = str(uuid4())
    pending = await ctf.request_member_operation(tid, "user", aid, "stop", request_id)
    assert pending["pending_operation"]["target_generation"] == old["generation"]
    return tid, aid, old, current, request_id


@pytest.mark.parametrize("boot_id", ["old-boot", "new-boot"])
async def test_stop_between_remote_register_and_binding_targets_confirmed_current_writer(
    ctf, boot_id
):
    tid, aid, old, current, request_id = await next_writer(ctf, drained=True)
    await ctf.record_execution(tid, aid, current["id"], current["generation"], boot_id)
    member = next(item for item in (await ctf.state(tid))["members"] if item["id"] == aid)
    pending = member["pending_operation"]
    assert pending["target_generation"] == current["generation"]
    assert pending["boot_id"] == boot_id
    before = await ctf.state(tid)
    for proof in (
        {"boot_id": "old-boot", "generation": old["generation"], "drained": True},
        {"boot_id": boot_id, "generation": current["generation"] + 1, "drained": True},
        {"boot_id": "foreign-boot", "generation": current["generation"], "drained": True},
    ):
        with pytest.raises(HTTPException, match="Remote drain proof mismatch"):
            await ctf.complete_member_operation(tid, aid, request_id, proof)
        assert await ctf.state(tid) == before
    with pytest.raises(HTTPException, match="has not drained"):
        await ctf.record_execution(tid, aid, current["id"], current["generation"], "foreign-boot")
    stopped = await ctf.complete_member_operation(
        tid,
        aid,
        request_id,
        {"boot_id": boot_id, "generation": current["generation"], "drained": True},
    )
    assert stopped["lifecycle"] == "stopped" and stopped["current_turn_id"] is None
    assert stopped["execution"] == {
        "boot_id": boot_id,
        "generation": current["generation"],
        "drained": True,
    }
    with pytest.raises(HTTPException, match="Stale execution registration"):
        await ctf.record_execution(tid, aid, current["id"], current["generation"], boot_id)


async def test_stop_before_remote_registration_can_still_drain_the_known_old_binding(ctf):
    tid, aid, old, current, request_id = await next_writer(ctf, drained=True)
    stopped = await ctf.complete_member_operation(
        tid,
        aid,
        request_id,
        {"boot_id": "old-boot", "generation": old["generation"], "drained": True},
    )
    assert stopped["lifecycle"] == "stopped"
    with pytest.raises(HTTPException):
        await ctf.record_execution(tid, aid, current["id"], current["generation"], "old-boot")


async def test_registration_never_retargets_an_undrained_previous_binding(ctf):
    tid, aid, _, current, _ = await next_writer(ctf, drained=False)
    before = await ctf.state(tid)
    with pytest.raises(HTTPException, match="has not drained"):
        await ctf.record_execution(tid, aid, current["id"], current["generation"], "old-boot")
    assert await ctf.state(tid) == before


async def test_stop_history_links_the_unregistered_turn_and_survives_resume(ctf):
    tid, aid, old, current, request_id = await next_writer(ctf, drained=True)
    proof = {"boot_id": "old-boot", "generation": old["generation"], "drained": True}
    stopped = await ctf.complete_member_operation(tid, aid, request_id, proof)
    operation = stopped["operation_history"][-1]
    assert operation["target_generation"] == old["generation"]
    assert operation["settled_turn_id"] == current["id"]
    assert operation["settled_turn_generation"] == current["generation"]
    before = await ctf.state(tid)
    assert await ctf.complete_member_operation(tid, aid, request_id, proof) == stopped
    assert await ctf.state(tid) == before
    await ctf.request_member_operation(tid, "user", aid, "resume", str(uuid4()))
    resumed = await ctf_turn(ctf, tid, aid)
    assert resumed["generation"] > current["generation"]
    after = await ctf.state(tid)
    member = next(item for item in after["members"] if item["id"] == aid)
    assert operation in member["operation_history"]
    saved = next(item for item in after["turns"] if item["id"] == current["id"])
    assert saved["generation"] == operation["settled_turn_generation"]
    assert saved["end_reason"] == "stopped"
