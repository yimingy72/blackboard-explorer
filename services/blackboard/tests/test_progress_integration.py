"""Progress transactions against an isolated PostgreSQL container."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.progress import ProgressService
from bbx_blackboard.store import schema as s
from bbx_contracts.models import Evidence, EvidenceType, FactKind, PostFactRequest, Usage
from bbx_contracts.profile import load_profile
from bbx_contracts.progress import (
    ProgressCandidate,
    ProgressCheckpoint,
    ProgressComplete,
    ProgressFence,
    ProgressNode,
    ProgressQuestion,
    ProgressReserve,
    ProgressSettings,
    ProgressSettle,
    ProgressSourceRef,
)
from fastapi import HTTPException
from sqlalchemy import select, update
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url
from test_store_integration import task

pytestmark = pytest.mark.integration


@pytest.fixture
async def progress_task(board_service):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    await ProfileStore(board_service.repo.engine).create("default", profile, "test")
    tid = await task(board_service)
    aid = await board_service.register_agent(tid, "explore")
    await board_service.post_fact(
        tid,
        aid,
        PostFactRequest(
            kind=FactKind.OBSERVATION,
            statement="Baseline fails",
            evidence=[
                Evidence(
                    type=EvidenceType.TEXT, uri="evidence/one", summary="A reproducible observation"
                )
            ],
        ),
    )
    svc = ProgressService(board_service.repo.engine)
    await svc.settings(tid, ProgressSettings(enabled=True, merge_seconds=0, min_interval_seconds=0))
    yield svc, tid
    try:
        await svc.settings(tid, ProgressSettings(enabled=False))
    except HTTPException as error:
        assert error.status_code == 404


async def scan(svc, tid, job, fence):
    after = job["source_version"]
    while True:
        page = await svc.sources(tid, job["job_id"], fence, after, 1)
        after = page["next_version"]
        if not page["has_more"]:
            return page


async def test_serial_queue_source_cutoff_candidates_and_idempotent_publish(
    progress_task, board_service
):
    svc, tid = progress_task
    job = await svc.claim()
    assert job and job["task_id"] == tid
    fence = ProgressFence(claim_token=UUID(job["claim_token"]), run_number=job["run_number"])
    before = await board_service.state(tid)
    assert await svc.claim() is None
    with pytest.raises(HTTPException, match="skip"):
        await svc.sources(tid, job["job_id"], fence, job["through_version"] + 1)
    page = await scan(svc, tid, job, fence)
    assert page["through_version"] == job["through_version"]
    # The source ceiling is immutable even if new solver facts arrive during inference.
    aid = next(iter(before["agents"]))
    await board_service.post_fact(
        tid,
        aid,
        PostFactRequest(
            kind=FactKind.OBSERVATION,
            statement="A later result",
            evidence=[
                Evidence(type=EvidenceType.TEXT, uri="evidence/one", summary="Later evidence")
            ],
        ),
    )
    events = await board_service.events(tid)
    first = next(e for e in events if e["type"] == "fact.posted")
    await svc.candidate(
        tid,
        job["job_id"],
        ProgressCandidate(
            **fence.model_dump(),
            expected_revision=0,
            upserts=[
                ProgressNode(
                    id="finding",
                    parent_id="goal",
                    kind="finding",
                    title="Baseline fails",
                    significance="Separates the failing input family",
                    source_refs=[ProgressSourceRef(kind="fact", id="F1", version=first["version"])],
                )
            ],
        ),
    )
    assert (await svc.view(tid))["revision"] == 0
    with pytest.raises(HTTPException):
        await svc.candidate(
            tid,
            job["job_id"],
            ProgressCandidate(
                **fence.model_dump(),
                expected_revision=0,
                upserts=[
                    ProgressNode(
                        id="foreign",
                        parent_id="goal",
                        kind="finding",
                        title="Bad reference",
                        source_refs=[
                            ProgressSourceRef(kind="fact", id="F2", version=events[-1]["version"])
                        ],
                    )
                ],
            ),
        )
    result = await svc.complete(tid, job["job_id"], ProgressComplete(**fence.model_dump()))
    assert await svc.complete(tid, job["job_id"], ProgressComplete(**fence.model_dump())) == result
    view = await svc.view(tid)
    assert view["source_version"] == job["through_version"]
    assert view["desired_source_version"] > view["source_version"]
    assert len(view["nodes"]) == 2
    after = await board_service.state(tid)
    assert after["last_change_version"] > before["last_change_version"]
    assert after["task"]["usage"] == before["task"]["usage"]
    history = await svc.view(tid, result["published_version"] - 1)
    assert history["revision"] == 0 and len(history["nodes"]) == 1
    assert (await svc.view(tid, result["published_version"]))["revision"] == 1


async def test_reservation_unknown_checkpoint_and_accounting_are_independent(
    progress_task, board_service
):
    svc, tid = progress_task
    job = await svc.claim()
    assert job
    fence = {"claim_token": UUID(job["claim_token"]), "run_number": job["run_number"]}
    request_id = uuid4()
    reserved = await svc.reserve(
        tid,
        job["job_id"],
        ProgressReserve(**fence, request_id=request_id, input_tokens=1000, max_output_tokens=256),
    )
    assert reserved["status"] == "reserved"
    assert (
        await svc.reserve(
            tid,
            job["job_id"],
            ProgressReserve(
                **fence, request_id=request_id, input_tokens=1000, max_output_tokens=256
            ),
        )
        == reserved
    )
    settled = await svc.settle(
        tid,
        job["job_id"],
        ProgressSettle(**fence, request_id=request_id, executed=None, usage_known=False),
    )
    assert settled["status"] == "unknown"
    with pytest.raises(HTTPException, match="confirmation"):
        await svc.refresh(tid, uuid4())
    with pytest.raises(HTTPException, match="confirmation"):
        await svc.question(tid, ProgressQuestion(id=uuid4(), content="A new question"))
    with pytest.raises(HTTPException, match="outcome"):
        await svc.reserve(
            tid,
            job["job_id"],
            ProgressReserve(**fence, request_id=uuid4(), input_tokens=1000, max_output_tokens=256),
        )
    await scan(svc, tid, job, ProgressFence(**fence))
    cp = ProgressCheckpoint(
        **fence,
        session={
            "state": {
                "bbx_progress_receipts": {
                    str(request_id): {
                        "status": "completed",
                        "job_id": str(job["job_id"]),
                        "usage_known": True,
                        "usage": {
                            "cache_hit_tokens": 0,
                            "cache_miss_tokens": 1000,
                            "output_tokens": 100,
                            "reasoning_tokens": 0,
                            "cost": "0",
                        },
                    }
                }
            }
        },
        expected_revision=0,
        source_version=job["through_version"],
    )
    assert (await svc.checkpoint(tid, job["job_id"], cp))["revision"] == 1
    with pytest.raises(HTTPException, match="revision"):
        await svc.checkpoint(tid, job["job_id"], cp)
    receipt = ProgressSettle(
        **fence, request_id=request_id, usage=Usage(cache_miss_tokens=1000, output_tokens=100)
    )
    confirmed = await svc.settle(tid, job["job_id"], receipt)
    assert confirmed["status"] == "settled"
    assert await svc.settle(tid, job["job_id"], receipt) == confirmed
    await svc.complete(tid, job["job_id"], ProgressComplete(**fence))
    view = await svc.view(tid)
    assert float(view["budget"]["unknown"]) == 0
    assert view["budget"]["spent"] == confirmed["cost"]
    assert (await board_service.state(tid))["task"]["usage"].get("cost", "0") == "0"
    async with svc.repo.engine.connect() as conn:
        assert not (
            await conn.execute(
                select(s.agent_runs).where(
                    s.agent_runs.c.task_id == tid, s.agent_runs.c.id == "__progress__"
                )
            )
        ).first()


async def test_expired_lease_keeps_unknown_reservation_and_late_publish_cannot_revive(
    progress_task,
):
    svc, tid = progress_task
    job = await svc.claim()
    assert job
    fence = {"claim_token": UUID(job["claim_token"]), "run_number": job["run_number"]}
    request_id = uuid4()
    await svc.reserve(
        tid,
        job["job_id"],
        ProgressReserve(**fence, request_id=request_id, input_tokens=100, max_output_tokens=256),
    )
    async with svc.repo.engine.begin() as conn:
        data = (
            await conn.execute(
                select(s.progress_jobs.c.data).where(s.progress_jobs.c.id == job["job_id"])
            )
        ).scalar_one()
        data["lease_until"] = "2000-01-01T00:00:00+00:00"
        await conn.execute(
            update(s.progress_jobs).where(s.progress_jobs.c.id == job["job_id"]).values(data=data)
        )
    assert await svc.claim() is None
    view = await svc.view(tid)
    assert view["status"] == "paused" and float(view["budget"]["unknown"]) > 0
    with pytest.raises(HTTPException):
        await svc.complete(tid, job["job_id"], ProgressComplete(**fence))
    # The service can reconcile the original unknown request without making another model call.
    await svc.settle(
        tid, job["job_id"], ProgressSettle(**fence, request_id=request_id, executed=False)
    )
    assert float((await svc.view(tid))["budget"]["unknown"]) == 0


async def test_delete_waits_for_inflight_accounting_and_late_callbacks_do_not_revive(progress_task):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    svc, tid = progress_task
    job = await svc.claim()
    assert job
    fence = {"claim_token": UUID(job["claim_token"]), "run_number": job["run_number"]}
    request_id = uuid4()
    await svc.reserve(
        tid,
        job["job_id"],
        ProgressReserve(**fence, request_id=request_id, input_tokens=100, max_output_tokens=256),
    )
    async with svc.repo.engine.begin() as conn:
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(status="finished"))
        await conn.execute(
            update(s.agent_runs).where(s.agent_runs.c.task_id == tid).values(status="finished")
        )
    archive = await svc.conversations.export_archive(tid)
    assert archive["progress"] is not None and archive["progress_jobs"]
    await svc.conversations.request_delete(tid)
    objects = SimpleNamespace(list=AsyncMock(return_value=[]), remove=AsyncMock())
    with pytest.raises(HTTPException, match="settling"):
        await svc.conversations.purge(tid, objects)
    await svc.settle(
        tid, job["job_id"], ProgressSettle(**fence, request_id=request_id, executed=False)
    )
    assert (await svc.conversations.purge(tid, objects))["purged"]
    with pytest.raises(HTTPException, match="not found"):
        await svc.complete(tid, job["job_id"], ProgressComplete(**fence))


async def test_user_historical_sources_and_global_settings_cas(progress_task):
    import httpx
    from bbx_blackboard.api import create_app
    from test_progress import settings

    svc, tid = progress_task
    config = settings()
    app = create_app(config, engine=svc.repo.engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer service-test"},
        trust_env=False,
    ) as client:
        root = (await client.get(f"/api/tasks/{tid}/progress")).json()
        assert "spent" in root["budget"] and not root["read_only"]
        sources = await client.get(
            f"/api/tasks/{tid}/progress/sources",
            params={"object_id": "F1", "through_version": root["desired_source_version"]},
        )
        assert (
            sources.status_code == 200 and sources.json()["object"]["statement"] == "Baseline fails"
        )
        assert sources.json()["object"]["evidence"][0]["historical_content_verified"] is False
        original = (await client.get("/api/progress/settings")).json()
        value = {
            **original,
            "expected_revision": original["revision"],
            "prompt": "Retain counterexamples",
        }
        saved = await client.put("/api/progress/settings", json=value)
        assert saved.status_code == 200 and saved.json()["revision"] == original["revision"] + 1
        assert (await client.put("/api/progress/settings", json=value)).status_code == 409
        value.update(expected_revision=saved.json()["revision"], enabled_tools=["post_fact"])
        assert (await client.put("/api/progress/settings", json=value)).status_code == 422


async def test_bootstrap_is_bounded_and_backfill_does_not_wait_for_live_interval(
    progress_task, board_service
):
    from bbx_blackboard.domain.rules import event

    svc, tid = progress_task
    source = next(e for e in await board_service.events(tid) if e["type"] == "fact.posted")
    async with svc.repo.engine.begin() as conn:
        await svc.repo.lock(conn, tid)
        await svc.repo.append(
            conn,
            tid,
            [
                event(
                    "fact.posted",
                    source["actor"],
                    {**source["payload"], "id": f"F{i}", "statement": f"Observation {i}"},
                    f"F{i}",
                )
                for i in range(2, 125)
            ],
        )
    await svc.settings(
        tid, ProgressSettings(enabled=True, merge_seconds=0, min_interval_seconds=60)
    )
    job = await svc.claim()
    assert job and job["through_version"] < (await svc.view(tid))["desired_source_version"]
    fence = ProgressFence(claim_token=UUID(job["claim_token"]), run_number=job["run_number"])
    await scan(svc, tid, job, fence)
    await svc.complete(tid, job["job_id"], ProgressComplete(**fence.model_dump()))
    next_job = await svc.claim()
    assert next_job and next_job["through_version"] > job["through_version"]
    next_fence = ProgressFence(
        claim_token=UUID(next_job["claim_token"]), run_number=next_job["run_number"]
    )
    await scan(svc, tid, next_job, next_fence)
    await svc.complete(tid, next_job["job_id"], ProgressComplete(**next_fence.model_dump()))
    assert (await svc.view(tid))["source_version"] == (await svc.view(tid))[
        "desired_source_version"
    ]


async def test_expired_job_recovers_confirmed_response_and_same_completion_without_new_inference(
    progress_task,
):
    svc, tid = progress_task
    job = await svc.claim()
    assert job
    fence = ProgressFence(claim_token=UUID(job["claim_token"]), run_number=job["run_number"])
    rid = uuid4()
    await svc.reserve(
        tid,
        job["job_id"],
        ProgressReserve(
            **fence.model_dump(), request_id=rid, input_tokens=1000, max_output_tokens=256
        ),
    )
    await scan(svc, tid, job, fence)
    usage = Usage(cache_miss_tokens=1000, output_tokens=100).model_dump(mode="json")
    await svc.checkpoint(
        tid,
        job["job_id"],
        ProgressCheckpoint(
            **fence.model_dump(),
            session={
                "state": {
                    "bbx_progress_receipts": {
                        str(rid): {
                            "job_id": str(job["job_id"]),
                            "status": "completed",
                            "usage_known": True,
                            "usage": usage,
                        }
                    },
                    "bbx_progress_completion": {
                        "job_id": str(job["job_id"]),
                        "through_version": job["through_version"],
                        "content": "Saved response",
                    },
                }
            },
            expected_revision=0,
            source_version=job["through_version"],
        ),
    )
    async with svc.repo.engine.begin() as conn:
        data = (
            await conn.execute(
                select(s.progress_jobs.c.data).where(s.progress_jobs.c.id == job["job_id"])
            )
        ).scalar_one()
        data["lease_until"] = "2000-01-01T00:00:00+00:00"
        await conn.execute(
            update(s.progress_jobs).where(s.progress_jobs.c.id == job["job_id"]).values(data=data)
        )
    recovered = await svc.claim()
    assert recovered and recovered["job_id"] == job["job_id"]
    assert float((await svc.view(tid))["budget"]["spent"]) > 0
    assert float((await svc.view(tid))["budget"]["unknown"]) == 0
    result = await svc.complete(tid, job["job_id"], ProgressComplete(**fence.model_dump()))
    assert result["source_version"] == job["through_version"]


async def test_resume_preserves_tool_knowledge_cut_independently_from_scanned_pages(
    progress_task, board_service
):
    from bbx_blackboard.domain.rules import event
    from bbx_contracts.progress import ProgressFail

    svc, tid = progress_task
    source = next(e for e in await board_service.events(tid) if e["type"] == "fact.posted")
    async with svc.repo.engine.begin() as conn:
        await svc.repo.lock(conn, tid)
        await svc.repo.append(
            conn,
            tid,
            [
                event(
                    "fact.posted",
                    source["actor"],
                    {**source["payload"], "id": f"F{i}", "statement": f"Observation {i}"},
                    f"F{i}",
                )
                for i in range(2, 125)
            ],
        )
    job = await svc.claim()
    assert job
    fence = ProgressFence(claim_token=UUID(job["claim_token"]), run_number=job["run_number"])
    first_page = await svc.sources(tid, job["job_id"], fence, 0, 30)
    assert first_page["next_version"] < job["through_version"]
    assert (await svc.view(tid))["session_knowledge_version"] == job["through_version"]
    # The model has seen the fixed cutoff and can read any object through it.
    await svc.checkpoint(
        tid,
        job["job_id"],
        ProgressCheckpoint(
            **fence.model_dump(),
            session={"state": {"saved_answer": "Includes a later object"}},
            expected_revision=0,
            source_version=first_page["next_version"],
            knowledge_version=job["through_version"],
        ),
    )
    await svc.fail(tid, job["job_id"], ProgressFail(**fence.model_dump(), error="Interrupted"))
    await svc.refresh(tid, uuid4())
    resumed = await svc.claim()
    assert resumed and resumed["through_version"] >= job["through_version"]
    next_fence = ProgressFence(
        claim_token=UUID(resumed["claim_token"]), run_number=resumed["run_number"]
    )
    with pytest.raises(HTTPException, match="fully scanned"):
        await svc.complete(
            tid,
            resumed["job_id"],
            ProgressComplete(**next_fence.model_dump(), content="Saved answer"),
        )
    await scan(svc, tid, resumed, next_fence)
    await svc.complete(tid, resumed["job_id"], ProgressComplete(**next_fence.model_dump()))
    assert (await svc.view(tid))["source_version"] >= job["through_version"]


async def test_acceptance_reference_id_and_retraction_invalidate_summary(progress_task):
    from bbx_blackboard.domain.rules import event

    svc, tid = progress_task
    async with svc.repo.engine.begin() as conn:
        await svc.repo.lock(conn, tid)
        events = await svc.repo.append(
            conn,
            tid,
            [
                event(
                    "acceptance.judged",
                    "close-1",
                    {
                        "judge_from_version": 1,
                        "verdicts": [
                            {
                                "id": "A1",
                                "verdict": "met",
                                "reason": "Evidence supports it",
                                "evidence_facts": ["F1"],
                            }
                        ],
                    },
                )
            ],
        )
    async with svc.repo.engine.begin() as conn:
        await svc.repo.lock(conn, tid)
        other = await svc.repo.append(
            conn,
            tid,
            [event("acceptance.judged", "close-1", {"judge_from_version": 1, "verdicts": []})],
        )
    judged = events[0]["version"]
    job = await svc.claim()
    assert job
    fence = ProgressFence(claim_token=UUID(job["claim_token"]), run_number=job["run_number"])
    with pytest.raises(HTTPException, match="ID mismatch"):
        await svc.candidate(
            tid,
            job["job_id"],
            ProgressCandidate(
                **fence.model_dump(),
                expected_revision=0,
                upserts=[
                    ProgressNode(
                        id="finding",
                        parent_id="goal",
                        kind="finding",
                        title="Acceptance met",
                        source_refs=[
                            ProgressSourceRef(kind="acceptance", id="foreign", version=judged)
                        ],
                    )
                ],
            ),
        )
    with pytest.raises(HTTPException, match="ID mismatch"):
        await svc.candidate(
            tid,
            job["job_id"],
            ProgressCandidate(
                **fence.model_dump(),
                expected_revision=0,
                upserts=[
                    ProgressNode(
                        id="wrong-event",
                        parent_id="goal",
                        kind="finding",
                        title="Wrong event",
                        source_refs=[
                            ProgressSourceRef(
                                kind="acceptance", id="A1", version=other[0]["version"]
                            )
                        ],
                    )
                ],
            ),
        )
    await svc.candidate(
        tid,
        job["job_id"],
        ProgressCandidate(
            **fence.model_dump(),
            expected_revision=0,
            upserts=[
                ProgressNode(
                    id="finding",
                    parent_id="goal",
                    kind="finding",
                    title="Acceptance met",
                    source_refs=[ProgressSourceRef(kind="acceptance", id="A1", version=judged)],
                )
            ],
        ),
    )
    await scan(svc, tid, job, fence)
    published = await svc.complete(tid, job["job_id"], ProgressComplete(**fence.model_dump()))
    assert (
        next(n for n in (await svc.view(tid))["nodes"] if n["id"] == "finding")["effective_state"]
        == "supported"
    )
    async with svc.repo.engine.begin() as conn:
        await svc.repo.lock(conn, tid)
        await svc.repo.append(
            conn, tid, [event("acceptance.reverted", "system", {"id": "A1", "fact_id": "F1"}, "A1")]
        )
    assert (
        next(n for n in (await svc.view(tid))["nodes"] if n["id"] == "finding")["effective_state"]
        == "sources_changed"
    )
    assert (
        next(
            n
            for n in (await svc.view(tid, published["published_version"]))["nodes"]
            if n["id"] == "finding"
        )["effective_state"]
        == "supported"
    )


async def test_explicit_question_recovers_pause_and_final_failure_marks_message(progress_task):
    from bbx_contracts.progress import ProgressFail, ProgressQuestion

    svc, tid = progress_task
    job = await svc.claim()
    assert job
    fence = ProgressFence(claim_token=UUID(job["claim_token"]), run_number=job["run_number"])
    await svc.fail(
        tid, job["job_id"], ProgressFail(**fence.model_dump(), error="Malformed candidate")
    )
    assert (await svc.view(tid))["status"] == "paused"
    mid = uuid4()
    await svc.question(tid, ProgressQuestion(id=mid, content="What remains?"))
    question = await svc.claim()
    assert question and question["job_id"] == mid
    qfence = ProgressFence(
        claim_token=UUID(question["claim_token"]), run_number=question["run_number"]
    )
    await svc.fail(tid, mid, ProgressFail(**qfence.model_dump(), error="Provider failure"))
    message = next(m for m in (await svc.messages(tid))["messages"] if m["id"] == mid)
    assert message["status"] == "failed" and message["error"] == "Provider failure"
    next_id = uuid4()
    await svc.question(tid, ProgressQuestion(id=next_id, content="Retry the question"))
    next_job = await svc.claim()
    assert next_job and next_job["job_id"] == next_id
    async with svc.repo.engine.begin() as conn:
        data = (
            await conn.execute(
                select(s.progress_jobs.c.data).where(s.progress_jobs.c.id == next_id)
            )
        ).scalar_one()
        data["lease_until"] = "2000-01-01T00:00:00+00:00"
        await conn.execute(
            update(s.progress_jobs).where(s.progress_jobs.c.id == next_id).values(data=data)
        )
    await svc.claim()
    failed = next(m for m in (await svc.messages(tid))["messages"] if m["id"] == next_id)
    assert failed["status"] == "failed" and failed["error"]


async def test_missing_reservation_is_404_and_nonexecution_keeps_session_cas(progress_task):
    svc, tid = progress_task
    job = await svc.claim()
    assert job
    fence = ProgressFence(claim_token=UUID(job["claim_token"]), run_number=job["run_number"])
    missing_id = uuid4()
    with pytest.raises(HTTPException) as missing:
        await svc.settle(
            tid,
            job["job_id"],
            ProgressSettle(**fence.model_dump(), request_id=missing_id, executed=False),
        )
    assert missing.value.status_code == 404
    page = await svc.sources(tid, job["job_id"], fence, 0)
    request_id = uuid4()
    session = {
        "state": {
            "bbx_progress_active_request": {"request_id": str(request_id), "status": "preparing"}
        }
    }
    await svc.checkpoint(
        tid,
        job["job_id"],
        ProgressCheckpoint(
            **fence.model_dump(),
            session=session,
            expected_revision=0,
            source_version=page["next_version"],
        ),
    )
    await svc.reserve(
        tid,
        job["job_id"],
        ProgressReserve(
            **fence.model_dump(), request_id=request_id, input_tokens=1, max_output_tokens=256
        ),
    )
    await svc.settle(
        tid,
        job["job_id"],
        ProgressSettle(**fence.model_dump(), request_id=request_id, executed=False),
    )
    async with svc.repo.engine.connect() as conn:
        saved = await svc.conversations._session(conn, tid, "__progress__")
        assert saved and saved["revision"] == 1 and saved["session"] == session


async def test_task_source_uses_created_event_at_cutoff_not_current_projection(progress_task):
    import httpx
    from bbx_blackboard.api import create_app
    from test_progress import settings

    svc, tid = progress_task
    async with svc.repo.engine.begin() as conn:
        created = (
            (
                await conn.execute(
                    select(s.events).where(
                        s.events.c.task_id == tid, s.events.c.type == "task.created"
                    )
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                goal="Later goal",
                domain_context="Later context",
                acceptance=[{"id": "A1", "desc": "Later criterion"}],
            )
        )
    app = create_app(settings(), engine=svc.repo.engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer service-test"},
        trust_env=False,
    ) as client:
        path = f"/api/tasks/{tid}/progress/sources"
        result = await client.get(
            path, params={"object_id": str(tid), "through_version": created["version"]}
        )
        assert result.status_code == 200
        value = result.json()["object"]
        assert value["id"] == str(tid)
        assert value["goal"] == value["statement"] == created["payload"]["goal"]
        assert value["acceptance"] == created["payload"]["acceptance"]
        assert value["domain_context"] == created["payload"]["domain_context"]
        assert value["version"] == value["created_version"] == created["version"]
        assert (
            await client.get(
                path, params={"object_id": str(tid), "through_version": created["version"] - 1}
            )
        ).status_code == 404
        assert (
            await client.get(
                path, params={"object_id": str(uuid4()), "through_version": created["version"]}
            )
        ).status_code == 404


async def test_terminal_claim_skips_caught_up_rows_and_keeps_updates_and_questions(
    progress_task, board_service, monkeypatch
):
    from unittest.mock import AsyncMock

    from bbx_blackboard.domain.rules import event

    svc, tid = progress_task
    await board_service.transition(tid, "stopped")
    final_job = await svc.claim()
    assert final_job
    fence = ProgressFence(
        claim_token=UUID(final_job["claim_token"]), run_number=final_job["run_number"]
    )
    await scan(svc, tid, final_job, fence)
    await svc.complete(tid, final_job["job_id"], ProgressComplete(**fence.model_dump()))
    read_state = AsyncMock(wraps=svc.state)
    monkeypatch.setattr(svc, "state", read_state)
    assert await svc.claim() is None
    # Every row selected FOR UPDATE enters state(); no terminal row was selected or locked.
    read_state.assert_not_awaited()
    async with svc.repo.engine.begin() as conn:
        await svc.repo.lock(conn, tid)
        added = await svc.repo.append(
            conn, tid, [event("task.report", "system", {"uri": f"reports/{tid}.md"})]
        )
    job = await svc.claim()
    assert job and job["task_id"] == tid and job["through_version"] >= added[0]["version"]
    read_state.assert_awaited()
    next_fence = ProgressFence(claim_token=UUID(job["claim_token"]), run_number=job["run_number"])
    await scan(svc, tid, job, next_fence)
    await svc.complete(tid, job["job_id"], ProgressComplete(**next_fence.model_dump()))
    question_id = uuid4()
    await svc.question(tid, ProgressQuestion(id=question_id, content="Explain the final result"))
    question_job = await svc.claim()
    assert question_job and question_job["job_id"] == question_id


async def test_registered_foreign_evidence_uri_cannot_grant_cross_task_reads(
    progress_task, board_service
):
    import hashlib
    from unittest.mock import AsyncMock

    import httpx
    from bbx_blackboard.api import create_app
    from bbx_blackboard.auth import issue_progress_token
    from test_progress import settings

    svc, tid = progress_task
    aid = next(iter((await board_service.state(tid))["agents"]))
    data = b"persisted evidence bytes"
    digest = hashlib.sha256(data).hexdigest()
    foreign_uri = f"traces/{uuid4()}/agent-1/proof.txt"
    own_uri = f"traces/{tid}/agent-1/proof.txt"
    # Legacy/general Fact registration accepts any existing ordinary object URI.
    for uri in (foreign_uri, own_uri):
        await board_service.post_fact(
            tid,
            aid,
            PostFactRequest(
                kind=FactKind.OBSERVATION,
                statement="Registered bytes",
                evidence=[
                    Evidence(
                        type=EvidenceType.TEXT,
                        summary="Proof",
                        uri=uri,
                        sha256=digest,
                        size=len(data),
                    )
                ],
            ),
        )
    events = {
        e["payload"]["evidence"][0]["uri"]: e
        for e in await board_service.events(tid)
        if e["type"] == "fact.posted"
    }
    job = await svc.claim()
    assert job
    fence = ProgressFence(claim_token=UUID(job["claim_token"]), run_number=job["run_number"])
    config = settings()

    class EvidenceObjects:
        def __init__(self):
            self.get = AsyncMock(return_value=data)

        async def exists(self, uri: str) -> bool:
            return True

    objects = EvidenceObjects()
    app = create_app(config, engine=svc.repo.engine, objects=objects)
    token = issue_progress_token(config, tid, job["job_id"], fence.claim_token, fence.run_number)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
    ) as client:
        endpoint = f"/api/tasks/{tid}/progress/evidence"
        denied = await client.get(
            endpoint,
            params={
                "version": events[foreign_uri]["version"],
                "uri": foreign_uri,
                "sha256": digest,
            },
        )
        assert denied.status_code == 403
        objects.get.assert_not_awaited()
        allowed = await client.get(
            endpoint,
            params={"version": events[own_uri]["version"], "uri": own_uri, "sha256": digest},
        )
        assert allowed.status_code == 200 and allowed.json()["content"] == data.decode()
        objects.get.assert_awaited_once_with(own_uri)
    with pytest.raises(HTTPException) as foreign_ref:
        await svc.candidate(
            tid,
            job["job_id"],
            ProgressCandidate(
                **fence.model_dump(),
                expected_revision=0,
                upserts=[
                    ProgressNode(
                        id="foreign-bytes",
                        parent_id="goal",
                        kind="finding",
                        title="Foreign bytes",
                        source_refs=[
                            ProgressSourceRef(
                                kind="evidence",
                                id="F2",
                                version=events[foreign_uri]["version"],
                                evidence_uri=foreign_uri,
                                sha256=digest,
                            )
                        ],
                    )
                ],
            ),
        )
    assert foreign_ref.value.status_code == 403
    await svc.candidate(
        tid,
        job["job_id"],
        ProgressCandidate(
            **fence.model_dump(),
            expected_revision=0,
            upserts=[
                ProgressNode(
                    id="own-bytes",
                    parent_id="goal",
                    kind="finding",
                    title="Own bytes",
                    source_refs=[
                        ProgressSourceRef(
                            kind="evidence",
                            id="F3",
                            version=events[own_uri]["version"],
                            evidence_uri=own_uri,
                            sha256=digest,
                        )
                    ],
                )
            ],
        ),
    )


async def test_initial_evidence_requires_created_manifest_and_bound_input_group(
    progress_task, board_service
):
    from datetime import UTC, datetime, timedelta

    svc, tid = progress_task
    other = await task(board_service)
    gid, fid = uuid4(), uuid4()
    uri = f"inputs/{gid}/{fid}/proof.txt"
    digest = "a" * 64
    attachment = {
        "id": str(fid),
        "uri": uri,
        "sha256": digest,
        "size": 1,
        "filename": "proof.txt",
        "path": f"/workspace/shared/inputs/{fid}/proof.txt",
    }
    async with svc.repo.engine.begin() as conn:
        created = (
            (
                await conn.execute(
                    select(s.events).where(
                        s.events.c.task_id == tid, s.events.c.type == "task.created"
                    )
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(
            s.task_input_groups.insert().values(
                id=gid,
                owner="test",
                expires_at=datetime.now(UTC) + timedelta(days=1),
                files=[attachment],
                bound_task_id=tid,
            )
        )
        with pytest.raises(HTTPException, match="historical attachment manifest"):
            await svc.authorize_evidence_uri(conn, tid, uri, digest, created["version"])
        await conn.execute(
            update(s.events)
            .where(s.events.c.version == created["version"])
            .values(payload={**created["payload"], "initial_attachments": [attachment]})
        )
        await svc.authorize_evidence_uri(conn, tid, uri, digest, created["version"])
        await conn.execute(
            update(s.task_input_groups)
            .where(s.task_input_groups.c.id == gid)
            .values(bound_task_id=other)
        )
        with pytest.raises(HTTPException, match="not bound"):
            await svc.authorize_evidence_uri(conn, tid, uri, digest, created["version"])
        with pytest.raises(HTTPException, match="does not belong"):
            await svc.authorize_evidence_uri(
                conn, tid, f"evidence/{tid}/../{other}/proof.txt", digest, created["version"]
            )
