"""One original, explicitly configured VPN session and short forwarding authority."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from bbx_contracts.runner import runner_json_text
from bbx_contracts.runner_bootstrap import management_document_digest, native_resume_pending
from bbx_contracts.runner_vpn import (
    VPN_CONFIGURE,
    VPN_DISCONNECT,
    VPN_PROFILE,
    VPN_STATUS,
    NativeVpnConfigure,
    NativeVpnDisconnect,
    NativeVpnLinkReport,
    NativeVpnObservation,
    NativeVpnOperation,
    NativeVpnPreflight,
    NativeVpnScope,
    NativeVpnStatus,
)
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select, text

from bbx_blackboard.store import schema as s

# One deployment-wide slot, even if an administrator changes the cert or publication revision.
VPN_SLOT_LOCK = 0x42425856504E
OBSERVED_MS = 18000
LEASE_MS = 15000
KERNEL_MS = 10000
VPN_RECORD = "vpn"

router = APIRouter(prefix="/api/tasks/{task_id}", tags=["runner"])


def millis():
    return int(datetime.now(UTC).timestamp() * 1000)


def vpn_enabled(record):
    return record["request"].get("publication", {}).get("vpn_profile") == VPN_PROFILE


def vpn_state(record):
    return record["data"].get(VPN_RECORD, {})


def vpn_applied(record, mode):
    state = vpn_state(record)
    applied = state.get("applied", {})
    if applied.get("mode") != mode or applied.get("applied_until_ms", 0) <= millis():
        return None
    return applied


def vpn_solver_deadline(record):
    if not vpn_enabled(record):
        return None
    applied = vpn_applied(record, "solver")
    if vpn_state(record).get("state") != "ready" or applied is None:
        raise HTTPException(409, "Original Task VPN forwarding is not confirmed")
    return applied["applied_until_ms"]


def vpn_retired(record):
    return vpn_enabled(record) and vpn_state(record).get("state") in {
        "disconnecting",
        "disconnected",
        "failed",
        "unknown",
    }


def vpn_result(value, *, error=False):
    return {
        "content": [{"type": "text", "text": runner_json_text(value)}],
        "structuredContent": {"result": value},
        "isError": error,
    }


async def vpn_exit_data(conn, record, proof, *, never_started=False):
    """Only an original positive Pod/CID exit can free the shared slot."""
    data = dict(record["data"])
    if not vpn_enabled(record):
        return data
    original = vpn_state(record)
    if never_started:
        if original.get("material") or original.get("credential_reserved"):
            raise HTTPException(409, "Original VPN effects are not ruled out")
        return data
    cid = record["observation"].get("runner", {}).get("egress_container_id")
    if (
        not cid
        or proof.get("egress_container_id") != cid
        or not all(
            proof.get(name) is True
            for name in (
                "runner_terminated",
                "egress_terminated",
                "sandbox_terminated",
                "pvcs_retained",
            )
        )
    ):
        raise HTTPException(409, "Original VPN egress container exit is not confirmed")
    if original.get("credential_reserved"):
        await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": VPN_SLOT_LOCK})
    data[VPN_RECORD] = {
        **original,
        "state": "disconnected",
        "credential_reserved": False,
        "applied": {},
        "egress_exit_confirmed": True,
    }
    return data


def _pause(task):
    return task["ctf_control"].get("native_pause", {})


def require_vpn_resume(record, pause_id):
    state = vpn_state(record)
    if (
        vpn_enabled(record)
        and state.get("credential_reserved")
        and (
            state.get("state") != "ready"
            or not state.get("tunnel")
            or state.get("hold", {}).get("pause_id") != str(pause_id)
        )
    ):
        raise HTTPException(
            409,
            "VPN pause is not confirmed; stop and create a new Task if the VPN is lost",
        )


def _resuming(task, state):
    pause = _pause(task)
    return (
        pause.get("state") == "resuming"
        and state.get("state") == "ready"
        and state.get("hold", {}).get("pause_id") == pause.get("pause_id")
    )


def _fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


class NativeVpn:
    def __init__(self, bootstrap):
        self.bootstrap, self.repo, self.control = bootstrap, bootstrap.repo, bootstrap.control
        self.gateway = bootstrap.gateway

    @staticmethod
    def _scope(record):
        observed = record["observation"]
        runner, sandbox = observed.get("runner", {}), observed.get("sandbox", {})
        config = record["config"]
        try:
            ips = {runner.get("pod_ip"), sandbox.get("pod_ip")}
            if (
                any(target["ipv4"] in ips for target in config["traffic_targets"])
                or config["vpn"]["peer"]["ipv4"] in ips
                or config["vpn"]["tun_ipv4"] in ips
            ):
                raise ValueError("VPN path overlaps its original Pod addresses")
            return NativeVpnScope.model_validate(
                {
                    "task_id": record["task_id"],
                    "operation_id": record["operation_id"],
                    "resource_generation": record["data"].get("resource_generation", 1),
                    "runner_pod_uid": runner.get("pod_uid"),
                    "sandbox_pod_uid": sandbox.get("pod_uid"),
                    "sandbox_container_id": sandbox.get("container_id"),
                    "runner_pod_ipv4": runner.get("pod_ip"),
                    "sandbox_pod_ipv4": sandbox.get("pod_ip"),
                    "egress_container_id": runner.get("egress_container_id"),
                    "profile_digest": management_document_digest(
                        {
                            "vpn": config["vpn"],
                            "traffic_targets": config["traffic_targets"],
                        }
                    ),
                }
            )
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(409, "Original VPN resource identity is unavailable") from error

    async def _write(self, conn, tid, record, state):
        await self.bootstrap._set(conn, tid, data={**record["data"], VPN_RECORD: state})

    async def material(self, conn, tid, record):
        if not vpn_enabled(record):
            raise HTTPException(409, "Task VPN is not published")
        scope = self._scope(record).model_dump(mode="json")
        state = vpn_state(record)
        existing = state.get("material")
        if existing:
            if existing["scope"] != scope:
                raise HTTPException(409, "Original VPN egress identity changed")
            key = self.gateway.cipher.decrypt(existing["key_ciphertext"].encode()).decode()
        else:
            key = secrets.token_urlsafe(48)
            await self._write(
                conn,
                tid,
                record,
                {
                    **state,
                    "material": {
                        "scope": scope,
                        "key_hash": _fingerprint(key),
                        "key_ciphertext": self.gateway.cipher.encrypt(key.encode()).decode(),
                    },
                },
            )
        return {"scope": scope, "report_key": key}

    @staticmethod
    def _status(record, state):
        phase = state.get("state", "idle")
        if phase == "ready" and vpn_applied(record, "solver") is None:
            phase = "unconfirmed"
        actions = ["status"]
        if phase == "idle":
            actions.append("configure")
        elif phase in {"requested", "starting", "ready", "unconfirmed", "unknown", "failed"}:
            actions.append("disconnect")
        return {
            "state": phase,
            "operation_id": state.get("operation_id"),
            "profile_id": record["config"]["vpn"]["id"],
            "allowed_actions": actions,
            "credential_reserved": state.get("credential_reserved", False),
            "credential_release": "after_original_task_containers_exit",
            "solver_exec": phase == "ready",
        }

    async def tool(self, conn, tid, owner, binding, name, arguments):
        record = await self.bootstrap._get(conn, tid)
        if (
            not record
            or not vpn_enabled(record)
            or (
                binding["binding"].get("agent_id") != "lead"
                or binding["business"].get("native_child_id")
                or owner["pod_uid"] != record["observation"].get("runner", {}).get("pod_uid")
            )
        ):
            return vpn_result(
                {"error": "Task VPN is available only to its current Lead"}, error=True
            )
        state = vpn_state(record)
        try:
            if name == VPN_STATUS:
                NativeVpnStatus.model_validate(arguments)
            elif name == VPN_CONFIGURE:
                body = NativeVpnConfigure.model_validate(arguments)
                if body.profile_id != record["config"]["vpn"]["id"]:
                    raise ValueError("VPN profile is not published")
                if state.get("state", "idle") == "idle":
                    material = state.get("material")
                    if not material or material["scope"] != self._scope(record).model_dump(
                        mode="json"
                    ):
                        return vpn_result(
                            {"error": "Original VPN egress is not identified"}, error=True
                        )
                    await conn.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": VPN_SLOT_LOCK}
                    )
                    occupied = (
                        await conn.execute(
                            select(s.task_runner_resources.c.task_id)
                            .where(
                                s.task_runner_resources.c.task_id != tid,
                                s.task_runner_resources.c.data[VPN_RECORD][
                                    "credential_reserved"
                                ].astext
                                == "true",
                            )
                            .limit(1)
                        )
                    ).first()
                    if occupied:
                        return vpn_result(
                            {"error": "The published VPN slot is still occupied"}, error=True
                        )
                    scope = material["scope"]
                    state = {
                        **state,
                        "state": "requested",
                        "credential_reserved": True,
                        "operation_id": str(
                            uuid5(
                                NAMESPACE_URL,
                                f"bbx-task-vpn:{record['operation_id']}:{scope['resource_generation']}",
                            )
                        ),
                        "owner_epoch": owner["epoch"],
                    }
                    await self._write(conn, tid, record, state)
            elif name == VPN_DISCONNECT:
                body = NativeVpnDisconnect.model_validate(arguments)
                if str(body.operation_id) != state.get("operation_id"):
                    raise ValueError("VPN operation is not original")
                if state.get("state") != "disconnected":
                    state = {**state, "state": "disconnecting", "applied": {}}
                    await self._write(conn, tid, record, state)
        except ValueError:
            return vpn_result(
                {"error": "VPN tool arguments do not match the frozen operation"}, error=True
            )
        return vpn_result(self._status(record, state))

    async def _authenticated(self, conn, tid, key, *, host=False):
        await self.repo.lock(conn, tid)
        if host:
            record, original_host = await self.gateway.sandbox.host(conn, tid, key)
            context = original_host["context"]
            observed = record["observation"]
            if (
                context.get("task_id") != str(tid)
                or context.get("operation_id") != record["operation_id"]
                or context.get("sandbox_id") != observed.get("sandbox", {}).get("id")
                or context.get("sandbox_pod_uid") != observed.get("sandbox", {}).get("pod_uid")
                or context.get("runner_pod_uid") != observed.get("runner", {}).get("pod_uid")
            ):
                raise HTTPException(409, "Original VPN Sandbox host changed")
        else:
            record = await self.bootstrap._get(conn, tid)
        if not record or not vpn_enabled(record):
            raise HTTPException(404, "Original VPN control is unavailable")
        state = vpn_state(record)
        material = state.get("material", {})
        if not host and not hmac.compare_digest(material.get("key_hash", ""), _fingerprint(key)):
            raise HTTPException(401, "Original VPN egress identity required")
        if material.get("scope") != self._scope(record).model_dump(mode="json"):
            raise HTTPException(409, "Original VPN resource identity changed")
        await self.bootstrap._global_fence(
            conn,
            record["controller_generation"],
            record["controller_backend_pid"],
            record["controller_backend_start"],
        )
        task = await self.control._task(conn, tid)
        return record, task, state

    def _operation(self, record, state, body):
        if (
            body.scope.model_dump(mode="json") != state["material"]["scope"]
            or str(body.vpn_operation_id) != state.get("operation_id")
            or state["material"]["scope"] != self._scope(record).model_dump(mode="json")
        ):
            raise HTTPException(409, "Original VPN operation identity changed")

    async def _live(self, conn, tid, record, task, state, *, allow_resume=False):
        await self.control._task(conn, tid, live=True)
        resuming = allow_resume and _resuming(task, state)
        if (
            self.bootstrap._stopping(task, record)
            or self.bootstrap._held(task)
            or (native_resume_pending(task) and not resuming)
        ):
            raise HTTPException(409, "Original VPN forwarding is held")
        owner = await self.control._current_owner(conn, tid)
        if (
            owner["epoch"] != state.get("owner_epoch")
            or owner["pod_uid"] != state["material"]["scope"]["runner_pod_uid"]
        ):
            raise HTTPException(409, "Original VPN owner changed")
        return owner

    async def desired(self, tid, key, *, host=False):
        async with self.repo.engine.begin() as conn:
            record, task, state = await self._authenticated(conn, tid, key, host=host)
            phase = state.get("state", "idle")
            pause = _pause(task)
            pause_id = None
            resume_pause_id = None
            if (
                self.bootstrap._stopping(task, record)
                and state.get("operation_id")
                and phase != "disconnected"
            ):
                phase = "disconnecting"
            elif _resuming(task, state):
                resume_pause_id = pause["pause_id"]
            elif phase == "ready" and pause.get("state") in {"draining", "paused", "resuming"}:
                phase = "held"
                pause_id = pause["pause_id"]
            elif phase in {"requested", "starting"} and pause.get("state") in {
                "draining",
                "paused",
            }:
                phase = "disconnecting"
            peer = state.get("egress_link" if host else "sandbox_link")
            return {
                "state": phase,
                "scope": state["material"]["scope"],
                "vpn_operation_id": state.get("operation_id"),
                "remote_public_key": peer.get("public_key") if peer else None,
                "can_probe": phase == "starting" and vpn_applied(record, "probe") is not None,
                "tunnel": state.get("tunnel"),
                **({"pause_id": pause_id} if pause_id else {}),
                **({"resume_pause_id": resume_pause_id} if resume_pause_id else {}),
            }

    async def claim(self, tid, key, body):
        async with self.repo.engine.begin() as conn:
            record, task, state = await self._authenticated(conn, tid, key)
            self._operation(record, state, body)
            if state.get("state") != "requested":
                return {"launch_allowed": False, "state": state["state"]}
            await self._live(conn, tid, record, task, state)
            await self._write(conn, tid, record, {**state, "state": "starting"})
            return {"launch_allowed": True, "state": "starting"}

    async def observe(self, tid, key, body):
        async with self.repo.engine.begin() as conn:
            record, task, state = await self._authenticated(conn, tid, key)
            self._operation(record, state, body)
            phase = state["state"]
            if body.phase == "closed":
                if phase == "requested" and not (
                    self.bootstrap._stopping(task, record)
                    or _pause(task).get("state") in {"draining", "paused"}
                ):
                    raise HTTPException(409, "Original VPN start was not claimed")
                state = {**state, "state": "disconnected", "applied": {}}
            elif body.phase == "held":
                pause = _pause(task)
                assert body.tunnel is not None
                if (
                    phase != "ready"
                    or pause.get("state") not in {"draining", "paused", "resuming"}
                    or str(body.pause_id) != pause.get("pause_id")
                    or body.tunnel.model_dump(mode="json") != state.get("tunnel")
                ):
                    raise HTTPException(409, "Original VPN pause identity changed")
                if pause.get("state") == "resuming" and state.get("hold", {}).get(
                    "pause_id"
                ) == str(body.pause_id):
                    return {"state": phase}
                state = {**state, "hold": {"pause_id": str(body.pause_id)}, "applied": {}}
            elif body.phase in {"failed", "unknown"}:
                if phase in {"starting", "ready"}:
                    state = {**state, "state": body.phase, "applied": {}}
            else:
                if phase not in {"starting", "ready"}:
                    raise HTTPException(409, "Original VPN cannot reopen")
                await self._live(conn, tid, record, task, state, allow_resume=True)
                if body.phase == "link":
                    assert body.link is not None
                    link = body.link.model_dump(mode="json")
                    if state.get("egress_link") not in (None, link):
                        state = {**state, "state": "unknown", "applied": {}}
                    else:
                        state = {**state, "egress_link": link}
                else:
                    assert body.tunnel is not None
                    tunnel = body.tunnel.model_dump(mode="json")
                    if (
                        state.get("egress_link") != tunnel["link"]
                        or body.tunnel.tun_ipv4 != record["config"]["vpn"]["tun_ipv4"]
                        or state.get("tunnel") not in (None, tunnel)
                    ):
                        state = {**state, "state": "unknown", "applied": {}}
                    else:
                        state = {**state, "tunnel": tunnel, "observed_ms": millis()}
                        if body.lease_revision is not None:
                            issued = state.get("issued", {})
                            applied_until = body.applied_until_ms
                            assert applied_until is not None
                            previous = state.get("applied", {})
                            if (
                                issued.get("grant_revision") != body.lease_revision
                                or not millis()
                                < applied_until
                                <= min(
                                    issued.get("authority_until_ms", 0),
                                    issued.get("issued_at_ms", 0) + KERNEL_MS,
                                )
                                or issued.get("mode")
                                != ("probe" if phase == "starting" else "solver")
                                or (
                                    previous.get("grant_revision") == body.lease_revision
                                    and previous.get("applied_until_ms") != applied_until
                                )
                            ):
                                raise HTTPException(409, "Original VPN lease readback changed")
                            state["applied"] = {**issued, "applied_until_ms": applied_until}
            await self._write(conn, tid, record, state)
            return {"state": state["state"]}

    async def link(self, tid, key, body):
        async with self.repo.engine.begin() as conn:
            record, task, state = await self._authenticated(conn, tid, key, host=True)
            self._operation(record, state, body)
            await self._live(conn, tid, record, task, state)
            if state.get("state") not in {"starting", "ready"}:
                raise HTTPException(409, "Original VPN link cannot reopen")
            link = body.link.model_dump(mode="json")
            if state.get("sandbox_link") not in (None, link):
                state = {**state, "state": "unknown", "applied": {}}
            else:
                state = {**state, "sandbox_link": link}
            await self._write(conn, tid, record, state)
            return {"state": state["state"]}

    async def preflight(self, tid, key, body):
        async with self.repo.engine.begin() as conn:
            record, task, state = await self._authenticated(conn, tid, key, host=True)
            self._operation(record, state, body)
            await self._live(conn, tid, record, task, state)
            target = record["config"]["traffic_targets"][0]
            proof = body.model_dump(mode="json")
            if state.get("preflight") == proof:
                return {"state": state["state"]}
            if (
                state.get("state") != "starting"
                or vpn_applied(record, "probe") is None
                or state.get("sandbox_link") != body.link.model_dump(mode="json")
                or state.get("tunnel") != body.tunnel.model_dump(mode="json")
                or (body.ipv4, body.port) != (target["ipv4"], target["port"])
            ):
                raise HTTPException(409, "Original VPN preflight proof changed")
            state = {
                **state,
                "state": "ready" if body.passed else "failed",
                "preflight": proof,
                "applied": {},
            }
            await self._write(conn, tid, record, state)
            return {"state": state["state"]}

    async def lease(self, tid, key, body):
        async with self.repo.engine.begin() as conn:
            record, task, state = await self._authenticated(conn, tid, key)
            self._operation(record, state, body)
            owner = await self._live(conn, tid, record, task, state, allow_resume=True)
            now = millis()
            phase = state.get("state")
            if (
                phase not in {"starting", "ready"}
                or not state.get("tunnel")
                or not state.get("sandbox_link")
            ):
                raise HTTPException(409, "Original VPN tunnel is not confirmed")
            until = min(
                now + LEASE_MS,
                state.get("observed_ms", 0) + OBSERVED_MS,
                int(owner["expires_at"].timestamp() * 1000),
                int(owner["hard_deadline"].timestamp() * 1000),
                int(record["hard_deadline"].timestamp() * 1000),
            )
            if until - now < 12000:
                raise HTTPException(409, "Original VPN forwarding permission is about to expire")
            grant = {
                "mode": "probe" if phase == "starting" else "solver",
                "grant_revision": state.get("issued", {}).get("grant_revision", 0) + 1,
                "issued_at_ms": now,
                "authority_until_ms": until,
            }
            await self._write(conn, tid, record, {**state, "issued": grant})
            return grant


def _service(request):
    bootstrap = request.app.state.native_runner_bootstrap
    if bootstrap is None:
        raise HTTPException(503, "Native VPN is unavailable")
    return bootstrap.vpn


def _key(request, *, host=False):
    name = "x-bbx-sandbox-report" if host else "x-bbx-vpn-egress"
    values = request.headers.getlist(name)
    if len(values) != 1 or not values[0] or len(values[0]) > 128:
        raise HTTPException(401, "Original VPN control identity required")
    return values[0]


async def _body(request, model, *, host=False):
    from bbx_blackboard.runner_gateway import decode

    key = _key(request, host=host)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4096:
            raise HTTPException(413, "Original VPN management body is too large")
        raw.extend(chunk)
    body, _ = decode(model, raw, 4096)
    return body, key


@router.get("/runner-vpn/desired")
async def vpn_egress_desired(request: Request, response: Response, task_id: UUID):
    response.headers["Cache-Control"] = "no-store"
    return await _service(request).desired(task_id, _key(request))


@router.post("/runner-vpn/claim")
async def vpn_egress_claim(request: Request, task_id: UUID):
    body, key = await _body(request, NativeVpnOperation)
    return await _service(request).claim(task_id, key, body)


@router.post("/runner-vpn/observe")
async def vpn_egress_observe(request: Request, task_id: UUID):
    body, key = await _body(request, NativeVpnObservation)
    return await _service(request).observe(task_id, key, body)


@router.post("/runner-vpn/lease")
async def vpn_egress_lease(request: Request, task_id: UUID):
    body, key = await _body(request, NativeVpnOperation)
    return await _service(request).lease(task_id, key, body)


@router.get("/sandbox-host/vpn")
async def vpn_sandbox_desired(request: Request, response: Response, task_id: UUID):
    response.headers["Cache-Control"] = "no-store"
    return await _service(request).desired(task_id, _key(request, host=True), host=True)


@router.post("/sandbox-host/vpn/link")
async def vpn_sandbox_link(request: Request, task_id: UUID):
    body, key = await _body(request, NativeVpnLinkReport, host=True)
    return await _service(request).link(task_id, key, body)


@router.post("/sandbox-host/vpn/preflight")
async def vpn_sandbox_preflight(request: Request, task_id: UUID):
    body, key = await _body(request, NativeVpnPreflight, host=True)
    return await _service(request).preflight(task_id, key, body)
