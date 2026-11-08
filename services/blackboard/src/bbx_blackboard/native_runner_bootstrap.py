"""Trusted native Task bootstrap; Pi remains the only Agent loop."""

import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from bbx_contracts.challenge_board import native_dispatch_enabled
from bbx_contracts.ctf import render_ctf_prompt, resolve_ctf_model
from bbx_contracts.model_limits import effective_max_output_tokens
from bbx_contracts.runner import (
    PROTOCOL,
    EffectiveRunnerModel,
    runner_json_text,
    runner_payload_digest,
)
from bbx_contracts.runner_bootstrap import (
    BINDING_CAPTURE_CONFIG_FIELDS,
    RUNTIME_FENCE_SEQUENCE,
    RUNTIME_LOCK_KEY,
    NativeBudgetIncreaseRequest,
    NativeBudgetIncreaseStatus,
    NativePauseRequest,
    NativePauseStatus,
    NativeRecoveryIntent,
    NativeRecoveryRequest,
    NativeRecoveryStatus,
    NativeResumeRequest,
    NativeRunnerAction,
    NativeRunnerClaim,
    NativeRunnerLaunch,
    management_document_digest,
    native_pause_state,
    native_recovery_frozen,
    validate_binding_capture_deployment,
)
from bbx_contracts.runner_inputs import INPUT_PROFILE
from bbx_contracts.runner_vpn import VPN_PROFILE, NativeVpnProfile, vpn_targets_compatible
from bbx_contracts.sandbox_tools import SANDBOX_TOOLS
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import insert, or_, select, text, update

from bbx_blackboard import native_recovery
from bbx_blackboard.auth import require_service, require_user_or_service
from bbx_blackboard.gateway_auth import issue_gateway_credentials
from bbx_blackboard.native_budget_increase import increase_budget
from bbx_blackboard.native_helpers import NativeHelpers
from bbx_blackboard.native_inputs import (
    NativeInputs,
    freeze_native_inputs,
    require_inputs_confirmed,
)
from bbx_blackboard.native_mcp_routes import NativeMCPRoutes
from bbx_blackboard.native_pause import NativePause
from bbx_blackboard.native_traffic import PROFILE, lost_captures, opted_in
from bbx_blackboard.native_vpn import (
    NativeVpn,
    vpn_enabled,
    vpn_exit_data,
    vpn_solver_deadline,
    vpn_state,
)
from bbx_blackboard.native_worker_dispatch import NativeWorkers
from bbx_blackboard.profiles import ProfileStore, ctf_profile
from bbx_blackboard.runner_auth import issue_runner_control_token, runner_credential_expiry
from bbx_blackboard.runner_control import OWNER_SECONDS, fence, row
from bbx_blackboard.runner_gateway import decode
from bbx_blackboard.store import schema as s

router = APIRouter(tags=["native-runner-bootstrap"])


def instant():
    return datetime.now(UTC)


def identity_id(operation, purpose):
    return str(uuid5(NAMESPACE_URL, "native-runner:" + operation + ":" + purpose))


def native_role_thinking(reasoning_effort):
    thinking = "off" if reasoning_effort == "none" else reasoning_effort
    if thinking not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise HTTPException(409, "Native bootstrap requires explicit supported role thinking")
    return thinking


def effective_role_model(profile, role_name, published):
    role = resolve_ctf_model(profile, role_name)
    model = dict(published)
    thinking = native_role_thinking(role.reasoning_effort)
    output = effective_max_output_tokens(role)
    if (
        output > model["max_output_tokens"]
        or (role.context_window and role.context_window > model["context_window"])
        or (role.supports_vision and "image" not in model["input"])
    ):
        raise HTTPException(409, "Frozen role exceeds native SDK model capabilities")
    model.update(
        thinking_level=thinking,
        max_output_tokens=output,
        context_window=role.context_window or model["context_window"],
        input=["text", "image"] if role.supports_vision else ["text"],
    )
    return EffectiveRunnerModel.model_validate(model).model_dump(mode="json")


class NativeRunnerBootstrap:
    def __init__(self, gateway):
        self.gateway, self.repo, self.control = gateway, gateway.repo, gateway.control
        self.settings, self.ctf = gateway.settings, gateway.ctf
        from bbx_blackboard.native_resource_retirement import NativeResourceRetirement

        self.retirement = NativeResourceRetirement(self)
        self.workers = NativeWorkers(self)
        self.helpers = NativeHelpers(self)
        self.pause_control = NativePause(self)
        self.recovery = native_recovery.NativeRecovery(self)
        self.inputs = NativeInputs(self)
        self.vpn = NativeVpn(self)
        gateway.vpn = self.vpn

    async def _get(self, conn, tid):
        found = (
            (
                await conn.execute(
                    select(s.task_runner_resources).where(s.task_runner_resources.c.task_id == tid)
                )
            )
            .mappings()
            .first()
        )
        return dict(found) if found else None

    async def _set(self, conn, tid, **values):
        await conn.execute(
            update(s.task_runner_resources)
            .where(s.task_runner_resources.c.task_id == tid)
            .values(revision=s.task_runner_resources.c.revision + 1, **values)
        )

    def _stopping(self, task, record):
        return (
            record["desired"] == "stopped"
            or task["ctf_control"].get("native_stop_requested")
            or instant() >= record["hard_deadline"]
            or NativeMCPRoutes.stopping(task)
        )

    async def _global_fence(self, conn, generation, pid, started_at):
        current = (
            (
                await conn.execute(
                    text(f"SELECT last_value, is_called FROM {RUNTIME_FENCE_SEQUENCE}")
                )
            )
            .mappings()
            .one()
        )
        held = (
            await conn.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks l JOIN pg_stat_activity a "
                    "ON a.pid=l.pid WHERE locktype='advisory' "
                    "AND granted AND l.pid=:pid AND a.backend_start=:started_at "
                    "AND mode='ExclusiveLock' AND classid=:high AND objid=:low "
                    "AND objsubid=1 AND database=(SELECT oid FROM pg_database "
                    "WHERE datname=current_database()))"
                ),
                {
                    "pid": pid,
                    "started_at": started_at,
                    "high": RUNTIME_LOCK_KEY >> 32,
                    "low": RUNTIME_LOCK_KEY & 0xFFFFFFFF,
                },
            )
        ).scalar_one()
        if not current["is_called"] or current["last_value"] != generation or not held:
            raise HTTPException(409, "Native Runtime instance fence is no longer current")

    async def _checked(self, conn, tid, action, *, live=False):
        await self.repo.lock(conn, tid)
        record = await self._get(conn, tid)
        if record is None:
            raise HTTPException(404, "Native Runner bootstrap is not registered")
        if (
            record["operation_id"] != str(action.operation_id)
            or record["controller_id"] != action.controller_id
            or record["controller_generation"] != action.controller_generation
            or record["data"].get("resource_generation", 1) != action.resource_generation
        ):
            raise HTTPException(409, "Native Runner controller fence changed")
        await self._global_fence(
            conn,
            action.controller_generation,
            record["controller_backend_pid"],
            record["controller_backend_start"],
        )
        task = await self.control._task(conn, tid)
        if live and self._stopping(task, record):
            raise HTTPException(409, "Native Runner bootstrap is stopped")
        if live and (record["data"].get("recovery") or native_recovery_frozen(task)):
            raise HTTPException(409, "Original native execution is frozen")
        return record, task

    @staticmethod
    def _public(record):
        if record is None:
            return {"state": "not_registered"}
        result = {
            key: record[key]
            for key in (
                "task_id",
                "operation_id",
                "controller_id",
                "controller_generation",
                "revision",
                "namespace",
                "state",
                "desired",
                "hard_deadline",
                "sandbox_create_claimed",
                "observation",
                "data",
                "last_error",
            )
        }
        retirement = record["data"].get("retirement", {})
        result["retirement"] = {
            **retirement.get("manifest", {}),
            **({"authority": retirement["authority"]} if retirement.get("authority") else {}),
        }
        result["retirement_id"] = retirement.get("operation_id")
        result["never_claimed"] = (
            record["config"] is None
            and record["controller_id"] is None
            and record["sandbox_create_claimed"] is False
            and not record["observation"]
        )
        return result

    def require_launch_configuration(self):
        if not self.settings.sandbox_tenant_resolver_token.get_secret_value():
            raise HTTPException(503, "Sandbox tenant resolver is unavailable")
        self.gateway.native._configured()
        self.gateway.routes.configuration()

    async def launch(self, tid, raw):
        body, digest = decode(NativeRunnerLaunch, raw, 262144)
        self.require_launch_configuration()
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            return await self.queue_locked(conn, tid, body, digest)

    async def queue_locked(self, conn, tid, body, digest, *, publication=None):
        if body.exchange_rate.observed_at.tzinfo is None:
            raise HTTPException(422, "Frozen exchange rate requires a timezone")
        task = await self.control._task(conn, tid, live=True)
        old = await self._get(conn, tid)
        if old:
            if old["request_digest"] != digest:
                raise HTTPException(409, "Native Runner launch request changed")
            return self._public(old)
        if task["status"] not in {"created", "provisioning"}:
            raise HTTPException(409, "Only an unstarted V1 Task can bootstrap")
        if (
            await conn.execute(
                select(s.runner_owners.c.epoch).where(s.runner_owners.c.task_id == tid).limit(1)
            )
        ).first():
            raise HTTPException(409, "Existing Runner owner cannot enter native bootstrap")
        if (body.helper_foreground or body.helper_background) and not native_dispatch_enabled(task):
            raise HTTPException(422, "Helpers require native Worker dispatch")
        if native_dispatch_enabled(task) and not 1 <= task["ctf_options"]["max_teammates"] <= 20:
            raise HTTPException(422, "Native Tasks allow 1 to 20 Worker seats")
        started = instant()
        remaining = task["budget"]["max_minutes"] * 60 - task["active_seconds"]
        if task["active_since"]:
            remaining -= max(0, (started - task["active_since"]).total_seconds())
        if remaining <= 0:
            raise HTTPException(409, "Task hard deadline has expired")
        try:
            deadline = started + timedelta(seconds=remaining)
        except OverflowError as error:
            raise HTTPException(422, "Task duration exceeds the native deadline range") from error
        input_manifest = None
        if task.get("initial_attachments"):
            if (publication or {}).get("inputs_profile") != INPUT_PROFILE:
                raise HTTPException(409, "Shared initial attachment capability is not frozen")
            try:
                input_manifest = freeze_native_inputs(tid, task["initial_attachments"])
            except ValueError as error:
                raise HTTPException(422, "Invalid native shared attachment manifest") from error
        key = "bbx-sandbox-" + secrets.token_urlsafe(32)
        operation = identity_id(str(tid), str(body.request_id))
        value = dict(
            task_id=tid,
            operation_id=operation,
            request_id=str(body.request_id),
            request_digest=digest,
            request={
                **body.model_dump(mode="json"),
                **({"publication": publication} if publication is not None else {}),
                **({"shared_inputs": input_manifest} if input_manifest else {}),
            },
            config=None,
            controller_id=None,
            controller_generation=0,
            revision=1,
            namespace="bbx-task-" + str(tid),
            state="queued",
            desired="active",
            hard_deadline=deadline,
            sandbox_create_claimed=False,
            tenant_key_hash=hashlib.sha256(key.encode()).hexdigest(),
            tenant_key_ciphertext=self.gateway.cipher.encrypt(key.encode()).decode(),
            observation={},
            data={
                **({"traffic_profile": PROFILE} if body.traffic_profile == PROFILE else {}),
                **({"inputs_profile": INPUT_PROFILE} if input_manifest else {}),
                **(
                    {"vpn_profile": VPN_PROFILE, "vpn": {"state": "idle"}}
                    if (publication or {}).get("vpn_profile") == VPN_PROFILE
                    else {}
                ),
            },
            last_error=None,
            next_check_at=started,
            created_at=started,
        )
        await conn.execute(insert(s.task_runner_resources).values(**value))
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                status="provisioning",
                active_since=task["active_since"] or started,
                ctf_control={**task["ctf_control"], "phase": "provisioning"},
            )
        )
        return self._public(value)

    @staticmethod
    def _configuration(value):
        required = {
            "source_id",
            "cluster_uid",
            "runner_image",
            "sandbox_image",
            "environment_revision",
            "runner_blackboard_url",
            "runner_model_proxy_url",
        }
        if not required <= value.keys() or any(
            not isinstance(value[key], str) or not value[key] for key in required
        ):
            raise HTTPException(422, "Trusted native resource deployment is incomplete")
        for key in ("runner_blackboard_url", "runner_model_proxy_url"):
            parsed = urlsplit(value[key])
            if (
                (
                    parsed.scheme != "https"
                    and not (
                        parsed.scheme == "http"
                        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                    )
                )
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise HTTPException(422, "Runner control origins must use the trusted TLS boundary")
        if "journal_writer_lock" in value and (
            value["journal_writer_lock"] is not True
            or value.get("runner_profile") != "node-sandbox-host-v1"
        ):
            raise HTTPException(422, "Native journal writer lock requires the Sandbox host profile")
        if any("@sha256:" not in value[key] for key in ("runner_image", "sandbox_image")):
            raise HTTPException(422, "Native resource images require immutable digests")
        if BINDING_CAPTURE_CONFIG_FIELDS & value.keys():
            try:
                validate_binding_capture_deployment(value)
            except ValueError as error:
                raise HTTPException(422, "Trusted native capture deployment is invalid") from error
        if "vpn" in value:
            try:
                validate_binding_capture_deployment(value)
                profile = NativeVpnProfile.model_validate(value["vpn"])
                if profile.sandbox_image.rsplit("@", 1)[-1] in {
                    value["traffic_sandbox_image"].rsplit("@", 1)[-1],
                    value["sandbox_image"].rsplit("@", 1)[-1],
                }:
                    raise ValueError("VPN requires a distinct frozen Sandbox image")
                vpn_targets_compatible(
                    profile, value["traffic_targets"], value.get("runner_control_egress", ())
                )
                if profile.peer_namespace and profile.peer_namespace in {
                    value.get("system_namespace"),
                    "default",
                    "kube-system",
                    "kube-public",
                    "kube-node-lease",
                }:
                    raise ValueError("VPN peer namespace overlaps a control namespace")
            except (ValueError, TypeError, KeyError) as error:
                raise HTTPException(422, "Trusted native VPN deployment is invalid") from error
        return value

    async def claim(self, tid, raw):
        body, _ = decode(NativeRunnerClaim, raw, 262144)
        config = self._configuration(body.resource_config)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            record = await self._get(conn, tid)
            if record is None:
                raise HTTPException(404, "Native Runner bootstrap is not registered")
            task = await self.control._task(conn, tid)
            if vpn_enabled(record) != ("vpn" in config):
                raise HTTPException(409, "Original Task VPN deployment changed")
            if opted_in(record) and "traffic_targets" not in config:
                raise HTTPException(
                    409, "Original Task requires the frozen native capture deployment"
                )
            if (
                native_dispatch_enabled(task)
                and config.get("runner_profile") != "node-sandbox-host-v1"
            ):
                raise HTTPException(
                    409, "Native Worker dispatch requires the original Sandbox host profile"
                )
            if task["deleting"] and record["config"] is None:
                raise HTTPException(
                    409, "An unclaimed deleting Task cannot acquire native resources"
                )
            expected = record["request"].get("publication", {}).get("runtime_config_digest")
            if expected is not None and management_document_digest(config) != expected:
                raise HTTPException(409, "Runtime does not match the Task's published environment")
            if record["config"] is not None and record["config"] != config:
                raise HTTPException(409, "Frozen native resource deployment changed")
            await self._global_fence(
                conn, body.controller_fence, body.controller_pid, body.controller_started_at
            )
            if body.controller_fence < record["controller_generation"] or (
                body.controller_fence == record["controller_generation"]
                and record["controller_id"] != body.controller_id
            ):
                raise HTTPException(409, "Native Runtime claim is stale")
            generation = body.controller_fence
            desired = "stopped" if self._stopping(task, record) else "active"
            data = dict(record["data"])
            state, last_error = record["state"], record["last_error"]
            if desired == "active" and not data.get("recovery"):
                if reason := await native_recovery.original_owner_issue(
                    conn, tid, record, instant()
                ):
                    data = await native_recovery.capture(
                        self.repo, conn, tid, record, task, reason, instant()
                    )
                    state, last_error = "frozen", reason
            if task["deleting"] and record["config"] is not None:
                if not data.get("retirement"):
                    data["retirement"] = {
                        "operation_id": str(
                            uuid5(NAMESPACE_URL, "native-retire:" + record["operation_id"])
                        ),
                        "policy": "user_delete",
                        "requested_at": instant().isoformat(),
                        "state": "pending",
                        "manifest": {},
                    }
                authority = body.retirement_single_apiserver
                if authority:
                    if set(authority) != {"pod_uid", "node_uid"} or not all(authority.values()):
                        raise HTTPException(
                            422, "Retirement requires a trusted single-apiserver identity"
                        )
                    if data["retirement"].get("authority") not in (None, authority):
                        raise HTTPException(409, "Frozen retirement apiserver authority changed")
                    data["retirement"] = {**data["retirement"], "authority": authority}
            await self._set(
                conn,
                tid,
                config=config,
                controller_id=body.controller_id,
                controller_backend_pid=body.controller_pid,
                controller_backend_start=body.controller_started_at,
                controller_generation=generation,
                desired=desired,
                state=state,
                last_error=last_error,
                data=data,
            )
            record = await self._get(conn, tid)
        assert record is not None
        return {
            **self._public(record),
            "config": record["config"],
            "tenant_key": self.gateway.cipher.decrypt(
                record["tenant_key_ciphertext"].encode()
            ).decode(),
        }

    async def view(self, tid):
        async with self.repo.engine.connect() as conn:
            return self._public(await self._get(conn, tid))

    async def recovery_status(self, tid):
        async with self.repo.engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            task = await self.control._task(conn, tid)
            return await native_recovery.status(conn, tid, await self._get(conn, tid), task)

    async def pending(self, after=None):
        async with self.repo.engine.connect() as conn:
            query = (
                select(s.task_runner_resources.c.task_id)
                .join(s.tasks, s.tasks.c.id == s.task_runner_resources.c.task_id)
                .where(
                    or_(s.task_runner_resources.c.state != "stopped", s.tasks.c.deleting.is_(True)),
                    or_(
                        s.task_runner_resources.c.next_check_at <= instant(),
                        s.tasks.c.deleting.is_(True),
                        s.tasks.c.status.in_(["closing", "stopped", "failed", "finished"]),
                    ),
                )
            )
            if after is not None:
                query = query.where(s.task_runner_resources.c.task_id > UUID(after))
            return [
                str(tid)
                for tid in (
                    await conn.execute(query.order_by(s.task_runner_resources.c.task_id).limit(20))
                ).scalars()
            ]

    @staticmethod
    def _merge_observation(record, incoming):
        old, config = record["observation"], record["config"]
        identity = {
            "task_id": str(record["task_id"]),
            "operation_id": record["operation_id"],
            "namespace": record["namespace"],
            "source_id": config["source_id"],
            "cluster_uid": config["cluster_uid"],
        }
        nested = {
            "journal_pvc",
            "workspace_pvc",
            "execd_secret",
            "runner_secret",
            "sandbox_host_secret",
            "vpn_egress_secret",
            "sandbox",
            "runner",
        }
        mutable = {
            "phase",
            "terminated",
            "host_terminated",
            "egress_terminated",
            "exit_code",
            "observation",
            "resource_version",
        }
        allowed = {
            "name",
            "uid",
            "id",
            "cr_uid",
            "pod_name",
            "pod_uid",
            "environment_revision",
            "container_id",
            "container_name",
            "host_container_id",
            "egress_container_id",
            "journal_writer_lock",
            "pod_ip",
        } | mutable
        if set(incoming) - (
            set(identity)
            | nested
            | {
                "namespace_uid",
                "resource_generation",
                "journal_subdir",
                "journal_lock_id",
                "journal_storage",
            }
        ):
            raise HTTPException(422, "Unexpected native resource evidence")
        generation = record["data"].get("resource_generation", 1)
        for key, expected in {
            "resource_generation": generation,
            "journal_subdir": f"epoch-{generation}" if config.get("journal_writer_lock") else None,
        }.items():
            if key in incoming and incoming[key] != expected:
                raise HTTPException(409, "Original native journal generation changed")
        if "journal_lock_id" in incoming and (
            not config.get("journal_writer_lock")
            or not isinstance(incoming["journal_lock_id"], str)
            or not re.fullmatch(r"[0-9]{1,20}:[0-9]{1,20}", incoming["journal_lock_id"])
        ):
            raise HTTPException(422, "Invalid native journal writer identity")
        if "journal_storage" in incoming:
            storage = incoming["journal_storage"]
            if (
                not config.get("journal_writer_lock")
                or not isinstance(storage, dict)
                or set(storage) != {"node", "node_uid", "journal_pv_uid", "workspace_pv_uid"}
                or not all(
                    isinstance(value, str) and 0 < len(value) <= 253 for value in storage.values()
                )
            ):
                raise HTTPException(422, "Invalid native journal storage identity")
        merged = dict(old)
        for name, expected in identity.items():
            if name in incoming and str(incoming[name]) != expected:
                raise HTTPException(409, "Native resource identity changed")
            merged[name] = expected
        for name, value in incoming.items():
            if name in identity:
                continue
            if name in nested:
                if not isinstance(value, dict) or set(value) - allowed:
                    raise HTTPException(422, "Invalid native resource projection")
                prior = old.get(name, {})
                for field, observed in value.items():
                    if (
                        field not in mutable
                        and prior.get(field) is not None
                        and prior[field] != observed
                    ):
                        raise HTTPException(409, "Original native resource UID changed")
                    if (
                        field in {"terminated", "egress_terminated"}
                        and prior.get(field) is True
                        and observed is not True
                    ):
                        raise HTTPException(409, "Terminated native resource cannot be revived")
                merged[name] = {**prior, **value}
            else:
                if old.get(name) is not None and old[name] != value:
                    raise HTTPException(409, "Original native resource UID changed")
                merged[name] = value
        history = record["data"].get("recovery_history", [])
        if history:
            original = history[-1]["resources"]
            for name, (scope, field) in native_recovery.identities_for(record).items():
                if (
                    scope in {"runner", "sandbox"}
                    and merged.get(scope, {}).get(field) == original[name]
                ):
                    raise HTTPException(
                        409, "Replacement resource reused an original execution identity"
                    )
        return merged

    @staticmethod
    def _resources_ready(observation):
        required = {
            "namespace_uid",
            "journal_pvc",
            "workspace_pvc",
            "sandbox",
            "runner",
            "runner_secret",
        }
        return (
            required <= observation.keys()
            and all(
                observation[key].get("uid")
                for key in ("journal_pvc", "workspace_pvc", "runner_secret")
            )
            and all(
                observation["sandbox"].get(key)
                for key in ("id", "cr_uid", "pod_uid", "container_id", "environment_revision")
            )
            and all(observation["runner"].get(key) for key in ("pod_uid", "container_id"))
            and observation["runner"].get("phase") == "Running"
            and (
                not observation.get("journal_subdir")
                or (
                    observation.get("journal_lock_id")
                    and observation.get("journal_storage")
                    and observation["runner"].get("journal_writer_lock") is True
                )
            )
            and all(
                observation[key].get("terminated") is not True
                and observation[key].get("host_terminated") is not True
                and observation[key].get("observation") != "unknown"
                and observation[key].get("phase") not in {"Failed", "Succeeded", "Unknown"}
                for key in ("runner", "sandbox")
            )
        )

    async def _lead(self, conn, tid, record):
        member = await row(conn, s.ctf_members, tid, "lead")
        data = record.get("data", {})
        generation = data.get("resource_generation", 1)
        turn_id = data.get("lead_turn_id") or identity_id(record["operation_id"], "lead-turn")
        if member["current_turn_id"] is not None:
            turn = await row(conn, s.ctf_turns, tid, member["current_turn_id"])
            if (
                turn["id"] != turn_id
                or turn["status"] != "running"
                or turn["generation"] != member["generation"]
            ):
                raise HTTPException(409, "Lead already belongs to another execution")
            return
        if (
            member["role"] != "lead"
            or member["lifecycle"] != "active"
            or member["run_state"] not in {"idle", "ready"}
            or member.get("pending_operation")
        ):
            raise HTTPException(409, "Lead is not ready for native bootstrap")
        messages = (
            (
                await conn.execute(
                    select(s.ctf_messages.c.id)
                    .where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.recipient_id == "lead",
                        s.ctf_messages.c.purpose == "execution",
                        s.ctf_messages.c.kind == "instruction",
                        s.ctf_messages.c.status == "queued",
                        s.ctf_messages.c.deferred.is_(False),
                    )
                    .order_by(s.ctf_messages.c.recipient_sequence)
                )
            )
            .scalars()
            .all()
        )
        if not messages:
            raise HTTPException(409, "Native Lead requires its original Task input")
        values = dict(
            id=turn_id,
            agent_id="lead",
            generation=member["generation"] + 1,
            runtime_instance="native:"
            + record["operation_id"]
            + (f":{generation}" if generation > 1 else ""),
            status="running",
            purpose="execution",
            assignment_message_ids=list(messages),
            explicit_reply_ids=[],
            checkpoint_revision=0,
            usage={},
            usage_requests=[],
            started_at=instant(),
        )
        await conn.execute(insert(s.ctf_turns).values(task_id=tid, **values))
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == "lead")
            .values(generation=values["generation"], current_turn_id=turn_id, run_state="running")
        )
        await self.ctf._emit(conn, tid, "turn.started", "system", values)

    async def tenant(self, key):
        async with self.repo.engine.connect() as conn:
            record = (
                (
                    await conn.execute(
                        select(s.task_runner_resources).where(
                            s.task_runner_resources.c.tenant_key_hash
                            == hashlib.sha256(key.encode()).hexdigest(),
                            s.task_runner_resources.c.config.is_not(None),
                            s.task_runner_resources.c.controller_id.is_not(None),
                            s.task_runner_resources.c.state.not_in(["exited", "stopped"]),
                        )
                    )
                )
                .mappings()
                .first()
            )
        if record is None:
            raise HTTPException(401, "Unknown Sandbox tenant")
        return {"namespace": record["namespace"], "ttl": 0}

    async def act(self, tid, raw):
        body, _ = decode(NativeRunnerAction, raw, 262144)
        if body.action == "vpn_material":
            async with self.repo.engine.begin() as conn:
                record, _ = await self._checked(conn, tid, body, live=True)
                if not self._resources_ready(record["observation"]) or not record[
                    "observation"
                ].get("vpn_egress_secret", {}).get("uid"):
                    raise HTTPException(409, "Original VPN resources are not ready")
                return await self.vpn.material(conn, tid, record)
        if body.action in {"sandbox_host_material", "sandbox_host_ready"}:
            async with self.repo.engine.begin() as conn:
                record, _ = await self._checked(conn, tid, body, live=True)
                if not self._resources_ready(record["observation"]):
                    raise HTTPException(409, "Original Task resources are not ready")
                if not record["observation"].get("sandbox_host_secret", {}).get(
                    "uid"
                ) or not record["observation"]["runner"].get("host_container_id"):
                    raise HTTPException(
                        409, "Original Sandbox host container and mount are not identified"
                    )
                if body.action == "sandbox_host_material":
                    return await self.gateway.sandbox.host_material(
                        conn, tid, record, body.proof.get("context", {})
                    )
                require_inputs_confirmed(record)
                host = record["sandbox_host"]
                if not host or body.proof.get(
                    "context_digest"
                ) != self.gateway.sandbox.context_digest(host["context"]):
                    raise HTTPException(409, "Original Sandbox host context changed")
                await conn.execute(
                    update(s.task_runner_resources)
                    .where(s.task_runner_resources.c.task_id == tid)
                    .values(sandbox_host={**host, "ready": True})
                )
                await self._set(conn, tid, data={**record["data"], "sandbox_host_ready": True})
                return self._public(await self._get(conn, tid))
        if body.action.startswith("recovery_"):
            return await self.recovery.act(tid, body)
        if body.action.startswith("retire_"):
            return await self.retirement.act(tid, body)
        if body.action in {"material", "renew"}:
            return await self._material(tid, body, renew=body.action == "renew")
        if body.action == "activate":
            return await self._activate(tid, body)
        if body.action == "stop":
            return await self._stop(tid, body)
        if body.action == "cleanup":
            return await self._cleanup(tid, body)
        async with self.repo.engine.begin() as conn:
            record, task = await self._checked(conn, tid, body)
            stopping = self._stopping(task, record)
            if body.action == "sandbox_create":
                allowed = (
                    not stopping
                    and not record["data"].get("recovery")
                    and not record["sandbox_create_claimed"]
                )
                if allowed:
                    await self._set(conn, tid, sandbox_create_claimed=True, state="preparing")
                return {"create_allowed": allowed, "operation_id": record["operation_id"]}
            if body.action == "observation":
                if record["state"] in {"exited", "stopped"}:
                    return self._public(record)
                observed = self._merge_observation(record, body.observation)
                ready = self._resources_ready(observed)
                if vpn_enabled(record):
                    ready = ready and bool(
                        observed.get("runner", {}).get("egress_container_id")
                        and observed.get("runner", {}).get("egress_terminated") is not True
                        and observed.get("sandbox", {}).get("pod_ip")
                        and observed.get("vpn_egress_secret", {}).get("uid")
                    )
                data = dict(record["data"])
                if not stopping and not data.get("recovery") and data.get("credentials_issued"):
                    reason = (
                        await native_recovery.original_owner_issue(conn, tid, record, instant())
                        if ready
                        else "native_resource_unknown"
                    )
                    if reason:
                        data = await native_recovery.capture(
                            self.repo, conn, tid, record, task, reason, instant()
                        )
                if ready and not stopping and not data.get("recovery"):
                    await self._lead(conn, tid, record)
                    data.setdefault(
                        "lead_turn_id", identity_id(record["operation_id"], "lead-turn")
                    )
                state = "stopping" if stopping else record["state"]
                if not stopping and data.get("recovery"):
                    state = "frozen"
                elif not ready and data.get("credentials_issued"):
                    state = "unknown"
                    if data.get("owner_epoch"):
                        await conn.execute(
                            update(s.runner_owners)
                            .where(
                                s.runner_owners.c.task_id == tid,
                                s.runner_owners.c.epoch == data["owner_epoch"],
                            )
                            .values(current=False, revoked=True)
                        )
                if (
                    not stopping
                    and not data.get("credentials_issued")
                    and record["state"] in {"queued", "preparing", "unknown"}
                ):
                    state = "resources_ready" if ready else "preparing"
                await self._set(
                    conn,
                    tid,
                    observation=observed,
                    data=data,
                    state=state,
                    desired="stopped" if stopping else "active",
                    last_error=(
                        data["recovery"]["reason"]
                        if state == "frozen"
                        else "native_resource_unknown"
                        if state == "unknown"
                        else None
                    ),
                )
            elif body.action == "error":
                if record["state"] == "stopped":
                    retirement = record["data"].get("retirement")
                    if retirement and retirement.get("state") != "retired":
                        await self._set(
                            conn,
                            tid,
                            data={
                                **record["data"],
                                "retirement": {
                                    **retirement,
                                    "state": "unknown",
                                    "last_error": body.error_code,
                                },
                            },
                            next_check_at=instant() + timedelta(seconds=20),
                        )
                        return self._public(await self._get(conn, tid))
                    return self._public(record)
                data = dict(record["data"])
                if not stopping and not data.get("recovery") and data.get("credentials_issued"):
                    reason = (
                        "native_resource_unknown"
                        if body.proof.get("revoke_owner") is True
                        else await native_recovery.original_owner_issue(
                            conn, tid, record, instant()
                        )
                    )
                    if reason:
                        data = await native_recovery.capture(
                            self.repo, conn, tid, record, task, reason, instant()
                        )
                state = "exited" if data.get("exit_confirmed") else "unknown"
                if not stopping and data.get("recovery"):
                    state = "frozen"
                await self._set(
                    conn,
                    tid,
                    state=state,
                    data=data,
                    last_error=data["recovery"]["reason"]
                    if state == "frozen"
                    else body.error_code or "native_bootstrap_unavailable",
                    next_check_at=instant() + timedelta(seconds=20),
                )
                if body.proof.get("revoke_owner") is True and record["data"].get("owner_epoch"):
                    await conn.execute(
                        update(s.runner_owners)
                        .where(
                            s.runner_owners.c.task_id == tid,
                            s.runner_owners.c.epoch == record["data"]["owner_epoch"],
                        )
                        .values(current=False, revoked=True)
                    )
            else:
                raise HTTPException(422, "Unsupported native bootstrap action")
            return self._public(await self._get(conn, tid))

    async def _stop(self, tid, body):
        async with self.repo.engine.begin() as conn:
            record, task = await self._checked(conn, tid, body)
            if record["state"] in {"exited", "stopped"}:
                return self._public(record)
            data = record["data"]
            if opted_in(record) and not data.get("traffic_preseal_until"):
                requested_at = (task.get("ctf_conclusion") or {}).get("requested_at")
                first_stop = datetime.fromisoformat(requested_at) if requested_at else instant()
                data = {
                    **data,
                    "traffic_preseal_until": min(
                        first_stop + timedelta(seconds=20), record["hard_deadline"]
                    ).isoformat(),
                }
            await self._set(conn, tid, desired="stopped", state="stopping", data=data)
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(ctf_control={**task["ctf_control"], "native_stop_requested": True})
            )
        if task["status"] not in {"stopped", "failed", "finished"} and task["ctf_control"].get(
            "phase"
        ) not in {"closing", "closed"}:
            expired = instant() >= record["hard_deadline"]
            await self.ctf.request_finish(
                tid,
                "service",
                identity_id(record["operation_id"], "stop"),
                {
                    "end_reason": "budget_exhausted" if expired else "user_stop",
                    "summary": "Native Task hard deadline reached"
                    if expired
                    else "Stopped by the native resource controller",
                },
            )
        async with self.repo.engine.begin() as conn:
            record, _ = await self._checked(conn, tid, body)
            if record["data"].get("owner_epoch"):
                owner = (
                    (
                        await conn.execute(
                            select(s.runner_owners).where(
                                s.runner_owners.c.task_id == tid,
                                s.runner_owners.c.epoch == record["data"]["owner_epoch"],
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    owner
                    and owner["current"]
                    and not owner["revoked"]
                    and owner["expires_at"] > instant()
                    and owner["hard_deadline"] > instant()
                ):
                    payload = runner_json_text({"kind": "shutdown"})
                    await self.control._enqueue(
                        conn,
                        tid,
                        dict(owner),
                        identity_id(record["operation_id"], "shutdown"),
                        payload,
                        runner_payload_digest(payload),
                    )
            return self._public(record)

    async def _cleanup(self, tid, body):
        async with self.repo.engine.begin() as conn:
            record, task = await self._checked(conn, tid, body)
            if record["state"] == "stopped":
                return self._public(record)
            if record["desired"] != "stopped":
                raise HTTPException(409, "Native cleanup requires persisted stop intent")
            if record["state"] != "exited":
                expected = runner_payload_digest(runner_json_text(record["observation"]))
                if body.proof.get("observation_digest") != expected:
                    raise HTTPException(409, "Native cleanup observation identity changed")
                never_dispatched = (
                    body.proof.get("not_started") is True
                    and record["sandbox_create_claimed"] is False
                    and not record["data"].get("owner_epoch")
                    and not record["observation"].get("sandbox")
                    and not record["observation"].get("runner")
                )
                positive = body.proof.get("pvcs_retained") is True and (
                    never_dispatched
                    or all(
                        body.proof.get(key) is True
                        for key in ("runner_terminated", "sandbox_terminated")
                    )
                )
                verifier = record["data"].get("recovery", {}).get("verifier")
                if not positive or (verifier and verifier.get("terminated") is not True):
                    await self._set(
                        conn, tid, state="unknown", last_error="native_exit_unconfirmed"
                    )
                    return self._public(await self._get(conn, tid))
                if not never_dispatched and (
                    not self._resources_identified(record["observation"])
                    or (
                        record["config"].get("runner_profile") == "node-sandbox-host-v1"
                        and not record["observation"].get("runner", {}).get("host_container_id")
                    )
                ):
                    raise HTTPException(
                        409, "Original native container identities are not registered"
                    )
                if record["data"].get("owner_epoch"):
                    await conn.execute(
                        update(s.runner_owners)
                        .where(
                            s.runner_owners.c.task_id == tid,
                            s.runner_owners.c.epoch == record["data"]["owner_epoch"],
                        )
                        .values(current=False, revoked=True)
                    )
                data = {
                    **await vpn_exit_data(conn, record, body.proof, never_started=never_dispatched),
                    "exit_confirmed": True,
                    "exit_reason": "not_dispatched" if never_dispatched else "terminated",
                    **({"network_scopes": lost_captures(record)} if opted_in(record) else {}),
                }
                await self._set(conn, tid, state="exited", data=data, last_error=None)
                if record["sandbox_host"]:
                    await conn.execute(
                        update(s.sandbox_command_records)
                        .where(
                            s.sandbox_command_records.c.task_id == tid,
                            s.sandbox_command_records.c.state.in_(["dispatched", "running"]),
                        )
                        .values(state="unknown")
                    )
                    await conn.execute(
                        update(s.runner_calls)
                        .where(
                            s.runner_calls.c.task_id == tid,
                            s.runner_calls.c.provider_key == "sandbox",
                            s.runner_calls.c.state == "dispatched",
                        )
                        .values(state="unknown")
                    )
                member = await row(conn, s.ctf_members, tid, "lead")
                if data.get("lead_turn_id") and member["current_turn_id"] == data["lead_turn_id"]:
                    turn = await row(conn, s.ctf_turns, tid, member["current_turn_id"])
                    if turn["status"] == "running":
                        await self.ctf._settle(conn, tid, member, turn, "stopped", "")
                await self.helpers.finish_after_task_exit(conn, tid)
                await self.workers.finish_after_task_exit(conn, tid)
        if task["ctf_control"].get("phase") == "closing":
            await self.ctf.finalize_close(tid, True)
        async with self.repo.engine.begin() as conn:
            current, task = await self._checked(conn, tid, body)
            if task["status"] not in {"stopped", "failed", "finished"}:
                raise HTTPException(409, "Native Task finalization is pending")
            await self._set(conn, tid, state="stopped", last_error=None)
            return self._public(await self._get(conn, tid))

    @staticmethod
    def _resources_identified(observation):
        return bool(observation.get("namespace_uid")) and all(
            observation.get(key, {}).get("pod_uid") and observation.get(key, {}).get("container_id")
            for key in ("runner", "sandbox")
        )

    @staticmethod
    def _held(task):
        return bool(
            task["ctf_control"].get("native_budget_hold")
            or task["ctf_control"].get("failure_hold")
            or native_recovery_frozen(task)
        )

    async def _control_material(self, tid, body):
        async with self.repo.engine.begin() as conn:
            record, _ = await self._checked(conn, tid, body)
            data = record["data"]
            runner = record["observation"].get("runner", {})
            if (
                record["state"] in {"exited", "stopped"}
                or instant() >= record["hard_deadline"]
                or not data.get("bootstrap")
                or not data.get("owner_epoch")
                or runner.get("terminated") is True
                or runner.get("observation") == "unknown"
                or not runner.get("container_id")
            ):
                raise HTTPException(409, "Original live Runner control scope is unavailable")
            owner = await self.control._current_owner(conn, tid)
            if (
                owner["epoch"] != data["owner_epoch"]
                or owner["pod_uid"] != runner["pod_uid"]
                or owner["hard_deadline"] > record["hard_deadline"]
            ):
                raise HTTPException(409, "Original Runner control owner changed")
            expires = min(
                owner["hard_deadline"],
                record["hard_deadline"],
                instant() + timedelta(seconds=OWNER_SECONDS),
            )
            await conn.execute(
                update(s.runner_owners)
                .where(s.runner_owners.c.task_id == tid, s.runner_owners.c.epoch == owner["epoch"])
                .values(expires_at=expires)
            )
            owner = {**owner, "expires_at": expires}
            token_expiry = runner_credential_expiry(owner)
            control = {
                "audience": "runner-control",
                "task_id": str(tid),
                "expires_at": token_expiry,
                "token": issue_runner_control_token(self.settings, owner, expires_at=token_expiry),
            }
            await self._set(
                conn,
                tid,
                data={**data, "last_rotation_at": instant().isoformat()},
                state="stopping" if record["desired"] == "stopped" else "held",
                next_check_at=instant() + timedelta(seconds=20),
            )
            return {
                "bootstrap": data["bootstrap"],
                "credentials": {"control": control},
                "control_only": True,
                "status": self._public(await self._get(conn, tid)),
            }

    async def _material(self, tid, body, *, renew):
        owner = None
        async with self.repo.engine.begin() as conn:
            record, task = await self._checked(conn, tid, body, live=True)
            require_inputs_confirmed(record)
            if vpn_enabled(record) and (
                not vpn_state(record).get("material")
                or record["observation"].get("runner", {}).get("egress_terminated") is True
            ):
                raise HTTPException(409, "Original VPN egress control is not identified")
            if not self._resources_ready(record["observation"]) or (
                record["config"].get("journal_writer_lock")
                and not record["observation"].get("journal_subdir")
            ):
                raise HTTPException(409, "Native environment and original Runner must be ready")
            if record["config"].get("runner_profile") == "node-sandbox-host-v1" and not record[
                "sandbox_host"
            ].get("ready"):
                raise HTTPException(409, "Original Sandbox host is not ready")
            if (
                record["observation"]["sandbox"]["environment_revision"]
                != record["config"]["environment_revision"]
            ):
                raise HTTPException(409, "Native environment revision changed")
            data = dict(record["data"])
            if data.get("credentials_issued") and not data.get("owner_epoch"):
                raise HTTPException(409, "Original native owner identity is unconfirmed")
            held = self._held(task) or native_pause_state(task) in {"draining", "paused"}
            if not held and data.get("worker_spec") and data.get("bootstrap"):
                owner = await self.control._current_owner(conn, tid)
                try:
                    await self.control._binding(conn, tid, data["binding_id"], owner, live=True)
                except HTTPException as error:
                    if error.status_code not in {403, 404, 409}:
                        raise
                    held = True
            if not held:
                continuation = data.get("continuation")
                previous_epoch = (
                    data["recovery_history"][-1]["original_epoch"] if continuation else 0
                )
                generation = data.get("resource_generation", 1)
                if not data.get("owner_epoch"):
                    owner = await self.control._register_owner(
                        conn,
                        tid,
                        runner_json_text(
                            {
                                "request_id": identity_id(
                                    record["operation_id"],
                                    "owner" if generation == 1 else f"owner:{generation}",
                                ),
                                "runner_id": identity_id(
                                    record["operation_id"],
                                    "runner" if generation == 1 else f"runner:{generation}",
                                ),
                                "pod_uid": record["observation"]["runner"]["pod_uid"],
                                "expected_epoch": previous_epoch,
                                "hard_deadline": record["hard_deadline"].isoformat(),
                            }
                        ).encode(),
                    )
                    data["owner_epoch"] = owner["epoch"]
                    await self._set(conn, tid, data=data)
                else:
                    owner = await self.control._current_owner(conn, tid)
                if (
                    owner["epoch"] != data["owner_epoch"]
                    or owner["pod_uid"] != record["observation"]["runner"]["pod_uid"]
                    or owner["hard_deadline"] > record["hard_deadline"]
                ):
                    raise HTTPException(409, "Original native owner scope changed")
                owner = await self.control._update_owner(conn, tid, owner["epoch"])
                if continuation and not data.get("binding_id"):
                    budget = await self.gateway.native._budget(conn, tid)
                    if not self.recovery._budget_available(budget):
                        raise HTTPException(409, "Original native budget is unconfirmed")
                    binding = await self.pause_control.register_continuation(
                        conn, tid, owner, continuation
                    )
                    data["binding_id"] = binding["id"]
                    await self._set(conn, tid, data=data)
        if held:
            return await self._control_material(tid, body)
        assert owner is not None
        if not data.get("binding_id"):
            await self.gateway.native.prepare(
                tid,
                runner_json_text(
                    {
                        "expected_epoch": owner["epoch"],
                        "accounting_source_id": self.settings.native_gateway_accounting_source_id,
                        "selected_models": [record["request"]["model_alias"]],
                        "exchange_rate": record["request"]["exchange_rate"],
                    }
                ).encode(),
            )
            await self.gateway.native.publish(tid)
            selected = await self.gateway.native.selection(tid, record["request"]["model_alias"])
            document = await ProfileStore(self.repo.engine).get(
                task["agent_profile"], task["agent_profile_version"]
            )
            profile = ctf_profile(document)
            model = effective_role_model(profile, "lead", selected["sdk_model"])
            worker_spec = None
            if native_dispatch_enabled(task):
                worker_tools = [
                    *task["ctf_control"]["business_tools"]["roles"]["teammate"],
                    *SANDBOX_TOOLS,
                ]
                worker_spec = {
                    "model": effective_role_model(profile, "teammate", selected["sdk_model"]),
                    "allowed_tools": worker_tools,
                    **(
                        {"helper_foreground": True}
                        if record["request"].get("helper_foreground")
                        else {}
                    ),
                    **(
                        {"helper_background": True}
                        if record["request"].get("helper_background")
                        else {}
                    ),
                    "instructions": {
                        f"member-{n}": render_ctf_prompt(
                            profile,
                            "teammate",
                            member_name=f"Worker {n}",
                            member_id=f"member-{n}",
                            task_id=str(tid),
                            agent_workspace=f"/workspace/agents/member-{n}",
                            shared_workspace="/workspace/shared",
                            allowed_tool_names=", ".join(worker_tools),
                        )
                        + "\nUse only your current challenge Assignment. "
                        "Record useful results as facts on its board."
                        for n in range(1, task["ctf_options"]["max_teammates"] + 1)
                    },
                }
            tools = task["ctf_control"]["business_tools"]["roles"]["lead"]
            if record["config"].get("runner_profile") == "node-sandbox-host-v1":
                async with self.repo.engine.connect() as conn:
                    await self.gateway.sandbox.require_ready(conn, tid)
                tools = [*tools, *SANDBOX_TOOLS]
            instructions = render_ctf_prompt(
                profile,
                "lead",
                member_name="Lead",
                member_id="lead",
                task_id=str(tid),
                agent_workspace="/workspace/agents/agent-1",
                shared_workspace="/workspace/shared",
                allowed_tool_names=", ".join(tools),
            )
            instructions += (
                "\nUse only the published tools. "
                "Commands and files use the shared low-privilege Task workspace."
            )
            binding = await self.control.register_binding(
                tid,
                runner_json_text(
                    {
                        "request_id": identity_id(record["operation_id"], "lead-binding"),
                        "expected_epoch": owner["epoch"],
                        "agent_id": "lead",
                        "model": model,
                        "model_credential_ref": "native-model-lead",
                        "mcp_credential_ref": "native-mcp-lead",
                        "allowed_tools": tools,
                        "instructions": instructions,
                        "profile_revision": task["agent_profile_version"],
                        "prompt_revision": 1,
                    }
                ).encode(),
            )
            async with self.repo.engine.begin() as conn:
                current, _ = await self._checked(conn, tid, body, live=True)
                data = {
                    **current["data"],
                    "owner_epoch": owner["epoch"],
                    "binding_id": binding["id"],
                    **(
                        {"worker_spec": worker_spec, "worker_binding_ids": []}
                        if worker_spec
                        else {}
                    ),
                }
                await self._set(conn, tid, data=data)
        else:
            binding = None
        scoped = await self.gateway.credentials(
            tid, data["binding_id"], runner_json_text({"expected_epoch": owner["epoch"]}).encode()
        )
        async with self.repo.engine.begin() as conn:
            current, task = await self._checked(conn, tid, body, live=True)
            if self._held(task) or native_pause_state(task) in {"draining", "paused"}:
                raise HTTPException(409, "Task admission is held")
            owner = await self.control._current_owner(conn, tid)
            if (
                owner["epoch"] != current["data"]["owner_epoch"]
                or owner["pod_uid"] != current["observation"]["runner"]["pod_uid"]
            ):
                raise HTTPException(409, "Native credential publication owner changed")
            lead = await self.control._binding(
                conn, tid, current["data"]["binding_id"], owner, live=True
            )
            lead_spec = json.loads(lead["payload_json"])["spec"]
            bootstrap = {
                "protocol": PROTOCOL,
                "fence": fence(owner),
                "control_url": current["config"]["runner_blackboard_url"].rstrip("/")
                + f"/api/tasks/{tid}/runner-control",
                "model_proxy_url": current["config"]["runner_model_proxy_url"].rstrip("/"),
                "mcp_gateway_url": scoped["mcp_gateway_url"],
                "control_credential_ref": "control",
                "credential_file": "/var/run/bbx/credentials.json",
                "journal_root": "/journal",
                "mcp_metadata_timeout_ms": 15000,
                **(
                    {"helper_foreground": True}
                    if current["request"].get("helper_foreground")
                    else {}
                ),
                **(
                    {"helper_background": True}
                    if current["request"].get("helper_background")
                    else {}
                ),
            }
            if (
                current["data"].get("bootstrap") is not None
                and current["data"]["bootstrap"] != bootstrap
            ):
                raise HTTPException(409, "Frozen native Runner bootstrap changed")
            expires = runner_credential_expiry(owner)
            credentials = {
                "control": {
                    "audience": "runner-control",
                    "task_id": str(tid),
                    "expires_at": expires,
                    "token": issue_runner_control_token(self.settings, owner, expires_at=expires),
                },
                lead_spec["model_credential_ref"]: scoped["model"],
                lead_spec["mcp_credential_ref"]: scoped["mcp"],
            }
            retained = []
            for assignment in task["ctf_control"].get("native_worker_active", {}).values():
                bid = assignment["binding_id"]
                if assignment["epoch"] != owner["epoch"]:
                    raise HTTPException(409, "Original native Worker credential owner changed")
                try:
                    await self.control._binding(conn, tid, bid, owner, live=True)
                except HTTPException as error:
                    if error.status_code not in {403, 404, 409}:
                        raise
                    retained.append(bid)
                    continue
                worker = issue_gateway_credentials(self.settings, owner, bid, "teammate")
                credentials["native-model-" + bid] = worker["model"]
                credentials["native-mcp-" + bid] = worker["mcp"]
            data = {
                **current["data"],
                "bootstrap": bootstrap,
                "credentials_issued": True,
                "last_rotation_at": instant().isoformat(),
            }
            await self._set(
                conn,
                tid,
                data=data,
                state="ready" if data.get("activated") else "runner_starting",
                last_error=None,
                next_check_at=instant() + timedelta(seconds=20),
            )
            return {
                "bootstrap": bootstrap,
                "credentials": credentials,
                "retain_binding_ids": retained,
                "status": self._public(await self._get(conn, tid)),
            }

    async def _activate(self, tid, body):
        budget_proof = None
        if body.resource_generation > 1:
            async with self.repo.engine.begin() as conn:
                record, task = await self._checked(conn, tid, body, live=True)
                require_inputs_confirmed(record)
                binding_id = record["data"].get("binding_id")
                if task["ctf_control"].get("native_recovery_rebuilding") and binding_id:
                    prepared = await row(conn, s.runner_bindings, tid, binding_id)
                    if prepared["status"] == "prepared" and prepared["cursor"]:
                        budget = await self.gateway.native._budget(conn, tid)
                        if self.recovery._budget_available(budget):
                            budget_proof = (budget,)
            if budget_proof:
                try:
                    budget_proof = (
                        *budget_proof,
                        *await self.recovery.read_budget(budget_proof[0]),
                    )
                except HTTPException:
                    budget_proof = None
        async with self.repo.engine.begin() as conn:
            record, task = await self._checked(conn, tid, body, live=True)
            require_inputs_confirmed(record)
            if native_pause_state(task) in {"draining", "paused"}:
                if record["data"].get("worker_spec"):
                    owner = await self.control._current_owner(conn, tid)
                    await self.helpers.reconcile(conn, tid, task, owner)
                    task = await row(conn, s.tasks, tid)
                await self.pause_control.progress(conn, tid, record, task)
                return {**self._public(await self._get(conn, tid)), "control_only": True}
            data = dict(record["data"])
            held = self._held(task)
            if held and not data.get("worker_spec"):
                return {**self._public(record), "control_only": True}
            owner = await self.control._current_owner(conn, tid)
            if (
                owner["epoch"] != data.get("owner_epoch")
                or owner["pod_uid"] != record["observation"]["runner"]["pod_uid"]
            ):
                raise HTTPException(409, "Original native Runner owner changed")
            if held:
                workers = await self.workers.reconcile(
                    conn, tid, task, record, owner, allow_new=False
                )
                if workers != data.get("worker_binding_ids"):
                    await self._set(conn, tid, data={**data, "worker_binding_ids": workers})
                return {**self._public(await self._get(conn, tid)), "control_only": True}
            try:
                binding = await self.control._binding(
                    conn, tid, data["binding_id"], owner, live=True
                )
            except HTTPException as error:
                if not data.get("worker_spec") or error.status_code not in {403, 404, 409}:
                    raise
                workers = await self.workers.reconcile(
                    conn, tid, task, record, owner, allow_new=False
                )
                if workers != data.get("worker_binding_ids"):
                    await self._set(conn, tid, data={**data, "worker_binding_ids": workers})
                return {**self._public(await self._get(conn, tid)), "control_only": True}
            if binding["status"] != "prepared" or not binding["sdk_session_id"]:
                return self._public(record)
            first = not data.get("activated")
            if first:
                control = {**task["ctf_control"], "phase": "running"}
                if native_pause_state(task) == "resuming":
                    if vpn_enabled(record) and vpn_state(record).get("credential_reserved"):
                        try:
                            vpn_solver_deadline(record)
                        except HTTPException:
                            if vpn_state(record).get("state") in {
                                "failed",
                                "unknown",
                                "disconnected",
                            }:
                                await conn.execute(
                                    update(s.tasks)
                                    .where(s.tasks.c.id == tid)
                                    .values(
                                        ctf_control={
                                            **task["ctf_control"],
                                            "native_pause": {
                                                **task["ctf_control"]["native_pause"],
                                                "blocker": "original_vpn_lost_stop_task",
                                            },
                                        }
                                    )
                                )
                            return self._public(record)
                    control["native_pause"] = {**control["native_pause"], "state": "resumed"}
                    data["pause_state"] = "resumed"
                if recovery_id := control.pop("native_recovery_rebuilding", None):
                    if not budget_proof or not await self.recovery.matching_budget(
                        conn, tid, budget_proof[0], budget_proof[2], budget_proof[3]
                    ):
                        if not data.get("recovery_activation_blocker"):
                            await self._set(
                                conn,
                                tid,
                                data={
                                    **data,
                                    "recovery_activation_blocker": "original_budget_unconfirmed",
                                },
                            )
                        return self._public(await self._get(conn, tid))
                    data.pop("recovery_activation_blocker", None)
                    await self.recovery.record_usage(conn, tid, budget_proof[1], budget_proof[2])
                    history = data["recovery_history"]
                    if (
                        history[-1]["incident_id"] != recovery_id
                        or history[-1]["replacement_epoch"] != owner["epoch"]
                        or not binding["cursor"]
                    ):
                        raise HTTPException(409, "Replacement SDK journal is not confirmed")
                    data["recovery_history"] = [
                        *history[:-1],
                        {**history[-1], "replacement_activated": True},
                    ]
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(
                        status="running",
                        ctf_control=control,
                        started_at=task["started_at"] or instant(),
                        run_started=True,
                    )
                )
                data.update(activated=True, sdk_session_id=binding["sdk_session_id"])
            if data.get("worker_spec"):
                task = await row(conn, s.tasks, tid)
                budget = await self.gateway.native._budget(conn, tid)
                allow_new = (
                    budget["publication_state"] == "ready"
                    and not budget["hold_reason"]
                    and task["status"] == "running"
                    and task["ctf_control"]["phase"] == "running"
                    and self._resources_ready(record["observation"])
                    and record["sandbox_host"].get("ready")
                )
                data["worker_binding_ids"] = await self.workers.reconcile(
                    conn, tid, task, record, owner, allow_new=allow_new
                )
            if first or data.get("worker_spec"):
                owner = await self.control._current_owner(conn, tid)
                messages = (
                    (
                        await conn.execute(
                            select(s.ctf_messages)
                            .where(
                                s.ctf_messages.c.task_id == tid,
                                s.ctf_messages.c.recipient_id == "lead",
                                s.ctf_messages.c.purpose == "execution",
                                s.ctf_messages.c.status == "queued",
                                s.ctf_messages.c.deferred.is_(False),
                            )
                            .order_by(s.ctf_messages.c.recipient_sequence)
                            .limit(100)
                        )
                    )
                    .mappings()
                    .all()
                )
                for message in messages:
                    payload = runner_json_text(
                        {
                            "kind": "input",
                            "binding_id": binding["id"],
                            "input_id": message["id"],
                            "content": message["body"],
                            "mode": "follow_up",
                        }
                    )
                    await self.control._enqueue(
                        conn,
                        tid,
                        owner,
                        identity_id(record["operation_id"], "input:" + message["id"]),
                        payload,
                        runner_payload_digest(payload),
                    )
            if data != record["data"]:
                await self._set(conn, tid, data=data, state="ready")
            return self._public(await self._get(conn, tid))


def service(request):
    value = request.app.state.native_runner_bootstrap
    if value is None:
        raise HTTPException(503, "Native Runner bootstrap is unavailable")
    return value


PREFIX = "/api/tasks/{task_id}/ctf/runtime/runner-bootstrap"


@router.post(PREFIX)
async def request_bootstrap(
    request: Request, response: Response, task_id: UUID, body: NativeRunnerLaunch
):
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await service(request).launch(task_id, await request.body())


@router.get(PREFIX)
async def bootstrap_status(request: Request, response: Response, task_id: UUID):
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await service(request).view(task_id)


def _user_pause(request: Request, response: Response):
    if require_user_or_service(request).kind != "user":
        raise HTTPException(
            403, "Native pause, continuation and budget changes require a user token"
        )
    response.headers["Cache-Control"] = "no-store"
    return service(request).pause_control


@router.get(PREFIX + "/pause", response_model=NativePauseStatus)
async def user_pause_status(request: Request, response: Response, task_id: UUID):
    return await _user_pause(request, response).status(task_id)


@router.post(PREFIX + "/pause", response_model=NativePauseStatus)
async def user_pause(request: Request, response: Response, task_id: UUID, body: NativePauseRequest):
    return await _user_pause(request, response).pause(task_id, body)


@router.post(PREFIX + "/resume", response_model=NativePauseStatus)
async def user_resume(
    request: Request, response: Response, task_id: UUID, body: NativeResumeRequest
):
    return await _user_pause(request, response).resume(task_id, body)


@router.post(PREFIX + "/budget-increase", response_model=NativeBudgetIncreaseStatus)
async def user_budget_increase(
    request: Request, response: Response, task_id: UUID, body: NativeBudgetIncreaseRequest
):
    return await increase_budget(_user_pause(request, response), task_id, body)


@router.get(PREFIX + "/recovery", response_model=NativeRecoveryStatus)
async def user_recovery_status(request: Request, response: Response, task_id: UUID):
    if require_user_or_service(request).kind != "user":
        raise HTTPException(403, "Native recovery status requires a user token")
    response.headers["Cache-Control"] = "no-store"
    return await service(request).recovery_status(task_id)


@router.post(PREFIX + "/recovery", response_model=NativeRecoveryIntent)
async def user_recovery(
    request: Request, response: Response, task_id: UUID, body: NativeRecoveryRequest
):
    if require_user_or_service(request).kind != "user":
        raise HTTPException(403, "Native recovery requires a user token")
    response.headers["Cache-Control"] = "no-store"
    return await service(request).recovery.request(task_id, body)


@router.post(PREFIX + "/claim")
async def claim_bootstrap(
    request: Request, response: Response, task_id: UUID, body: NativeRunnerClaim
):
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await service(request).claim(task_id, await request.body())


@router.post(PREFIX + "/actions")
async def bootstrap_action(
    request: Request, response: Response, task_id: UUID, body: NativeRunnerAction
):
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return await service(request).act(task_id, await request.body())


@router.get("/api/native-runner/bootstraps")
async def pending_bootstraps(request: Request, response: Response, after: UUID | None = None):
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return {"task_ids": await service(request).pending(str(after) if after is not None else None)}


@router.get("/api/opensandbox/tenants")
async def sandbox_tenant(request: Request, response: Response):
    expected = request.app.state.settings.sandbox_tenant_resolver_token.get_secret_value()
    if not expected:
        raise HTTPException(503, "Sandbox tenant resolver is unavailable")
    values = request.headers.getlist("x-bbx-sandbox-resolver")
    keys = request.headers.getlist("open-sandbox-api-key")
    if (
        len(values) != 1
        or not hmac.compare_digest(values[0].encode(), expected.encode())
        or len(keys) != 1
        or not 1 <= len(keys[0]) <= 512
    ):
        raise HTTPException(401, "Sandbox resolver identity is required")
    response.headers["Cache-Control"] = "no-store"
    return await service(request).tenant(keys[0])
