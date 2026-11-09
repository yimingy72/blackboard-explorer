"""Keep a protected legacy Task out of stale scheduler and egress paths."""

from uuid import UUID

from bbx_contracts.legacy_network import (
    LEGACY_NETWORK_HEADER,
    LEGACY_NETWORK_VERSION,
    LegacyNetworkAbsentReceipt,
    LegacyNetworkReceipt,
    LegacyNetworkSnapshot,
    LegacyNetworkView,
    load_legacy_network_policy,
    snapshot,
)
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import exists, insert, select, text, update

from bbx_blackboard.auth import require_service
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import Repository

router = APIRouter(tags=["system"])
VPN_SLOT_LOCK = 0x4242584C4E56
LIVE_TASK = {"created", "provisioning", "running", "closing"}
BOOT_PHASES = {"prepared", "starting", "ready"}
ORIGINAL_FIELDS = (
    "envd_id",
    "envd_started_at",
    "network_id",
    "relay_id",
    "relay_started_at",
    "relay_network_id",
    "gateway_id",
    "gateway_started_at",
    "gateway_network_id",
)


def runtime_compatible(request: Request) -> bool:
    return request.headers.getlist(LEGACY_NETWORK_HEADER) == [LEGACY_NETWORK_VERSION]


def require_runtime_version(request: Request) -> None:
    if not runtime_compatible(request):
        raise HTTPException(409, "Protected Task requires the versioned legacy Runtime")


def deployment_snapshot(settings) -> LegacyNetworkSnapshot | None:
    try:
        return snapshot(load_legacy_network_policy(settings.legacy_network_policy_file))
    except (OSError, ValueError):
        # Preserve old read and stop paths during a bad platform publication.
        return None


def protected_task():
    return exists(
        select(s.legacy_execution_networks.c.task_id).where(
            s.legacy_execution_networks.c.task_id == s.tasks.c.id
        )
    )


async def task_snapshot(conn, tid):
    row = (
        await conn.execute(
            select(s.legacy_execution_networks.c.snapshot).where(
                s.legacy_execution_networks.c.task_id == tid
            )
        )
    ).scalar_one_or_none()
    return None if row is None else LegacyNetworkSnapshot.model_validate(row)


async def run_receipt(conn, tid, run_number):
    table = s.legacy_execution_receipts
    return (
        await conn.execute(
            select(table.c.receipt).where(table.c.task_id == tid, table.c.run_number == run_number)
        )
    ).scalar_one_or_none()


def public_network(policy: LegacyNetworkSnapshot, receipt: dict | None) -> LegacyNetworkView:
    return LegacyNetworkView(mode=policy.mode, state=receipt["phase"] if receipt else "pending")


async def require_run_ready(conn, task):
    policy = await task_snapshot(conn, task["id"])
    if policy is None:
        return
    receipt = await run_receipt(conn, task["id"], task["run_number"])
    if not receipt or receipt["phase"] != "ready":
        raise HTTPException(409, "Protected execution network and capture are not confirmed")


async def require_run_retired(conn, task):
    policy = await task_snapshot(conn, task["id"])
    if policy is None:
        return
    receipt = await run_receipt(conn, task["id"], task["run_number"])
    if not receipt or (
        receipt["phase"] != "absent"
        and not (receipt["phase"] == "exited" and receipt["capture_closed"] is True)
    ):
        raise HTTPException(409, "Original execution network or capture is not closed")


async def reserve_vpn_slot(conn, tid):
    await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": VPN_SLOT_LOCK})
    policy, runs = s.legacy_execution_networks, s.legacy_execution_receipts
    occupied = (
        await conn.execute(
            select(runs.c.task_id)
            .select_from(runs.join(policy, policy.c.task_id == runs.c.task_id))
            .where(
                runs.c.task_id != tid,
                policy.c.snapshot["mode"].astext == "vpn",
                runs.c.receipt["phase"].astext.not_in(["absent", "exited"]),
            )
            .limit(1)
        )
    ).first()
    if occupied:
        raise HTTPException(409, "The original legacy VPN slot is still occupied")


async def record_network(engine, tid, observed, published):
    repo = Repository(engine)
    async with engine.begin() as conn:
        try:
            await repo.lock(conn, tid)
        except KeyError:
            raise HTTPException(404, "Task not found") from None
        task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
        policy = await task_snapshot(conn, tid)
        if (
            policy is None
            or observed.policy_digest != policy.policy_digest
            or observed.run_number != task["run_number"]
        ):
            raise HTTPException(409, "Original protected Task policy or run changed")
        prior = await run_receipt(conn, tid, observed.run_number)
        value = observed.model_dump(mode="json")
        if value == prior:
            return {"receipt": prior, "launch_allowed": False}
        phase = observed.phase
        if phase in BOOT_PHASES and (
            task["status"] != "provisioning" or task["deleting"] or policy != published
        ):
            raise HTTPException(
                409, "Protected execution cannot start on this deployment or Task state"
            )
        if isinstance(observed, LegacyNetworkAbsentReceipt):
            if prior is not None or (task["status"] in LIVE_TASK and not task["deleting"]):
                raise HTTPException(409, "Original execution cannot be confirmed absent")
        else:
            forced = policy.mode != "default"
            if forced != bool(observed.gateway_id) or not observed.relay_id:
                raise HTTPException(409, "Observed protected network topology is incomplete")
            if observed.capture_closed and phase != "exited":
                raise HTTPException(409, "Capture closure requires the original exit")
            if prior is None:
                if phase != "prepared" or observed.capture_ready or observed.probe_ready:
                    raise HTTPException(
                        409, "Protected execution requires the original idle receipt"
                    )
                if observed.run_number > 1:
                    previous = await run_receipt(conn, tid, observed.run_number - 1)
                    if not previous or previous["phase"] not in {"exited", "absent"}:
                        raise HTTPException(409, "Previous protected execution is not retired")
                if policy.mode == "vpn":
                    await reserve_vpn_slot(conn, tid)
            else:
                previous_phase = prior["phase"]
                if any(prior.get(field) != value[field] for field in ORIGINAL_FIELDS):
                    raise HTTPException(409, "Original protected execution identity changed")
                if (
                    phase == previous_phase
                    and phase in {"fenced", "unknown", "exited"}
                    and not (
                        phase == "exited"
                        and observed.capture_closed
                        and not prior["capture_closed"]
                    )
                ):
                    return {"receipt": prior, "launch_allowed": False}
                permitted = (
                    (
                        phase == "starting"
                        and forced
                        and previous_phase == "prepared"
                        and not (observed.capture_ready or observed.probe_ready)
                    )
                    or (
                        phase == "ready"
                        and previous_phase == ("starting" if forced else "prepared")
                        and observed.capture_ready
                        and observed.probe_ready == forced
                    )
                    or (
                        phase in {"fenced", "unknown"}
                        and previous_phase in {"prepared", "starting", "ready", "fenced", "unknown"}
                        and not (previous_phase == "fenced" and phase == "unknown")
                    )
                    or (
                        phase == "exited"
                        and (
                            previous_phase in {"fenced", "unknown"}
                            or (
                                previous_phase == "exited"
                                and observed.capture_closed
                                and not prior["capture_closed"]
                            )
                        )
                    )
                )
                if not permitted:
                    raise HTTPException(409, "Original protected execution phase cannot reopen")
                if phase in {"fenced", "unknown", "exited"}:
                    value["capture_ready"] = prior["capture_ready"]
                    value["probe_ready"] = prior["probe_ready"]
                if policy.mode == "vpn" and phase == "exited":
                    await conn.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": VPN_SLOT_LOCK}
                    )
        table = s.legacy_execution_receipts
        if prior is None:
            await conn.execute(
                insert(table).values(task_id=tid, run_number=observed.run_number, receipt=value)
            )
        else:
            await conn.execute(
                update(table)
                .where(table.c.task_id == tid, table.c.run_number == observed.run_number)
                .values(receipt=value)
            )
        return {"receipt": value, "launch_allowed": phase == "starting"}


@router.post("/api/tasks/{task_id}/execution-network")
async def acknowledge_execution_network(
    request: Request, task_id: UUID, body: LegacyNetworkReceipt | LegacyNetworkAbsentReceipt
):
    require_service(request)
    require_runtime_version(request)
    return await record_network(
        request.app.state.engine, task_id, body, request.app.state.legacy_network_publication
    )
