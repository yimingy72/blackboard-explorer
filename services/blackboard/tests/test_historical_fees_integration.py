"""Historical fees and review identity remain available without a gateway service."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.service import BoardService
from bbx_blackboard.store import schema as s
from bbx_contracts.pi_models import PiModelConfig
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import insert, select, update
from sqlalchemy.engine import make_url
from test_board_flags_integration import Objects
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url

pytestmark = pytest.mark.integration


async def test_offline_old_fee_and_resume_block_keep_original_cny_and_unknown(ctf, ctf_url):
    from pathlib import Path

    engine = ctf.repo.engine
    db = make_url(ctf_url)
    options = settings().model_copy(
        update={
            "postgres_host": db.host,
            "postgres_port": db.port,
            "postgres_user": db.username,
            "postgres_password": SecretStr(db.password or ""),
            "postgres_db": db.database,
            "pi_site_id": "primary",
            "pi_model_authority": "fixture-authority",
            "pi_model_operator": "alice",
        }
    )
    objects = Objects()
    app = create_app(options, objects=objects, dispatcher=AsyncMock())
    await ProfileStore(engine).ensure_bundled(
        Path(__file__).resolve().parents[3] / "profiles/default"
    )
    tid = await BoardService(engine, objects).create_task(
        {
            "goal": "Keep historical values offline",
            "agent_profile": "default",
            "acceptance": [{"id": "A1", "desc": "Read history"}],
            "budget": {"max_cost": "10", "currency": "CNY", "max_minutes": 10},
        }
    )
    attributes = {
        "bbx_site_id": "original-historical-site",
        "bbx_task_id": str(tid),
        "bbx_agent_id": "fixture",
        "bbx_conversation_id": str(uuid4()),
        "bbx_model_alias": "retired-model",
        "bbx_purpose": "review",
    }
    known, unknown = uuid4(), uuid4()
    async with engine.begin() as conn:
        draft = PiModelConfig(
            label="Imported model; settings incomplete",
            upstream_model="fixture-import",
            base_url="https://relay.invalid/v1",
        )
        await conn.execute(
            insert(s.platform_configs).values(
                kind="pi-models",
                name="imported-draft",
                version=1,
                label=draft.label,
                config=draft.model_dump(mode="json", exclude={"label", "enabled"}),
                enabled=True,
                credential_source="stored",
                created_by="fixture-migration",
            )
        )
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                status="stopped",
                usage={"cost": "7.1234"},
            )
        )
        for log_id, attempt, amount, state in (
            ("original-known", known, "0.02", "known"),
            ("original-unknown", unknown, "0.90", "unknown"),
        ):
            await conn.execute(
                insert(s.native_request_costs).values(
                    accounting_source_id="historical-source",
                    native_log_id=log_id,
                    task_id=tid,
                    attempt_id=str(attempt),
                    state=state,
                    cost_usd=amount,
                    evidence={"attributes": attributes},
                    created_at=datetime.now(UTC),
                )
            )
            await conn.execute(
                insert(s.gateway_execution_attempts).values(
                    task_id=tid,
                    id=attempt,
                    purpose="review",
                    accounting_source_id="historical-source",
                    key_name="retired",
                    body_sha256="a" * 64,
                    attributes=attributes,
                    phase="complete",
                    native_log_id=log_id,
                    fee_state=state,
                )
            )
    user = {"Authorization": "Bearer " + issue_user_token(options, "alice")}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        models = await http.get("/api/platform/pi-models", headers=user)
        assert models.status_code == 200 and models.json()["enabled"] is True
        [draft_model] = models.json()["models"]
        assert (
            not draft_model["has_secret"]
            and draft_model["context_window"] is None
            and draft_model["max_output_tokens"] is None
            and draft_model["tool_call"] is None
            and draft_model["usd_rates"] is None
        )
        blocked_model = await http.post(
            "/api/tasks",
            headers=user,
            json={
                "goal": "An incomplete draft cannot send",
                "acceptance": [{"id": "A1", "desc": "No provider call"}],
                "budget": {"max_cost": "1", "currency": "USD", "max_minutes": 10},
                "pi_model": {"model_id": "imported-draft", "model_revision": 1},
            },
        )
        assert blocked_model.status_code == 422
        detail = await http.get(f"/api/tasks/{tid}", headers=user)
        assert detail.status_code == 200, detail.text
        assert detail.json()["execution_blocked_reason"] == "legacy_execution_unmapped"
        assert detail.json()["usage"]["cost"] == "7.1234"
        assert detail.json()["cost_currency"] == "CNY"
        fees = await http.get(f"/api/tasks/{tid}/gateway-model/review-costs", headers=user)
        assert fees.status_code == 200, fees.text
        assert fees.json()["confirmed_usd"] == "0.02" and fees.json()["pending"] == 1
        assert {call["site_id"] for call in fees.json()["calls"]} == {"original-historical-site"}
        refused = await http.post(
            f"/api/tasks/{tid}/resume",
            headers=user,
            json={
                "request_id": str(uuid4()),
                "additional_cost": "0",
                "additional_minutes": 10,
            },
        )
        assert refused.status_code == 409, refused.text
    async with engine.connect() as conn:
        assert await conn.scalar(select(s.tasks.c.run_number).where(s.tasks.c.id == tid)) == 1


async def test_stopped_ctf_member_keeps_original_generation_across_read_only_turns(ctf):
    from bbx_blackboard.gateway_tasks import review_source
    from bbx_blackboard.store import schema as s
    from bbx_blackboard.store.repository import row
    from bbx_contracts.pi_models import PiModelVersion, PiReviewSelection
    from sqlalchemy import insert, update
    from test_ctf_integration import team

    tid = await team(ctf)
    member = await ctf.create_member(tid, "user", "Analyst", str(uuid4()))
    aid = member["id"]
    engine = ctf.repo.engine
    async with engine.begin() as conn:
        task = await row(conn, s.tasks, tid)
    assert task["status"] == "running"
    assert await review_source(engine, task, aid) is None  # Active/idle is not a finished member.
    async with engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid)
            .values(lifecycle="stopped", run_state="stopped", generation=8)
        )
    source = await review_source(engine, task, aid)
    assert source == {"generation": 8, "run_number": 1}
    model = PiReviewSelection(
        authority="fixture-authority",
        pi_model=PiModelVersion.model_validate(
            {
                "id": "fixture-ctf",
                "label": "Fixture",
                "model_revision": 1,
                "upstream_model": "fixture",
                "base_url": "https://fixture.invalid/v1",
                "context_window": 32000,
                "max_output_tokens": 1000,
                "input_modalities": ["text"],
                "tool_call": True,
                "has_secret": True,
            }
        ),
    )
    first, second = str(uuid4()), str(uuid4())
    for message in (first, second):
        await ctf.post_message(
            tid,
            "user",
            aid,
            "What did you find?",
            message,
            review_model=model,
            review_origin=source,
            request_user="fixture-owner",
        )
    turn = await ctf.claim_review_turn(tid, aid, "isolated-review")
    assert turn["generation"] == 9 and turn["review_origin_generation"] == 8
    messages = await ctf.claim_messages(tid, aid, turn["id"], turn["generation"])
    assert [item["id"] for item in messages] == [first]
    async with engine.begin() as conn:
        await ctf.repo.lock(conn, tid)
        await conn.execute(
            insert(s.gateway_execution_attempts).values(
                task_id=tid,
                id=uuid4(),
                purpose="review",
                accounting_source_id="fixture",
                key_name="review",
                body_sha256="d" * 64,
                attributes={"bbx_conversation_id": turn["id"]},
                phase="dispatched",
            )
        )
        current = await row(conn, s.ctf_members, tid, aid)
        await ctf._settle_review(conn, tid, current, turn, "interrupted", "")
    assert await review_source(engine, task, aid) == source
    later = await ctf.claim_review_turn(tid, aid, "isolated-review")
    assert later["generation"] == 10 and later["assignment_message_ids"] == [second]
    async with engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid)
            .values(lifecycle="active", run_state="running", generation=11)
        )
    with pytest.raises(HTTPException):
        await ctf.authorize_review(tid, aid, later["generation"], later["id"])
