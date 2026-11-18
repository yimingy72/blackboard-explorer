"""Once-only Pi sends and site-local accounting for explicitly frozen P6a calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from bbx_contracts.billing import add_usage
from bbx_contracts.pi_models import (
    PiCallDispatch,
    PiCallDispatchResult,
    PiCallPrepare,
    PiCallPrepared,
    PiCallSettle,
    PiModelRef,
    PiModelVersion,
)
from bbx_contracts.runner import BACKGROUND_HELPER_TOOLS
from bbx_contracts.runner_control import BoardRunIdentity
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import and_, false, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bbx_blackboard.auth import require_task_reader, require_user_or_service
from bbx_blackboard.board_runner_scope import (
    EXPLORATION_CONTRACT,
    board_runner_frozen,
    require_board_run,
)
from bbx_blackboard.domain.rules import event
from bbx_blackboard.gateway_auth import require_gateway_identity, task_direct_mcp
from bbx_blackboard.pi_registry import (
    RUNTIME_HEADER,
    registry,
    trusted_header,
)
from bbx_blackboard.pi_scopes import ordinary_scope, progress_scope, review_scope
from bbx_blackboard.runner_control import row
from bbx_blackboard.store import Repository
from bbx_blackboard.store import schema as s

CALLS = "/api/runtime/pi-model-calls"
RUNNER_HEADER = "x-bbx-runner-model"
PLAYGROUND_TTL = timedelta(minutes=5)
router = APIRouter(include_in_schema=False)
public = APIRouter(tags=["pi-fees"])
t = s.pi_model_calls


def denied(message: str = "The original Pi call is not authorized") -> HTTPException:
    return HTTPException(409, message)


async def freeze_pi_model(conn, task_id: UUID, authority: str, model: PiModelVersion) -> None:
    await conn.execute(
        insert(s.pi_task_models).values(
            task_id=task_id,
            authority=authority,
            snapshot=model.model_dump(mode="json"),
        )
    )


async def task_pi_model(conn, task_id: UUID) -> tuple[str, PiModelVersion] | None:
    found = (
        (await conn.execute(select(s.pi_task_models).where(s.pi_task_models.c.task_id == task_id)))
        .mappings()
        .first()
    )
    if found is None:
        return None
    return found["authority"], PiModelVersion.model_validate(found["snapshot"])


def uncertain():
    return or_(
        t.c.state == "unknown",
        and_(t.c.state == "complete", t.c.amount_usd.is_(None)),
        and_(t.c.state == "dispatched", t.c.updated_at < func.now() - text("interval '5 minutes'")),
    )


async def pi_totals(conn, task_id, site_id, purpose="solver") -> tuple[Decimal, int]:
    value, unknown = (
        await conn.execute(
            select(
                func.coalesce(func.sum(t.c.amount_usd), 0), func.count().filter(uncertain())
            ).where(t.c.task_id == task_id, t.c.site_id == site_id, t.c.purpose == purpose)
        )
    ).one()
    return Decimal(value), int(unknown)


async def pi_pending(conn, task_id, *, solver_only=False) -> bool:
    conditions = [t.c.task_id == task_id, t.c.state.in_(("dispatched", "unknown"))]
    if solver_only:
        conditions.append(t.c.purpose == "solver")
    return await conn.scalar(select(t.c.call_id).where(*conditions).limit(1)) is not None


async def pi_budget_available(conn, task_id, site_id) -> bool:
    task = await row(conn, s.tasks, task_id)
    used, unknown = await pi_totals(conn, task_id, site_id)
    return (
        task["budget"].get("currency") == "USD"
        and not unknown
        and used < Decimal(str(task["budget"]["max_cost"]))
    )


@dataclass(frozen=True)
class Scope:
    purpose: str
    agent: str
    turn: str
    model: PiModelVersion
    cap: Decimal | None
    request_user: str | None = None
    allowed_tools: list[str] | None = None
    max_output_tokens: int | None = None


class PiLedger:
    def __init__(self, request: Request, response: Response):
        self.registry = registry(request)
        settings = self.registry.settings
        trusted_header(request, RUNTIME_HEADER, settings.pi_model_runtime_token.get_secret_value())
        response.headers["Cache-Control"] = "no-store"
        self.request = request
        self.site = settings.pi_site_id
        self.authority = settings.pi_model_authority
        self.runner = request.app.state.runner_gateway
        self.repo = Repository(request.app.state.engine)
        if not self.registry.enabled:
            raise HTTPException(503, "Pi call accounting is not configured")

    def _identity(self, body):
        headers = self.request.headers.getlist(RUNNER_HEADER)
        if len(headers) != 1:
            raise HTTPException(401, "A scoped Runner capability is required")
        token = headers[0]
        if body.task_id is None:
            raise denied()
        identity = require_gateway_identity(
            self.request,
            body.task_id,
            "model",
            authorization=token if token.startswith("Bearer ") else "Bearer " + token,
        )
        if (
            identity.binding_id != body.proof.binding.binding_id
            or identity.fence != body.proof.fence
        ):
            raise denied()
        return identity

    async def scope(self, conn, body: PiCallPrepare) -> Scope:
        tid, proof = body.task_id, body.proof
        if proof.kind == "playground":
            item = await self.lookup(conn, body.call_id)
            if (
                proof.authority != self.authority
                or item["purpose"] != "playground"
                or item["task_id"] is not None
                or item["request_user"] != self.registry.settings.pi_model_operator
                or item["intent"] != body.model_dump(mode="json")
            ):
                raise denied("The original model test is not authorized")
            return Scope(
                "playground",
                "",
                str(proof.request_id),
                PiModelVersion.model_validate(item["model_snapshot"]),
                None,
                item["request_user"],
                allowed_tools=[],
                max_output_tokens=1024,
            )
        assert tid is not None
        frozen = await task_pi_model(conn, tid)
        if proof.kind == "review":
            turn, review = await review_scope(conn, tid, proof)
            if not isinstance(review, dict) or review.get("authority") != self.authority:
                raise denied("The original review did not freeze a Pi model")
            model = PiModelVersion.model_validate(review["pi_model"])
            if proof.message_id is not None:
                user = await conn.scalar(
                    select(s.agent_messages.c.request_user).where(
                        s.agent_messages.c.task_id == tid,
                        s.agent_messages.c.id == proof.message_id,
                    )
                )
            else:
                current = await row(conn, s.ctf_turns, tid, str(proof.turn_id))
                user = await conn.scalar(
                    select(s.ctf_messages.c.request_user)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.id.in_(current["assignment_message_ids"]),
                        s.ctf_messages.c.claim_turn_id == str(proof.turn_id),
                        s.ctf_messages.c.sender_kind == "user",
                        s.ctf_messages.c.purpose == "review",
                        s.ctf_messages.c.request_user.is_not(None),
                    )
                    .limit(1)
                )
            if not user:
                raise denied("Review must retain its original user")
            scope = Scope("review", proof.agent_id, turn, model, None, user)
        elif proof.kind == "progress":
            _, job, current = await progress_scope(conn, tid, proof)
            spec = job["data"]["pi_model"]
            if spec["authority"] != self.authority:
                raise denied("Progress model authority changed")
            model = PiModelVersion.model_validate(spec["snapshot"])
            scope = Scope(
                "progress",
                "__progress__",
                str(proof.job_id),
                model,
                Decimal(str(current["settings"]["max_cost"])),
            )
        elif frozen is None or frozen[0] != self.authority:
            raise denied("The Task did not freeze a model from this Pi authority")
        elif proof.kind == "ordinary":
            if await board_runner_frozen(conn, tid):
                raise denied("The original Blackboard task uses scoped native Pi execution")
            task, agent = await ordinary_scope(conn, tid, proof)
            if task["budget"].get("currency") != "USD":
                raise denied("The frozen Task budget is not USD")
            scope = Scope(
                "solver",
                proof.agent_id,
                f"{task['run_number']}:{agent['started_at'].isoformat()}:{agent['derive_round']}",
                frozen[1],
                Decimal(str(task["budget"]["max_cost"])),
            )
        else:
            if not await task_direct_mcp(conn, tid):
                raise denied("The original Runner does not use direct Pi execution")
            _, _, binding = await self.runner._scope(
                conn,
                tid,
                self._identity(body),
                sdk=proof.sdk_session_id,
                journal=proof.journal.model_dump(mode="json"),
            )
            spec = json.loads(binding["payload_json"])["spec"]
            turn = proof.sdk_session_id
            if binding["binding"]["business_contract"] == EXPLORATION_CONTRACT:
                original = BoardRunIdentity.model_validate(binding["business"]["board_run"])
                await require_board_run(
                    conn, tid, proof.binding.agent_id, original, action="model_send"
                )
                turn = self._board_turn(proof.sdk_session_id, original)
            if (
                binding["binding"] != proof.binding.model_dump(mode="json")
                or spec["profile_revision"] != proof.profile_revision
                or binding["prompt_revision"] != proof.prompt_revision
                or spec["model"].get("model_revision") != frozen[1].model_revision
                or spec["model"]["id"] != frozen[1].id
            ):
                raise denied("Runner model binding changed")
            task = await row(conn, s.tasks, tid)
            if task["budget"].get("currency") != "USD":
                raise denied("The frozen Task budget is not USD")
            allowed = set(spec["allowed_tools"])
            if binding["business"].get("native_assignment_id"):
                if spec.get("helper_foreground"):
                    allowed.add("delegate_helper")
                if spec.get("helper_background"):
                    allowed.update(BACKGROUND_HELPER_TOOLS)
            scope = Scope(
                "solver",
                proof.binding.agent_id,
                turn,
                frozen[1],
                Decimal(str(task["budget"]["max_cost"])),
                allowed_tools=sorted(allowed),
                max_output_tokens=spec["model"]["max_output_tokens"],
            )
        if (scope.model.id, scope.model.model_revision) != (body.model_id, body.model_revision):
            raise denied("The frozen model revision changed")
        return scope

    def key(self, call_id):
        return and_(t.c.site_id == self.site, t.c.call_id == call_id)

    async def lookup(self, conn, call_id, *, lock=False):
        query = select(t).where(self.key(call_id))
        if lock:
            query = query.with_for_update()
        found = (await conn.execute(query)).mappings().first()
        if found is None:
            raise denied("Pi call is not prepared")
        return found

    async def locked(self, conn, call_id, attempt_id):
        existing = await self.lookup(conn, call_id)
        if existing["task_id"] is None:
            current = await self.lookup(conn, call_id, lock=True)
        else:
            await self.repo.lock(conn, existing["task_id"])
            current = await self.lookup(conn, call_id)
        if current["attempt_id"] != attempt_id:
            raise denied("The original Pi attempt changed")
        return current

    async def prepare(self, body: PiCallPrepare) -> PiCallPrepared:
        if body.proof.kind == "playground":
            async with self.repo.engine.begin() as conn:
                item = await self.locked(conn, body.call_id, body.attempt_id)
                scope = await self.scope(conn, body)
                if (
                    item["state"] != "prepared"
                    or item["created_at"] < datetime.now(UTC) - PLAYGROUND_TTL
                ):
                    raise denied("The model test has crossed or expired its send window")
            return await self.material(body, item, scope)
        assert body.task_id is not None
        intent = body.model_dump(mode="json")
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, body.task_id)
            scope = await self.scope(conn, body)
            if scope.cap is not None and scope.model.usd_rates is None:
                raise denied("A budgeted Pi call requires a verified price")
            await conn.execute(
                pg_insert(t)
                .values(
                    site_id=self.site,
                    call_id=body.call_id,
                    attempt_id=body.attempt_id,
                    task_id=body.task_id,
                    purpose=scope.purpose,
                    agent_id=scope.agent,
                    turn_id=scope.turn,
                    request_user=scope.request_user,
                    logical_call_id=body.logical_call_id,
                    intent=intent,
                    model_snapshot=scope.model.model_dump(mode="json"),
                    state="prepared",
                )
                .on_conflict_do_nothing()
            )
            item = (
                (
                    await conn.execute(
                        select(t).where(
                            t.c.site_id == self.site,
                            t.c.task_id == body.task_id,
                            t.c.purpose == scope.purpose,
                            t.c.logical_call_id == body.logical_call_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            original = {
                key: value for key, value in intent.items() if key not in {"call_id", "attempt_id"}
            }
            if (
                item is None
                or {
                    key: value
                    for key, value in item["intent"].items()
                    if key not in {"call_id", "attempt_id"}
                }
                != original
                or item["state"] != "prepared"
                or item["request_user"] != scope.request_user
            ):
                raise denied("The original Pi call already crossed its send boundary or conflicts")
        return await self.material(body, item, scope)

    async def material(self, body, item, scope) -> PiCallPrepared:
        model, secret = await self.registry.frozen(
            PiModelRef(model_id=body.model_id, model_revision=body.model_revision)
        )
        if model != scope.model:
            raise denied("The frozen model document changed")
        return PiCallPrepared(
            model=model,
            api_key=secret,
            call_id=item["call_id"],
            attempt_id=item["attempt_id"],
            allowed_tools=scope.allowed_tools,
            max_output_tokens=scope.max_output_tokens,
        )

    async def dispatch(self, call_id: UUID, body: PiCallDispatch) -> PiCallDispatchResult:
        async with self.repo.engine.begin() as conn:
            current = await self.locked(conn, call_id, body.attempt_id)
            if current["state"] != "prepared":
                if current["request_sha256"] != body.request_sha256:
                    raise denied("The original Pi payload changed")
                return PiCallDispatchResult(send_allowed=False, state=current["state"])
            if (
                current["purpose"] == "playground"
                and current["created_at"] < datetime.now(UTC) - PLAYGROUND_TTL
            ):
                await conn.execute(
                    update(t)
                    .where(self.key(call_id))
                    .values(state="not_sent", updated_at=func.now())
                )
                return PiCallDispatchResult(send_allowed=False, state="not_sent")
            scope = await self.scope(conn, PiCallPrepare.model_validate(current["intent"]))
            if (
                scope.purpose != current["purpose"]
                or scope.turn != current["turn_id"]
                or scope.model.model_dump(mode="json") != current["model_snapshot"]
            ):
                raise denied("The original Pi scope changed")
            group = [
                t.c.site_id == self.site,
                t.c.task_id == current["task_id"],
                t.c.purpose == scope.purpose,
            ]
            if scope.purpose == "playground":
                group.append(t.c.call_id == call_id)
            if scope.cap is None:
                group.append(t.c.turn_id == scope.turn)
            unresolved = await conn.scalar(
                select(t.c.call_id)
                .where(
                    *group,
                    or_(
                        t.c.state == "unknown",
                        and_(t.c.state == "dispatched", t.c.agent_id == scope.agent),
                        uncertain() if scope.cap is not None else false(),
                    ),
                )
                .limit(1)
            )
            if unresolved is not None:
                raise denied("A previous Pi call requires confirmation")
            if scope.cap is not None:
                if scope.model.usd_rates is None:
                    raise denied("A budgeted Pi call requires a verified price")
                used = await conn.scalar(
                    select(func.coalesce(func.sum(t.c.amount_usd), 0)).where(*group)
                )
                if Decimal(used or 0) >= scope.cap:
                    raise HTTPException(429, "Pi budget threshold reached")
            await conn.execute(
                update(t)
                .where(self.key(call_id))
                .values(
                    state="dispatched", request_sha256=body.request_sha256, updated_at=func.now()
                )
            )
            return PiCallDispatchResult(send_allowed=True, state="dispatched")

    @staticmethod
    def _board_turn(sdk_session_id, identity):
        started = identity.started_at.isoformat()
        return f"{sdk_session_id}:{identity.run_number}:{started}:{identity.derive_round}"

    async def _project_board_usage(self, conn, current, body):
        intent = current["intent"]
        proof = intent.get("proof", {})
        wire = proof.get("binding", {})
        if (
            body.outcome != "complete"
            or proof.get("kind") != "native"
            or wire.get("business_contract") != EXPLORATION_CONTRACT
        ):
            return
        tid, aid = current["task_id"], current["agent_id"]
        bindings, agents = s.runner_bindings, s.agent_runs
        original = await conn.scalar(
            select(bindings.c.business["board_run"]).where(
                bindings.c.task_id == tid,
                bindings.c.id == wire.get("binding_id"),
                bindings.c.binding == wire,
            )
        )
        task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().first()
        agent = (
            (await conn.execute(select(agents).where(agents.c.task_id == tid, agents.c.id == aid)))
            .mappings()
            .first()
        )
        if original is None or task is None or agent is None or task["mode"] != "blackboard":
            return
        identity = BoardRunIdentity.model_validate(original)
        if (
            current["turn_id"] != self._board_turn(proof.get("sdk_session_id"), identity)
            or task["run_number"] != identity.run_number
            or agent["started_at"] != identity.started_at
            or (agent["derive_round"] if agent["task_type"] == "derive" else None)
            != identity.derive_round
        ):
            return
        steps = int(current["state"] != "complete" and intent.get("action") == "turn")
        previous = current["settlement"] or {}
        observed = (
            body.usage if current["state"] != "complete" or previous.get("usage") is None else None
        )
        progress = {
            "agent_id": aid,
            "steps": steps,
            "accounting_only": agent["status"] not in {"running", "concluding"},
        }
        tokens = {}
        if observed is not None:
            tokens = {
                "cache_hit_tokens": observed.cacheRead,
                "cache_miss_tokens": observed.input + observed.cacheWrite,
                "output_tokens": observed.output,
                "reasoning_tokens": observed.reasoning or 0,
            }
            progress.update(
                usage=tokens,
                context_tokens=observed.input + observed.cacheRead + observed.cacheWrite,
            )
        elif not steps:
            return
        events = [event("agent.progress", aid, progress, aid)]
        if observed is not None:
            usage = add_usage(task["usage"], tokens)
            exhausted = Decimal(str(usage.get("cost", 0))) >= Decimal(
                str(task["budget"]["max_cost"])
            )
            events.append(
                event("budget.updated", "system", {"usage": usage, "exhausted": exhausted})
            )
        written = await self.repo.append(conn, tid, events)
        await self.repo.notify(tid, written[-1]["version"], connection=conn)

    async def settle(self, call_id: UUID, body: PiCallSettle):
        result = body.model_dump(mode="json")
        async with self.repo.engine.begin() as conn:
            current = await self.locked(conn, call_id, body.attempt_id)
            if current["request_sha256"] != body.request_sha256:
                raise denied("Pi settlement must match the original provider payload")
            if (
                current["purpose"] == "playground"
                and body.outcome == "complete"
                and body.result_text is None
                or current["purpose"] != "playground"
                and body.result_text is not None
            ):
                raise denied("Only a complete model test may record a reply")
            if current["purpose"] == "playground" and current["state"] == "complete":
                original = current["settlement"] or {}
                if original.get("result_text") != body.result_text:
                    raise denied("The original model test reply cannot change")
            if current["settlement"] == result:
                return {
                    "call_id": call_id,
                    "attempt_id": body.attempt_id,
                    "state": current["state"],
                }
            previous = current["settlement"] or {}
            resolving = (
                body.outcome == "complete"
                and body.amount_usd is not None
                and current["state"] in {"unknown", "complete"}
                and current["amount_usd"] is None
                and previous.get("provider_request_id") in {None, body.provider_request_id}
                and (
                    current["state"] == "unknown"
                    or previous.get("usage") in (None, result["usage"])
                )
            )
            late_playground = (
                current["purpose"] == "playground"
                and current["state"] == "unknown"
                and current["settlement"] is None
                and body.outcome in {"unknown", "complete"}
            )
            if not (
                current["state"] == "dispatched"
                or current["state"] == "prepared"
                and body.outcome == "not_sent"
                or resolving
                or late_playground
            ):
                raise denied(
                    "A different Pi settlement already exists or the call was never dispatched"
                )
            if body.amount_usd is not None:
                rates = PiModelVersion.model_validate(current["model_snapshot"]).usd_rates
                if rates is None or body.usage is None:
                    raise denied("Pi fees require frozen prices and complete usage")
                if body.amount_usd == 0 and any(
                    (
                        rates.input and (body.usage.input or body.usage.cacheWrite1h),
                        rates.output and body.usage.output,
                        rates.cache_read and body.usage.cacheRead,
                        rates.cache_write
                        and body.usage.cacheWrite - (body.usage.cacheWrite1h or 0),
                    )
                ):
                    raise denied("A priced Pi call cannot settle paid usage at zero")
            await conn.execute(
                update(t)
                .where(self.key(call_id))
                .values(
                    state=body.outcome,
                    settlement=result,
                    amount_usd=body.amount_usd,
                    updated_at=func.now(),
                )
            )
            if current["purpose"] == "solver" and body.amount_usd is not None:
                used, _ = await pi_totals(conn, current["task_id"], self.site)
                original = (
                    await conn.execute(
                        select(s.tasks.c.usage).where(s.tasks.c.id == current["task_id"])
                    )
                ).scalar_one_or_none()
                if original is not None:
                    usage = {**original, "cost": str(used)}
                    await conn.execute(
                        update(s.tasks)
                        .where(s.tasks.c.id == current["task_id"])
                        .values(usage=usage)
                    )
            if current["purpose"] == "solver":
                await self._project_board_usage(conn, current, body)
            return {"call_id": call_id, "attempt_id": body.attempt_id, "state": body.outcome}


@router.post(CALLS + "/prepare", response_model=PiCallPrepared)
async def prepare(request: Request, response: Response, body: PiCallPrepare):
    return await PiLedger(request, response).prepare(body)


@router.post(CALLS + "/{call_id}/dispatch", response_model=PiCallDispatchResult)
async def dispatch(request: Request, response: Response, call_id: UUID, body: PiCallDispatch):
    return await PiLedger(request, response).dispatch(call_id, body)


@router.post(CALLS + "/{call_id}/settle")
async def settle(request: Request, response: Response, call_id: UUID, body: PiCallSettle):
    return await PiLedger(request, response).settle(call_id, body)


@public.get("/api/tasks/{task_id}/pi-fees")
async def task_fees(request: Request, task_id: UUID) -> dict:
    identity = require_task_reader(request, task_id)
    if identity.kind not in {"user", "service"}:
        raise HTTPException(403, "A user or Runtime identity is required")
    settings = request.app.state.settings
    async with request.app.state.engine.connect() as conn:
        task = await row(conn, s.tasks, task_id)
        frozen = await task_pi_model(conn, task_id)
        if frozen is None:
            return {"enabled": False}
        agents = (
            await conn.execute(
                select(
                    t.c.agent_id,
                    func.coalesce(func.sum(t.c.amount_usd), 0),
                    func.count().filter(uncertain()),
                )
                .where(
                    t.c.task_id == task_id,
                    t.c.site_id == settings.pi_site_id,
                    t.c.purpose == "solver",
                    t.c.request_sha256.is_not(None),
                    t.c.state != "not_sent",
                )
                .group_by(t.c.agent_id)
            )
        ).all()
        scopes = {}
        for name in ("solver", "progress"):
            amount, unknown = await pi_totals(conn, task_id, settings.pi_site_id, name)
            scopes[name] = {"estimated_usd": str(amount), "unknown_calls": unknown}
    return {
        "enabled": True,
        "currency": "USD",
        "threshold_usd": task["budget"]["max_cost"],
        "agents": {
            aid: {"estimated_usd": str(amount), "unknown_calls": unknown}
            for aid, amount, unknown in agents
        },
        **scopes,
    }


@public.get("/api/platform/pi-review-fees")
async def review_fees(request: Request, response: Response) -> dict:
    identity = require_user_or_service(request)
    if identity.kind != "user":
        raise HTTPException(403, "User identity required")
    response.headers["Cache-Control"] = "no-store"
    async with request.app.state.engine.connect() as conn:
        totals = (
            await conn.execute(
                select(
                    t.c.site_id,
                    func.coalesce(func.sum(t.c.amount_usd), 0),
                    func.count().filter(uncertain()),
                )
                .where(t.c.purpose == "review", t.c.request_user == identity.name)
                .group_by(t.c.site_id)
            )
        ).all()
    sites = [
        {"site_id": site, "estimated_usd": str(amount), "unknown_calls": int(unknown)}
        for site, amount, unknown in totals
    ]
    return {
        "currency": "USD",
        "estimated_usd": str(sum((Decimal(item["estimated_usd"]) for item in sites), Decimal(0))),
        "unknown_calls": sum(item["unknown_calls"] for item in sites),
        "sites": sites,
        "partial": False,
    }
