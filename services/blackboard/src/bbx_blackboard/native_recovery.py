"""Preserve one frozen original execution and project its existing ledgers."""

from uuid import uuid4

from sqlalchemy import case, func, or_, select, update

from bbx_blackboard.runner_gateway import cancel_admitted
from bbx_blackboard.store import schema as s

IDENTITIES = {
    "runner_pod_uid": ("runner", "pod_uid"),
    "runner_container_id": ("runner", "container_id"),
    "host_container_id": ("runner", "host_container_id"),
    "sandbox_id": ("sandbox", "id"),
    "sandbox_cr_uid": ("sandbox", "cr_uid"),
    "sandbox_pod_uid": ("sandbox", "pod_uid"),
    "sandbox_container_id": ("sandbox", "container_id"),
    "journal_pvc_uid": ("journal_pvc", "uid"),
    "workspace_pvc_uid": ("workspace_pvc", "uid"),
}


async def original_owner_issue(conn, tid, record, instant):
    data = record["data"]
    if not data.get("credentials_issued") or not data.get("owner_epoch"):
        return None
    owner = (
        (
            await conn.execute(
                select(s.runner_owners).where(
                    s.runner_owners.c.task_id == tid,
                    s.runner_owners.c.epoch == data["owner_epoch"],
                )
            )
        )
        .mappings()
        .first()
    )
    if (
        not owner
        or not owner["current"]
        or owner["revoked"]
        or owner["pod_uid"] != record["observation"].get("runner", {}).get("pod_uid")
    ):
        return "native_owner_unavailable"
    if owner["expires_at"] <= instant or owner["hard_deadline"] <= instant:
        return "native_owner_expired"
    return None


async def capture(repo, conn, tid, record, task, reason, instant):
    data, control = record["data"], task["ctf_control"]
    epoch = data.get("owner_epoch")
    if data.get("recovery") or not data.get("credentials_issued") or not epoch:
        return data
    observed = record["observation"]
    resources = {
        name: value
        for name, (scope, field) in IDENTITIES.items()
        if (value := observed.get(scope, {}).get(field))
    }
    if observed.get("namespace_uid"):
        resources["namespace_uid"] = observed["namespace_uid"]
    incident = {
        "incident_id": str(uuid4()),
        "operation_id": record["operation_id"],
        "original_epoch": epoch,
        "controller_generation": record["controller_generation"],
        "captured_at": instant.isoformat(),
        "reason": reason,
        "sandbox_create_claimed": record["sandbox_create_claimed"],
        "resources": resources,
        "assignment_ids": {
            item["binding_id"]: item["assignment_ref"]
            for item in control.get("native_worker_active", {}).values()
        },
    }
    await conn.execute(
        update(s.tasks)
        .where(s.tasks.c.id == tid)
        .values(
            ctf_control={
                **control,
                "native_recovery": incident["incident_id"],
                "failure_hold": control.get("failure_hold")
                or {"task_wide": True, "source": "native_recovery"},
            }
        )
    )
    owners = s.runner_owners
    await conn.execute(
        update(owners)
        .where(owners.c.task_id == tid, or_(owners.c.epoch == epoch, owners.c.current.is_(True)))
        .values(current=False, revoked=True)
    )
    bindings = s.runner_bindings
    await conn.execute(
        update(bindings)
        .where(bindings.c.task_id == tid, bindings.c.permit_expires_at.is_not(None))
        .values(permit_expires_at=None)
    )
    await cancel_admitted(conn, tid)
    await repo.notify(tid, 0, connection=conn)
    return {**data, "recovery": incident}


async def _counts(conn, table, field, tid, *where):
    return dict(
        (
            await conn.execute(
                select(field, func.count()).where(table.c.task_id == tid, *where).group_by(field)
            )
        )
        .tuples()
        .all()
    )


async def status(conn, tid, record, task):
    incident = (record or {}).get("data", {}).get("recovery")
    if not incident:
        return {"state": "not_frozen"}
    epoch = incident["original_epoch"]
    bindings = s.runner_bindings
    found = (
        (
            await conn.execute(
                select(
                    bindings.c.id,
                    bindings.c.binding,
                    bindings.c.business,
                    bindings.c.status,
                    bindings.c.sdk_session_id,
                    bindings.c.cursor,
                    bindings.c.permit_revision,
                )
                .where(bindings.c.task_id == tid, bindings.c.owner_epoch == epoch)
                .order_by(
                    bindings.c.status.not_in(("registered", "prepared", "closing")), bindings.c.id
                )
                .limit(33)
            )
        )
        .mappings()
        .all()
    )
    calls, commands = s.runner_calls, s.sandbox_command_records
    inputs, flags, fees = s.runner_commands, s.flag_submissions, s.native_request_costs
    original_models = (calls.c.owner_epoch == epoch, calls.c.kind == "model")
    state = "frozen"
    if record["state"] == "stopped":
        state = "stopped"
    elif (
        task["deleting"]
        or record["desired"] == "stopped"
        or task["status"] in {"closing", "stopped", "failed", "finished"}
    ):
        state = "stopping"
    return {
        **{key: value for key, value in incident.items() if key != "assignment_ids"},
        "state": state,
        "bindings": [
            {
                "binding_id": binding["id"],
                "agent_id": binding["binding"]["agent_id"],
                "state": binding["status"],
                "assignment_id": incident["assignment_ids"].get(binding["id"])
                or binding["business"].get("native_assignment_id"),
                "sdk_session_id": binding["sdk_session_id"],
                "confirmed_cursor": binding["cursor"],
                "sdk_start_unconfirmed": binding["permit_revision"] > 0
                and binding["sdk_session_id"] is None,
            }
            for binding in found[:32]
        ],
        "bindings_truncated": len(found) > 32,
        "ledger": {
            "model_attempts": await _counts(conn, calls, calls.c.state, tid, *original_models),
            "model_transport": await _counts(
                conn,
                calls,
                func.coalesce(calls.c.data["transport"]["phase"].astext, "unconfirmed"),
                tid,
                *original_models,
                calls.c.state != "not_sent",
            ),
            "tool_invocations": await _counts(
                conn,
                calls,
                calls.c.state,
                tid,
                calls.c.owner_epoch == epoch,
                calls.c.kind == "tool",
            ),
            "sandbox_commands": await _counts(
                conn, commands, commands.c.state, tid, commands.c.epoch == epoch
            ),
            "sandbox_scope": await _counts(
                conn,
                commands,
                func.coalesce(commands.c.data["scope_state"].astext, "unconfirmed"),
                tid,
                commands.c.epoch == epoch,
            ),
            "runner_inputs": await _counts(
                conn,
                inputs,
                case(
                    (
                        (inputs.c.status == "applied") & inputs.c.native_proof.is_(None),
                        "application_unconfirmed",
                    ),
                    else_=inputs.c.status,
                ),
                tid,
                inputs.c.owner_epoch == epoch,
                inputs.c.kind == "input",
            ),
            "task_flag_submissions": await _counts(conn, flags, flags.c.status, tid),
            "task_native_fees": await _counts(conn, fees, fees.c.state, tid),
        },
    }
