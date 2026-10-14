"""CTF template publication is atomic, CAS-protected, and leaves old snapshots intact."""

from copy import deepcopy

import pytest
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.ctf_profile_publish import FAMILIES, publish_ctf_profiles
from bbx_blackboard.profiles import ProfileStore, ctf_profile
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine
from test_ctf_integration import ctf_url as ctf_url
from test_ctf_profile_publish import profile_rows

pytestmark = pytest.mark.integration


@pytest.fixture
async def publication_store(ctf_url):
    engine = create_async_engine(ctf_url)
    store = ProfileStore(engine)
    try:
        initial = {
            name: await store.create(name, ctf_profile(row), "synthetic-seed")
            for name, row in profile_rows().items()
        }
        yield store, initial
    finally:
        await engine.dispose()


async def test_dry_run_is_read_only_and_apply_preserves_old_tasks_sessions_and_profile_versions(
    publication_store,
):
    store, initial = publication_store
    expected = {name: row["version"] for name, row in initial.items()}
    ctf = CtfService(store.engine)
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Synthetic retained task",
            "budget": {"max_cost": "10", "max_minutes": 5},
            "agent_profile": "ctf-task-settings",
        },
        profile_version=expected["ctf-task-settings"],
    )
    await ctf.provision(tid)
    await ctf.start(tid)
    turn = await ctf.claim_turn(tid, "lead", "synthetic-profile-test")
    assert turn is not None
    await ctf.checkpoint(
        tid,
        "lead",
        turn["id"],
        turn["generation"],
        {"session_id": "synthetic-session", "state": {"in_memory": {"messages": []}}},
        "Synthetic original opening instructions",
        0,
        [],
    )
    prior_task = deepcopy((await ctf.state(tid))["task"])
    prior_session = await ctf.sessions.get_session(tid, "lead")
    planned = await publish_ctf_profiles(
        store, context_threshold=606784, reserved_output_tokens=393216
    )
    assert planned["applied"] is False
    assert {name: (await store.get(name))["version"] for name in FAMILIES} == expected
    applied = await publish_ctf_profiles(
        store,
        apply=True,
        expected_versions=expected,
        context_threshold=606784,
        reserved_output_tokens=393216,
    )
    assert applied["applied"] is True
    assert applied["profiles"] == planned["profiles"]
    for name, row in initial.items():
        assert await store.get(name, row["version"]) == row
    assert (await ctf.state(tid))["task"] == prior_task
    assert await ctf.sessions.get_session(tid, "lead") == prior_session
    latest = {name: (await store.get(name))["version"] for name in FAMILIES}
    repeated = await publish_ctf_profiles(store, apply=True, expected_versions=latest)
    assert not any(profile["changed"] for profile in repeated["profiles"].values())


async def test_apply_requires_explicit_reviewed_versions(publication_store):
    store, initial = publication_store
    with pytest.raises(ValueError, match="expected"):
        await publish_ctf_profiles(store, apply=True)
    assert {name: (await store.get(name))["version"] for name in FAMILIES} == {
        name: row["version"] for name, row in initial.items()
    }


@pytest.mark.parametrize("family", FAMILIES)
async def test_concurrent_edit_rejects_entire_publication_instead_of_overwriting(
    publication_store, monkeypatch, family
):
    store, initial = publication_store
    expected = {name: row["version"] for name, row in initial.items()}
    original = store.create_in_connection
    edited = ctf_profile(initial[family])
    edited.exec_image = "synthetic-concurrent-image:latest"
    concurrent = None

    async def race(conn, name, profile, actor, **kwargs):
        nonlocal concurrent
        if concurrent is None:
            other = ProfileStore(store.engine)
            concurrent = await other.create(
                family, edited, "synthetic-concurrent-editor", expected_version=expected[family]
            )
        return await original(conn, name, profile, actor, **kwargs)

    monkeypatch.setattr(store, "create_in_connection", race)
    with pytest.raises(HTTPException) as conflict:
        await publish_ctf_profiles(store, apply=True, expected_versions=expected)
    assert conflict.value.status_code == 409
    assert concurrent is not None
    assert await store.get(family) == concurrent
    other_family = next(name for name in FAMILIES if name != family)
    assert await store.get(other_family) == initial[other_family]
