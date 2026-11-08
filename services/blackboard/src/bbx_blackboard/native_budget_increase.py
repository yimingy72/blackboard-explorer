"""Raise the original threshold once; resolve a lost write response by readback."""

from copy import deepcopy
from decimal import Decimal, DecimalException

from bbx_contracts.native_gateway import native_budget_threshold_usd
from fastapi import HTTPException
from sqlalchemy import update

from bbx_blackboard.board_events import digest, emit, prior
from bbx_blackboard.store import schema as s


def increase_value(config, value, additional_cny):
    try:
        cny = Decimal(config["threshold_cny"]) + additional_cny
        usd = native_budget_threshold_usd(cny, Decimal(config["exchange_rate"]["rate_cny_per_usd"]))
        amount = float(usd)
        if usd <= Decimal(config["threshold_usd"]):
            raise ValueError
    except (DecimalException, ValueError, OverflowError) as error:
        raise HTTPException(
            422, "Added amount does not produce a representable increase"
        ) from error
    desired = deepcopy(value)
    desired["budgets"][0]["limit"]["amount"] = amount
    return {"threshold_cny": str(cny), "threshold_usd": str(usd), "native_value": desired}


async def increase_budget(pause, tid, request):
    native, repo = pause.native, pause.repo
    hashed = digest({"operation": "native_budget_increase", **request.model_dump(mode="json")})

    async def scope(conn):
        task, _, _, snapshot = await pause._confirmed_pause(conn, tid, request.pause_id)
        budget = await native._budget(conn, tid)
        hold_id = pause._budget_pause(task, snapshot, budget)
        if budget["config"].get("last_increase_hold_id") == hold_id:
            raise HTTPException(409, "Original threshold pause already has a confirmed increase")
        return hold_id, budget

    async with repo.engine.begin() as conn:
        await repo.lock(conn, tid)
        if previous := await prior(conn, tid, "user", request.request_id, hashed):
            return previous
        hold_id, budget = await scope(conn)
        config = budget["config"]
        intent = config.get("pending_increase")
        if intent and (intent["request_hash"] != hashed or intent["hold_id"] != hold_id):
            raise HTTPException(409, "Another native increase requires readback")

    if not intent:
        value = await native.verified_key(
            budget, config["native_value"], exact=bool(config.get("last_increase_hold_id"))
        )
        desired = increase_value(config, value, request.additional_cny)
        _, used, _ = await native.threshold_status(budget)
        async with repo.engine.begin() as conn:
            await repo.lock(conn, tid)
            if previous := await prior(conn, tid, "user", request.request_id, hashed):
                return previous
            current_hold, current = await scope(conn)
            current_config = current["config"]
            if (
                current_hold != hold_id
                or pause._budget_identity(current) != pause._budget_identity(budget)
                or current_config != config
            ):
                raise HTTPException(409, "Original budget changed; read back the current increase")
            if used < await native.usage_floor(conn, tid, current):
                raise HTTPException(409, "Original native cumulative usage has regressed")
            if used >= Decimal(desired["threshold_usd"]):
                raise HTTPException(
                    422, "Increased threshold must exceed original cumulative usage"
                )
            intent = {
                **desired,
                "previous_native_value": value,
                "request_hash": hashed,
                "hold_id": hold_id,
                "used_floor": str(used),
            }
            await conn.execute(
                update(s.native_task_budgets)
                .where(s.native_task_budgets.c.task_id == tid)
                .values(config={**config, "pending_increase": intent})
            )
        # Only the request that committed the intent can issue a PUT. Even an old-value
        # readback cannot show that a lost write won't apply later.
        try:
            await native.admin.update_key(budget["native_resource_id"], intent["native_value"])
        except HTTPException:
            pass

    desired = {key: intent[key] for key in ("threshold_cny", "threshold_usd", "native_value")}
    await native.verified_key(budget, desired["native_value"], exact=True)
    status, used, exceeded = await native.threshold_status({**budget, "config": desired})
    async with repo.engine.begin() as conn:
        await repo.lock(conn, tid)
        if previous := await prior(conn, tid, "user", request.request_id, hashed):
            return previous
        current_hold, current = await scope(conn)
        current_config = dict(current["config"])
        if (
            current_hold != hold_id
            or pause._budget_identity(current) != pause._budget_identity(budget)
            or current_config.pop("pending_increase", None) != intent
            or current_config
            != {key: value for key, value in config.items() if key != "pending_increase"}
            or exceeded
            or used >= Decimal(desired["threshold_usd"])
            or used
            < max(Decimal(intent["used_floor"]), await native.usage_floor(conn, tid, current))
        ):
            raise HTTPException(409, "Original native increase readback cannot be confirmed")
        await conn.execute(
            update(s.native_task_budgets)
            .where(s.native_task_budgets.c.task_id == tid)
            .values(
                config={**current_config, **desired, "last_increase_hold_id": hold_id},
                status_snapshot={"latest": status, "used_high_water_usd": str(used)},
            )
        )
        return await emit(
            repo,
            conn,
            tid,
            "user",
            "board.request.linked",
            request.request_id,
            hashed,
            {
                "request_id": str(request.request_id),
                "pause_id": str(request.pause_id),
                "threshold_cny": desired["threshold_cny"],
                "threshold_usd": desired["threshold_usd"],
            },
        )
