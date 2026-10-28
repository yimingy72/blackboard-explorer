"""Leased presentation jobs, bounded historical sources and independent accounting."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from bbx_contracts.billing import effective_price, token_cost
from bbx_contracts.model_limits import effective_max_input_tokens, effective_max_output_tokens
from bbx_contracts.models import ModelConfig, Price
from bbx_contracts.progress import (
    ProgressCandidate,
    ProgressCheckpoint,
    ProgressComplete,
    ProgressFail,
    ProgressFence,
    ProgressGlobalSettings,
    ProgressNode,
    ProgressQuestion,
    ProgressRefresh,
    ProgressReserve,
    ProgressSettings,
    ProgressSettle,
)
from bbx_contracts.storage import storage_safe
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import BigInteger, Numeric, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from bbx_blackboard.auth import (
    issue_progress_token,
    principal,
    require_service,
    require_task_reader,
    require_user_or_service,
)
from bbx_blackboard.conversations import Conversations, _public
from bbx_blackboard.domain.rules import event
from bbx_blackboard.store import Repository
from bbx_blackboard.store import schema as s

AGENT_ID = "__progress__"
LEASE_SECONDS = 300
MAX_SOURCE_EVENTS = 100
SOURCE_TYPES = (
    "fact.posted",
    "fact.disputed",
    "fact.undisputed",
    "intent.posted",
    "intent.claimed",
    "intent.released",
    "intent.closed",
    "acceptance.judged",
    "acceptance.reverted",
    "task.created",
    "task.report",
    "task.resumed",
    "task.finished",
    "task.failed",
    "task.stopped",
)
router = APIRouter(prefix="/api", tags=["progress"])


def now() -> datetime:
    return datetime.now(UTC)


def check(value: Any, message: str, status: int = 409) -> None:
    if not value:
        raise HTTPException(status, message)


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def initial(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "overview": "",
        "nodes": [
            ProgressNode(
                id="goal",
                kind="goal",
                title=task["goal"][:200],
                question=task["goal"][:2000],
                source_refs=[],
            ).model_dump(mode="json")
        ],
        "revision": 0,
        "source_version": 0,
        "desired_source_version": 0,
        "scanned_version": 0,
        "session_source_version": 0,
        "session_knowledge_version": 0,
        "published_version": 0,
        "settings": ProgressSettings().model_dump(mode="json"),
        "cost": "0",
        "reserved": "0",
        "unknown": "0",
        "last_started_at": None,
        "updated_at": None,
        "error": None,
        "run_number": task["run_number"],
        "status": "disabled",
    }


def apply_candidate(tree: dict[str, Any], body: ProgressCandidate) -> dict[str, Any]:
    check(body.expected_revision == tree["revision"], "Graph revision conflict")
    nodes = {node["id"]: node for node in tree["nodes"]}
    if body.nodes is not None:
        check(len({n.id for n in body.nodes}) == len(body.nodes), "Duplicate node IDs", 422)
        nodes = {node.id: node.model_dump(mode="json") for node in body.nodes}
    check(len({n.id for n in body.upserts}) == len(body.upserts), "Duplicate upserts", 422)
    for key in body.removed_ids:
        nodes.pop(key, None)
    nodes.update({node.id: node.model_dump(mode="json") for node in body.upserts})
    check(0 < len(nodes) <= 300, "Tree must contain 1–300 nodes", 422)
    roots = [n for n in nodes.values() if n["parent_id"] is None]
    check(len(roots) == 1 and roots[0]["kind"] == "goal", "Tree requires one goal root", 422)
    for node in nodes.values():
        seen: set[str] = set()
        current = node
        while current["parent_id"] is not None:
            check(current["id"] not in seen, "Tree cycle", 422)
            seen.add(current["id"])
            check(len(seen) < 8, "Tree exceeds depth 8", 422)
            check(current["parent_id"] in nodes, "Orphan node", 422)
            current = nodes[current["parent_id"]]
        if node["kind"] in {"finding", "attempt"}:
            check(node["source_refs"], "Findings and attempts require sources", 422)
    result = {
        "overview": tree["overview"] if body.overview is None else body.overview,
        "nodes": list(nodes.values()),
        "revision": tree["revision"],
    }
    check(
        len(json.dumps(result, ensure_ascii=False).encode()) <= 1_000_000,
        "Tree exceeds serialized size limit",
        422,
    )
    return result


def fold_sources(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fold only supplied historical events; never consult current projection objects."""
    objects: dict[str, dict[str, Any]] = {}
    for evt in events:
        kind, payload, version = evt["type"], evt["payload"], evt["version"]
        if kind in {"fact.posted", "intent.posted"}:
            value = {**payload, "version": version, "created_version": version}
            if kind == "fact.posted":
                value["status"] = "proposed"
            if kind == "intent.posted":
                value.update(
                    status="claimed" if payload.get("claim") else "open",
                    holder=payload["author"] if payload.get("claim") else None,
                    result=None,
                    result_facts=[],
                    notes=[],
                )
            objects[payload["id"]] = value
        elif kind.startswith("intent.") and payload.get("intent_id") in objects:
            value = objects[payload["intent_id"]]
            value["version"] = version
            if kind == "intent.claimed":
                value.update(status="claimed", holder=payload["holder"])
            elif kind == "intent.released":
                value.update(status="open", holder=None)
                value["notes"].append(
                    {
                        "by": payload.get("holder"),
                        "at": str(evt.get("created_at") or ""),
                        "text": payload.get("note", ""),
                    }
                )
            elif kind == "intent.closed":
                value.update(
                    status="closed", holder=None, result=payload["result"], closed_by=payload["by"]
                )
        elif kind in {"fact.disputed", "fact.undisputed"}:
            fid = payload.get("fact_id") or evt.get("object_id")
            if fid in objects:
                objects[fid]["version"] = version
                objects[fid]["status"] = "disputed" if kind == "fact.disputed" else "proposed"
    return objects


class ProgressService:
    def __init__(self, engine, objects=None, profiles_dir: Path | None = None):
        self.repo = Repository(engine)
        self.conversations = Conversations(engine)
        self.objects = objects
        self.profiles_dir = profiles_dir

    async def task(self, conn, tid, *, writable=False, allow_deleting=False):
        if writable:
            try:
                await self.repo.lock(conn, tid)
            except KeyError:
                raise HTTPException(404, "Task not found") from None
        task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().first()
        check(task is not None, "Task not found", 404)
        assert task is not None
        check(task["mode"] == "blackboard", "Progress requires an ordinary task")
        if writable:
            check(allow_deleting or not task["deleting"], "Task is being deleted")
        return dict(task)

    async def state(self, conn, task):
        value = (
            await conn.execute(
                select(s.task_progress.c.data).where(s.task_progress.c.task_id == task["id"])
            )
        ).scalar_one_or_none()
        return value or initial(task)

    async def save(self, conn, tid, data):
        await conn.execute(
            insert(s.task_progress)
            .values(task_id=tid, data=storage_safe(data))
            .on_conflict_do_update(index_elements=["task_id"], set_={"data": storage_safe(data)})
        )

    async def emit(self, conn, tid, kind, payload):
        written = await self.repo.append(
            conn, tid, [event(kind, AGENT_ID, jsonable_encoder(payload))]
        )
        await self.repo.notify(tid, written[-1]["version"], connection=conn)
        return written[-1]

    async def global_settings(self, body=None):
        allowed = {
            "read_progress_sources",
            "get",
            "search",
            "read_evidence",
            "get_progress_tree",
            "update_progress_tree",
        }
        default_prompt = ""
        if self.profiles_dir is not None:
            prompt_path = self.profiles_dir / "prompts" / "progress.md.j2"
            if prompt_path.is_file():
                default_prompt = prompt_path.read_text(encoding="utf-8")
        async with self.repo.engine.begin() as conn:
            await conn.execute(
                insert(s.app_settings)
                .values(
                    key="progress",
                    value={
                        "revision": 0,
                        "prompt": default_prompt,
                        "model_id": None,
                        "enabled_tools": sorted(allowed),
                    },
                )
                .on_conflict_do_nothing(index_elements=["key"])
            )
            value = (
                await conn.execute(
                    select(s.app_settings.c.value)
                    .where(s.app_settings.c.key == "progress")
                    .with_for_update()
                )
            ).scalar_one()
            if body is not None:
                check(body.expected_revision == value["revision"], "Settings revision conflict")
                check(set(body.enabled_tools) <= allowed, "Unsupported progress tools", 422)
                check(
                    {"read_progress_sources", "get_progress_tree", "update_progress_tree"}
                    <= set(body.enabled_tools),
                    "Core progress tools cannot be disabled",
                    422,
                )
                if body.model_id is not None:
                    model = (
                        await conn.execute(
                            select(s.platform_configs.c.config)
                            .where(
                                s.platform_configs.c.kind == "models",
                                s.platform_configs.c.name == body.model_id,
                                s.platform_configs.c.enabled.is_(True),
                                s.platform_configs.c.deleted.is_(False),
                            )
                            .order_by(s.platform_configs.c.version.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    check(model is not None, "Progress model is unavailable", 422)
                value = {
                    **body.model_dump(mode="json", exclude={"expected_revision", "revision"}),
                    "revision": value["revision"] + 1,
                }
                await conn.execute(
                    update(s.app_settings)
                    .where(s.app_settings.c.key == "progress")
                    .values(value=value)
                )
            return value

    async def settings(self, tid, body):
        async with self.repo.engine.begin() as conn:
            task = await self.task(conn, tid, writable=True)
            state = await self.state(conn, task)
            check(
                not body.enabled or body.max_cost >= self.occupied(state),
                "Limit is below committed or reserved use",
            )
            effective = {**state["settings"], **body.model_dump(mode="json", exclude_unset=True)}
            if effective["enabled"] and (
                not state["settings"]["enabled"]
                or effective.get("model_id") != state["settings"].get("model_id")
                or not state.get("model")
            ):
                global_value = (
                    await conn.execute(
                        select(s.app_settings.c.value).where(s.app_settings.c.key == "progress")
                    )
                ).scalar_one_or_none() or {}
                chosen = {
                    **effective,
                    "model_id": effective.get("model_id") or global_value.get("model_id"),
                }
                state["model"] = await self.model(conn, task, chosen)
            previous = state["settings"]
            state["settings"] = {**previous, **body.model_dump(mode="json", exclude_unset=True)}
            unresolved = Decimal(state["unknown"]) > 0
            state["status"] = (
                "paused" if body.enabled and unresolved else "idle" if body.enabled else "disabled"
            )
            if not unresolved:
                state["error"] = None
            if body.enabled and not previous["enabled"] and not unresolved:
                await self.refresh_queue(conn, task, state, automatic=True)
            if unresolved:
                await self.fail_blocked_queue(conn, tid, state)
            await self.save(conn, tid, state)
            await self.emit(conn, tid, "progress.settings.updated", {"settings": state["settings"]})
            return state["settings"]

    @staticmethod
    def occupied(state):
        return sum((Decimal(state[key]) for key in ("cost", "reserved", "unknown")), Decimal(0))

    async def latest_source(self, conn, tid):
        return int(
            (
                await conn.execute(
                    select(func.max(s.events.c.version)).where(
                        s.events.c.task_id == tid, s.events.c.type.in_(SOURCE_TYPES)
                    )
                )
            ).scalar_one()
            or 0
        )

    async def refresh_queue(
        self, conn, task, state, *, request_id=None, automatic=False, continued_unknown=False
    ):
        tid = task["id"]
        if request_id is not None:
            previous = (
                (
                    await conn.execute(
                        select(s.progress_jobs).where(s.progress_jobs.c.id == request_id)
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                check(
                    previous["task_id"] == tid and previous["kind"] == "refresh",
                    "Request ID conflict",
                )
                return dict(previous)
        latest = await self.latest_source(conn, tid)
        state["desired_source_version"] = latest
        # A task-created skeleton alone does not justify a model call.
        meaningful = (
            await conn.execute(
                select(s.events.c.version)
                .where(
                    s.events.c.task_id == tid, s.events.c.type.in_(("fact.posted", "intent.posted"))
                )
                .limit(1)
            )
        ).first()
        if not meaningful or latest <= state["source_version"]:
            return None
        existing = (
            (
                await conn.execute(
                    select(s.progress_jobs).where(
                        s.progress_jobs.c.task_id == tid,
                        s.progress_jobs.c.kind == "refresh",
                        s.progress_jobs.c.status == "queued",
                    )
                )
            )
            .mappings()
            .first()
        )
        if existing:
            return dict(existing)
        jid = request_id or uuid4()
        previous = (
            (await conn.execute(select(s.progress_jobs).where(s.progress_jobs.c.id == jid)))
            .mappings()
            .first()
        )
        if previous:
            check(
                previous["task_id"] == tid and previous["kind"] == "refresh", "Request ID conflict"
            )
            return dict(previous)
        data = {
            "attempts": 0,
            "automatic": automatic,
            "continued_unknown": continued_unknown,
            "not_before": (
                now()
                + timedelta(
                    seconds=state["settings"]["merge_seconds"]
                    if automatic and state["source_version"] >= state.get("last_target_version", 0)
                    else 0
                )
            ).isoformat(),
            "receipts": {},
        }
        row = (
            (
                await conn.execute(
                    insert(s.progress_jobs)
                    .values(
                        id=jid,
                        task_id=tid,
                        kind="refresh",
                        status="queued",
                        run_number=task["run_number"],
                        data=data,
                    )
                    .returning(s.progress_jobs)
                )
            )
            .mappings()
            .one()
        )
        return dict(row)

    async def resume_explicit(self, conn, task, state, request_id):
        check(
            self.occupied(state) < Decimal(state["settings"]["max_cost"]),
            "Progress budget exhausted",
            402,
        )
        continuing = Decimal(state["unknown"]) > 0
        if continuing:
            jobs = (
                (
                    await conn.execute(
                        select(s.progress_jobs).where(s.progress_jobs.c.task_id == task["id"])
                    )
                )
                .mappings()
                .all()
            )
            check(
                not any(job["status"] == "running" for job in jobs),
                "Running progress execution requires confirmation before continuation",
                409,
            )
            unknown = {
                (str(job["id"]), key): receipt
                for job in jobs
                for key, receipt in job["data"]["receipts"].items()
                if receipt["status"] == "unknown"
            }
            check(
                all(
                    job["status"] == "failed"
                    for job in jobs
                    if any(r["status"] == "unknown" for r in job["data"]["receipts"].values())
                ),
                "Unknown progress requests require failed jobs before continuation",
                409,
            )
            check(
                sum((Decimal(receipt["reserved"]) for receipt in unknown.values()), Decimal(0))
                == Decimal(state["unknown"]),
                "Unknown progress usage ledger requires confirmation",
                409,
            )
            session = await self.conversations._session(conn, task["id"], AGENT_ID)
            saved_state = (session or {}).get("session", {}).get("state", {})
            active = saved_state.get("bbx_progress_active_request")
            if active:
                assert session is not None
                check(
                    active.get("status") == "dispatching"
                    and (active.get("job_id"), active.get("request_id")) in unknown,
                    "Active progress request does not match the unknown usage ledger",
                    409,
                )
                saved_state.setdefault("bbx_progress_retired_requests", {})[
                    active["request_id"]
                ] = {
                    **active,
                    "outcome": "unknown",
                    "retired_by": str(request_id),
                    "retired_at": now().isoformat(),
                }
                saved_state.pop("bbx_progress_active_request")
                await self.conversations._save_session(
                    conn,
                    task["id"],
                    AGENT_ID,
                    session["session"],
                    session["opening_instructions"],
                    session["origin"],
                    session["revision"],
                )
            await self.fail_blocked_queue(
                conn,
                task["id"],
                state,
                preserve_continuations=state["status"] not in {"paused", "budget_exhausted"},
            )
        if state["status"] in {"paused", "budget_exhausted"}:
            state.update(status="idle", error=None)
            state.pop("diagnostic", None)
        return continuing

    async def refresh(self, tid, request_id):
        async with self.repo.engine.begin() as conn:
            task = await self.task(conn, tid, writable=True)
            state = await self.state(conn, task)
            check(state["settings"]["enabled"], "Progress is disabled")
            existing = (
                (
                    await conn.execute(
                        select(s.progress_jobs).where(s.progress_jobs.c.id == request_id)
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                check(
                    existing["task_id"] == tid and existing["kind"] == "refresh",
                    "Request ID conflict",
                )
                return {"job_id": str(existing["id"]), "status": existing["status"]}
            continuing = await self.resume_explicit(conn, task, state, request_id)
            job = await self.refresh_queue(
                conn, task, state, request_id=request_id, continued_unknown=continuing
            )
            await self.save(conn, tid, state)
            return {
                "job_id": str(job["id"]) if job else None,
                "status": job["status"] if job else "idle",
            }

    async def question(self, tid, body: ProgressQuestion):
        async with self.repo.engine.begin() as conn:
            task = await self.task(conn, tid, writable=True)
            state = await self.state(conn, task)
            check(state["settings"]["enabled"], "Progress is disabled")
            existing = (
                (
                    await conn.execute(
                        select(s.agent_messages).where(s.agent_messages.c.id == body.id)
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                check(
                    existing["task_id"] == tid
                    and existing["agent_id"] == AGENT_ID
                    and existing["content"] == body.content,
                    "Message ID conflict",
                )
                return _public(existing)
            occupied_id = (
                await conn.execute(
                    select(s.progress_jobs.c.id).where(s.progress_jobs.c.id == body.id)
                )
            ).first()
            check(occupied_id is None, "Message ID conflicts with an existing progress request")
            continuing = await self.resume_explicit(conn, task, state, body.id)
            check(
                body.node_id is None or any(n["id"] == body.node_id for n in state["nodes"]),
                "Unknown node",
                422,
            )
            msg = (
                (
                    await conn.execute(
                        insert(s.agent_messages)
                        .values(
                            id=body.id,
                            task_id=tid,
                            agent_id=AGENT_ID,
                            role="user",
                            content=storage_safe(body.content),
                            status="queued",
                        )
                        .returning(s.agent_messages)
                    )
                )
                .mappings()
                .one()
            )
            await conn.execute(
                insert(s.progress_jobs).values(
                    id=body.id,
                    task_id=tid,
                    kind="question",
                    status="queued",
                    run_number=task["run_number"],
                    data={
                        "message_id": str(body.id),
                        "node_id": body.node_id,
                        "attempts": 0,
                        "automatic": False,
                        "continued_unknown": continuing,
                        "receipts": {},
                    },
                )
            )
            await self.save(conn, tid, state)
            await self.emit(conn, tid, "progress.message.posted", {"message": _public(msg)})
            return _public(msg)

    async def messages(self, tid, limit=100, before=None):
        async with self.repo.engine.connect() as conn:
            await self.task(conn, tid)
            query = select(s.agent_messages).where(
                s.agent_messages.c.task_id == tid, s.agent_messages.c.agent_id == AGENT_ID
            )
            if before is not None:
                query = query.where(s.agent_messages.c.created_at < before)
            rows = (
                (
                    await conn.execute(
                        query.order_by(
                            s.agent_messages.c.created_at.desc(), s.agent_messages.c.id.desc()
                        ).limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            return {
                "messages": [_public(r) for r in reversed(rows)],
                "has_more": len(rows) == limit,
            }

    async def model(self, conn, task, settings):
        if settings.get("model_id"):
            query = select(s.platform_configs).where(
                s.platform_configs.c.kind == "models",
                s.platform_configs.c.name == settings["model_id"],
                s.platform_configs.c.deleted.is_(False),
                s.platform_configs.c.enabled.is_(True),
            )
            if settings.get("model_version"):
                query = query.where(s.platform_configs.c.version == settings["model_version"])
            row = (
                (await conn.execute(query.order_by(s.platform_configs.c.version.desc()).limit(1)))
                .mappings()
                .first()
            )
            check(row is not None, "Progress model is unavailable")
            assert row is not None
            return {**row["config"], "platform_id": row["name"], "platform_version": row["version"]}
        models = (
            await conn.execute(
                select(s.agent_profiles.c.models).where(
                    s.agent_profiles.c.name == task["agent_profile"],
                    s.agent_profiles.c.version == task["agent_profile_version"],
                )
            )
        ).scalar_one()
        return models["derive"]

    async def job(self, conn, tid, jid, fence, *, completed=False, accounting=False):
        task = await self.task(conn, tid, writable=True, allow_deleting=accounting)
        job = (
            (
                await conn.execute(
                    select(s.progress_jobs).where(
                        s.progress_jobs.c.task_id == tid, s.progress_jobs.c.id == jid
                    )
                )
            )
            .mappings()
            .first()
        )
        check(job is not None, "Job not found", 404)
        assert job is not None
        job = dict(job)
        data = job["data"]
        check(
            data.get("claim_token") == str(fence.claim_token)
            and job["run_number"] == fence.run_number
            and (accounting or task["run_number"] == fence.run_number),
            "Stale progress execution",
        )
        if accounting:
            return task, job
        if completed and job["status"] == "completed":
            return task, job
        check(
            job["status"] == "running" and stamp(data["lease_until"]) > now(), "Job lease expired"
        )
        # A tool response can disclose any source within the fixed job boundary.
        # Keep knowledge distinct from pagination coverage and checkpoint delivery.
        state = await self.state(conn, task)
        if state.get("session_knowledge_version", 0) < data["through_version"]:
            state["session_knowledge_version"] = data["through_version"]
            await self.save(conn, tid, state)
        return task, job

    async def fail_question(self, conn, job, error):
        if job["kind"] == "question" and job["status"] == "failed":
            await conn.execute(
                update(s.agent_messages)
                .where(
                    s.agent_messages.c.id == job["id"],
                    s.agent_messages.c.task_id == job["task_id"],
                    s.agent_messages.c.agent_id == AGENT_ID,
                )
                .values(status="failed", error=error, updated_at=now())
            )

    async def fail_blocked_queue(
        self, conn, tid, state, *, cause_job_id=None, preserve_continuations=False
    ):
        """A paused curator must not leave unstarted questions waiting forever."""
        if state["status"] not in {"paused", "budget_exhausted"} and Decimal(state["unknown"]) == 0:
            return 0
        reason = (
            "上一次模型请求用量待确认，成果整理已暂停；本条请求尚未调用模型。"
            if Decimal(state["unknown"]) > 0
            else "整理额度不足以预留下一次模型调用，请调整模型输出上限或独立整理额度。"
            "本条请求尚未调用模型。"
            if state["status"] == "budget_exhausted"
            else "上一次成果整理失败，已暂停后续请求；检查报错后可重新发送。"
        )
        queued = (
            (
                await conn.execute(
                    select(s.progress_jobs).where(
                        s.progress_jobs.c.task_id == tid,
                        s.progress_jobs.c.status == "queued",
                    )
                )
            )
            .mappings()
            .all()
        )
        if preserve_continuations:
            queued = [row for row in queued if not row["data"].get("continued_unknown")]
        for row in queued:
            job = dict(row)
            job["status"] = "failed"
            job["data"] = {
                **job["data"],
                "error": reason,
                "never_started": True,
                "blocked_by": str(cause_job_id) if cause_job_id else None,
            }
            await self.save_job(conn, job)
            await self.fail_question(conn, job, reason)
        if queued:
            await self.emit(
                conn,
                tid,
                "progress.turn.failed",
                {
                    "error": reason,
                    "retryable": False,
                    "blocked_requests": len(queued),
                    "cause_job_id": str(cause_job_id) if cause_job_id else None,
                },
            )
        return len(queued)

    async def save_job(self, conn, job):
        await conn.execute(
            update(s.progress_jobs)
            .where(s.progress_jobs.c.id == job["id"])
            .values(
                status=job["status"], run_number=job["run_number"], data=storage_safe(job["data"])
            )
        )

    async def claim(self):
        # Task row locks serialize refresh coalescing, questions, checkpoints and fees.
        async with self.repo.engine.begin() as conn:
            queued_or_running = (
                select(s.progress_jobs.c.id)
                .where(
                    s.progress_jobs.c.task_id == s.tasks.c.id,
                    s.progress_jobs.c.status.in_(("queued", "running")),
                )
                .correlate(s.tasks)
                .exists()
            )
            running_job = (
                select(s.progress_jobs.c.id)
                .where(
                    s.progress_jobs.c.task_id == s.tasks.c.id,
                    s.progress_jobs.c.status == "running",
                )
                .correlate(s.tasks)
                .exists()
            )
            explicit_continuation = (
                select(s.progress_jobs.c.id)
                .where(
                    s.progress_jobs.c.task_id == s.tasks.c.id,
                    s.progress_jobs.c.status.in_(("queued", "running")),
                    s.progress_jobs.c.data["continued_unknown"].as_boolean().is_(True),
                )
                .correlate(s.tasks)
                .exists()
            )
            pending_sources = (
                select(s.events.c.version)
                .where(
                    s.events.c.task_id == s.tasks.c.id,
                    s.events.c.type.in_(SOURCE_TYPES),
                    s.events.c.version
                    > func.coalesce(
                        cast(s.task_progress.c.data["source_version"].astext, BigInteger), 0
                    ),
                )
                .correlate(s.tasks, s.task_progress)
                .exists()
            )
            enabled = (
                (
                    await conn.execute(
                        select(s.tasks)
                        .join(s.task_progress, s.task_progress.c.task_id == s.tasks.c.id)
                        .where(
                            s.tasks.c.deleting.is_(False),
                            s.tasks.c.mode == "blackboard",
                            s.task_progress.c.data["settings"]["enabled"].as_boolean(),
                            s.task_progress.c.data["status"].astext.not_in(
                                ("paused", "budget_exhausted")
                            ),
                            or_(
                                func.coalesce(
                                    cast(s.task_progress.c.data["unknown"].astext, Numeric), 0
                                )
                                == 0,
                                running_job,
                                explicit_continuation,
                            ),
                            or_(
                                s.tasks.c.status.in_(
                                    ("created", "provisioning", "running", "closing")
                                ),
                                queued_or_running,
                                pending_sources,
                            ),
                        )
                        .order_by(s.tasks.c.created_at)
                        .with_for_update(skip_locked=True, of=s.tasks)
                    )
                )
                .mappings()
                .all()
            )
            for task_row in enabled:
                task = dict(task_row)
                tid = task["id"]
                state = await self.state(conn, task)
                running = (
                    (
                        await conn.execute(
                            select(s.progress_jobs).where(
                                s.progress_jobs.c.task_id == tid,
                                s.progress_jobs.c.status == "running",
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if running:
                    if (
                        running["run_number"] == task["run_number"]
                        and stamp(running["data"]["lease_until"]) > now()
                    ):
                        continue
                    # An expired writer's scanned data can never be hidden by an older checkpoint.
                    state["session_knowledge_version"] = max(
                        state.get("session_knowledge_version", 0),
                        running["data"]["through_version"],
                        running["data"].get("scanned_version", 0),
                    )
                    # Recover confirmed responses before treating a lost lease as unknown use.
                    dead = dict(running)
                    session = await self.conversations._session(conn, tid, AGENT_ID)
                    saved_state = (session or {}).get("session", {}).get("state", {})
                    for request_id, receipt in dead["data"]["receipts"].items():
                        saved = saved_state.get("bbx_progress_receipts", {}).get(request_id, {})
                        if (
                            receipt["status"] in {"reserved", "unknown"}
                            and saved.get("job_id") == str(dead["id"])
                            and saved.get("status") in {"completed", "failed"}
                            and saved.get("usage_known") is True
                        ):
                            usage = ProgressSettle.model_validate(
                                {
                                    "claim_token": dead["data"]["claim_token"],
                                    "run_number": dead["run_number"],
                                    "request_id": request_id,
                                    "usage": saved["usage"],
                                }
                            )
                            amount = token_cost(
                                usage.usage, Price.model_validate(receipt["price"])
                            )[0]
                            bucket = "reserved" if receipt["status"] == "reserved" else "unknown"
                            state[bucket] = str(
                                Decimal(state[bucket]) - Decimal(receipt["reserved"])
                            )
                            state["cost"] = str(Decimal(state["cost"]) + amount)
                            receipt.update(
                                status="settled",
                                response_status=saved["status"],
                                cost=str(amount),
                                settlement=usage.model_dump(
                                    mode="json", exclude={"claim_token", "run_number"}
                                ),
                            )
                            await self.emit(
                                conn,
                                tid,
                                "progress.usage.settled",
                                {
                                    "job_id": str(dead["id"]),
                                    "request_id": request_id,
                                    "cost": str(amount),
                                    "usage": saved["usage"],
                                    "pricing": receipt["pricing"],
                                    "recovered": True,
                                },
                            )
                    active = saved_state.get("bbx_progress_active_request", {})
                    failed_terminal = next(
                        (
                            receipt
                            for receipt in saved_state.get("bbx_progress_receipts", {}).values()
                            if receipt.get("job_id") == str(dead["id"])
                            and receipt.get("status") == "failed"
                            and receipt.get("usage_known") is True
                        ),
                        None,
                    )
                    if (
                        active.get("status") == "preparing"
                        and active.get("request_id") not in dead["data"]["receipts"]
                        and session
                    ):
                        saved_state.pop("bbx_progress_active_request", None)
                        saved = await self.conversations._save_session(
                            conn,
                            tid,
                            AGENT_ID,
                            session["session"],
                            session["opening_instructions"],
                            session["origin"],
                            session["revision"],
                        )
                        session["revision"] = saved["revision"]
                        active = {}
                    if (
                        dead["run_number"] == task["run_number"]
                        and session is not None
                        and not active
                        and failed_terminal is None
                        and all(r["status"] == "settled" for r in dead["data"]["receipts"].values())
                    ):
                        dead["data"]["lease_until"] = (
                            now() + timedelta(seconds=LEASE_SECONDS)
                        ).isoformat()
                        await self.save_job(conn, dead)
                        await self.save(conn, tid, state)
                        message = None
                        if dead["kind"] == "question":
                            row = (
                                (
                                    await conn.execute(
                                        select(s.agent_messages).where(
                                            s.agent_messages.c.id == dead["id"]
                                        )
                                    )
                                )
                                .mappings()
                                .one()
                            )
                            message = _public(row)
                        return {
                            "job_id": dead["id"],
                            "task_id": tid,
                            "kind": dead["kind"],
                            "run_number": dead["run_number"],
                            "claim_token": dead["data"]["claim_token"],
                            "through_version": dead["data"]["through_version"],
                            "base_revision": dead["data"]["base_revision"],
                            "source_version": state["source_version"],
                            "settings": dead["data"]["settings"],
                            "model": dead["data"]["model"],
                            "message": message,
                            "node_id": dead["data"].get("node_id"),
                            "session": session,
                            "tree": dead["data"].get("candidate")
                            or {
                                k: state[k]
                                for k in ("overview", "nodes", "revision", "source_version")
                            },
                        }
                    self.unknown_reservations(state, dead["data"])
                    dead["status"] = "failed"
                    dead["data"]["error"] = (
                        "Model response failed; confirmed usage was recovered"
                        if failed_terminal
                        else "Lease expired; request outcome requires confirmation"
                    )
                    if failed_terminal:
                        dead["data"]["diagnostic"] = failed_terminal.get("diagnostic")
                        state["diagnostic"] = failed_terminal.get("diagnostic")
                    await self.save_job(conn, dead)
                    await self.fail_question(conn, dead, dead["data"]["error"])
                    await self.emit(
                        conn,
                        tid,
                        "progress.turn.failed",
                        {
                            "job_id": str(dead["id"]),
                            "error": dead["data"]["error"],
                            "retryable": False,
                        },
                    )
                    state.update(
                        status="paused" if Decimal(state["unknown"]) or failed_terminal else "idle",
                        error=dead["data"]["error"],
                    )
                    await self.fail_blocked_queue(conn, tid, state, cause_job_id=dead["id"])
                if state["status"] in {"paused", "budget_exhausted"} or self.occupied(
                    state
                ) >= Decimal(state["settings"]["max_cost"]):
                    await self.save(conn, tid, state)
                    continue
                if Decimal(state["unknown"]) == 0:
                    await self.refresh_queue(conn, task, state, automatic=True)
                jobs = (
                    (
                        await conn.execute(
                            select(s.progress_jobs)
                            .where(
                                s.progress_jobs.c.task_id == tid,
                                s.progress_jobs.c.status == "queued",
                            )
                            .order_by(
                                (s.progress_jobs.c.kind == "question").desc(),
                                s.progress_jobs.c.created_at,
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in jobs:
                    job = dict(row)
                    data = job["data"]
                    if Decimal(state["unknown"]) > 0 and not data.get("continued_unknown"):
                        continue
                    if data.get("not_before") and stamp(data["not_before"]) > now():
                        continue
                    if (
                        job["kind"] == "refresh"
                        and data.get("automatic")
                        and state["last_started_at"]
                        and state.get("source_version", 0) >= state.get("last_target_version", 0)
                        and (now() - stamp(state["last_started_at"])).total_seconds()
                        < state["settings"]["min_interval_seconds"]
                    ):
                        continue
                    global_value = (
                        await conn.execute(
                            select(s.app_settings.c.value).where(s.app_settings.c.key == "progress")
                        )
                    ).scalar_one_or_none() or {}
                    effective_settings = {
                        **state["settings"],
                        "system_prompt": global_value.get("prompt", ""),
                        "prompt": global_value.get("prompt", ""),
                        "enabled_tools": global_value.get(
                            "enabled_tools",
                            [
                                "read_progress_sources",
                                "get",
                                "search",
                                "read_evidence",
                                "get_progress_tree",
                                "update_progress_tree",
                            ],
                        ),
                    }
                    effective_settings["model_id"] = effective_settings.get(
                        "model_id"
                    ) or global_value.get("model_id")
                    model = state.get("model") or await self.model(conn, task, effective_settings)
                    floor = max(
                        state["source_version"],
                        state["session_source_version"],
                        state.get("session_knowledge_version", 0),
                    )
                    if floor > state["source_version"]:
                        through = floor
                    else:
                        versions = (
                            (
                                await conn.execute(
                                    select(s.events.c.version)
                                    .where(
                                        s.events.c.task_id == tid,
                                        s.events.c.version > floor,
                                        s.events.c.type.in_(SOURCE_TYPES),
                                    )
                                    .order_by(s.events.c.version)
                                    .limit(MAX_SOURCE_EVENTS)
                                )
                            )
                            .scalars()
                            .all()
                        )
                        through = versions[-1] if versions else floor
                    data.update(
                        claim_token=str(uuid4()),
                        lease_until=(now() + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                        through_version=through,
                        base_revision=state["revision"],
                        scanned_version=state["source_version"],
                        candidate=None,
                        model=model,
                        settings=effective_settings,
                        attempts=data["attempts"] + 1,
                    )
                    job.update(status="running", run_number=task["run_number"])
                    await self.save_job(conn, job)
                    state.update(
                        status="running",
                        last_started_at=now().isoformat(),
                        run_number=task["run_number"],
                        last_target_version=state["desired_source_version"],
                    )
                    await self.save(conn, tid, state)
                    session = await self.conversations._session(conn, tid, AGENT_ID)
                    message = None
                    if job["kind"] == "question":
                        message = (
                            (
                                await conn.execute(
                                    update(s.agent_messages)
                                    .where(s.agent_messages.c.id == job["id"])
                                    .values(status="processing", updated_at=now())
                                    .returning(s.agent_messages)
                                )
                            )
                            .mappings()
                            .one()
                        )
                        message = _public(message)
                    return {
                        "job_id": job["id"],
                        "task_id": tid,
                        "kind": job["kind"],
                        "run_number": job["run_number"],
                        "claim_token": data["claim_token"],
                        "through_version": through,
                        "base_revision": state["revision"],
                        "source_version": state["source_version"],
                        "settings": effective_settings,
                        "model": model,
                        "message": message,
                        "node_id": data.get("node_id"),
                        "session": session,
                        "tree": {
                            k: state[k] for k in ("overview", "nodes", "revision", "source_version")
                        },
                    }
                await self.save(conn, tid, state)
        return None

    @staticmethod
    def unknown_reservations(state, data):
        for receipt in data["receipts"].values():
            if receipt["status"] == "reserved":
                receipt["status"] = "unknown"
                state["reserved"] = str(Decimal(state["reserved"]) - Decimal(receipt["reserved"]))
                state["unknown"] = str(Decimal(state["unknown"]) + Decimal(receipt["reserved"]))

    async def sources(self, tid, jid, fence, after, limit=50):
        async with self.repo.engine.begin() as conn:
            _, job = await self.job(conn, tid, jid, fence)
            data = job["data"]
            check(after <= data["scanned_version"], "Cannot skip unscanned sources")
            through = data["through_version"]
            rows = (
                (
                    await conn.execute(
                        select(s.events)
                        .where(
                            s.events.c.task_id == tid,
                            s.events.c.version > after,
                            s.events.c.version <= through,
                            s.events.c.type.in_(SOURCE_TYPES),
                        )
                        .order_by(s.events.c.version)
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            selected, size = [], 0
            for row in rows[:limit]:
                value = dict(row)
                length = len(json.dumps(storage_safe(value), default=str).encode())
                check(length <= 1_000_000, "Source event exceeds the supported page size", 413)
                if selected and size + length > 500_000:
                    break
                selected.append(value)
                size += length
            more = len(rows) > len(selected)
            cursor = selected[-1]["version"] if more and selected else through
            data["scanned_version"] = max(data["scanned_version"], cursor)
            data["lease_until"] = (now() + timedelta(seconds=LEASE_SECONDS)).isoformat()
            await self.save_job(conn, job)
            return {
                "events": selected,
                "after_version": after,
                "next_version": cursor,
                "through_version": through,
                "has_more": more,
            }

    async def historical_objects(self, conn, tid, through, ids=None):
        query = select(s.events).where(
            s.events.c.task_id == tid,
            s.events.c.version <= through,
            s.events.c.type.in_(SOURCE_TYPES),
        )
        if ids is not None:
            query = query.where(
                (s.events.c.object_id.in_(ids))
                | (s.events.c.payload["intent_id"].astext.in_(ids))
                | (s.events.c.payload["fact_id"].astext.in_(ids))
            )
        events = [
            dict(r) for r in (await conn.execute(query.order_by(s.events.c.version))).mappings()
        ]
        return fold_sources(events)

    async def authorize_evidence_uri(self, conn, tid, uri: str, sha256: str, through: int):
        parts = uri.split("/")
        check(
            "\\" not in uri and all(part not in {"", ".", ".."} for part in parts),
            "Evidence URI does not belong to this task",
            403,
        )
        if uri.startswith(
            tuple(f"{prefix}/{tid}/" for prefix in ("evidence", "toolcalls", "traces"))
        ):
            return
        check(
            len(parts) >= 4 and parts[0] == "inputs",
            "Evidence URI does not belong to this task",
            403,
        )
        try:
            group_id = UUID(parts[1])
        except ValueError:
            raise HTTPException(403, "Evidence URI does not belong to this task") from None
        created = (
            await conn.execute(
                select(s.events.c.payload)
                .where(
                    s.events.c.task_id == tid,
                    s.events.c.type == "task.created",
                    s.events.c.version <= through,
                )
                .order_by(s.events.c.version)
                .limit(1)
            )
        ).scalar_one_or_none()
        registered = (created or {}).get("initial_attachments", [])
        check(
            any(entry.get("uri") == uri and entry.get("sha256") == sha256 for entry in registered),
            "Initial evidence is absent from this task's historical attachment manifest",
            403,
        )
        group = (
            (
                await conn.execute(
                    select(s.task_input_groups).where(
                        s.task_input_groups.c.id == group_id,
                        s.task_input_groups.c.bound_task_id == tid,
                        s.task_input_groups.c.deleting.is_(False),
                    )
                )
            )
            .mappings()
            .first()
        )
        check(
            group is not None
            and any(
                entry.get("uri") == uri and entry.get("sha256") == sha256
                for entry in group["files"]
            ),
            "Initial evidence group is not bound to this task",
            403,
        )

    async def validate_refs(self, conn, tid, tree, through):
        refs = [
            ref
            for node in tree["nodes"]
            for ref in [
                *node["source_refs"],
                *(r for step in node["key_steps"] for r in step["source_refs"]),
            ]
        ]
        if not refs:
            return
        versions = {r["version"] for r in refs}
        found = {
            r["version"]: dict(r)
            for r in (
                await conn.execute(
                    select(s.events).where(
                        s.events.c.task_id == tid,
                        s.events.c.version.in_(versions),
                        s.events.c.version <= through,
                    )
                )
            ).mappings()
        }
        authorized_evidence = set()
        for ref in refs:
            check(ref["version"] in found, "Source belongs to another task or future version", 422)
            evt = found[ref["version"]]
            kind = ref["kind"]
            check(evt["type"] in SOURCE_TYPES, "Unsupported source event", 422)
            if kind in {"fact", "intent"}:
                check(
                    evt["object_id"] == ref["id"] or evt["payload"].get(f"{kind}_id") == ref["id"],
                    "Source ID mismatch",
                    422,
                )
                check(evt["type"].startswith(kind + "."), "Source kind mismatch", 422)
            elif kind == "task":
                check(
                    ref["id"] == str(tid) and evt["type"] == "task.created",
                    "Task source mismatch",
                    422,
                )
            elif kind == "acceptance":
                check(
                    evt["type"] in {"acceptance.judged", "acceptance.reverted"},
                    "Acceptance source mismatch",
                    422,
                )
                task_acceptance = (
                    await conn.execute(select(s.tasks.c.acceptance).where(s.tasks.c.id == tid))
                ).scalar_one()
                ids = {item["id"] for item in task_acceptance}
                event_ids = (
                    {
                        v.get("id") or v.get("acceptance_id")
                        for v in evt["payload"].get("verdicts", [])
                    }
                    if evt["type"] == "acceptance.judged"
                    else {evt["payload"].get("id") or evt["payload"].get("acceptance_id")}
                )
                check(
                    ref["id"] in ids and ref["id"] in event_ids,
                    "Acceptance source ID mismatch",
                    422,
                )
            elif kind == "event":
                check(ref["id"] == str(evt["version"]), "Event source mismatch", 422)
            elif kind == "evidence":
                evidence = evt["payload"].get("evidence", []) + evt["payload"].get(
                    "initial_attachments", []
                )
                match = next((e for e in evidence if e.get("uri") == ref.get("evidence_uri")), None)
                check(
                    match is not None
                    and ref.get("sha256")
                    and match.get("sha256") == ref["sha256"],
                    "Historical evidence identity cannot be verified",
                    422,
                )
                identity = (ref["evidence_uri"], ref["sha256"], ref["version"])
                if identity not in authorized_evidence:
                    await self.authorize_evidence_uri(conn, tid, *identity)
                    authorized_evidence.add(identity)

    async def candidate(self, tid, jid, body):
        async with self.repo.engine.begin() as conn:
            task, job = await self.job(conn, tid, jid, body)
            state = await self.state(conn, task)
            check(state["revision"] == job["data"]["base_revision"], "Graph revision conflict")
            tree = job["data"].get("candidate") or state
            candidate = apply_candidate(tree, body)
            await self.validate_refs(conn, tid, candidate, job["data"]["through_version"])
            job["data"]["candidate"] = candidate
            await self.save_job(conn, job)
            return {"saved": True, "published": False, "revision": state["revision"]}

    async def checkpoint(self, tid, jid, body):
        async with self.repo.engine.begin() as conn:
            task, job = await self.job(conn, tid, jid, body)
            state = await self.state(conn, task)
            check(
                state["session_source_version"]
                <= body.source_version
                <= job["data"]["through_version"],
                "Session source boundary conflict",
            )
            check(
                body.knowledge_version is None
                or body.knowledge_version <= job["data"]["through_version"],
                "Session knowledge exceeds the job boundary",
            )
            check(
                body.source_version <= job["data"]["scanned_version"],
                "Sources have not been scanned",
            )
            result = await self.conversations._save_session(
                conn,
                tid,
                AGENT_ID,
                body.session,
                body.opening_instructions,
                body.origin,
                body.expected_revision,
            )
            state["session_source_version"] = body.source_version
            await self.save(conn, tid, state)
            job["data"]["lease_until"] = (now() + timedelta(seconds=LEASE_SECONDS)).isoformat()
            job["data"]["checkpoint_revision"] = result["revision"]
            await self.save_job(conn, job)
            return result

    async def reserve(self, tid, jid, body):
        async with self.repo.engine.begin() as conn:
            task, job = await self.job(conn, tid, jid, body)
            state = await self.state(conn, task)
            receipts = job["data"]["receipts"]
            key = str(body.request_id)
            args = {"input_tokens": body.input_tokens, "max_output_tokens": body.max_output_tokens}
            if key in receipts:
                check(receipts[key]["args"] == args, "Usage request ID conflict")
                return receipts[key]
            check(state["settings"]["enabled"], "Progress is disabled")
            check(
                not any(r["status"] in {"reserved", "unknown"} for r in receipts.values()),
                "Previous request outcome must be confirmed",
            )
            model = ModelConfig.model_validate(job["data"]["model"])
            price, pricing = effective_price(model, now())
            check(
                price.currency == "CNY" and price.is_complete(),
                "Complete CNY model prices required",
            )
            assert (
                price.cache_hit_per_m is not None
                and price.cache_miss_per_m is not None
                and price.output_per_m is not None
            )
            # Reserve at the most expensive input rate; a cache guess cannot lower the cap.
            amount = (
                max(price.cache_hit_per_m, price.cache_miss_per_m) * body.input_tokens
                + price.output_per_m * body.max_output_tokens
            ) / Decimal(1_000_000)
            check(
                body.max_output_tokens <= effective_max_output_tokens(model),
                "Output limit exceeds model configuration",
            )
            check(
                self.occupied(state) + amount <= Decimal(state["settings"]["max_cost"]),
                "Progress budget exhausted",
                402,
            )
            receipt = {
                "args": args,
                "status": "reserved",
                "reserved": str(amount),
                "pricing": pricing,
                "price": price.model_dump(mode="json"),
                "requested_at": now().isoformat(),
                "checkpoint_revision": job["data"].get("checkpoint_revision", 0),
            }
            receipts[key] = receipt
            state["reserved"] = str(Decimal(state["reserved"]) + amount)
            await self.save_job(conn, job)
            await self.save(conn, tid, state)
            return receipt

    async def settle(self, tid, jid, body):
        async with self.repo.engine.begin() as conn:
            task, job = await self.job(conn, tid, jid, body, accounting=True)
            state = await self.state(conn, task)
            receipt = job["data"]["receipts"].get(str(body.request_id))
            check(receipt is not None, "Usage reservation not found", 404)
            payload = body.model_dump(mode="json", exclude={"claim_token", "run_number"})
            if receipt["status"] == "settled":
                check(receipt["settlement"] == payload, "Usage settlement conflict")
                return receipt
            previous = receipt["status"]
            if body.executed and body.usage_known:
                session = await self.conversations._session(conn, tid, AGENT_ID)
                saved = (
                    (session or {})
                    .get("session", {})
                    .get("state", {})
                    .get("bbx_progress_receipts", {})
                    .get(str(body.request_id))
                )
                check(
                    saved is not None
                    and saved.get("status") in {"completed", "failed"}
                    and saved.get("job_id") == str(jid)
                    and saved.get("usage_known") is True
                    and saved.get("usage") == body.usage.model_dump(mode="json"),
                    "Usage is absent from the confirmed progress Session",
                )
                assert saved is not None
                receipt["response_status"] = saved["status"]
            if body.executed is None or (body.executed and not body.usage_known):
                if previous == "reserved":
                    state["reserved"] = str(
                        Decimal(state["reserved"]) - Decimal(receipt["reserved"])
                    )
                    state["unknown"] = str(Decimal(state["unknown"]) + Decimal(receipt["reserved"]))
                receipt.update(status="unknown", settlement=payload)
            else:
                amount = (
                    token_cost(body.usage, Price.model_validate(receipt["price"]))[0]
                    if body.executed
                    else Decimal(0)
                )
                bucket = "reserved" if previous == "reserved" else "unknown"
                state[bucket] = str(Decimal(state[bucket]) - Decimal(receipt["reserved"]))
                state["cost"] = str(Decimal(state["cost"]) + amount)
                receipt.update(status="settled", cost=str(amount), settlement=payload)
                await self.emit(
                    conn,
                    tid,
                    "progress.usage.settled",
                    {
                        "job_id": str(jid),
                        "request_id": str(body.request_id),
                        "cost": str(amount),
                        "usage": body.usage.model_dump(mode="json"),
                        "pricing": receipt["pricing"],
                    },
                )
            await self.save_job(conn, job)
            await self.save(conn, tid, state)
            return receipt

    async def complete(self, tid, jid, body):
        async with self.repo.engine.begin() as conn:
            task, job = await self.job(conn, tid, jid, body, completed=True)
            if job["status"] == "completed":
                return job["data"]["result"]
            state = await self.state(conn, task)
            data = job["data"]
            check(
                data["scanned_version"] >= data["through_version"],
                "Sources have not been fully scanned",
            )
            check(
                all(r["status"] == "settled" for r in data["receipts"].values()),
                "Usage outcome requires confirmation",
            )
            check(
                not any(r.get("response_status") == "failed" for r in data["receipts"].values()),
                "Failed model responses cannot publish progress",
            )
            check(state["revision"] == data["base_revision"], "Graph revision conflict")
            previous_nodes = {node["id"]: node for node in state["nodes"]}
            candidate = data.get("candidate")
            check(
                not data["receipts"] or data.get("checkpoint_revision", 0) > 0,
                "Model calls require a persisted Session checkpoint",
            )
            if candidate:
                await self.validate_refs(conn, tid, candidate, data["through_version"])
                state.update(
                    overview=candidate["overview"],
                    nodes=candidate["nodes"],
                    revision=state["revision"] + 1,
                )
            state.update(
                source_version=data["through_version"],
                scanned_version=data["scanned_version"],
                updated_at=now().isoformat(),
                error=None,
                status="idle" if state["settings"]["enabled"] else "disabled",
            )
            if job["kind"] == "question":
                check(body.content.strip(), "Question requires an answer", 422)
                await conn.execute(
                    update(s.agent_messages)
                    .where(s.agent_messages.c.id == jid)
                    .values(status="completed")
                )
                reply = (
                    (
                        await conn.execute(
                            insert(s.agent_messages)
                            .values(
                                id=uuid4(),
                                task_id=tid,
                                agent_id=AGENT_ID,
                                role="assistant",
                                content=storage_safe(body.content),
                                status="completed",
                                reply_to=jid,
                                usage={"source_version": data["through_version"]},
                            )
                            .returning(s.agent_messages)
                        )
                    )
                    .mappings()
                    .one()
                )
                await self.emit(conn, tid, "progress.message.replied", {"message": _public(reply)})
            evt = await self.emit(
                conn,
                tid,
                "progress.tree.updated",
                {
                    "overview": state["overview"],
                    "upserts": [
                        node for node in state["nodes"] if previous_nodes.get(node["id"]) != node
                    ],
                    "removed_ids": sorted(
                        set(previous_nodes) - {node["id"] for node in state["nodes"]}
                    ),
                    "revision": state["revision"],
                    "source_version": state["source_version"],
                    "run_number": task["run_number"],
                    "updated_at": state["updated_at"],
                },
            )
            state["published_version"] = evt["version"]
            await self.emit(
                conn,
                tid,
                "progress.turn.completed",
                {"job_id": str(jid), "source_version": state["source_version"]},
            )
            job["status"] = "completed"
            data["result"] = {
                "completed": True,
                "revision": state["revision"],
                "source_version": state["source_version"],
                "published_version": evt["version"],
            }
            await self.save_job(conn, job)
            await self.save(conn, tid, state)
            return data["result"]

    async def fail(self, tid, jid, body):
        async with self.repo.engine.begin() as conn:
            task, job = await self.job(conn, tid, jid, body)
            state = await self.state(conn, task)
            self.unknown_reservations(state, job["data"])
            unknown = any(r["status"] == "unknown" for r in job["data"]["receipts"].values())
            retry = (
                body.retryable
                and not body.budget_exhausted
                and not unknown
                and job["data"]["attempts"] < 3
            )
            queued_refresh = (
                (
                    await conn.execute(
                        select(s.progress_jobs.c.id).where(
                            s.progress_jobs.c.task_id == tid,
                            s.progress_jobs.c.kind == "refresh",
                            s.progress_jobs.c.status == "queued",
                        )
                    )
                ).first()
                if job["kind"] == "refresh"
                else None
            )
            job["status"] = "queued" if retry and queued_refresh is None else "failed"
            job["data"].update(
                error=body.error,
                budget_exhausted=body.budget_exhausted,
                not_before=(now() + timedelta(seconds=30 * job["data"]["attempts"])).isoformat(),
            )
            state.update(
                error=body.error,
                status="budget_exhausted"
                if body.budget_exhausted
                else "idle"
                if retry
                else "paused",
            )
            diagnostic = body.model_dump(mode="json").get("diagnostic")
            if diagnostic is not None:
                job["data"]["diagnostic"] = diagnostic
                state["diagnostic"] = diagnostic
            else:
                state.pop("diagnostic", None)
            await self.save_job(conn, job)
            await self.fail_question(conn, job, body.error)
            await self.fail_blocked_queue(conn, tid, state, cause_job_id=jid)
            await self.save(conn, tid, state)
            await self.emit(
                conn,
                tid,
                "progress.turn.failed",
                {
                    "error": body.error,
                    "retryable": retry,
                    "diagnostic": diagnostic,
                    "budget_exhausted": body.budget_exhausted,
                },
            )
            return {"status": job["status"]}

    async def view(self, tid, at_version=None):
        async with self.repo.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            task = await self.task(conn, tid)
            state = await self.state(conn, task)
            if at_version is not None:
                history = (
                    (
                        await conn.execute(
                            select(s.events)
                            .where(
                                s.events.c.task_id == tid,
                                s.events.c.type.in_(
                                    (
                                        "progress.tree.updated",
                                        "progress.settings.updated",
                                        "progress.usage.settled",
                                    )
                                ),
                                s.events.c.version <= at_version,
                            )
                            .order_by(s.events.c.version)
                        )
                    )
                    .mappings()
                    .all()
                )
                state = initial(task)
                nodes = {node["id"]: node for node in state["nodes"]}
                for evt in history:
                    payload = evt["payload"]
                    if evt["type"] == "progress.settings.updated":
                        state["settings"] = payload["settings"]
                        state["status"] = "idle" if payload["settings"]["enabled"] else "disabled"
                        continue
                    if evt["type"] == "progress.usage.settled":
                        state["cost"] = str(Decimal(state["cost"]) + Decimal(payload["cost"]))
                        continue
                    # Full snapshots remain readable if a pre-release deployment wrote one.
                    if "nodes" in payload:
                        nodes = {node["id"]: node for node in payload["nodes"]}
                    else:
                        for key in payload.get("removed_ids", []):
                            nodes.pop(key, None)
                        nodes.update({node["id"]: node for node in payload.get("upserts", [])})
                    state.update(
                        {
                            key: payload[key]
                            for key in (
                                "overview",
                                "revision",
                                "source_version",
                                "run_number",
                                "updated_at",
                            )
                            if key in payload
                        }
                    )
                    state["published_version"] = evt["version"]
                state["nodes"] = list(nodes.values())
                cutoff = at_version
            else:
                cutoff = await self.latest_source(conn, tid)
                state["desired_source_version"] = cutoff
            ids = {
                r["id"]
                for n in state["nodes"]
                for r in [
                    *n["source_refs"],
                    *(ref for step in n["key_steps"] for ref in step["source_refs"]),
                ]
                if r["kind"] in {"fact", "intent"}
            }
            objects = await self.historical_objects(conn, tid, cutoff, ids) if ids else {}
            verdict_events = (
                (
                    await conn.execute(
                        select(s.events)
                        .where(
                            s.events.c.task_id == tid,
                            s.events.c.version <= cutoff,
                            s.events.c.type.in_(("acceptance.judged", "acceptance.reverted")),
                        )
                        .order_by(s.events.c.version)
                    )
                )
                .mappings()
                .all()
            )
            acceptance = {}
            acceptance_versions = {}
            verdict_ids_by_version = {}
            for evt in verdict_events:
                event_ids = (
                    {
                        v.get("id") or v.get("acceptance_id")
                        for v in evt["payload"].get("verdicts", [])
                    }
                    if evt["type"] == "acceptance.judged"
                    else {evt["payload"].get("id") or evt["payload"].get("acceptance_id")}
                )
                verdict_ids_by_version[evt["version"]] = event_ids
                acceptance_versions.update({aid: evt["version"] for aid in event_ids})
                if evt["type"] == "acceptance.judged":
                    for verdict in evt["payload"].get("verdicts", []):
                        acceptance[verdict.get("id") or verdict.get("acceptance_id")] = verdict.get(
                            "verdict"
                        )
                else:
                    acceptance[evt["payload"].get("id") or evt["payload"].get("acceptance_id")] = (
                        "unmet"
                    )
            achieved = bool(task["acceptance"]) and all(
                acceptance.get(item["id"]) == "met" for item in task["acceptance"]
            )
            nodes = []
            for original in state["nodes"]:
                node = dict(original)
                refs = [
                    *node["source_refs"],
                    *(ref for step in node["key_steps"] for ref in step["source_refs"]),
                ]
                sources = [objects[r["id"]] for r in refs if r["id"] in objects]
                changed = any(
                    objects[r["id"]]["version"] > r["version"] for r in refs if r["id"] in objects
                )
                changed = changed or any(
                    acceptance_versions.get(aid, 0) > ref["version"]
                    for ref in refs
                    for aid in (
                        {ref["id"]}
                        if ref["kind"] == "acceptance"
                        else verdict_ids_by_version.get(ref["version"], set())
                        if ref["kind"] == "event"
                        else set()
                    )
                )
                disputed = any(o.get("status") == "disputed" for o in sources)
                rejected = any(o.get("result") == "rejected" for o in sources)
                effective = (
                    "sources_changed"
                    if changed
                    else "needs_evidence"
                    if disputed or not refs
                    else "excluded"
                    if rejected and node["kind"] == "attempt"
                    else "supported"
                )
                if node["kind"] == "gap" and not changed:
                    effective = "needs_evidence"
                if node["kind"] == "direction" and not refs:
                    effective = "exploring"
                if node["kind"] == "goal" and achieved and not changed:
                    effective = "achieved"
                node.update(
                    effective_state=effective,
                    participants=sorted({o["author"] for o in sources if o.get("author")}),
                    contributors=sorted({o["author"] for o in sources if o.get("author")}),
                    source_states=[
                        {
                            "id": o["id"],
                            "version": o["version"],
                            "status": o.get("status"),
                            "result": o.get("result"),
                            "author": o.get("author"),
                            "holder": o.get("holder"),
                        }
                        for o in sources
                    ],
                )
                nodes.append(node)
            cost = Decimal(state["cost"])
            model = ModelConfig.model_validate(state["model"]) if state.get("model") else None
            return {
                **state,
                "model_limits": {
                    "context_window": model.context_window,
                    "max_input_tokens": effective_max_input_tokens(model),
                    "max_output_tokens": effective_max_output_tokens(model),
                    "reasoning_effort": model.reasoning_effort,
                }
                if model is not None
                else None,
                "nodes": nodes,
                "enabled": state["settings"]["enabled"],
                "read_only": at_version is not None,
                "budget": {
                    "max_cost": state["settings"]["max_cost"],
                    "spent": str(cost),
                    "cost": str(cost),
                    "reserved": state["reserved"],
                    "unknown": state["unknown"],
                    "currency": "CNY",
                },
                "cost_breakdown": {
                    "solver": str(task["usage"].get("cost", 0)),
                    "progress": str(cost),
                    "total": str(Decimal(str(task["usage"].get("cost", 0))) + cost),
                },
            }


def service(request: Request) -> ProgressService:
    check(request.app.state.engine is not None, "Progress service unavailable", 503)
    return ProgressService(
        request.app.state.engine, request.app.state.objects, request.app.state.settings.profiles_dir
    )


def tool_fence(request: Request, tid: UUID):
    identity = require_task_reader(request, tid)
    check(
        identity.purpose == "progress"
        and identity.progress_job_id is not None
        and identity.progress_claim_token is not None
        and identity.progress_run_number is not None,
        "Progress capability required",
        403,
    )
    assert identity.progress_claim_token is not None and identity.progress_run_number is not None
    return identity.progress_job_id, ProgressFence(
        claim_token=identity.progress_claim_token, run_number=identity.progress_run_number
    )


@router.get("/progress/settings")
async def global_get(request: Request):
    require_user_or_service(request)
    return await service(request).global_settings()


@router.put("/progress/settings")
async def global_put(request: Request, body: ProgressGlobalSettings):
    require_user_or_service(request)
    return await service(request).global_settings(body)


@router.get("/tasks/{tid}/progress")
async def progress_get(request: Request, tid: UUID, at_version: int | None = Query(None, ge=0)):
    identity = require_task_reader(request, tid)
    if identity.purpose == "progress":
        jid, fence = tool_fence(request, tid)
        async with service(request).repo.engine.begin() as conn:
            task, job = await service(request).job(conn, tid, jid, fence)
            check(at_version is None, "Historical replay is a user-only read", 403)
            state = await service(request).state(conn, task)
            return {key: state[key] for key in ("overview", "nodes", "revision", "source_version")}
    return await service(request).view(tid, at_version)


@router.get("/tasks/{tid}/progress/settings")
async def settings_get(request: Request, tid: UUID):
    require_user_or_service(request)
    return (await service(request).view(tid))["settings"]


@router.put("/tasks/{tid}/progress/settings")
async def settings_put(request: Request, tid: UUID, body: ProgressSettings):
    require_user_or_service(request)
    check("at_version" not in request.query_params, "Historical progress is read-only", 403)
    await service(request).settings(tid, body)
    return await service(request).view(tid)


@router.post("/tasks/{tid}/progress/refresh")
async def refresh_post(request: Request, tid: UUID, body: ProgressRefresh):
    require_user_or_service(request)
    check("at_version" not in request.query_params, "Historical progress is read-only", 403)
    return await service(request).refresh(tid, body.request_id)


@router.get("/tasks/{tid}/progress/messages")
async def messages_get(
    request: Request,
    tid: UUID,
    limit: int = Query(100, ge=1, le=200),
    before: datetime | None = None,
):
    require_user_or_service(request)
    return await service(request).messages(tid, limit, before)


@router.post("/tasks/{tid}/progress/messages")
async def messages_post(request: Request, tid: UUID, body: ProgressQuestion):
    require_user_or_service(request)
    check("at_version" not in request.query_params, "Historical progress is read-only", 403)
    return await service(request).question(tid, body)


@router.post("/progress/jobs/claim")
async def claim_post(request: Request):
    require_service(request)
    result = await service(request).claim()
    if result:
        result["token"] = issue_progress_token(
            request.app.state.settings,
            result["task_id"],
            result["job_id"],
            UUID(result["claim_token"]),
            result["run_number"],
        )
    return result


@router.get("/tasks/{tid}/progress/sources")
async def sources_get(
    request: Request,
    tid: UUID,
    after_version: int = Query(0, ge=0),
    through_version: int | None = Query(None, ge=0),
    object_id: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=100),
):
    identity = require_task_reader(request, tid)
    if identity.purpose == "progress":
        jid, fence = tool_fence(request, tid)
        result = await service(request).sources(tid, jid, fence, after_version, limit)
        check(
            through_version is None or through_version == result["through_version"],
            "Source upper bound differs from the job",
        )
        return result
    require_user_or_service(request)
    svc = service(request)
    async with svc.repo.engine.connect() as conn:
        await svc.task(conn, tid)
        latest = await svc.latest_source(conn, tid)
        through = latest if through_version is None else through_version
        latest_event = int(
            (
                await conn.execute(
                    select(func.max(s.events.c.version)).where(s.events.c.task_id == tid)
                )
            ).scalar_one()
            or 0
        )
        check(through <= latest_event, "Source upper bound is in the future", 422)
        if object_id is not None:
            objects = await svc.historical_objects(conn, tid, through, [object_id])
            if object_id not in objects:
                query = select(s.events).where(
                    s.events.c.task_id == tid, s.events.c.version <= through
                )
                if object_id == str(tid):
                    query = query.where(s.events.c.type == "task.created")
                elif object_id.isdecimal():
                    query = query.where(s.events.c.version == int(object_id))
                else:
                    query = query.where(
                        s.events.c.type.in_(("acceptance.judged", "acceptance.reverted"))
                    )
                events = (
                    (await conn.execute(query.order_by(s.events.c.version.desc()))).mappings().all()
                )
                for evt in events:
                    if object_id == str(tid):
                        payload = evt["payload"]
                        objects[object_id] = {
                            key: payload.get(key)
                            for key in (
                                "name",
                                "goal",
                                "acceptance",
                                "domain_context",
                                "initial_attachments",
                            )
                        }
                        objects[object_id].update(
                            id=object_id,
                            statement=payload["goal"],
                            author=evt["actor"],
                            version=evt["version"],
                            created_version=evt["version"],
                        )
                        break
                    if object_id.isdecimal():
                        objects[object_id] = {
                            "id": object_id,
                            "statement": evt["type"],
                            "author": evt["actor"],
                            "version": evt["version"],
                            "record": evt["payload"],
                        }
                        break
                    payload = evt["payload"]
                    verdict = next(
                        (
                            v
                            for v in payload.get("verdicts", [])
                            if (v.get("id") or v.get("acceptance_id")) == object_id
                        ),
                        None,
                    )
                    if verdict or (
                        evt["type"] == "acceptance.reverted" and payload.get("id") == object_id
                    ):
                        objects[object_id] = {
                            "id": object_id,
                            "statement": (verdict or {}).get("completion_reason")
                            or (verdict or {}).get("missing")
                            or "验收裁定",
                            "status": (verdict or {}).get("verdict", "unmet"),
                            "author": evt["actor"],
                            "version": evt["version"],
                            "record": verdict or payload,
                        }
                        break
            check(object_id in objects, "Source object not found", 404)
            value = objects[object_id]
            value["evidence"] = [
                {
                    **entry,
                    "historical_content_verified": False,
                    "registered_uri": entry.get("uri"),
                    "uri": None,
                    "registration_version": value.get("created_version", value.get("version")),
                    "summary": entry["summary"]
                    + (
                        "（历史证据内容不可校验）"
                        if not entry.get("sha256")
                        else "（需校验登记内容身份后读取）"
                    ),
                }
                for entry in value.get("evidence", [])
            ]
            return {"object": value, "state": value.get("status"), "through_version": through}
        rows = (
            (
                await conn.execute(
                    select(s.events)
                    .where(
                        s.events.c.task_id == tid,
                        s.events.c.version > after_version,
                        s.events.c.version <= through,
                        s.events.c.type.in_(SOURCE_TYPES),
                    )
                    .order_by(s.events.c.version)
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        values, size = [], 0
        for row in rows[:limit]:
            value = dict(row)
            length = len(json.dumps(jsonable_encoder(value), ensure_ascii=False).encode())
            check(length <= 1_000_000, "Source event exceeds the supported page size", 413)
            if values and size + length > 500_000:
                break
            values.append(value)
            size += length
        more = len(rows) > len(values)
        return {
            "events": values,
            "after_version": after_version,
            "next_version": values[-1]["version"] if more else through,
            "has_more": more,
            "through_version": through,
        }


@router.get("/tasks/{tid}/progress/objects/{oid}")
async def object_get(request: Request, tid: UUID, oid: str):
    jid, fence = tool_fence(request, tid)
    svc = service(request)
    async with svc.repo.engine.begin() as conn:
        _, job = await svc.job(conn, tid, jid, fence)
        objects = await svc.historical_objects(conn, tid, job["data"]["through_version"], [oid])
        check(oid in objects, "Source object not found", 404)
        return objects[oid]


@router.get("/tasks/{tid}/progress/search")
async def search_get(
    request: Request,
    tid: UUID,
    q: str = Query(min_length=1, max_length=200),
    after_version: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
):
    jid, fence = tool_fence(request, tid)
    svc = service(request)
    async with svc.repo.engine.begin() as conn:
        _, job = await svc.job(conn, tid, jid, fence)
        rows = (
            (
                await conn.execute(
                    select(s.events)
                    .where(
                        s.events.c.task_id == tid,
                        s.events.c.version > after_version,
                        s.events.c.version <= job["data"]["through_version"],
                        s.events.c.type.in_(("fact.posted", "intent.posted")),
                        s.events.c.payload["statement"].astext.icontains(q, autoescape=True),
                    )
                    .order_by(s.events.c.version)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        return {
            "items": [dict(r) for r in rows],
            "through_version": job["data"]["through_version"],
            "next_version": rows[-1]["version"] if rows else after_version,
        }


@router.get("/tasks/{tid}/progress/evidence")
async def evidence_get(
    request: Request,
    tid: UUID,
    version: int = Query(ge=0),
    uri: str = Query(max_length=1000),
    sha256: str = Query(pattern=r"^[a-f0-9]{64}$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(16000, ge=1, le=64000),
):
    identity = require_task_reader(request, tid)
    svc = service(request)
    async with svc.repo.engine.begin() as conn:
        if identity.purpose == "progress":
            jid, fence = tool_fence(request, tid)
            _, job = await svc.job(conn, tid, jid, fence)
            through = job["data"]["through_version"]
        else:
            require_user_or_service(request)
            await svc.task(conn, tid)
            through = int(
                (
                    await conn.execute(
                        select(func.max(s.events.c.version)).where(s.events.c.task_id == tid)
                    )
                ).scalar_one()
                or 0
            )
        check(version <= through, "Future evidence denied", 403)
        evt = (
            (
                await conn.execute(
                    select(s.events).where(s.events.c.task_id == tid, s.events.c.version == version)
                )
            )
            .mappings()
            .first()
        )
        check(evt is not None, "Evidence registration not found", 404)
        assert evt is not None
        await svc.authorize_evidence_uri(conn, tid, uri, sha256, version)
        entries = evt["payload"].get("evidence", []) + evt["payload"].get("initial_attachments", [])
        entry = next(
            (e for e in entries if e.get("uri") == uri and e.get("sha256") == sha256), None
        )
        check(entry is not None, "Historical evidence content cannot be verified", 409)
    assert entry is not None
    check(svc.objects is not None, "Object service unavailable", 503)
    check(
        int(entry.get("size") or 0) <= 4_000_000, "Evidence too large for bounded text reader", 413
    )
    assert svc.objects is not None
    data = await svc.objects.get(uri)
    check(
        len(data) <= 4_000_000 and hashlib.sha256(data).hexdigest() == sha256,
        "Historical evidence content identity mismatch",
        409,
    )
    return {
        "content": data[offset : offset + limit].decode("utf-8", errors="replace"),
        "offset": offset,
        "next_offset": min(len(data), offset + limit),
        "has_more": offset + limit < len(data),
    }


@router.get("/tasks/{tid}/progress/session")
async def session_get(
    request: Request, tid: UUID, job_id: UUID, claim_token: UUID, run_number: int
):
    require_service(request)
    svc = service(request)
    async with svc.repo.engine.begin() as conn:
        await svc.job(
            conn, tid, job_id, ProgressFence(claim_token=claim_token, run_number=run_number)
        )
        row = await svc.conversations._session(conn, tid, AGENT_ID)
        check(row is not None, "Progress Session not found", 404)
        return row


@router.post("/tasks/{tid}/progress/jobs/{jid}/candidate")
async def candidate_post(request: Request, tid: UUID, jid: UUID, body: ProgressCandidate):
    identity = principal(request)
    if identity.kind != "service":
        token_jid, fence = tool_fence(request, tid)
        check(
            token_jid == jid
            and fence.claim_token == body.claim_token
            and fence.run_number == body.run_number,
            "Capability fence mismatch",
            403,
        )
    return await service(request).candidate(tid, jid, body)


@router.post("/tasks/{tid}/progress/jobs/{jid}/checkpoint")
async def checkpoint_post(request: Request, tid: UUID, jid: UUID, body: ProgressCheckpoint):
    require_service(request)
    return await service(request).checkpoint(tid, jid, body)


@router.put("/tasks/{tid}/progress/session")
async def session_put(request: Request, tid: UUID, job_id: UUID, body: ProgressCheckpoint):
    require_service(request)
    return await service(request).checkpoint(tid, job_id, body)


@router.post("/tasks/{tid}/progress/jobs/{jid}/reserve")
async def reserve_post(request: Request, tid: UUID, jid: UUID, body: ProgressReserve):
    require_service(request)
    return await service(request).reserve(tid, jid, body)


@router.post("/tasks/{tid}/progress/jobs/{jid}/settle")
async def settle_post(request: Request, tid: UUID, jid: UUID, body: ProgressSettle):
    require_service(request)
    return await service(request).settle(tid, jid, body)


@router.post("/tasks/{tid}/progress/jobs/{jid}/complete")
async def complete_post(request: Request, tid: UUID, jid: UUID, body: ProgressComplete):
    require_service(request)
    return await service(request).complete(tid, jid, body)


@router.post("/tasks/{tid}/progress/jobs/{jid}/fail")
async def fail_post(request: Request, tid: UUID, jid: UUID, body: ProgressFail):
    require_service(request)
    return await service(request).fail(tid, jid, body)
