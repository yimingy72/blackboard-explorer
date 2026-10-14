"""Real PostgreSQL delivery atomicity and challenge-scoped evidence checks."""

import hashlib
from typing import Any
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from bbx_contracts.ctf import CtfRecord
from ctf_solution_fixtures import solution
from fastapi import HTTPException
from sqlalchemy import func, select
from test_ctf_candidate_delivery_integration import owned_challenge
from test_ctf_integration import ArtifactObjects
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url

pytestmark = pytest.mark.integration


def delivery(challenge, **patch):
    return dict(
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=challenge["revision"],
        work_status="completed",
        solution=solution(),
        **patch,
    )


async def snapshot(ctf, tid, challenge):
    async with ctf.repo.engine.connect() as conn:
        count = (
            await conn.execute(
                select(func.count())
                .select_from(s.events)
                .where(
                    s.events.c.task_id == tid,
                )
            )
        ).scalar_one()
    return (
        await ctf.get_challenge(tid, challenge["id"]),
        await ctf.list_records(tid, challenge["id"]),
        count,
    )


@pytest.mark.parametrize("candidate", [True, False])
async def test_missing_solution_has_no_events_records_or_status_side_effects(ctf, candidate):
    tid, owner, _, challenge = await owned_challenge(ctf)
    before = await snapshot(ctf, tid, challenge)
    arguments = delivery(challenge)
    arguments.pop("solution")
    with pytest.raises(HTTPException) as exc:
        if candidate:
            await ctf.record_candidate(tid, **owner, **arguments, status="unknown", summary="Done")
        else:
            await ctf.update_challenge(tid, **owner, **arguments, action="set_status")
    assert exc.value.status_code == 422
    assert await snapshot(ctf, tid, challenge) == before


async def test_status_delivery_reopen_and_new_delivery_preserve_old_solution(ctf):
    tid, owner, _, challenge = await owned_challenge(ctf)
    note = await ctf.append_record(
        tid,
        **owner,
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        body="Sample inspected",
        progress={"stage": "attempt", "title": "Inspect header", "summary": "Format unresolved"},
    )
    report = solution()
    report["steps"][0]["record_ids"] = [note["id"]]
    arguments = delivery(challenge)
    arguments["solution"] = report
    result = await ctf.update_challenge(tid, **owner, **arguments, action="set_status")
    assert await ctf.update_challenge(tid, **owner, **arguments, action="set_status") == result
    records = (await ctf.list_records(tid, challenge["id"]))["records"]
    assert len(records) == 2
    saved = CtfRecord.model_validate(records[1])
    assert str(saved.id) == result["solution_record_id"]
    assert saved.kind == "solution" and saved.author_id == owner["actor"]
    assert saved.solution_challenge_revision == result["revision"]
    assert saved.solution_run_number == 1
    assert saved.solution_generation == owner["generation"]
    assert str(saved.solution_turn_id) == owner["turn_id"]
    assert result["verification"] == {}
    with pytest.raises(HTTPException, match="Request ID conflict"):
        await ctf.update_challenge(
            tid,
            **owner,
            **{**arguments, "solution": solution()},
            action="set_status",
        )
    reopened = await ctf.update_challenge(
        tid,
        **owner,
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=result["revision"],
        action="reopen",
    )
    assert reopened["solution_record_id"] is None
    assert (await ctf.list_records(tid, challenge["id"]))["records"] == records
    before = await snapshot(ctf, tid, challenge)
    with pytest.raises(HTTPException) as exc:
        await ctf.update_challenge(tid, **owner, **delivery(challenge), action="set_status")
    detail: Any = exc.value.detail
    assert detail["code"] == "revision_conflict"
    assert await snapshot(ctf, tid, challenge) == before
    completed = await ctf.update_challenge(tid, **owner, **delivery(reopened), action="set_status")
    assert completed["solution_record_id"] != result["solution_record_id"]
    assert (await ctf.list_records(tid, challenge["id"]))["records"][:2] == records
    await ctf.repo.replay(tid)
    assert await ctf.get_challenge(tid, challenge["id"]) == completed


async def registered_artifact(ctf, tid, identity):
    ctf.objects = ArtifactObjects()
    content = b"synthetic reproducible inspection"
    uri = f"evidence/{tid}/{uuid4()}/inspection.txt"
    ctf.objects.files[uri] = content
    return await ctf.register_artifact(
        tid,
        agent_id=identity["actor"],
        generation=identity["generation"],
        turn_id=identity["turn_id"],
        request_id=str(uuid4()),
        path="/workspace/shared/inspection.txt",
        uri=uri,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        filename="inspection.txt",
    )


@pytest.mark.parametrize("candidate", [True, False])
async def test_foreign_record_and_exclusive_artifact_references_are_rejected(ctf, candidate):
    tid, owner, _, challenge = await owned_challenge(ctf)
    other = await ctf.create_challenge(tid, **owner, request_id=str(uuid4()), title="Other")
    artifact = await registered_artifact(ctf, tid, owner)
    note = await ctf.append_record(
        tid,
        "user",
        challenge_id=other["id"],
        request_id=str(uuid4()),
        body="Other work",
        artifact_ids=[artifact["id"]],
    )
    other_tid, other_owner, _, other_challenge = await owned_challenge(ctf)
    foreign = await ctf.append_record(
        other_tid,
        **other_owner,
        challenge_id=other_challenge["id"],
        request_id=str(uuid4()),
        body="Another task",
    )
    before = await snapshot(ctf, tid, challenge)
    with pytest.raises(HTTPException, match="exclusively to another challenge"):
        await ctf.append_record(
            tid,
            **owner,
            challenge_id=challenge["id"],
            request_id=str(uuid4()),
            body="Attempt to relabel foreign evidence",
            artifact_ids=[artifact["id"]],
        )
    assert await snapshot(ctf, tid, challenge) == before
    for record_ids, artifact_ids in [
        ([note["id"]], []),
        ([foreign["id"]], []),
        ([], [artifact["id"]]),
        ([], [str(uuid4())]),
    ]:
        report = solution()
        report["steps"][0].update(record_ids=record_ids, artifact_ids=artifact_ids)
        arguments = {**delivery(challenge), "solution": report}
        with pytest.raises(HTTPException):
            if candidate:
                await ctf.record_candidate(
                    tid, **owner, **arguments, status="unknown", summary="Done"
                )
            else:
                await ctf.update_challenge(tid, **owner, **arguments, action="set_status")
        assert await snapshot(ctf, tid, challenge) == before


async def test_new_registered_file_candidate_solution_and_status_commit_together(ctf):
    tid, owner, _, challenge = await owned_challenge(ctf)
    artifact = await registered_artifact(ctf, tid, owner)
    report = solution()
    report["steps"][0]["artifact_ids"] = [artifact["id"]]
    arguments = {**delivery(challenge), "solution": report, "evidence_refs": [artifact["id"]]}
    result = await ctf.record_candidate(tid, **owner, **arguments, status="unknown", summary="Done")
    current = await ctf.get_challenge(tid, challenge["id"])
    records = (await ctf.list_records(tid, challenge["id"]))["records"]
    assert len(records) == 2 and {record["kind"] for record in records} == {
        "solution",
        "verification",
    }
    saved = next(record for record in records if record["kind"] == "solution")
    assert saved["id"] == current["solution_record_id"]
    assert saved["artifact_refs"] == [artifact]
    assert current["work_status"] == "completed" and current["verification"]["status"] == "unknown"
    assert (
        await ctf.record_candidate(tid, **owner, **arguments, status="unknown", summary="Done")
        == result
    )
    assert (await ctf.list_records(tid, challenge["id"]))["records"] == records


async def test_collaborator_cannot_complete_and_cancel_needs_no_solution(ctf):
    tid, owner, helper, challenge = await owned_challenge(ctf)
    challenge = await ctf.update_challenge(
        tid,
        "user",
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=challenge["revision"],
        action="collaborators",
        collaborator_ids=[helper["actor"]],
    )
    before = await snapshot(ctf, tid, challenge)
    with pytest.raises(HTTPException, match="Only the owner or Lead"):
        await ctf.update_challenge(tid, **helper, **delivery(challenge), action="set_status")
    assert await snapshot(ctf, tid, challenge) == before
    with pytest.raises(HTTPException):
        await ctf.update_challenge(
            tid,
            **{**owner, "generation": owner["generation"] + 1},
            **delivery(challenge),
            action="set_status",
        )
    assert await snapshot(ctf, tid, challenge) == before
    cancelled = await ctf.update_challenge(
        tid,
        **owner,
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=challenge["revision"],
        action="set_status",
        work_status="cancelled",
    )
    assert cancelled["work_status"] == "cancelled"


async def test_record_readback_is_exact_and_scoped_to_task_and_challenge(ctf):
    tid, owner, _, challenge = await owned_challenge(ctf)
    note = await ctf.append_record(
        tid,
        **owner,
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        body="Original note",
    )
    completed = await ctf.update_challenge(tid, **owner, **delivery(challenge), action="set_status")
    saved = await ctf.get_record(tid, challenge["id"], completed["solution_record_id"])
    assert saved["solution"] == solution()
    assert await ctf.get_record(tid, challenge["id"], note["id"]) == note
    other = await ctf.create_challenge(tid, **owner, request_id=str(uuid4()), title="Other")
    other_tid, _, _, other_challenge = await owned_challenge(ctf)
    for task_id, challenge_id, record_id in (
        (tid, other["id"], note["id"]),
        (other_tid, other_challenge["id"], note["id"]),
        (tid, challenge["id"], str(uuid4())),
        (tid, str(uuid4()), note["id"]),
    ):
        with pytest.raises(HTTPException) as exc:
            await ctf.get_record(task_id, challenge_id, record_id)
        assert exc.value.status_code == 404


async def test_delivery_batches_reference_queries_and_preserves_file_order(ctf):
    from sqlalchemy import event

    tid, owner, _, challenge = await owned_challenge(ctf)
    first = await registered_artifact(ctf, tid, owner)
    second = await registered_artifact(ctf, tid, owner)
    notes = []
    for artifact in (first, second):
        notes.append(
            await ctf.append_record(
                tid,
                **owner,
                challenge_id=challenge["id"],
                request_id=str(uuid4()),
                body="Synthetic detailed evidence " * 100,
                artifact_ids=[artifact["id"]],
            )
        )
    report = solution()
    report["steps"][0].update(
        record_ids=[notes[1]["id"], notes[0]["id"], notes[1]["id"]],
        artifact_ids=[second["id"], first["id"], second["id"]],
    )
    report["steps"].append({**report["steps"][0], "title": "Repeat inspection"})
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "ctf_records" in statement:
            queries.append(statement)

    event.listen(ctf.repo.engine.sync_engine, "before_cursor_execute", capture)
    try:
        completed = await ctf.update_challenge(
            tid,
            **owner,
            **{**delivery(challenge), "solution": report},
            action="set_status",
        )
    finally:
        event.remove(ctf.repo.engine.sync_engine, "before_cursor_execute", capture)
    assert len(queries) == 3
    record_query, registration_query, association_query = queries
    assert "ctf_records.id IN" in record_query and "AS challenge_id" in record_query
    assert "ctf_records.id IN" in registration_query and "AS artifact" in registration_query
    assert "AS challenge_id" in association_query and "AS artifact_refs" in association_query
    assert "@>" in association_query and " OR " in association_query
    for query in queries:
        projection = query.split("FROM", 1)[0]
        assert "ctf_records.data AS " not in projection
        assert "SELECT ctf_records.data \n" not in projection
    saved = await ctf.get_record(tid, challenge["id"], completed["solution_record_id"])
    assert saved["artifact_refs"] == [second, first]
    assert saved["solution"] == report
