"""Historical review usage reconciles only persisted original writer identities."""

from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_review_integration import terminal
from test_ctf_review_settlement_recovery_integration import saved_review

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def historical_usage(ctf):
    tid, aid = await terminal(ctf)
    old, _ = await saved_review(ctf, tid, aid, receipt=False)
    saved = await ctf.sessions.get_session(tid, aid)
    entry = saved["session"]["state"]["ctf_review_usage_outbox"][0]
    await ctf.recover_reviews(tid, "new-runtime")
    await ctf.post_message(tid, "user", aid, "Review a second arithmetic result", str(uuid4()))
    current = await ctf.claim_review_turn(tid, aid, "new-runtime")
    return tid, aid, old, current, saved, entry


@pytest.mark.parametrize("legacy", [False, True])
async def test_review_outbox_keeps_old_and_current_receipts_on_their_original_ledgers(ctf, legacy):
    tid, aid, old, current, saved, entry = await historical_usage(ctf)
    current_entry = {
        **entry,
        "request_id": str(uuid4()),
        "turn_id": current["id"],
        "generation": current["generation"],
        "reservation_id": f"{current['id']}:1",
        "usage": {"cost": "0.11", "output_tokens": 3},
    }
    snapshot = deepcopy(saved["session"])
    state = snapshot["state"]
    state["ctf_steps"][current["id"]] = 1
    state["ctf_review_usage_outbox"] = [entry, current_entry]
    if legacy:
        for item in state["ctf_review_usage_outbox"]:
            for field in ("turn_id", "agent_id", "generation"):
                item.pop(field)
    await ctf.checkpoint(
        tid, aid, current["id"], current["generation"], snapshot, "Preserved", saved["revision"], []
    )
    await ctf.bill_usage(
        tid,
        aid,
        current["id"],
        current["generation"],
        current_entry["request_id"],
        current_entry["usage"],
    )
    before = await ctf.state(tid)
    first = await ctf.reconcile_usage_outbox(tid, aid)
    second = await ctf.reconcile_usage_outbox(tid, aid)
    assert first == second == {"request_ids": [entry["request_id"], current_entry["request_id"]]}
    after = await ctf.state(tid)
    old_ledger = next(turn for turn in after["turns"] if turn["id"] == old["id"])
    current_ledger = next(turn for turn in after["turns"] if turn["id"] == current["id"])
    assert old_ledger["usage"]["cost"] == "0.25"
    assert old_ledger["usage_requests"] == [entry["request_id"]]
    assert current_ledger["usage"]["cost"] == "0.11"
    assert current_ledger["usage_requests"] == [current_entry["request_id"]]
    assert after["task"]["ctf_control"]["review_usage"]["cost"] == "0.36"
    assert after["task"]["usage"] == before["task"]["usage"]
    assert after["members"] == before["members"]
    assert (await ctf.sessions.get_session(tid, aid))["session"] == snapshot


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", -1),
        ("generation", True),
        ("agent_id", "other"),
        ("turn_id", str(uuid4())),
        ("request_id", ""),
        ("request_id", None),
        ("request_id", 1),
    ],
)
async def test_review_outbox_rejects_persisted_identity_mismatch_without_changing_fees(
    ctf, field, value
):
    tid, aid, _, current, saved, entry = await historical_usage(ctf)
    snapshot = deepcopy(saved["session"])
    snapshot["state"]["ctf_review_usage_outbox"] = [{**entry, field: value}]
    await ctf.checkpoint(
        tid, aid, current["id"], current["generation"], snapshot, "Preserved", saved["revision"], []
    )
    before = await ctf.state(tid)
    with pytest.raises(HTTPException):
        await ctf.reconcile_usage_outbox(tid, aid)
    assert await ctf.state(tid) == before


async def test_review_outbox_cannot_alias_receipt_belonging_to_another_turn(ctf):
    tid, aid, old, current, saved, entry = await historical_usage(ctf)
    assert await ctf.reconcile_usage_outbox(tid, aid) == {"request_ids": [entry["request_id"]]}
    snapshot = deepcopy(saved["session"])
    state = snapshot["state"]
    state["ctf_steps"][current["id"]] = 1
    state["ctf_review_usage_outbox"] = [
        {
            **entry,
            "turn_id": current["id"],
            "generation": current["generation"],
            "reservation_id": f"{current['id']}:1",
        }
    ]
    await ctf.checkpoint(
        tid, aid, current["id"], current["generation"], snapshot, "Preserved", saved["revision"], []
    )
    before = await ctf.state(tid)
    with pytest.raises(HTTPException, match="Usage request belongs to another turn"):
        await ctf.reconcile_usage_outbox(tid, aid)
    assert await ctf.state(tid) == before
    assert (
        next(turn for turn in before["turns"] if turn["id"] == old["id"])["usage"]["cost"] == "0.25"
    )
    assert next(turn for turn in before["turns"] if turn["id"] == current["id"])["usage"] == {}
