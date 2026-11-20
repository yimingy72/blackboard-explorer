"""Real transaction races for first work, peer capacity and stable bindings."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from bbx_blackboard.auth import Principal
from bbx_blackboard.challenge_board import ChallengeBoardService
from bbx_blackboard.collaboration_board import CollaborationBoardService
from bbx_blackboard.collaboration_review import CollaborationReview
from bbx_blackboard.flag_board import FlagBoardService
from bbx_blackboard.gateway_auth import GatewayIdentity, RuntimeBusinessContext
from bbx_blackboard.native_worker_pool import NativeWorkerPool
from bbx_blackboard.runner_control import fence, row
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import BOARD_CONTRACT, business_manifest
from bbx_contracts.ctf import CtfChallenge
from bbx_contracts.ctf_collaboration import ChallengeCollaboration
from bbx_contracts.runner import RunnerFence, runner_json_text
from bbx_contracts.runner_bootstrap import NativeRunnerLaunch
from fastapi import HTTPException
from sqlalchemy import select, update
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_native_runner_bootstrap_integration import config, fixture_pi_model
from test_runner_control_integration import connect, cursor, empty_cursor, registration

pytestmark = pytest.mark.integration


class EvidenceObjects:
    def __init__(self):
        self.values = {}

    async def exists(self, uri):
        return uri in self.values

    async def put(self, uri, value, **_kwargs):
        self.values[uri] = value


async def synthetic_principal(ctf, pool, tid, aid):
    async with ctf.repo.engine.begin() as conn:
        await ctf.repo.lock(conn, tid)
        task = await row(conn, s.tasks, tid)
        owner = await pool.control._current_owner(conn, tid)
        if aid == "lead":
            binding = await pool.control._register_binding(
                conn, tid, runner_json_text(registration({"fence": fence(owner)})).encode()
            )
            bid = binding["id"]
        else:
            bid = task["ctf_control"]["native_worker_sessions"][aid]["binding_id"]
        sdk, journal = str(uuid4()), cursor(empty_cursor(), "synthetic-confirmed-prefix")
        await conn.execute(
            update(s.runner_bindings)
            .where(s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == bid)
            .values(
                status="prepared",
                sdk_session_id=sdk,
                cursor=journal,
                permit_expires_at=datetime.now(UTC) + timedelta(seconds=60),
                permit_revision=1,
            )
        )
        member = await row(conn, s.ctf_members, tid, aid)
        return Principal(
            "agent",
            aid,
            task_id=tid,
            agent_id=aid,
            mode="ctf",
            generation=member["generation"],
            turn_id=UUID(member["current_turn_id"]),
            runtime_context=RuntimeBusinessContext(
                GatewayIdentity(
                    RunnerFence.model_validate(fence(owner)),
                    bid,
                    owner["token_version"],
                    "business-mcp",
                    int((datetime.now(UTC) + timedelta(minutes=1)).timestamp()),
                    member["role"],
                ),
                sdk,
                journal,
            ),
        )


@asynccontextmanager
async def pool_task(ctf, workers):
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Synthetic peer collaboration",
            "budget": {"max_cost": "1", "currency": "USD", "max_minutes": 60},
            "ctf_options": {"dispatch_mode": "automatic", "max_teammates": workers},
        },
        business_contract=BOARD_CONTRACT,
        pi_model=fixture_pi_model(),
    )
    async with connect(ctf) as (app, http):
        native = app.state.native_runner_bootstrap
        native.settings.pi_model_authority = "fixture-authority"
        operation = str(uuid4())
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await native.queue_locked(
                conn,
                tid,
                NativeRunnerLaunch(
                    request_id=uuid4(),
                    model_alias="synthetic-model",
                    budget_currency="USD",
                ),
                "synthetic-launch",
            )
            await native._lead(conn, tid, {"operation_id": operation})
            task = await row(conn, s.tasks, tid)
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    status="running",
                    run_started=True,
                    active_since=datetime.now(UTC),
                    ctf_control={
                        **task["ctf_control"],
                        "phase": "running",
                        "business_tools": business_manifest(automatic=True, collaboration=True),
                    },
                )
            )
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            owner = {
                "fence": fence(
                    await native.control._register_owner(
                        conn,
                        tid,
                        runner_json_text(
                            {
                                "request_id": str(uuid4()),
                                "runner_id": "fixture-runner",
                                "pod_uid": str(uuid4()),
                                "expected_epoch": 0,
                            }
                        ).encode(),
                    )
                )
            }
            existing = (
                (
                    await conn.execute(
                        select(s.task_runner_resources).where(
                            s.task_runner_resources.c.task_id == tid
                        )
                    )
                )
                .mappings()
                .one()
            )
            await conn.execute(
                update(s.task_runner_resources)
                .where(s.task_runner_resources.c.task_id == tid)
                .values(
                    config={**config(), "mcp_transport": "direct-v2"},
                    data={
                        **existing["data"],
                        "worker_spec": {
                            "model": registration(owner)["model"],
                            "allowed_tools": business_manifest(collaboration=True)["roles"][
                                "teammate"
                            ],
                            "instructions": {
                                f"member-{n}": "Synthetic Worker" for n in range(1, workers + 1)
                            },
                        },
                    },
                )
            )
        yield tid, NativeWorkerPool(ctf, native.control)


async def publish(ctf, tid, *, hard=False):
    item = CtfChallenge(
        id=uuid4(),
        task_id=tid,
        title="Synthetic challenge",
        revision=1,
        collaboration=ChallengeCollaboration(
            difficulty="hard" if hard else "medium", assistance_target=1 if hard else 0
        ),
    ).model_dump(mode="json")
    async with ctf.repo.engine.begin() as conn:
        await ctf.repo.lock(conn, tid)
        await ctf._board_event(conn, tid, "lead", "ctf.challenge.created", uuid4(), "fixture", item)
        await CollaborationBoardService._ready(
            conn, tid, await row(conn, s.tasks, tid), item, "ready", 1
        )
    return item


async def reconcile(ctf, tid, pool):
    async with ctf.repo.engine.begin() as conn:
        await ctf.repo.lock(conn, tid)
        task = await row(conn, s.tasks, tid)
        resources = (
            (
                await conn.execute(
                    select(s.task_runner_resources).where(s.task_runner_resources.c.task_id == tid)
                )
            )
            .mappings()
            .one()
        )
        owner = await pool.control._current_owner(conn, tid)
        return await pool.reconcile(conn, tid, task, resources, owner, allow_new=True)


async def test_first_assignments_consume_only_available_seats(ctf):
    async with pool_task(ctf, 20) as (tid, pool):
        for _ in range(30):
            await publish(ctf, tid)
        assert len(await reconcile(ctf, tid, pool)) == 20
        assert len(await reconcile(ctf, tid, pool)) == 20
        async with ctf.repo.engine.connect() as conn:
            task = await row(conn, s.tasks, tid)
            active = task["ctf_control"]["native_worker_active"]
            assert len({item["work_id"] for item in active.values()}) == 20
            assert len(task["ctf_control"]["native_worker_ready"]) == 10
            members = (
                (
                    await conn.execute(
                        select(s.ctf_members).where(
                            s.ctf_members.c.task_id == tid, s.ctf_members.c.role == "teammate"
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert all(
                member["has_worked"] and member["run_state"] == "running" for member in members
            )
            bindings = (
                (
                    await conn.execute(
                        select(s.runner_bindings).where(s.runner_bindings.c.task_id == tid)
                    )
                )
                .mappings()
                .all()
            )
            assert len(bindings) == 20 and all(
                binding["business"]["worker_pool"] for binding in bindings
            )


async def test_simultaneous_first_claims_have_one_owner_and_one_assistant(ctf):
    async with pool_task(ctf, 3) as (tid, pool):
        item = await publish(ctf, tid, hard=True)

        async def take(aid):
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                return await pool.claim(
                    conn,
                    tid,
                    await row(conn, s.tasks, tid),
                    await row(conn, s.ctf_members, tid, aid),
                    await ctf._challenge(conn, tid, item["id"]),
                    first=True,
                )

        results = await asyncio.gather(
            *(take(f"member-{n}") for n in range(1, 4)), return_exceptions=True
        )
        assert sum(isinstance(result, dict) for result in results) == 2
        assert sum(isinstance(result, HTTPException) for result in results) == 1
        async with ctf.repo.engine.connect() as conn:
            found = await ctf._challenge(conn, tid, item["id"])
            assert len(found["collaborator_ids"]) == 1
            assert found["owner_id"] not in found["collaborator_ids"]
            assert found["work_status"] == "in_progress"
            assert len((await row(conn, s.tasks, tid))["ctf_control"]["native_worker_active"]) == 2


async def test_peers_claim_directions_and_rejection_atomically_keeps_the_team(ctf):
    ctf.objects = EvidenceObjects()
    async with pool_task(ctf, 2) as (tid, pool):
        challenge = await publish(ctf, tid, hard=True)
        await reconcile(ctf, tid, pool)
        main = await synthetic_principal(ctf, pool, tid, "member-1")
        peer = await synthetic_principal(ctf, pool, tid, "member-2")
        lead = await synthetic_principal(ctf, pool, tid, "lead")
        uri, attachment_id = f"evidence/{tid}/shared/source.txt", str(uuid4())
        ctf.objects.values[uri] = b"Synthetic observation"
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    initial_attachments=[{"id": attachment_id, "uri": uri}],
                    ctf_control={**task["ctf_control"], "public_input_ids": [attachment_id]},
                )
            )
            challenge = await ctf._challenge(conn, tid, challenge["id"])
        board = ChallengeBoardService(ctf)
        fact = await board.write(
            tid,
            UUID(challenge["id"]),
            main,
            {
                "request_id": str(uuid4()),
                "expected_revision": challenge["revision"],
                "kind": "observation",
                "statement": "The synthetic sample has a header",
                "evidence": [{"type": "text", "uri": uri, "summary": "Provided sample"}],
            },
            kind="fact",
        )
        direction = await board.write(
            tid,
            UUID(challenge["id"]),
            main,
            {
                "request_id": str(uuid4()),
                "expected_revision": challenge["revision"],
                "statement": "Read the header",
                "based_on": [fact["id"]],
                "expected": "Identify its format",
                "method": "Inspect the provided sample",
            },
            kind="intent",
        )
        request = {
            "request_id": str(uuid4()),
            "challenge_id": challenge["id"],
            "expected_revision": challenge["revision"],
            "intent_id": direction["id"],
            "expected_version": direction["object"]["version"],
        }
        claims = await asyncio.gather(
            board.claim_intent(tid, main, request),
            board.claim_intent(tid, peer, {**request, "request_id": str(uuid4())}),
            return_exceptions=True,
        )
        winners = [value for value in claims if isinstance(value, dict)]
        assert len(winners) == 1
        assert (await board.board(tid, UUID(challenge["id"]), peer))["intents"][direction["id"]][
            "holder"
        ] in {"member-1", "member-2"}
        review = CollaborationReview(ctf)
        waiting = await review.submit(
            tid,
            peer,
            {
                "request_id": str(uuid4()),
                "challenge_id": challenge["id"],
                "expected_revision": challenge["revision"],
                "summary": "Candidate result",
                "fact_refs": [fact["id"]],
            },
        )
        assert waiting["work_status"] == "awaiting_review"
        rejected = await review.review(
            tid,
            lead,
            {
                "request_id": str(uuid4()),
                "challenge_id": challenge["id"],
                "expected_revision": waiting["revision"],
                "review_revision": 1,
                "decision": "rejected",
                "reason": "The sample header is not a verified result",
                "fact_refs": [fact["id"]],
            },
        )
        assert (
            rejected["work_status"] == "in_progress" and rejected["owner_id"] == waiting["owner_id"]
        )
        facts = (await board.board(tid, UUID(challenge["id"]), peer))["facts"]
        assert (
            facts[rejected["collaboration"]["feedback_fact_id"]]["statement"]
            == "The sample header is not a verified result"
        )
        async with ctf.repo.engine.connect() as conn:
            task = await row(conn, s.tasks, tid)
            assert len(task["ctf_control"]["native_worker_active"]) == 2
            assert all(
                member["run_state"] == "running"
                for member in (
                    await conn.execute(
                        select(s.ctf_members).where(
                            s.ctf_members.c.task_id == tid, s.ctf_members.c.role == "teammate"
                        )
                    )
                ).mappings()
            )
        flags = FlagBoardService(ctf, "synthetic-peer-flags")
        candidates = await asyncio.gather(
            *(
                flags.candidate(
                    tid,
                    UUID(challenge["id"]),
                    identity,
                    {
                        "request_id": str(uuid4()),
                        "expected_revision": rejected["revision"],
                        "value": value,
                        "fact_refs": [fact["id"]],
                    },
                )
                for identity, value in [
                    (main, "FLAG{synthetic-main}"),
                    (peer, "FLAG{synthetic-peer}"),
                ]
            )
        )
        async with ctf.repo.engine.connect() as conn:
            current = await ctf._challenge(conn, tid, challenge["id"])
        assert current["collaboration"]["review_revision"] == 2
        assert set(current["collaboration"]["review_candidate_ids"]) == {
            item["id"] for item in candidates
        }
        decision = {
            "request_id": str(uuid4()),
            "challenge_id": challenge["id"],
            "expected_revision": current["revision"],
            "review_revision": 2,
            "decision": "accepted",
            "reason": "Synthetic local result checked",
            "candidate_ids": [item["id"] for item in candidates],
            "fact_refs": [fact["id"]],
        }
        accepted = await review.review(tid, lead, decision)
        assert accepted["work_status"] == "completed"
        assert await review.review(tid, lead, decision) == accepted
        with pytest.raises(HTTPException, match="Revision|revision"):
            await review.review(
                tid,
                lead,
                {
                    **decision,
                    "request_id": str(uuid4()),
                    "decision": "rejected",
                },
            )
