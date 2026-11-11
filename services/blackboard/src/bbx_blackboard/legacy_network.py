"""Read original ordinary network evidence and the approved deployment policy."""

from bbx_contracts.legacy_network import (
    LegacyNetworkSnapshot,
    LegacyNetworkView,
    load_legacy_network_policy,
    snapshot,
)
from fastapi import HTTPException
from sqlalchemy import exists, select

from bbx_blackboard.store import schema as s


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
