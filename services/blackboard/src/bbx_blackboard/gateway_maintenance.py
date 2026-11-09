"""A durable, explicit idle maintenance fence shared by the configured consumers."""

import hmac
from urllib.parse import urlsplit

from bbx_contracts.gateway_models import GatewayMaintenanceAck, GatewayMaintenanceFence
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, exists, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from bbx_blackboard.store import schema as s

router = APIRouter(prefix="/api/platform/gateway-models", tags=["agentgateway-maintenance"])
FENCE = "gateway_model_maintenance:v1"
LOCK = "bbx:gateway-admission:v1"
RELEASE = FENCE + ":released:"
GENERATION = FENCE + ":generation"
TERMINAL = ("finished", "failed", "stopped")


async def admission(conn, expected=None):
    """Use inside the final transaction that commits a new admission."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"), {"key": LOCK}
    )
    if await conn.scalar(select(s.app_settings.c.value).where(s.app_settings.c.key == FENCE)):
        raise HTTPException(409, "AgentGateway model maintenance is in progress")
    current = await _generation(conn)
    if expected is not None and current != expected:
        raise HTTPException(409, "AgentGateway changed while selecting the model")
    return current


async def _generation(conn):
    value = await conn.scalar(
        select(s.app_settings.c.value).where(s.app_settings.c.key == GENERATION)
    )
    return (value or {}).get("value", 0)


async def observed_generation(engine):
    async with engine.begin() as conn:
        return await admission(conn)


async def fence_state(conn):
    return await conn.scalar(select(s.app_settings.c.value).where(s.app_settings.c.key == FENCE))


async def _busy(conn):
    checks = (
        exists(
            select(s.tasks.c.id)
            .join(s.gateway_task_models, s.gateway_task_models.c.task_id == s.tasks.c.id)
            .where(s.tasks.c.status.not_in(TERMINAL))
        ),
        exists(
            select(s.gateway_execution_attempts.c.id).where(
                s.gateway_execution_attempts.c.fee_state != "known"
            )
        ),
        exists(
            select(s.runner_calls.c.id).where(
                s.runner_calls.c.kind == "model",
                s.runner_calls.c.state.in_(("dispatched", "unknown")),
            )
        ),
        exists(
            select(s.progress_jobs.c.id).where(s.progress_jobs.c.status.in_(("queued", "running")))
        ),
        exists(
            select(s.ctf_turns.c.id).where(
                s.ctf_turns.c.purpose == "review", s.ctf_turns.c.status == "running"
            )
        ),
        exists(
            select(s.ctf_messages.c.id).where(
                s.ctf_messages.c.purpose == "review",
                s.ctf_messages.c.sender_kind == "user",
                s.ctf_messages.c.status.in_(("queued", "leased")),
            )
        ),
        exists(
            select(s.agent_messages.c.id).where(
                s.agent_messages.c.purpose == "review",
                s.agent_messages.c.status.in_(("queued", "processing")),
            )
        ),
    )
    return bool(await conn.scalar(select(or_(*checks))))


async def handle_fence(engine, body):
    identity = str(body.request_id)
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": LOCK}
        )
        original = await fence_state(conn)
        if original is not None and original["request_id"] != identity:
            raise HTTPException(409, "Another AgentGateway maintenance operation is active")
        release_key = RELEASE + identity
        if body.action == "release":
            if original:
                await conn.execute(delete(s.app_settings).where(s.app_settings.c.key == FENCE))
            await conn.execute(
                insert(s.app_settings)
                .values(key=release_key, value={"request_id": identity})
                .on_conflict_do_nothing(index_elements=[s.app_settings.c.key])
            )
            return GatewayMaintenanceAck(
                request_id=body.request_id, site_id=body.site_id, state="released"
            )
        if await conn.scalar(
            select(s.app_settings.c.key).where(s.app_settings.c.key == release_key)
        ):
            return GatewayMaintenanceAck(
                request_id=body.request_id, site_id=body.site_id, state="released"
            )
        if original is None:
            version = {"value": await _generation(conn) + 1}
            await conn.execute(
                insert(s.app_settings)
                .values(key=GENERATION, value=version)
                .on_conflict_do_update(
                    index_elements=[s.app_settings.c.key], set_={"value": version}
                )
            )
            await conn.execute(
                insert(s.app_settings).values(key=FENCE, value={"request_id": identity})
            )
        state = "busy" if await _busy(conn) else "idle"
    return GatewayMaintenanceAck(request_id=body.request_id, site_id=body.site_id, state=state)


@router.post("/maintenance/fence", include_in_schema=False)
async def private_fence(request: Request, body: GatewayMaintenanceFence) -> GatewayMaintenanceAck:
    settings = request.app.state.settings
    token = settings.gateway_maintenance_key.get_secret_value()
    supplied = request.headers.get("x-bbx-gateway-maintenance", "")
    if not token or not hmac.compare_digest(token.encode(), supplied.encode()):
        raise HTTPException(403, "Gateway maintenance service authorization required")
    if body.site_id != settings.gateway_site_id:
        raise HTTPException(409, "Gateway maintenance site changed")
    return await handle_fence(request.app.state.engine, body)


def _peer_origin(raw):
    try:
        url = urlsplit(raw)
    except ValueError:
        url = None
    if (
        url is None
        or url.scheme not in {"https", "http"}
        or not url.hostname
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise HTTPException(503, "Gateway maintenance peer configuration is unavailable")
    return raw.rstrip("/")
