"""Safe AgentGateway catalog and fee projections; management stays in AgentGateway."""

import asyncio
import hmac
from urllib.parse import urlsplit

from bbx_contracts.gateway_models import GatewayDirectory
from fastapi import APIRouter, HTTPException, Request, Response

from bbx_blackboard.auth import require_service, require_user_or_service
from bbx_blackboard.gateway_maintenance import fence_state
from bbx_blackboard.gateway_review_key import publish_review_key

router = APIRouter(prefix="/api/platform", tags=["agentgateway"])
internal = APIRouter(prefix="/api/internal", include_in_schema=False)


def _administrator(request: Request, identity) -> bool:
    administrators = {
        name
        for entry in request.app.state.settings.admin_users.get_secret_value().split(",")
        if (name := entry.partition(":")[0]) and entry.partition(":")[1]
    }
    return identity.kind == "user" and identity.name in administrators


def _management_url(value: str) -> str | None:
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if (
        url.username
        or url.password
        or url.fragment
        or url.query
        or url.path not in {"/ui", "/ui/", "/ui/llm/models"}
        or not url.hostname
        or url.scheme != "https"
        and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})
    ):
        return None
    return value


@router.get("/gateway-models", response_model_exclude_none=True)
async def gateway_directory(request: Request, response: Response) -> GatewayDirectory:
    identity = require_user_or_service(request)
    response.headers["Cache-Control"] = "no-store"
    manager = request.app.state.gateway_models
    settings = request.app.state.settings
    management_url = (
        _management_url(settings.gateway_model_management_url)
        if _administrator(request, identity)
        else None
    )
    maintenance = False
    if request.app.state.engine is not None:
        async with request.app.state.engine.connect() as conn:
            maintenance = await fence_state(conn) is not None
    try:
        models = await manager.list_active()
    except HTTPException:
        return GatewayDirectory(
            status="unavailable", models=[], management_url=management_url, maintenance=maintenance
        )
    return GatewayDirectory(
        status="ready",
        models=[model.public() for model in models],
        management_url=management_url,
        maintenance=maintenance,
    )


@internal.post("/gateway-review-key/prepare")
async def ensure_review_key(request: Request, response: Response) -> dict:
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await publish_review_key(
        request.app.state.settings,
        request.app.state.engine,
        request.app.state.gateway_execution.budgets.admin,
    )


@router.get("/gateway-models/internal/review-costs", include_in_schema=False)
async def peer_review_costs(request: Request, response: Response, site_id: str) -> dict:
    from bbx_blackboard.gateway_execution import review_cost_ledger

    settings = request.app.state.settings
    token = settings.gateway_maintenance_key.get_secret_value()
    supplied = request.headers.get("x-bbx-gateway-maintenance", "")
    if not token or not hmac.compare_digest(token.encode(), supplied.encode()):
        raise HTTPException(403, "Gateway peer authorization required")
    if site_id != settings.gateway_site_id:
        raise HTTPException(409, "Gateway site changed")
    response.headers["Cache-Control"] = "no-store"
    return {
        **await review_cost_ledger(request.app.state.engine, site_id),
        "accounting_source_id": settings.native_gateway_accounting_source_id,
    }


@router.get("/gateway-models/review-costs")
async def shared_review_costs(request: Request, response: Response) -> dict:
    from decimal import Decimal

    import httpx

    from bbx_blackboard.gateway_execution import review_cost_ledger
    from bbx_blackboard.gateway_maintenance import _peer_origin

    identity = require_user_or_service(request)
    if not _administrator(request, identity):
        raise HTTPException(403, "Only publishing administrators can read shared review costs")
    response.headers["Cache-Control"] = "no-store"
    settings = request.app.state.settings
    peers = settings.gateway_maintenance_peers
    token = settings.gateway_maintenance_key.get_secret_value()
    if not token or len(peers) < 2 or settings.gateway_site_id not in peers:
        raise HTTPException(503, "Every AgentGateway accounting site must be configured")
    origins = {site: _peer_origin(origin) for site, origin in peers.items()}
    if len(set(origins.values())) != len(origins):
        raise HTTPException(503, "AgentGateway accounting sites must be distinct")

    async def receipt(client, site, origin):
        if site == settings.gateway_site_id:
            return await review_cost_ledger(request.app.state.engine, site)
        reply = await client.get(
            origin + "/api/platform/gateway-models/internal/review-costs",
            params={"site_id": site},
            headers={"x-bbx-gateway-maintenance": token},
        )
        reply.raise_for_status()
        result = reply.json()
        if (
            result["site_id"] != site
            or result["accounting_source_id"] != settings.native_gateway_accounting_source_id
        ):
            raise ValueError("Gateway accounting identity differs")
        return result

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        results = await asyncio.gather(
            *(receipt(client, site, origin) for site, origin in origins.items()),
            return_exceptions=True,
        )
    if any(isinstance(item, BaseException) for item in results):
        raise HTTPException(503, "A shared AgentGateway accounting site is unavailable")
    receipts = [item for item in results if isinstance(item, dict)]
    known = [
        Decimal(item["confirmed_usd"]) for item in receipts if item["confirmed_usd"] is not None
    ]
    calls = sorted(
        [call for item in receipts for call in item["calls"]],
        key=lambda call: call["at"],
        reverse=True,
    )
    return {
        "confirmed_usd": str(sum(known, Decimal(0))) if known else None,
        "pending": sum(item["pending"] for item in receipts),
        "more": len(calls) > 100 or any(item["more"] for item in receipts),
        "calls": calls[:100],
    }
