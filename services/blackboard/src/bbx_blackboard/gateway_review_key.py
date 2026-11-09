"""One trusted unlimited key for terminal reviews on a shared AgentGateway."""

import hashlib

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from bbx_blackboard.store.schema import app_settings

REVIEW_KEY_NAME = "bbx-platform-terminal-review-v1"
REVIEW_INTENT = "gateway_terminal_review_key:v1"


def _identity(settings):
    key = settings.gateway_review_key.get_secret_value()
    if len(key) < 32 or not settings.native_gateway_accounting_source_id:
        raise HTTPException(503, "The terminal review key is not configured")
    fingerprint = "sha256:" + hashlib.sha256(key.encode()).hexdigest()
    return key, fingerprint


async def _find(admin, fingerprint):
    matching = [
        item
        for item in await admin.resources()
        if item["value"].get("keyHash") == fingerprint
        or (item["value"].get("metadata") or {}).get("name") == REVIEW_KEY_NAME
    ]
    if not matching:
        return None
    if len(matching) != 1:
        raise HTTPException(409, "The terminal review key conflicts")
    found = matching[0]
    value = found["value"]
    if (
        value.get("keyHash") != fingerprint
        or (value.get("metadata") or {}).get("name") != REVIEW_KEY_NAME
        or value.get("budgets") not in (None, [])
        or value.get("allowedModels") not in (None, [])
    ):
        raise HTTPException(409, "The original terminal review key changed")
    return found


async def read_review_key(settings, engine, admin):
    key, fingerprint = _identity(settings)
    found = await _find(admin, fingerprint)
    if found is None:
        raise HTTPException(503, "The original terminal review key is not ready")
    if settings.gateway_review_key_owner:
        async with engine.connect() as conn:
            original = (
                await conn.execute(
                    select(app_settings.c.value).where(app_settings.c.key == REVIEW_INTENT)
                )
            ).scalar_one_or_none()
        expected = (original or {}).get("resource_id")
    else:
        expected = settings.gateway_review_key_id
    if expected != found["id"]:
        raise HTTPException(409, "The original terminal review key identity is not confirmed")
    return key, REVIEW_KEY_NAME


async def publish_review_key(settings, engine, admin):
    if not settings.gateway_review_key_owner:
        raise HTTPException(403, "Only the publishing site can create the terminal review key")
    _, fingerprint = _identity(settings)
    found = await _find(admin, fingerprint)
    async with engine.begin() as conn:
        original = (
            await conn.execute(
                select(app_settings.c.value).where(app_settings.c.key == REVIEW_INTENT)
            )
        ).scalar_one_or_none()
        identity = {
            "fingerprint": fingerprint,
            "source": settings.native_gateway_accounting_source_id,
        }
        if original is not None and any(
            original.get(name) != value for name, value in identity.items()
        ):
            raise HTTPException(409, "The original terminal review identity changed")
        claim = False
        if original is None:
            result = await conn.execute(
                insert(app_settings)
                .values(key=REVIEW_INTENT, value=identity)
                .on_conflict_do_nothing(index_elements=[app_settings.c.key])
                .returning(app_settings.c.key)
            )
            claim = result.scalar_one_or_none() is not None
    if found is None and claim:
        await admin.create_key(
            {
                "keyHash": fingerprint,
                "metadata": {"name": REVIEW_KEY_NAME},
            }
        )
        found = await _find(admin, fingerprint)
    if found is None:
        raise HTTPException(503, "The original terminal review key requires readback")
    async with engine.begin() as conn:
        original = (
            await conn.execute(
                select(app_settings.c.value)
                .where(app_settings.c.key == REVIEW_INTENT)
                .with_for_update()
            )
        ).scalar_one()
        if original.get("resource_id") not in {None, found["id"]}:
            raise HTTPException(409, "The original terminal review key identity changed")
        await conn.execute(
            update(app_settings)
            .where(app_settings.c.key == REVIEW_INTENT)
            .values(value={**original, "resource_id": found["id"]})
        )
    return {"ready": True, "resource_id": found["id"]}
