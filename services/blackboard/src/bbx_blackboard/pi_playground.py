"""One registered operator model test, reusing the trusted Pi sender."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from bbx_contracts.pi_models import (
    PiCallPlaygroundProof,
    PiCallPrepare,
    PiPlaygroundInvocation,
    PiPlaygroundRequest,
    PiPlaygroundView,
    context_sha256,
    playground_context,
)
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert

from bbx_blackboard.auth import principal
from bbx_blackboard.pi_ledger import PLAYGROUND_TTL
from bbx_blackboard.pi_registry import peer_tls, playground_runtime_url, registry
from bbx_blackboard.store.schema import pi_model_calls as t

PATH = "/api/platform/pi-model-tests"
router = APIRouter(tags=["pi-model-tests"])


def operator(request: Request, response: Response):
    r = registry(request)
    if not r.operator(request):
        raise HTTPException(403, "只有主站指定操作者可以演练模型")
    response.headers["Cache-Control"] = "no-store"
    return r, principal(request).name


def identity(site: str, user: str, request_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"bbx-pi-playground:{site}:{user}:{request_id}")


def where(site: str, user: str, request_id: UUID):
    return (
        t.c.site_id == site,
        t.c.call_id == identity(site, user, request_id),
        t.c.request_user == user,
        t.c.purpose == "playground",
    )


async def existing(engine, conditions):
    async with engine.connect() as conn:
        item = (await conn.execute(select(t).where(*conditions))).mappings().first()
    if (
        item
        and item["state"] in {"prepared", "dispatched"}
        and item["updated_at"] < datetime.now(UTC) - PLAYGROUND_TTL
    ):
        return await close_unfinished(engine, conditions, cutoff=item["updated_at"])
    return item


async def close_unfinished(engine, conditions, *, cutoff=None):
    async with engine.begin() as conn:
        await conn.execute(
            update(t)
            .where(
                *conditions,
                t.c.state.in_(("prepared", "dispatched")),
                *([t.c.updated_at <= cutoff] if cutoff is not None else []),
            )
            .values(
                state=case((t.c.state == "prepared", "not_sent"), else_="unknown"),
                updated_at=func.now(),
            )
        )
        return (await conn.execute(select(t).where(*conditions))).mappings().first()


def view(item, request_id: UUID) -> PiPlaygroundView:
    result = item["settlement"] or {}
    return PiPlaygroundView(
        request_id=request_id,
        model_id=item["intent"]["model_id"],
        model_revision=item["intent"]["model_revision"],
        state=item["state"],
        result_text=result.get("result_text"),
        usage=result.get("usage"),
        amount_usd=item["amount_usd"],
    )


def replay(item, body: PiPlaygroundRequest, digest: str) -> PiPlaygroundView:
    old = item["intent"]
    if (old["model_id"], old["model_revision"], old["context_sha256"]) != (
        body.model_id,
        body.model_revision,
        digest,
    ):
        raise HTTPException(409, "原模型演练的输入或版本已变化")
    return view(item, body.request_id)


@router.get(PATH + "/{request_id}", response_model=PiPlaygroundView)
async def read(request: Request, response: Response, request_id: UUID):
    r, user = operator(request, response)
    item = await existing(r.platform.engine, where(r.settings.pi_site_id, user, request_id))
    if item is None:
        raise HTTPException(404, "找不到这次模型演练")
    return view(item, request_id)


@router.post(PATH, response_model=PiPlaygroundView)
async def run(request: Request, response: Response, body: PiPlaygroundRequest):
    r, user = operator(request, response)
    engine, site = r.platform.engine, r.settings.pi_site_id
    context, options = playground_context(body.prompt)
    digest = context_sha256(context, options)
    conditions = where(site, user, body.request_id)
    if item := await existing(engine, conditions):
        return replay(item, body, digest)
    endpoint = playground_runtime_url(r.settings)
    if endpoint is None:
        raise HTTPException(503, "模型演练发送服务未配置")
    try:
        verify = peer_tls(r.settings) if endpoint.startswith("https://") else True
    except OSError:
        raise HTTPException(503, "模型演练发送服务的私网证书不可用") from None
    chosen = next((model for model in await r.list() if model.id == body.model_id), None)
    if (
        chosen is None
        or chosen.model_revision != body.model_revision
        or not chosen.enabled
        or not chosen.has_secret
        or not chosen.has_text_limits()
    ):
        raise HTTPException(422, "请选择已启用且配置凭据、文本输入和限制的当前模型版本")
    call_id = identity(site, user, body.request_id)
    intent = PiCallPrepare(
        model_id=chosen.id,
        model_revision=chosen.model_revision,
        task_id=None,
        call_id=call_id,
        attempt_id=uuid4(),
        logical_call_id=sha256(body.request_id.bytes).hexdigest(),
        context_sha256=digest,
        action="turn",
        proof=PiCallPlaygroundProof(
            kind="playground", request_id=body.request_id, authority=r.settings.pi_model_authority
        ),
    )
    async with engine.begin() as conn:
        staged = await conn.scalar(
            insert(t)
            .values(
                site_id=site,
                call_id=call_id,
                attempt_id=intent.attempt_id,
                purpose="playground",
                agent_id="",
                turn_id=str(body.request_id),
                request_user=user,
                logical_call_id=intent.logical_call_id,
                intent=intent.model_dump(mode="json"),
                model_snapshot=chosen.model_dump(mode="json"),
                state="prepared",
            )
            .on_conflict_do_nothing()
            .returning(t.c.call_id)
        )
    if staged is None:
        duplicate = await existing(engine, conditions)
        if duplicate is None:
            raise HTTPException(409, "原模型演练标识冲突")
        return replay(duplicate, body, digest)

    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=145,
        transport=httpx.AsyncHTTPTransport(retries=0, verify=verify),
    ) as sender:
        try:
            await sender.post(
                endpoint,
                headers={
                    "Authorization": "Bearer "
                    + r.settings.pi_model_runtime_token.get_secret_value()
                },
                json=PiPlaygroundInvocation(intent=intent, prompt=body.prompt).model_dump(
                    mode="json"
                ),
            )
        except httpx.HTTPError:
            pass
    # This row lock closes a still-unclaimed send or preserves the ambiguity of
    # an already-dispatched request. Only Runtime can supply late evidence.
    item = await close_unfinished(engine, conditions)
    assert item is not None
    return view(item, body.request_id)
