"""Coalesce committed challenge changes into existing mailboxes and prompt deltas."""

import hashlib
import json
from datetime import UTC, datetime

from bbx_contracts.challenge_board import CHALLENGE_KNOWLEDGE_EVENTS
from bbx_contracts.runner_bootstrap import native_pause_state
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, update

from bbx_blackboard.domain.challenge_collaboration import (
    available_work,
    clock_state,
    due_reminder,
    participants,
    reminder_deadline,
)
from bbx_blackboard.domain.rules import dispute_fields
from bbx_blackboard.native_worker_pool import work_context, worker_id
from bbx_blackboard.runner_control import row
from bbx_blackboard.store import schema as s


async def clock_changes(ctf, conn, tid, task, *, pause=False):
    now = datetime.now(UTC)
    running = (
        task["status"] == "running"
        and native_pause_state(task) not in {"draining", "paused", "resuming"}
        and not task["ctf_control"].get("failure_hold")
        and not task["ctf_control"].get("native_recovery")
        and not task["ctf_control"].get("native_stop_requested")
        and not pause
    )
    challenges = list(
        (
            await conn.scalars(
                select(s.ctf_challenges.c.data).where(s.ctf_challenges.c.task_id == tid)
            )
        ).all()
    )
    deadlines = []
    for challenge in challenges:
        old = challenge.get("collaboration")
        if not old or challenge["work_status"] != "in_progress":
            continue
        value = challenge
        if bool(old.get("solve_started_at")) != running:
            value = {
                **challenge,
                "collaboration": clock_state(challenge, now, running=running).model_dump(
                    mode="json"
                ),
            }
            await ctf._board_event(
                conn,
                tid,
                "system",
                "ctf.challenge.updated",
                worker_id(tid, challenge["id"], now, "clock"),
                "clock",
                value,
            )
        due = due_reminder(task, value, now) if running else None
        if due:
            await ctf._board_event(
                conn,
                tid,
                "system",
                "ctf.challenge.updated",
                worker_id(tid, challenge["id"], "thirty-minutes"),
                "thirty-minutes",
                due,
            )
            await ctf._message(
                conn,
                tid,
                "system",
                due["owner_id"],
                json.dumps(
                    {
                        "challenge_id": due["id"],
                        "revision": due["revision"],
                        "instruction": (
                            "Thirty minutes elapsed. Read the board and assess assistance. "
                            "If no assistant has joined, recruitment is now open."
                        ),
                    }
                ),
                worker_id(tid, due["id"], "thirty-minute-notice"),
                "notification",
                notification_purpose="assistance_reminder",
                challenge_id=due["id"],
            )
            value = due
        if deadline := reminder_deadline(value):
            deadlines.append(deadline)
    current = await row(conn, s.tasks, tid)
    await conn.execute(
        update(s.tasks)
        .where(s.tasks.c.id == tid)
        .values(
            ctf_control={
                **current["ctf_control"],
                "worker_next_deadline": min(deadlines).isoformat() if deadlines else None,
            }
        )
    )


async def notify_changes(ctf, conn, tid):
    task = await row(conn, s.tasks, tid)
    control = task["ctf_control"]
    since = control.get("worker_board_cursor", 0)
    events = (
        (
            await conn.execute(
                select(s.events)
                .where(
                    s.events.c.task_id == tid,
                    s.events.c.version > since,
                )
                .order_by(s.events.c.version)
            )
        )
        .mappings()
        .all()
    )
    if not events:
        return
    changed = {}
    for event in events:
        payload, kind = event["payload"], event["type"]
        cid = payload.get("challenge_id") or (payload.get("context") or {}).get("challenge_id")
        if kind in {"ctf.challenge.created", "ctf.challenge.updated"}:
            if payload.get("request_hash") in {"clock", "thirty-minutes"}:
                continue
            cid = payload["result"]["id"]
        if cid and kind in {
            *CHALLENGE_KNOWLEDGE_EVENTS,
            "ctf.challenge.created",
            "ctf.challenge.updated",
            "ctf.native_worker.idle",
        }:
            changed.setdefault(cid, []).append(event)
    for cid, changes in changed.items():
        challenge = await ctf._challenge(conn, tid, cid)
        state_change = any(
            event["type"] == "ctf.challenge.updated"
            and event["payload"].get("request_hash") != "clock"
            for event in changes
        )
        recipients = participants(task, cid)
        recipients = {
            **recipients,
            **(
                {"lead": {}}
                if challenge["work_status"] == "awaiting_review" and state_change
                else {}
            ),
        }
        for aid in recipients:
            idle = False
            seen_version = 0
            if aid != "lead":
                session = control.get("native_worker_sessions", {}).get(aid)
                if not session:
                    continue
                binding = await row(conn, s.runner_bindings, tid, session["binding_id"])
                if binding["status"] == "registered":
                    continue
                idle = bool(binding["business"].get("worker_idle"))
                seen_version = binding["business"].get("worker_board_seen", 0)
                if challenge["work_status"] == "completed" and idle:
                    continue
            if not state_change and not (idle and challenge["work_status"] == "in_progress"):
                continue
            if not state_change:
                unseen = await conn.scalar(
                    select(s.events.c.version)
                    .where(
                        s.events.c.task_id == tid,
                        s.events.c.version > seen_version,
                        s.events.c.type.in_(CHALLENGE_KNOWLEDGE_EVENTS),
                        s.events.c.payload["challenge_id"].astext == cid,
                        s.events.c.actor != aid,
                    )
                    .limit(1)
                )
                if unseen is None:
                    continue
            instructions = {
                "awaiting_review": "Read the latest board. Wait for Lead verification.",
                "completed": "Lead accepted this challenge. End this work; await platform release.",
                "in_progress": "Read the board and Lead feedback, then continue with your peers.",
            }
            instruction = (
                "Verify the saved result. Rejection requires a clear reason fact."
                if aid == "lead"
                else instructions.get(challenge["work_status"], "Read the updated challenge.")
            )
            body = json.dumps(
                {
                    "challenge_id": cid,
                    "revision": challenge["revision"],
                    "status": challenge["work_status"],
                    "feedback_fact_id": (challenge.get("collaboration") or {}).get(
                        "feedback_fact_id"
                    ),
                    "through_version": changes[-1]["version"],
                    "instruction": instruction,
                },
                ensure_ascii=False,
            )
            await conn.execute(
                update(s.ctf_messages)
                .where(
                    s.ctf_messages.c.task_id == tid,
                    s.ctf_messages.c.recipient_id == aid,
                    s.ctf_messages.c.challenge_id == cid,
                    s.ctf_messages.c.status == "queued",
                    s.ctf_messages.c.notification_purpose == "board_update",
                )
                .values(status="cancelled")
            )
            await ctf._message(
                conn,
                tid,
                "system",
                aid,
                body,
                worker_id(tid, aid, cid, changes[-1]["version"], "board-update"),
                "notification",
                notification_purpose="board_update",
                challenge_id=cid,
            )
    current = await row(conn, s.tasks, tid)
    await conn.execute(
        update(s.tasks)
        .where(s.tasks.c.id == tid)
        .values(
            ctf_control={
                **current["ctf_control"],
                "worker_board_cursor": events[-1]["version"],
            }
        )
    )


async def prompt_context(repo, conn, tid, task, aid, known_version, known_work_revision):
    context = work_context(task, aid)
    version = int(
        await conn.scalar(select(func.max(s.events.c.version)).where(s.events.c.task_id == tid))
        or 0
    )
    if known_version > version:
        from fastapi import HTTPException

        raise HTTPException(409, "Confirmed board context moved backwards")
    if known_version == version and known_work_revision == context["revision"]:
        return None
    current = task["ctf_control"].get("native_worker_active", {}).get(aid)
    full = known_work_revision != context["revision"] or not known_version
    relevant = (
        (
            await conn.execute(
                select(s.events)
                .where(
                    s.events.c.task_id == tid,
                    s.events.c.version > known_version,
                    s.events.c.type.in_(
                        [
                            *CHALLENGE_KNOWLEDGE_EVENTS,
                            "ctf.challenge.created",
                            "ctf.challenge.updated",
                        ]
                    ),
                )
                .order_by(s.events.c.version)
            )
        )
        .mappings()
        .all()
    )
    if current is not None:
        relevant = [
            event
            for event in relevant
            if (
                event["payload"].get("challenge_id")
                or (event["payload"].get("result") or {}).get("id")
            )
            == current["work_id"]
        ]
    if not full and not relevant:
        return {"version": version, "content": "", "source_digest": hashlib.sha256(b"").hexdigest()}
    if current is None:
        challenges = list(
            (
                await conn.scalars(
                    select(s.ctf_challenges.c.data).where(s.ctf_challenges.c.task_id == tid)
                )
            ).all()
        )
        content = json.dumps(
            {"worker_status": "idle", "available": available_work(task, challenges)},
            ensure_ascii=False,
        )
    else:
        challenge = await conn.scalar(
            select(s.ctf_challenges.c.data).where(
                s.ctf_challenges.c.task_id == tid,
                s.ctf_challenges.c.id == current["work_id"],
            )
        )
        state = await repo.load(
            conn, tid, include_tool_calls=False, challenge_id=current["work_id"]
        )
        affected = {event["object_id"] for event in relevant}
        statuses = dispute_fields(state.facts)
        facts = {
            key: value
            for key, value in state.facts.items()
            if full or value["version"] > known_version or key in affected
        }
        facts = {key: {**value, **statuses[key]} for key, value in facts.items()}
        intents = {
            key: value
            for key, value in state.intents.items()
            if full or value["version"] > known_version
        }
        content = json.dumps(
            jsonable_encoder(
                {
                    "assignment": current,
                    "challenge": challenge,
                    "snapshot": full,
                    "facts": facts,
                    "intents": intents,
                    "instruction": (
                        "This is the latest board. Earlier notifications do not override it. "
                        "Read full objects with board_read when needed."
                    ),
                }
            ),
            ensure_ascii=False,
        )
    if len(content) > 130000:
        content = (
            content[:129500] + "\n[Excerpt; use board_challenges for the full challenge, "
            "board_read for knowledge, or board_available_work for choices.]"
        )
    return {
        "version": version,
        "content": content,
        "source_digest": hashlib.sha256(content.encode()).hexdigest(),
    }
