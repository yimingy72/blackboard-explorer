"""Persisted execution watermarks and real PostgreSQL collaboration transactions."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bbx_blackboard.collaboration_board import CollaborationBoardService
from bbx_blackboard.native_worker_pool import work_context
from bbx_blackboard.runner_auth import RunnerIdentity
from bbx_blackboard.runner_control import row
from bbx_blackboard.runner_gateway import RunnerGateway
from bbx_blackboard.store import schema as s
from bbx_blackboard.worker_board_updates import clock_changes, notify_changes
from bbx_contracts.ctf_collaboration import WorkerContext
from bbx_contracts.models import Fact
from bbx_contracts.runner import WorkerIdleProof, runner_json_text
from bbx_contracts.runner_bootstrap import NativeResumeRequest
from fastapi import HTTPException
from runner_gateway_support import invocation
from sqlalchemy import insert, select, update
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_runner_control_integration import connect
from test_worker_pool_integration import pool_task, publish, reconcile, synthetic_principal

pytestmark = pytest.mark.integration


async def idle_observation(ctf, pool, tid, aid, sequence):
    async with ctf.repo.engine.begin() as conn:
        await ctf.repo.lock(conn, tid)
        task = await row(conn, s.tasks, tid)
        context = work_context(task, aid)
        bid = task["ctf_control"]["native_worker_sessions"][aid]["binding_id"]
        binding = await row(conn, s.runner_bindings, tid, bid)
        entry = {"id": str(uuid4()), "customType": "bbx.worker-idle", "data": {"context": context}}
        # SDK journal hashing itself is covered by the real Pi Runner suite.
        body = SimpleNamespace(
            worker_idle=WorkerIdleProof(
                entry_id=entry["id"], seq=sequence, context=WorkerContext.model_validate(context)
            ),
            previous=SimpleNamespace(seq=sequence - 1),
            entries=json.dumps(entry) + "\n",
        )
        await pool.record_idle(conn, tid, binding, body)
        return await row(conn, s.runner_bindings, tid, bid)


async def model_projection(conn, tid, assignment, sequence, state):
    call_id = uuid4()
    await conn.execute(
        insert(s.pi_model_calls).values(
            site_id="peer-lifecycle-test",
            call_id=call_id,
            attempt_id=uuid4(),
            task_id=tid,
            purpose="execution",
            agent_id=assignment["agent_id"],
            turn_id=assignment["turn_id"],
            logical_call_id=str(call_id),
            model_snapshot={},
            state=state,
            intent={
                "proof": {
                    "binding": {"binding_id": assignment["binding_id"]},
                    "journal": {"seq": sequence},
                }
            },
        )
    )
    return call_id


async def change_challenge(ctf, conn, tid, cid, **changes):
    original = await ctf._challenge(conn, tid, cid)
    value = {**original, **changes, "revision": original["revision"] + 1}
    await ctf._board_event(conn, tid, "system", "ctf.challenge.updated", uuid4(), "fixture", value)
    return value


async def test_late_idle_cannot_release_after_a_new_completed_model(ctf):
    async with pool_task(ctf, 1) as (tid, pool):
        challenge = await publish(ctf, tid)
        await reconcile(ctf, tid, pool)
        principal = await synthetic_principal(ctf, pool, tid, "member-1")
        assert (await idle_observation(ctf, pool, tid, "member-1", 40))["business"]["worker_idle"]
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            assignment = task["ctf_control"]["native_worker_active"]["member-1"]
            await model_projection(conn, tid, assignment, 45, "completed")
            await change_challenge(ctf, conn, tid, challenge["id"], work_status="completed")
        # Recheck on release, even if a stale idle observation was already saved.
        await reconcile(ctf, tid, pool)
        async with ctf.repo.engine.connect() as conn:
            assert (await row(conn, s.tasks, tid))["ctf_control"]["native_worker_active"]
            binding = await row(conn, s.runner_bindings, tid, assignment["binding_id"])
        # No later model may be erased by another arrival of the old observation.
        old = await idle_observation(ctf, pool, tid, "member-1", 40)
        assert old["business"].get("worker_idle") == binding["business"].get("worker_idle")
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await conn.execute(
                update(s.runner_commands)
                .where(
                    s.runner_commands.c.task_id == tid,
                    s.runner_commands.c.kind == "input",
                )
                .values(status="applied", native_proof={"seq": 49})
            )
            await conn.execute(
                update(s.ctf_messages)
                .where(
                    s.ctf_messages.c.task_id == tid,
                )
                .values(status="delivered")
            )
        await idle_observation(ctf, pool, tid, "member-1", 60)
        await reconcile(ctf, tid, pool)
        new = await publish(ctf, tid)
        await reconcile(ctf, tid, pool)
        async with ctf.repo.engine.connect() as conn:
            task = await row(conn, s.tasks, tid)
            assert not task["ctf_control"]["native_worker_active"]
            assert (await row(conn, s.ctf_members, tid, "member-1"))["has_worked"]
            assert (
                task["ctf_control"]["native_worker_sessions"]["member-1"]["binding_id"]
                == (assignment["binding_id"])
            )
        request = {"request_id": str(uuid4()), "challenge_id": new["id"], "expected_revision": 1}
        board = CollaborationBoardService(ctf)
        choices = await board.available(tid, principal)
        assert choices["new_challenges"][0]["challenge_id"] == new["id"]
        assert choices["board_version"] > 0
        claimed = await board.claim(tid, principal, request)
        assert claimed["assignment"]["work_id"] == new["id"]
        assert claimed["assignment"]["binding_id"] == assignment["binding_id"]
        assert await board.claim(tid, principal, request) == claimed


async def test_unknown_old_call_blocks_only_its_worker_release(ctf):
    async with pool_task(ctf, 2) as (tid, pool):
        challenge = await publish(ctf, tid, hard=True)
        await reconcile(ctf, tid, pool)
        for aid in ("member-1", "member-2"):
            await synthetic_principal(ctf, pool, tid, aid)
            await idle_observation(ctf, pool, tid, aid, 20)
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await conn.execute(
                update(s.ctf_messages)
                .where(
                    s.ctf_messages.c.task_id == tid,
                )
                .values(status="delivered")
            )
            task = await row(conn, s.tasks, tid)
            assignment = task["ctf_control"]["native_worker_active"]["member-2"]
            # A recovered session's idle is not evidence that an older call ended.
            old = {**assignment, "binding_id": str(uuid4()), "turn_id": str(uuid4())}
            pending = await model_projection(conn, tid, old, 5, "unknown")
            await change_challenge(ctf, conn, tid, challenge["id"], work_status="completed")
        await reconcile(ctf, tid, pool)
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            assert set(task["ctf_control"]["native_worker_active"]) == {"member-2"}
            await conn.execute(
                update(s.pi_model_calls)
                .where(
                    s.pi_model_calls.c.call_id == pending,
                )
                .values(state="completed")
            )
        await reconcile(ctf, tid, pool)
        async with ctf.repo.engine.connect() as conn:
            assert not (await row(conn, s.tasks, tid))["ctf_control"]["native_worker_active"]


@pytest.mark.parametrize("has_assistant", [False, True])
async def test_thirty_minute_event_is_once_and_excludes_review(ctf, has_assistant):
    async with pool_task(ctf, 2 if has_assistant else 1) as (tid, pool):
        challenge = await publish(ctf, tid, hard=has_assistant)
        await reconcile(ctf, tid, pool)
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            current = await ctf._challenge(conn, tid, challenge["id"])
            config = {
                **current["collaboration"],
                "solve_started_at": (datetime.now(UTC) - timedelta(minutes=31)).isoformat(),
            }
            await change_challenge(
                ctf, conn, tid, challenge["id"], work_status="awaiting_review", collaboration=config
            )
            await clock_changes(ctf, conn, tid, await row(conn, s.tasks, tid))
            untouched = await ctf._challenge(conn, tid, challenge["id"])
            assert untouched["collaboration"]["assistance_reminded"] is False
            await change_challenge(ctf, conn, tid, challenge["id"], work_status="in_progress")
            for _ in range(2):
                await clock_changes(ctf, conn, tid, await row(conn, s.tasks, tid))
            updated = await ctf._challenge(conn, tid, challenge["id"])
            assert updated["collaboration"]["assistance_target"] == 1
            assert updated["collaboration"]["assistance_reminded"] is True
            notices = (
                (
                    await conn.execute(
                        select(s.ctf_messages).where(
                            s.ctf_messages.c.task_id == tid,
                            s.ctf_messages.c.notification_purpose == "assistance_reminder",
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert len(notices) == 1 and notices[0]["recipient_id"] == updated["owner_id"]


async def test_large_first_work_and_coalesced_review_notification(ctf):
    async with pool_task(ctf, 2) as (tid, pool):
        challenge = await publish(ctf, tid, hard=True)
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await change_challenge(
                ctf, conn, tid, challenge["id"], description="large input " * 5000
            )
        await reconcile(ctf, tid, pool)
        for aid in ("member-1", "member-2"):
            await synthetic_principal(ctf, pool, tid, aid)
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            messages = list(
                (
                    await conn.execute(
                        select(s.ctf_messages).where(
                            s.ctf_messages.c.task_id == tid,
                            s.ctf_messages.c.challenge_id == challenge["id"],
                        )
                    )
                ).mappings()
            )
            assert len(messages) == 2
            assert all(
                len(message["body"]) <= 20000 and "board_read" in message["body"]
                for message in messages
            )
            await change_challenge(ctf, conn, tid, challenge["id"], work_status="awaiting_review")
            await notify_changes(ctf, conn, tid)
            await notify_changes(ctf, conn, tid)
            notices = list(
                (
                    await conn.execute(
                        select(s.ctf_messages).where(
                            s.ctf_messages.c.task_id == tid,
                            s.ctf_messages.c.notification_purpose == "board_update",
                            s.ctf_messages.c.status == "queued",
                        )
                    )
                ).mappings()
            )
            assert {message["recipient_id"] for message in notices} == {
                "lead",
                "member-1",
                "member-2",
            }
            assert len(notices) == 3


async def test_lead_cannot_finish_partial_work_but_user_stop_is_retained(ctf):
    async with pool_task(ctf, 1) as (tid, pool):
        await publish(ctf, tid)
        lead = await synthetic_principal(ctf, pool, tid, "lead")
        with pytest.raises(HTTPException, match="Unfinished challenges"):
            await ctf.request_finish(
                tid, "lead", uuid4(), {"end_reason": "partial"}, lead.generation, lead.turn_id
            )
        stopped = await ctf.request_finish(
            tid, "user", uuid4(), {"end_reason": "user_stop", "summary": "Synthetic stop"}
        )
        assert stopped["phase"] == "closing"


@pytest.mark.parametrize("status", ["in_progress", "awaiting_review"])
async def test_pause_continuation_keeps_members_and_intent_claims_on_double_restore(ctf, status):
    async with pool_task(ctf, 2) as (tid, pool), connect(ctf) as (app, _):
        challenge = await publish(ctf, tid, hard=True)
        await reconcile(ctf, tid, pool)
        lead = await synthetic_principal(ctf, pool, tid, "lead")
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            old = task["ctf_control"]
            claim = old["native_worker_active"]["member-2"]
            origin = {
                "agent_id": "member-2",
                "generation": claim["generation"],
                "turn_id": claim["turn_id"],
                "epoch": claim["epoch"],
                "owner_id": "member-1",
                "scope_revision": claim["scope_revision"],
                "assignment_id": claim["assignment_ref"],
            }
            await conn.execute(
                insert(s.intents).values(
                    task_id=tid,
                    id="I1",
                    challenge_id=challenge["id"],
                    assignment_ref=origin,
                    claim_assignment_ref=origin,
                    statement="Persisted test direction",
                    based_on=["F1"],
                    expected="Compare result",
                    method="Read saved sample",
                    relates_to=[],
                    status="claimed",
                    holder="member-2",
                    result_facts=[],
                    notes=[],
                    attempts=0,
                    author="member-2",
                    version=1,
                )
            )
            await change_challenge(ctf, conn, tid, challenge["id"], work_status=status)
            await clock_changes(ctf, conn, tid, await row(conn, s.tasks, tid), pause=True)
            task = await row(conn, s.tasks, tid)
            resource = (
                (
                    await conn.execute(
                        select(s.task_runner_resources).where(
                            s.task_runner_resources.c.task_id == tid,
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert lead.runtime_context is not None
            lead_bid = lead.runtime_context.identity.binding_id
            snapshot = {
                "lead_binding_id": lead_bid,
                "binding_ids": [
                    lead_bid,
                    *(v["binding_id"] for v in old["native_worker_sessions"].values()),
                ],
                "assignments": old["native_worker_active"],
                "worker_sessions": old["native_worker_sessions"],
            }
            # stage_continuation is entered only after the original close receipts.
            await conn.execute(
                update(s.runner_bindings)
                .where(
                    s.runner_bindings.c.task_id == tid,
                    s.runner_bindings.c.id.in_(snapshot["binding_ids"]),
                )
                .values(status="closed")
            )
            await app.state.native_runner_bootstrap.pause_control.stage_continuation(
                conn,
                tid,
                task,
                resource,
                snapshot,
                NativeResumeRequest(request_id=uuid4(), pause_id=uuid4()),
                "synthetic-resume",
            )

        async def restore():
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                task = await row(conn, s.tasks, tid)
                await pool.restore(
                    conn, tid, task, resource, await pool.control._current_owner(conn, tid)
                )

        await asyncio.gather(restore(), restore())
        async with ctf.repo.engine.connect() as conn:
            task = await row(conn, s.tasks, tid)
            current = task["ctf_control"]
            assert current["native_worker_continuations"] == {}
            assert set(current["native_worker_active"]) == {"member-1", "member-2"}
            for aid, original in old["native_worker_active"].items():
                restored = current["native_worker_active"][aid]
                assert restored["work_id"] == original["work_id"]
                assert restored["role"] == original["role"]
                assert restored["assignment_ref"] != original["assignment_ref"]
                assert restored["binding_id"] != original["binding_id"]
                assert (await row(conn, s.ctf_members, tid, aid))["has_worked"]
            intent = await row(conn, s.intents, tid, "I1")
            assert intent["status"] == "claimed" and intent["holder"] == "member-2"
            assert (
                intent["claim_assignment_ref"]["assignment_id"]
                == (current["native_worker_active"]["member-2"]["assignment_ref"])
            )
            saved = await ctf._challenge(conn, tid, challenge["id"])
            assert saved["work_status"] == status and saved["owner_id"] == "member-1"
            assert saved["collaborator_ids"] == ["member-2"]
            fresh = list(
                (
                    await conn.execute(
                        select(s.ctf_messages).where(
                            s.ctf_messages.c.task_id == tid,
                            s.ctf_messages.c.recipient_id.in_(["member-1", "member-2"]),
                            s.ctf_messages.c.deferred.is_(False),
                            s.ctf_messages.c.status == "queued",
                        )
                    )
                ).mappings()
            )
            if status == "awaiting_review":
                assert fresh == []
            else:
                assert len(fresh) == 2
                assert all("Resume your existing challenge" in item["body"] for item in fresh)


async def test_worker_prompt_reads_committed_snapshot_and_empty_unrelated_delta(ctf):
    async with pool_task(ctf, 1) as (tid, pool):
        challenge = await publish(ctf, tid)
        await reconcile(ctf, tid, pool)
        principal = await synthetic_principal(ctf, pool, tid, "member-1")
        assert principal.runtime_context is not None
        identity = principal.runtime_context.identity
        runner = RunnerIdentity(identity.fence, identity.token_version)
        snapshot = await pool.control.prompt(tid, identity.binding_id, runner, 0)
        content = json.loads(snapshot["board_context"]["content"])
        assert content["snapshot"] and content["challenge"]["id"] == challenge["id"]
        version, context = snapshot["board_context"]["version"], snapshot["worker_context"]
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await ctf._emit(conn, tid, "coordination.reminded", "system", {"reason": "fixture"})
        delta = await pool.control.prompt(
            tid, identity.binding_id, runner, 1, version, context["revision"]
        )
        assert delta["board_context"]["version"] > version
        assert delta["board_context"]["content"] == ""
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await change_challenge(ctf, conn, tid, challenge["id"], description="New shared clue")
        changed = await pool.control.prompt(
            tid, identity.binding_id, runner, 1, version, context["revision"]
        )
        assert (
            json.loads(changed["board_context"]["content"])["challenge"]["description"]
            == "New shared clue"
        )


async def test_gateway_keeps_original_model_work_and_recovers_saved_tool_result(ctf):
    async with pool_task(ctf, 1) as (tid, pool):
        await publish(ctf, tid)
        await reconcile(ctf, tid, pool)
        principal = await synthetic_principal(ctf, pool, tid, "member-1")
        assert principal.runtime_context is not None
        context = principal.runtime_context
        gateway = RunnerGateway(ctf.repo.engine, settings())
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            active = task["ctf_control"]["native_worker_active"]["member-1"]
            binding = await row(conn, s.runner_bindings, tid, active["binding_id"])
            source = await model_projection(conn, tid, active, 1, "complete")
            await conn.execute(
                update(s.pi_model_calls)
                .where(s.pi_model_calls.c.call_id == source)
                .values(
                    intent={
                        "proof": {
                            "binding": binding["binding"],
                            "worker_context": work_context(task, "member-1"),
                        }
                    },
                )
            )
        scope = {
            "tid": tid,
            "binding": binding["binding"],
            "sdk": context.sdk_session_id,
            "journal": context.journal,
            "owner": {"fence": context.identity.fence.model_dump(mode="json")},
        }
        first = {
            **invocation(scope, "board_available_work", {}),
            "source_model_call_id": str(source),
        }
        permit = await gateway.admit_tool(tid, context.identity, runner_json_text(first).encode())
        assert permit["disposition"] == "admitted"
        saved = {"content": [{"type": "text", "text": "Original saved result"}]}
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            call = await row(conn, s.runner_calls, tid, "tool:" + first["invocation_id"])
            await gateway._save_tool_result(conn, tid, call, saved)
            task = await row(conn, s.tasks, tid)
            sessions = task["ctf_control"]["native_worker_sessions"]
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={
                        **task["ctf_control"],
                        "native_worker_sessions": {
                            **sessions,
                            "member-1": {**sessions["member-1"], "work_revision": 99},
                        },
                    }
                )
            )
        recovered = await gateway.admit_tool(
            tid, context.identity, runner_json_text(first).encode()
        )
        assert json.loads(recovered["result_json"]) == saved
        later = {
            **invocation(scope, "board_available_work", {}, "late"),
            "source_model_call_id": str(source),
        }
        rejected = await gateway.admit_tool(tid, context.identity, runner_json_text(later).encode())
        assert rejected["disposition"] == "completed"
        assert json.loads(rejected["result_json"])["isError"] is True
        async with ctf.repo.engine.connect() as conn:
            call = await row(conn, s.runner_calls, tid, "tool:" + later["invocation_id"])
            assert call["data"]["business"]["native_assignment_id"] == active["assignment_ref"]
            assert call["data"]["business"]["worker_context"]["revision"] != 99


async def test_idle_does_not_spin_and_delivers_peer_changes_missed_while_busy(ctf):
    async with pool_task(ctf, 2) as (tid, pool):
        challenge = await publish(ctf, tid, hard=True)
        await reconcile(ctf, tid, pool)
        await synthetic_principal(ctf, pool, tid, "member-1")
        binding = await idle_observation(ctf, pool, tid, "member-1", 20)
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await notify_changes(ctf, conn, tid)
            query = select(s.ctf_messages.c.id).where(
                s.ctf_messages.c.task_id == tid,
                s.ctf_messages.c.recipient_id == "member-1",
                s.ctf_messages.c.notification_purpose == "board_update",
            )
            assert list(await conn.scalars(query)) == []
            await conn.execute(
                update(s.runner_bindings)
                .where(
                    s.runner_bindings.c.task_id == tid,
                    s.runner_bindings.c.id == binding["id"],
                )
                .values(business={**binding["business"], "worker_idle": None})
            )
            task = await row(conn, s.tasks, tid)
            peer = task["ctf_control"]["native_worker_active"]["member-2"]
            fact = Fact.model_validate(
                {
                    "id": "F1",
                    "kind": "observation",
                    "statement": "A peer observation",
                    "provenance": "self_reported",
                    "author": "member-2",
                    "challenge_id": challenge["id"],
                    "assignment_ref": {
                        "agent_id": "member-2",
                        "generation": peer["generation"],
                        "turn_id": peer["turn_id"],
                        "epoch": peer["epoch"],
                        "owner_id": "member-1",
                        "scope_revision": 1,
                        "assignment_id": peer["assignment_ref"],
                    },
                    "evidence": [
                        {
                            "type": "text",
                            "uri": f"evidence/{tid}/fixture.txt",
                            "summary": "Synthetic fixture",
                        }
                    ],
                }
            )
            written = await ctf.repo.append(
                conn,
                tid,
                [
                    {
                        "type": "fact.posted",
                        "actor": "member-2",
                        "object_id": "F1",
                        "payload": fact.model_dump(mode="json"),
                        "addressed_to": ["member-1"],
                    }
                ],
            )
            await notify_changes(ctf, conn, tid)
            assert list(await conn.scalars(query)) == []
        await idle_observation(ctf, pool, tid, "member-1", 30)
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await notify_changes(ctf, conn, tid)
            assert len(list(await conn.scalars(query))) == 1
            await pool.record_board_cursor(
                conn,
                tid,
                binding,
                json.dumps(
                    {
                        "type": "custom",
                        "customType": "bbx.board-context",
                        "data": {"version": written[-1]["version"]},
                    }
                )
                + "\n",
            )
        await idle_observation(ctf, pool, tid, "member-1", 40)
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await notify_changes(ctf, conn, tid)
            assert len(list(await conn.scalars(query))) == 1
