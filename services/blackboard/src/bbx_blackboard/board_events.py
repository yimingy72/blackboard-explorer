"""Board request identity and public projections; protected values stay immutable."""

from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from bbx_blackboard.store import schema as s


def digest(data) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def result(event):
    value = dict(event["payload"]["result"])
    for key in ("created_version", "updated_version"):
        if key in value and value[key] == 0:
            value[key] = event["version"]
    return value


async def prior(conn, tid, actor, request_id, request_hash):
    previous = (
        (
            await conn.execute(
                select(s.events)
                .where(
                    s.events.c.task_id == tid,
                    s.events.c.payload["request_actor"].astext == actor,
                    s.events.c.payload["request_id"].astext == str(request_id),
                )
                .order_by(s.events.c.version)
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    if previous is None:
        return None
    if previous["payload"]["request_hash"] != request_hash:
        raise HTTPException(409, "Request ID conflict")
    return result(previous)


async def emit(repo, conn, tid, actor, kind, request_id, request_hash, value, *, extra=None):
    written = await repo.append(
        conn,
        tid,
        [
            {
                "type": kind,
                "actor": actor,
                "object_id": value.get("id"),
                "addressed_to": None,
                "payload": {
                    "request_actor": actor,
                    "request_id": str(request_id),
                    "request_hash": request_hash,
                    "challenge_id": value.get("challenge_id"),
                    "result": value,
                    **(extra or {}),
                },
            }
        ],
    )
    await repo.notify(tid, written[-1]["version"], connection=conn)
    return result(written[0])


async def cancel_prepared(conn, tid, version):
    rows = (
        await conn.execute(
            select(s.flag_submissions).where(
                s.flag_submissions.c.task_id == tid,
                s.flag_submissions.c.status == "prepared",
                s.flag_submissions.c.platform_call_id.is_(None),
            )
        )
    ).mappings()
    for entry in rows:
        value = {**entry["data"], "status": "cancelled", "updated_version": version}
        await conn.execute(
            update(s.flag_submissions)
            .where(
                s.flag_submissions.c.task_id == tid,
                s.flag_submissions.c.id == entry["id"],
            )
            .values(status="cancelled", data=value, version=version)
        )


async def apply_board_event(conn, event):
    tid, kind = event["task_id"], event["type"]
    value = result(event)
    version = event["version"]
    if kind == "board.request.linked":
        return
    if kind == "board.announcement.published":
        await conn.execute(
            insert(s.board_announcements)
            .values(
                task_id=tid,
                id=value["id"],
                revision=value["revision"],
                data=value,
                version=version,
            )
            .on_conflict_do_update(
                index_elements=["task_id", "id"], set_={"data": value, "version": version}
            )
        )
    elif kind == "board.flag.candidate":
        # The encrypted value is an immutable private record, never a public event field.
        changed = await conn.execute(
            update(s.flag_candidates)
            .where(
                s.flag_candidates.c.task_id == tid,
                s.flag_candidates.c.id == value["id"],
            )
            .values(data=value, version=version)
        )
        if changed.rowcount != 1:
            raise ValueError("Protected candidate value record is missing")
    else:
        await conn.execute(
            insert(s.flag_submissions)
            .values(
                task_id=tid,
                id=value["id"],
                challenge_id=value["challenge_id"],
                candidate_id=value["candidate_id"],
                verification_round=value["verification_round"],
                status=value["status"],
                platform_call_id=value["platform_call_id"],
                data=value,
                version=version,
            )
            .on_conflict_do_update(
                index_elements=["task_id", "id"],
                set_={
                    "status": value["status"],
                    "platform_call_id": value["platform_call_id"],
                    "data": value,
                    "version": version,
                },
            )
        )
        if change := event["payload"].get("challenge_update"):
            await conn.execute(
                update(s.ctf_challenges)
                .where(
                    s.ctf_challenges.c.task_id == tid,
                    s.ctf_challenges.c.id == change["id"],
                )
                .values(data=change, revision=change["revision"])
            )
