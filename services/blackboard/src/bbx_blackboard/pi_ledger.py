"""Once-only Pi sends and site-local accounting for explicitly frozen P6a calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
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
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import and_, false, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bbx_blackboard.auth import require_task_reader, require_user_or_service
from bbx_blackboard.gateway_auth import require_gateway_identity
from bbx_blackboard.pi_registry import (
    AUTHORITY_HEADER,
    RUNTIME_HEADER,
    normalized_url,
    registry,
    trusted_header,
)
from bbx_blackboard.runner_control import row
from bbx_blackboard.store import schema as s

CALLS = "/api/runtime/pi-model-calls"
RUNNER_HEADER = "x-bbx-runner-model"
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
        self.legacy = request.app.state.gateway_execution
        self.runner = request.app.state.runner_gateway
        self.repo = self.legacy.repo
        if not self.registry.enabled:
            raise HTTPException(503, "Pi call accounting is not configured")

    def _identity(self, body):
        headers = self.request.headers.getlist(RUNNER_HEADER)
        if len(headers) != 1:
            raise HTTPException(401, "A scoped Runner capability is required")
        token = headers[0]
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
        frozen = await task_pi_model(conn, tid)
        if proof.kind == "review":
            turn, review = await self.legacy._review(conn, tid, proof)
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
            _, job, current = await self.legacy._progress_scope(conn, tid, proof, pi=True)
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
            task, agent = await self.legacy._ordinary_scope(conn, tid, proof)
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
            _, _, binding = await self.runner._scope(
                conn,
                tid,
                self._identity(body),
                sdk=proof.sdk_session_id,
                journal=proof.journal.model_dump(mode="json"),
            )
            spec = json.loads(binding["payload_json"])["spec"]
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
                proof.sdk_session_id,
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

    async def lookup(self, conn, call_id):
        found = (await conn.execute(select(t).where(self.key(call_id)))).mappings().first()
        if found is None:
            raise denied("Pi call is not prepared")
        return found

    async def locked(self, conn, call_id, attempt_id):
        existing = await self.lookup(conn, call_id)
        await self.repo.lock(conn, existing["task_id"])
        current = await self.lookup(conn, call_id)
        if current["attempt_id"] != attempt_id:
            raise denied("The original Pi attempt changed")
        return current

    async def prepare(self, body: PiCallPrepare) -> PiCallPrepared:
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
            scope = await self.scope(conn, PiCallPrepare.model_validate(current["intent"]))
            if (
                scope.purpose != current["purpose"]
                or scope.model.model_dump(mode="json") != current["model_snapshot"]
            ):
                raise denied("The original Pi scope changed")
            group = [
                t.c.site_id == self.site,
                t.c.task_id == current["task_id"],
                t.c.purpose == scope.purpose,
            ]
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

    async def settle(self, call_id: UUID, body: PiCallSettle):
        result = body.model_dump(mode="json")
        async with self.repo.engine.begin() as conn:
            current = await self.locked(conn, call_id, body.attempt_id)
            if current["request_sha256"] != body.request_sha256:
                raise denied("Pi settlement must match the original provider payload")
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
            if not (
                current["state"] == "dispatched"
                or current["state"] == "prepared"
                and body.outcome == "not_sent"
                or resolving
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


async def local_review(request: Request, user: str) -> dict:
    site = request.app.state.settings.pi_site_id
    async with request.app.state.engine.connect() as conn:
        amount, unknown = (
            await conn.execute(
                select(
                    func.coalesce(func.sum(t.c.amount_usd), 0), func.count().filter(uncertain())
                ).where(t.c.site_id == site, t.c.purpose == "review", t.c.request_user == user)
            )
        ).one()
    return {"site_id": site, "estimated_usd": str(amount), "unknown_calls": int(unknown)}


@router.get("/api/runtime/pi-review-fees")
async def peer_review(request: Request, response: Response, user: str):
    settings = request.app.state.settings
    trusted_header(request, AUTHORITY_HEADER, settings.pi_model_authority_token.get_secret_value())
    if request.headers.get("x-bbx-model-authority") != settings.pi_model_authority:
        raise HTTPException(403, "Review authority changed")
    response.headers["Cache-Control"] = "no-store"
    return await local_review(request, user)


@public.get("/api/platform/pi-review-fees")
async def review_fees(request: Request, response: Response) -> dict:
    identity = require_user_or_service(request)
    if identity.kind != "user":
        raise HTTPException(403, "User identity required")
    settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    local = await local_review(request, identity.name)
    sites = [local]
    if settings.pi_review_peer_site and settings.pi_review_peer_site != local["site_id"]:
        try:
            target, _ = normalized_url(settings.pi_review_peer_url)
            parsed = urlsplit(target)
            if parsed.scheme != "https" and parsed.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise ValueError("Review peer requires HTTPS")
            token = settings.pi_model_authority_token.get_secret_value()
            if len(token) < 32 or token == settings.service_token.get_secret_value():
                raise ValueError("A dedicated peer identity is required")
            async with httpx.AsyncClient(
                timeout=5, trust_env=False, follow_redirects=False
            ) as http:
                result = await http.get(
                    target + "/api/runtime/pi-review-fees",
                    params={"user": identity.name},
                    headers={
                        AUTHORITY_HEADER: token,
                        "x-bbx-model-authority": settings.pi_model_authority,
                    },
                )
                result.raise_for_status()
            peer = result.json()
            if peer["site_id"] != settings.pi_review_peer_site:
                raise ValueError("Unexpected review site")
            if (
                not Decimal(peer["estimated_usd"]).is_finite()
                or type(peer["unknown_calls"]) is not int
            ):
                raise ValueError("Invalid review projection")
            sites.append(peer)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, ArithmeticError):
            pass
    return {
        "currency": "USD",
        "estimated_usd": str(sum(Decimal(item["estimated_usd"]) for item in sites)),
        "unknown_calls": sum(item["unknown_calls"] for item in sites),
        "sites": sites,
        "partial": len(sites) != 2,
    }
