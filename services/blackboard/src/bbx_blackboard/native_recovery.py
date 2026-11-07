"""Freeze, verify and replace a native execution without replaying its original ledgers."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

from bbx_contracts.runner import runner_json_text, runner_payload_digest
from bbx_contracts.runner_bootstrap import (
    NativeResumeRequest,
    native_pause_state,
    retirement_manifest_digest,
)
from fastapi import HTTPException
from sqlalchemy import and_, case, false, func, or_, select, update

from bbx_blackboard.board_events import digest, emit, prior
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


def _original_scope(incident, commands):
    binding = commands.c.data["binding"]
    return and_(
        commands.c.epoch == incident["original_epoch"],
        binding["epoch"].astext.is_not_distinct_from(str(incident["original_epoch"])),
        binding["sandbox_id"].astext.is_not_distinct_from(incident["resources"]["sandbox_id"]),
        binding["pod_uid"].astext.is_not_distinct_from(incident["resources"]["sandbox_pod_uid"]),
        binding["execution_context_id"].astext.is_not_distinct_from(
            incident["execution_context_id"]
        ),
    )


def exited_scopes(resource, commands):
    return or_(
        false(),
        *(
            _original_scope(incident, commands)
            for incident in resource["data"].get("recovery_history", [])
            if incident["exit_proof"]["sandbox_terminated"] is True
        ),
    )


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
    host_context = record["sandbox_host"].get("context", {})
    context_matches = (
        host_context.get("sandbox_id") == resources.get("sandbox_id")
        and host_context.get("sandbox_pod_uid") == resources.get("sandbox_pod_uid")
        and host_context.get("runner_pod_uid") == resources.get("runner_pod_uid")
    )
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
        "assignments": control.get("native_worker_active", {}),
        "lead_binding_id": data.get("binding_id"),
        "resource_generation": data.get("resource_generation", 1),
        "sandbox_create_op_id": data.get("sandbox_create_op_id", record["operation_id"]),
        "original_observation": observed,
        "execution_context_id": host_context.get("execution_context_id")
        if context_matches
        else None,
    }
    await conn.execute(
        update(s.tasks)
        .where(s.tasks.c.id == tid)
        .values(
            ctf_control={
                **control,
                "native_recovery": incident["incident_id"],
                "failure_hold": control.get("failure_hold")
                or {
                    "task_wide": True,
                    "source": "native_recovery",
                    **(
                        {"incident_id": incident["incident_id"]}
                        if (record["config"] or {}).get("journal_writer_lock") is True
                        else {}
                    ),
                },
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
    data = (record or {}).get("data", {})
    active = data.get("recovery")
    history = data.get("recovery_history", [])
    incident = active or (history[-1] if history else None)
    if not record or not incident:
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
    if active:
        state = "requested" if incident.get("request") else "frozen"
    else:
        state = "resumed" if incident.get("replacement_activated") else "rebuilding"
    if record["state"] == "stopped":
        state = "stopped"
    elif (
        task["deleting"]
        or record["desired"] == "stopped"
        or task["status"] in {"closing", "stopped", "failed", "finished"}
    ):
        state = "stopping"
    return {
        **{
            key: incident[key]
            for key in (
                "incident_id",
                "operation_id",
                "reason",
                "captured_at",
                "original_epoch",
                "controller_generation",
                "sandbox_create_claimed",
                "resources",
            )
        },
        "state": state,
        "blocker": incident.get("blocker") if active else data.get("recovery_activation_blocker"),
        "request_id": incident.get("request", {}).get("request_id"),
        "replacement_epoch": incident.get("replacement_epoch"),
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


class NativeRecovery:
    def __init__(self, bootstrap):
        self.bootstrap = bootstrap
        self.repo, self.control = bootstrap.repo, bootstrap.control
        self.native = bootstrap.gateway.native

    def _context(self, record, task, incident_id):
        data, control = record["data"], task["ctf_control"]
        incident = data.get("recovery")
        if (
            not incident
            or incident["incident_id"] != str(incident_id)
            or control.get("native_recovery") != str(incident_id)
            or record["state"] != "frozen"
            or task["status"] != "running"
            or self.bootstrap._stopping(task, record)
        ):
            raise HTTPException(409, "Original recovery is no longer current")
        if (
            not data.get("worker_spec")
            or not incident.get("lead_binding_id")
            or not incident.get("sandbox_create_claimed")
            or record["config"].get("journal_writer_lock") is not True
            or incident.get("original_observation", {}).get("runner", {}).get("journal_writer_lock")
            is not True
        ):
            raise HTTPException(409, "Original execution lacks recovery writer proof support")
        return incident

    @staticmethod
    def _independent_hold(task, incident):
        control = task["ctf_control"]
        return (
            control.get("phase") != "running"
            or control.get("failure_hold")
            != {
                "task_wide": True,
                "source": "native_recovery",
                "incident_id": incident["incident_id"],
            }
            or bool(control.get("native_budget_hold"))
            or native_pause_state(task) in {"draining", "paused", "resuming"}
        )

    async def request(self, tid, request):
        body = request.model_dump(mode="json")
        hashed = digest({"operation": "native_user_recovery", **body})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            if previous := await prior(conn, tid, "user", request.request_id, hashed):
                return previous
            task, record = await self.control._task(conn, tid), await self.bootstrap._get(conn, tid)
            if not record:
                raise HTTPException(409, "Original recovery is unavailable")
            incident = self._context(record, task, request.incident_id)
            if self._independent_hold(task, incident) or incident.get("request"):
                raise HTTPException(409, "Original recovery is held or already requested")
            await self._write(conn, tid, record, {**incident, "request": body, "blocker": None})
            return await emit(
                self.repo,
                conn,
                tid,
                "user",
                "board.request.linked",
                request.request_id,
                hashed,
                {"incident_id": str(request.incident_id), "request_id": str(request.request_id)},
            )

    async def _write(self, conn, tid, record, incident):
        await self.bootstrap._set(
            conn,
            tid,
            data={**record["data"], "recovery": incident},
            next_check_at=datetime.now(UTC),
        )

    async def _blocked(self, conn, tid, record, incident, reason):
        if incident.get("blocker") != reason:
            await self._write(conn, tid, record, {**incident, "blocker": reason})
        return {"blocker": reason}

    async def _effects(self, conn, tid, incident):
        epoch = incident["original_epoch"]
        calls, inputs, commands = s.runner_calls, s.runner_commands, s.sandbox_command_records
        checks = [
            (
                "original_call_unresolved",
                calls,
                (
                    calls.c.owner_epoch == epoch,
                    or_(
                        calls.c.state.not_in(["not_sent", "completed"]),
                        (calls.c.kind == "tool")
                        & (calls.c.state == "completed")
                        & or_(
                            calls.c.data["result_digest"].astext.is_(None),
                            calls.c.data["result_ciphertext"].astext.is_(None),
                        ),
                        (calls.c.kind == "model")
                        & (calls.c.state != "not_sent")
                        & or_(
                            calls.c.data["transport"]["phase"].astext.is_distinct_from(
                                "response_ended"
                            ),
                            calls.c.data["fee_state"].astext.is_distinct_from("known"),
                        ),
                    ),
                ),
            ),
            (
                "original_command_unresolved",
                commands,
                (
                    commands.c.epoch == epoch,
                    or_(
                        commands.c.state.not_in(["completed", "failed"]),
                        commands.c.execution_id.is_(None),
                        commands.c.exit_code.is_(None),
                        commands.c.finished_at.is_(None),
                        ~_original_scope(incident, commands),
                    ),
                ),
            ),
            (
                "original_input_unconfirmed",
                inputs,
                (
                    inputs.c.owner_epoch == epoch,
                    inputs.c.kind == "input",
                    or_(inputs.c.status != "applied", inputs.c.native_proof.is_(None)),
                ),
            ),
            (
                "original_flag_unresolved",
                s.flag_submissions,
                (s.flag_submissions.c.status.in_(["prepared", "dispatched", "unknown"]),),
            ),
            (
                "original_cost_unresolved",
                s.native_request_costs,
                (s.native_request_costs.c.state != "known",),
            ),
        ]
        for reason, table, where in checks:
            if (
                await conn.execute(
                    select(table.c.task_id).where(table.c.task_id == tid, *where).limit(1)
                )
            ).first():
                return reason
        return None

    async def _manifest(self, conn, tid, task, incident):
        if self._independent_hold(task, incident):
            return None, "budget_or_failure_hold"
        if task["ctf_control"].get("unresolved_checkpoints"):
            return None, "original_checkpoint_unresolved"
        if not incident.get("exit_proof"):
            return None, "original_exit_unconfirmed"
        epoch = incident["original_epoch"]
        # Exact Sandbox exit proves the old scopes cannot run; command results still need receipts.
        if not incident.get("execution_context_id"):
            return None, "original_scope_unconfirmed"
        if reason := await self._effects(conn, tid, incident):
            return None, reason
        table = s.runner_bindings
        bindings = (
            (
                await conn.execute(
                    select(table)
                    .where(table.c.task_id == tid, table.c.owner_epoch == epoch)
                    .order_by(table.c.id)
                    .limit(33)
                )
            )
            .mappings()
            .all()
        )
        ids = {row["id"] for row in bindings}
        active = {incident["lead_binding_id"], *incident["assignment_ids"]}
        if (
            len(bindings) > 32
            or not active <= ids
            or (task["ctf_control"].get("native_worker_active", {}) != incident["assignments"])
        ):
            return None, "original_bindings_unconfirmed"
        for binding in bindings:
            if (
                bool(binding["sdk_session_id"]) != bool(binding["cursor"])
                or (binding["permit_revision"] and not binding["sdk_session_id"])
                or (
                    binding["id"] not in active
                    and binding["status"] in {"registered", "prepared", "closing"}
                )
            ):
                return None, "original_journal_unconfirmed"
        generation = incident["resource_generation"]
        subdir = incident["original_observation"].get("journal_subdir")
        lock_id = incident["original_observation"].get("journal_lock_id")
        if (
            not lock_id
            or not incident["original_observation"].get("journal_storage")
            or subdir != f"epoch-{generation}"
        ):
            return None, "journal_writer_unconfirmed"
        return {
            "incident_id": incident["incident_id"],
            "resource_generation": generation + 1,
            "original_journal_subdir": subdir,
            "journal_lock_id": lock_id,
            "bindings": [
                {
                    "binding_id": binding["id"],
                    "binding": binding["binding"],
                    "sdk_session_id": binding["sdk_session_id"],
                    "cursor": binding["cursor"],
                    "prepare_digest": binding["payload_digest"],
                }
                for binding in bindings
            ],
        }, None

    async def act(self, tid, body):
        proof = body.proof
        async with self.repo.engine.begin() as conn:
            record, task = await self.bootstrap._checked(conn, tid, body)
            if body.action == "recovery_verifier_observe":
                return await self._observe_verifier(conn, tid, record, proof)
            incident = self._context(record, task, proof.get("incident_id"))
            if not incident.get("request"):
                raise HTTPException(409, "User recovery is not requested")
            if body.action == "recovery_blocked":
                reason = proof.get("blocker")
                if reason not in {
                    "original_exit_unconfirmed",
                    "journal_writer_unconfirmed",
                    "original_journal_unconfirmed",
                    "replacement_resources_unconfirmed",
                }:
                    raise HTTPException(422, "Invalid recovery blocker")
                return await self._blocked(conn, tid, record, incident, reason)
            if body.action == "recovery_exit":
                observation = record["observation"]
                if (
                    any(
                        proof.get(key) is not True
                        for key in (
                            "runner_terminated",
                            "sandbox_terminated",
                            "pvcs_retained",
                        )
                    )
                    or proof.get("observation_digest")
                    != runner_payload_digest(runner_json_text(observation))
                    or set(IDENTITIES) | {"namespace_uid"} != incident["resources"].keys()
                    or any(
                        observation.get(scope, {}).get(field) != incident["resources"][name]
                        for name, (scope, field) in IDENTITIES.items()
                    )
                    or observation.get("namespace_uid") != incident["resources"]["namespace_uid"]
                ):
                    raise HTTPException(409, "Original execution exit is not confirmed")
                if not incident.get("exit_proof"):
                    await self._write(
                        conn,
                        tid,
                        record,
                        {
                            **incident,
                            "exit_proof": proof,
                            "exit_observation": observation,
                            "blocker": None,
                        },
                    )
                return self.bootstrap._public(await self.bootstrap._get(conn, tid))
            manifest, blocker = await self._manifest(conn, tid, task, incident)
            if manifest is None:
                return await self._blocked(conn, tid, record, incident, blocker)
            manifest_hash = retirement_manifest_digest(manifest)
            if body.action == "recovery_prepare":
                return {"manifest": manifest, "manifest_digest": manifest_hash}
            if body.action == "recovery_verifier_admit":
                if proof.get("manifest_digest") != manifest_hash:
                    raise HTTPException(409, "Original journal manifest changed")
                verifier = incident.get("verifier")
                if verifier:
                    if verifier["manifest_digest"] != manifest_hash:
                        raise HTTPException(409, "Original journal verifier manifest changed")
                    return {"create_allowed": False}
                await self._write(
                    conn,
                    tid,
                    record,
                    {
                        **incident,
                        "verifier": {
                            "manifest_digest": manifest_hash,
                            "claimed_at": datetime.now(UTC).isoformat(),
                            "expected_work_copy": self._expected_work_copy(manifest),
                        },
                    },
                )
                return {"create_allowed": True}
            self._work_copy(incident, manifest, manifest_hash, proof)
            budget = await self.native._budget(conn, tid)
            if not self._budget_available(budget):
                return await self._blocked(
                    conn, tid, record, incident, "original_budget_unconfirmed"
                )
        try:
            native, used, exceeded = await self.read_budget(budget)
        except HTTPException:
            async with self.repo.engine.begin() as conn:
                record, task = await self.bootstrap._checked(conn, tid, body)
                incident = self._context(record, task, proof.get("incident_id"))
                return await self._blocked(
                    conn, tid, record, incident, "original_budget_unconfirmed"
                )
        if not exceeded and used < Decimal(budget["config"]["threshold_usd"]):
            try:
                await self.bootstrap.gateway.routes.retire_for_recovery(
                    tid, body, self, budget, used, exceeded
                )
            except HTTPException:
                async with self.repo.engine.begin() as conn:
                    record, task = await self.bootstrap._checked(conn, tid, body)
                    incident = self._context(record, task, proof.get("incident_id"))
                    return await self._blocked(
                        conn, tid, record, incident, "original_route_unconfirmed"
                    )
        async with self.repo.engine.begin() as conn:
            record, task = await self.bootstrap._checked(conn, tid, body)
            incident = self._context(record, task, proof.get("incident_id"))
            current_manifest, blocker = await self._manifest(conn, tid, task, incident)
            if current_manifest != manifest:
                return await self._blocked(
                    conn, tid, record, incident, blocker or "original_journal_unconfirmed"
                )
            if not await self.matching_budget(conn, tid, budget, used, exceeded):
                return await self._blocked(
                    conn, tid, record, incident, "original_budget_unconfirmed"
                )
            route = await self.bootstrap.gateway.routes._get(conn, tid)
            if not route or (route["desired"], route["publication_state"]) != (
                "recovery",
                "retired",
            ):
                return await self._blocked(
                    conn, tid, record, incident, "original_route_unconfirmed"
                )
            await self.record_usage(conn, tid, native, used)
            return await self._replace(conn, tid, record, task, incident, manifest, proof, route)

    async def read_budget(self, budget):
        await self.native.verified_key(budget, budget["config"]["native_value"])
        return await self.native.threshold_status(budget)

    async def matching_budget(self, conn, tid, budget, used, exceeded):
        current = await self.native._budget(conn, tid)
        return (
            self._budget_available(current)
            and self.bootstrap.pause_control._budget_identity(current)
            == self.bootstrap.pause_control._budget_identity(budget)
            and current["config"] == budget["config"]
            and not exceeded
            and used < Decimal(current["config"]["threshold_usd"])
            and used >= await self.native.usage_floor(conn, tid, current)
        )

    @staticmethod
    async def record_usage(conn, tid, native, used):
        await conn.execute(
            update(s.native_task_budgets)
            .where(s.native_task_budgets.c.task_id == tid)
            .values(status_snapshot={"latest": native, "used_high_water_usd": str(used)})
        )

    @staticmethod
    def _budget_available(budget):
        return bool(
            budget["publication_state"] == "ready"
            and budget["native_resource_id"]
            and not budget["hold_reason"]
            and not budget["config"].get("pending_increase")
        )

    async def _observe_verifier(self, conn, tid, record, proof):
        incident = record["data"].get("recovery") or {}
        verifier = incident.get("verifier")
        if (
            not verifier
            or incident["incident_id"] != proof.get("incident_id")
            or verifier["manifest_digest"] != proof.get("manifest_digest")
        ):
            raise HTTPException(409, "Original journal verifier is not claimed")
        terminated = proof.get("verifier_terminated")
        work_copy = proof.get("work_copy")
        identities = {
            "pod_uid": proof.get("verifier_pod_uid"),
            "init_container_id": proof.get("verifier_init_container_id"),
            "container_id": proof.get("verifier_container_id"),
        }
        if (
            type(terminated) is not bool
            or not identities["pod_uid"]
            or identities["pod_uid"]
            in (incident["resources"]["runner_pod_uid"], incident["resources"]["sandbox_pod_uid"])
            or (terminated and not all(identities.values()))
            or (
                work_copy is not None
                and (terminated is not True or work_copy != verifier["expected_work_copy"])
            )
            or any(
                value is not None and (not isinstance(value, str) or not 0 < len(value) <= 256)
                for value in identities.values()
            )
            or any(
                verifier.get(key) not in (None, value)
                for key, value in identities.items()
                if value is not None
            )
        ):
            raise HTTPException(409, "Original journal verifier identity or exit changed")
        observed = {
            **verifier,
            **{key: value for key, value in identities.items() if value is not None},
            "terminated": bool(verifier.get("terminated") or terminated),
            **({"work_copy": work_copy} if work_copy is not None else {}),
        }
        if observed != verifier:
            await self._write(conn, tid, record, {**incident, "verifier": observed})
        return self.bootstrap._public(await self.bootstrap._get(conn, tid))

    @staticmethod
    def _expected_work_copy(manifest):
        return {
            "directory": f"epoch-{manifest['resource_generation']}",
            "bindings": [
                {key: item[key] for key in ("binding_id", "sdk_session_id", "cursor")}
                for item in manifest["bindings"]
            ],
        }

    def _work_copy(self, incident, manifest, manifest_hash, proof):
        expected = self._expected_work_copy(manifest)
        verifier = incident.get("verifier", {})
        if (
            proof.get("manifest_digest") != manifest_hash
            or verifier.get("manifest_digest") != manifest_hash
            or verifier.get("terminated") is not True
            or verifier.get("pod_uid") != proof.get("verifier_pod_uid")
            or proof.get("work_copy") != expected
            or verifier.get("work_copy") != expected
            or proof.get("writer_released") is not True
            or proof.get("journal_pvc_uid") != incident["resources"]["journal_pvc_uid"]
            or proof.get("workspace_pvc_uid") != incident["resources"]["workspace_pvc_uid"]
            or not isinstance(proof.get("verifier_pod_uid"), str)
            or not proof["verifier_pod_uid"]
        ):
            raise HTTPException(409, "Original journal working copy is not confirmed")

    async def _replace(self, conn, tid, record, task, incident, manifest, proof, route):
        generation = manifest["resource_generation"]
        data = record["data"]
        request = NativeResumeRequest(
            pause_id=incident["incident_id"],
            request_id=incident["request"]["request_id"],
            instruction=incident["request"]["instruction"],
        )
        _, continuation = await self.bootstrap.pause_control.stage_continuation(
            conn,
            tid,
            task,
            {**record, "data": {**data, "resource_generation": generation}},
            {
                "lead_binding_id": incident["lead_binding_id"],
                "binding_ids": [item["binding_id"] for item in manifest["bindings"]],
                "assignments": incident["assignments"],
                "owner_epoch": incident["original_epoch"],
            },
            request,
            digest({"operation": "native_recovery_continuation", **incident["request"]}),
            recovery=True,
        )
        archived = {
            **incident,
            "blocker": None,
            "replacement_epoch": incident["original_epoch"] + 1,
            "replacement_proof": proof,
            "original_route": {
                key: route[key] for key in ("resource_id", "revision", "operation_id", "value")
            },
        }
        retained = {
            key: record["observation"][key]
            for key in (
                "task_id",
                "operation_id",
                "namespace",
                "namespace_uid",
                "source_id",
                "cluster_uid",
                "journal_pvc",
                "workspace_pvc",
                "execd_secret",
                "journal_lock_id",
                "journal_storage",
            )
            if key in record["observation"]
        }
        await self.bootstrap._set(
            conn,
            tid,
            sandbox_create_claimed=False,
            sandbox_host={},
            observation={
                **retained,
                "resource_generation": generation,
                "journal_subdir": proof["work_copy"]["directory"],
            },
            data={
                "resource_generation": generation,
                "sandbox_create_op_id": str(
                    uuid5(NAMESPACE_URL, "native-recovery-sandbox:" + incident["incident_id"])
                ),
                "journal_subdir": proof["work_copy"]["directory"],
                "recovery_history": [*data.get("recovery_history", []), archived],
                "continuation": continuation,
                "lead_turn_id": continuation["lead_turn_id"],
                "worker_spec": data["worker_spec"],
                "worker_binding_ids": [],
            },
            state="preparing",
            last_error=None,
            next_check_at=datetime.now(UTC),
        )
        await self.repo.notify(tid, 0, connection=conn)
        return self.bootstrap._public(await self.bootstrap._get(conn, tid))
