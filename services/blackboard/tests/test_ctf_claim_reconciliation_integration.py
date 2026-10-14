"""Lost-ACK reclaims cannot create an orphan after their observed turn finishes."""

from uuid import uuid4

import pytest
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_turn, team
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_review_integration import terminal
from test_ctf_review_settlement_recovery_integration import saved_review

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("review", [False, True])
async def test_expected_reclaim_requires_the_observed_current_turn_and_never_creates_another(
    ctf, review
):
    if review:
        tid, aid = await terminal(ctf)
        old, _ = await saved_review(ctf, tid, aid)
        claim = ctf.claim_review_turn
        instance = "joined-runtime"
    else:
        tid = await team(ctf)
        aid = "lead"
        old = await ctf_turn(ctf, tid)
        claim = ctf.claim_turn
        instance = "budget-test"
    same = await claim(tid, aid, instance, expected_turn_id=old["id"])
    assert same["id"] == old["id"]
    assert await claim(tid, aid, instance, expected_turn_id=str(uuid4())) is None
    await ctf.finish_turn(tid, aid, old["id"], old["generation"], "completed", "Saved arithmetic")
    await ctf.post_message(tid, "user", aid, "A newer arithmetic assignment", str(uuid4()))
    before = await ctf.state(tid)
    assert await claim(tid, aid, instance, expected_turn_id=old["id"]) is None
    assert await ctf.state(tid) == before
    current = await claim(tid, aid, instance)
    assert current["id"] != old["id"]
    after = await ctf.state(tid)
    assert len(after["turns"]) == len(before["turns"]) + 1
    # A different current writer must also survive a stale observed-turn reclaim.
    assert await claim(tid, aid, instance, expected_turn_id=old["id"]) is None
    assert await ctf.state(tid) == after
