"""One original, explicitly configured VPN session and short forwarding authority."""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from anyio import BrokenWorkerProcess, CapacityLimiter
from bbx_contracts.legacy_network import LegacyNetworkPolicy, snapshot
from bbx_contracts.runner import runner_json_text
from bbx_contracts.runner_bootstrap import (
    board_network_policy,
    management_document_digest,
    native_resume_pending,
)
from bbx_contracts.runner_vpn import (
    NETWORK_USE_DEFAULT,
    VPN_CONFIGURE,
    VPN_DISCONNECT,
    VPN_DYNAMIC_PROFILE,
    VPN_PROFILES,
    VPN_STATUS,
    NativeSocksEdge,
    NativeTaskNetworkStatus,
    NativeTaskRoutePolicy,
    NativeTaskVpnConfigure,
    NativeTaskVpnNetwork,
    NativeTaskVpnProfile,
    NativeVpnConfigure,
    NativeVpnDisconnect,
    NativeVpnLinkReport,
    NativeVpnObservation,
    NativeVpnOperation,
    NativeVpnPreflight,
    NativeVpnScope,
    NativeVpnStatus,
)
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from httpx import HTTPError
from sqlalchemy import insert, select, text, update

from bbx_blackboard.native_vpn_sources import (
    BUILTIN_DENY,
    checked_address,
    fetch_vpn_configuration,
    parse_private_configuration,
    pin_address,
    require_source_origin,
    resolve_vpn_ipv4,
    vpn_asset_cipher,
)
from bbx_blackboard.store import schema as s

# One deployment-wide slot, even if an administrator changes the cert or publication revision.
VPN_SLOT_LOCK = 0x42425856504E
OBSERVED_MS = 18000
LEASE_MS = 15000
KERNEL_MS = 10000
SOURCE_ACQUIRE_MS = 16000
VPN_RECORD = "vpn"

router = APIRouter(prefix="/api/tasks/{task_id}", tags=["runner"])


def millis():
    return int(datetime.now(UTC).timestamp() * 1000)


def vpn_enabled(record):
    return vpn_profile(record) in VPN_PROFILES


def dynamic_vpn(record):
    return vpn_profile(record) == VPN_DYNAMIC_PROFILE


def vpn_profile(record):
    if record["data"].get("execution_contract") == "exploration-board-v1":
        return record["data"].get("vpn_profile")
    return record["request"].get("publication", {}).get("vpn_profile")


def vpn_selected(record):
    return vpn_enabled(record) and (
        not dynamic_vpn(record) or record["data"].get("native_network") == "vpn"
    )


def _board_network(record):
    if record["data"].get("execution_contract") != "exploration-board-v1":
        return None
    return board_network_policy(record.get("config") or {})


def board_direct_policy(record):
    board = _board_network(record)
    return board if board and board["mode"] == "default" else None


def board_protected_policy(record):
    board = _board_network(record)
    return (
        LegacyNetworkPolicy.model_validate(board["legacy_policy"])
        if (board and board["mode"] in {"vpn", "socks5"})
        else None
    )


def default_network(record):
    data = record["data"]
    board = board_direct_policy(record)
    if (
        (dynamic_vpn(record) or board)
        and data.get("native_network") == "default"
        and (data.get("network_policy") or {}).get("network_digest") == data.get("network_digest")
        and data.get("network_digest")
        and data.get("route_policy")
        and (not board or data.get("dns_ipv4") == board["dns_ipv4"])
    ):
        return {
            **{key: data[key] for key in ("network_digest", "route_policy")},
            **({"dns_ipv4": board["dns_ipv4"]} if board else {}),
        }
    return None


def vpn_network(record):
    return vpn_state(record).get("network", {})


def _dynamic_denied(record, profile):
    config = record["config"]
    observation = record["observation"]
    infrastructure = {
        config["sandbox_network"]["ipv4"],
        observation.get("runner", {}).get("pod_ip"),
        observation.get("sandbox", {}).get("pod_ip"),
    }
    return [
        *profile.deny_cidrs,
        *[
            cidr
            for entry in config.get("runner_control_egress", ())
            for cidr in entry["cidrs"]
            if ipaddress.ip_network(cidr).version == 4
        ],
        *[f"{ip}/32" for ip in infrastructure if ip],
    ]


def _route_policy(record, profile, allowed, extra_denied=()):
    def normalize(values):
        return [str(value) for value in sorted({ipaddress.IPv4Network(value) for value in values})]

    return NativeTaskRoutePolicy(
        allow_cidrs=normalize(allowed),
        deny_cidrs=normalize([*BUILTIN_DENY, *_dynamic_denied(record, profile), *extra_denied]),
    )


def vpn_state(record):
    return record["data"].get(VPN_RECORD, {})


def vpn_applied(record, mode):
    state = vpn_state(record)
    applied = state.get("applied", {})
    if applied.get("mode") != mode or applied.get("applied_until_ms", 0) <= millis():
        return None
    return applied


def vpn_solver_deadline(record):
    if (dynamic_vpn(record) and not vpn_selected(record)) or board_direct_policy(record):
        if default_network(record) is None:
            raise HTTPException(409, "The original Task network is not ready")
        return None
    if not vpn_selected(record):
        return None
    applied = vpn_applied(record, "solver")
    if vpn_state(record).get("state") != "ready" or applied is None:
        raise HTTPException(409, "The original Task VPN is not ready")
    return applied["applied_until_ms"]


def vpn_retired(record):
    return vpn_selected(record) and vpn_state(record).get("state") in {
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
    return (task.get("ctf_control") or {}).get("native_pause", {})


def require_vpn_resume(record, pause_id):
    state = vpn_state(record)
    if (
        vpn_selected(record)
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
        self.asset_cipher = vpn_asset_cipher(bootstrap.gateway.settings)
        self.parse_slots = CapacityLimiter(2)
        self.resolve_source = resolve_vpn_ipv4
        self.source_tls = None
        self.source_jobs = set()

    async def parse_source(self, raw):
        return await parse_private_configuration(raw, self.parse_slots)

    @staticmethod
    def _scope(record):
        observed = record["observation"]
        runner, sandbox = observed.get("runner", {}), observed.get("sandbox", {})
        config = record["config"]
        try:
            ips = {runner.get("pod_ip"), sandbox.get("pod_ip")}
            if not dynamic_vpn(record) and (
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

    async def _write(self, conn, tid, record, state, **data):
        await self.bootstrap._set(conn, tid, data={**record["data"], **data, VPN_RECORD: state})

    def default_route_fields(self, record):
        if board := board_direct_policy(record):
            config, observed = record["config"], record["observation"]
            denied = {
                *BUILTIN_DENY,
                *board["deny_cidrs"],
                config["sandbox_network"]["ipv4"] + "/32",
                observed["runner"]["pod_ip"] + "/32",
                observed["sandbox"]["pod_ip"] + "/32",
                *(
                    cidr
                    for entry in config.get("runner_control_egress", ())
                    for cidr in entry["cidrs"]
                    if ipaddress.ip_network(cidr).version == 4
                ),
            }
            policy = NativeTaskRoutePolicy(
                allow_cidrs=board["allow_cidrs"],
                deny_cidrs=[str(cidr) for cidr in sorted(map(ipaddress.IPv4Network, denied))],
            ).model_dump(mode="json")
            dns = checked_address(board["dns_ipv4"], policy["allow_cidrs"], policy["deny_cidrs"])
            fields = {"route_policy": policy, "dns_ipv4": dns}
            return {
                **fields,
                "network_digest": management_document_digest(
                    {
                        "board_network": board,
                        "operation_id": record["operation_id"],
                        "resource_generation": record["data"].get("resource_generation", 1),
                        "sandbox_pod_uid": observed["sandbox"]["pod_uid"],
                        **fields,
                    }
                ),
            }
        profile = NativeTaskVpnProfile.model_validate(record["config"]["vpn"])
        policy = _route_policy(record, profile, profile.default_cidrs).model_dump(mode="json")
        scope = self._scope(record).model_dump(mode="json")
        return {
            "route_policy": policy,
            "network_digest": management_document_digest({"scope": scope, "route_policy": policy}),
        }

    async def default_ready(self, conn, tid, record, proof):
        if (
            not (dynamic_vpn(record) or board_direct_policy(record))
            or record["data"].get("native_network") != "default"
        ):
            raise HTTPException(409, "The original Task did not select the default network")
        expected = self.default_route_fields(record)
        try:
            valid = (
                set(proof) == {"network_digest", "policy_name", "policy_uid"}
                and proof["policy_name"] == "bbx-binding-direct"
                and str(UUID(proof["policy_uid"])) == proof["policy_uid"]
                and proof["network_digest"] == expected["network_digest"]
                and all(record["data"].get(key) == value for key, value in expected.items())
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        prior = record["data"].get("network_policy")
        if not valid or (prior is not None and prior != proof):
            raise HTTPException(409, "The original default network policy changed")
        if prior is None:
            await self._write(conn, tid, record, vpn_state(record), network_policy=proof)
        return self.bootstrap._public(await self.bootstrap._get(conn, tid))

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
            state = {
                **state,
                "material": {
                    "scope": scope,
                    "key_hash": _fingerprint(key),
                    "key_ciphertext": self.gateway.cipher.encrypt(key.encode()).decode(),
                },
            }
            if (board := board_protected_policy(record)) is not None:
                state = await self._prepare_board_edge(conn, tid, record, board, state)
            await self._write(conn, tid, record, state)
        return {"scope": scope, "report_key": key}

    @staticmethod
    def _board_edge_network(record, board):
        assert board.gateway is not None
        gateway = board.gateway
        profile = NativeTaskVpnProfile.model_validate(record["config"]["vpn"])
        peer = gateway.upstream.model_dump(mode="json", exclude_none=True)
        checked_address(peer["ipv4"], profile.peer_cidrs, _dynamic_denied(record, profile))
        routes = _route_policy(
            record,
            profile,
            gateway.allow_cidrs,
            [*gateway.deny_cidrs, peer["ipv4"] + "/32"],
        ).model_dump(mode="json")
        preflight = {"ipv4": gateway.probe.ipv4, "port": gateway.probe.port}
        checked_address(preflight["ipv4"], routes["allow_cidrs"], routes["deny_cidrs"])
        safe = {"peer": peer, "route_policy": routes, "preflight": preflight}
        return {
            **safe,
            "network_digest": management_document_digest(
                {
                    **safe,
                    "policy_digest": snapshot(board).policy_digest,
                    "scope": record["operation_id"],
                    "resource_generation": record["data"].get("resource_generation", 1),
                }
            ),
        }

    async def _prepare_board_edge(self, conn, tid, record, board, state):
        owner = await self.control._current_owner(conn, tid)
        if (
            state.get("state", "idle") != "idle"
            or owner["pod_uid"] != state["material"]["scope"]["runner_pod_uid"]
            or not vpn_selected(record)
        ):
            raise HTTPException(409, "Original Blackboard protected network cannot reopen")
        await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": VPN_SLOT_LOCK})
        occupied = (
            await conn.execute(
                select(s.task_runner_resources.c.task_id)
                .where(
                    s.task_runner_resources.c.task_id != tid,
                    s.task_runner_resources.c.data[VPN_RECORD]["credential_reserved"].astext
                    == "true",
                )
                .limit(1)
            )
        ).first()
        if occupied:
            raise HTTPException(409, "The published VPN slot is still occupied")
        try:
            network = self._board_edge_network(record, board)
        except ValueError as error:
            raise HTTPException(409, "Original Blackboard protected policy is invalid") from error
        return {
            **state,
            "state": "awaiting_policy",
            "credential_reserved": True,
            "operation_id": str(
                uuid5(
                    NAMESPACE_URL,
                    f"bbx-task-vpn:{record['operation_id']}:{state['material']['scope']['resource_generation']}",
                )
            ),
            "owner_epoch": owner["epoch"],
            "network": network,
        }

    @staticmethod
    def _status(record, state):
        if dynamic_vpn(record) and not vpn_selected(record):
            mode = record["data"].get("native_network", "unselected")
            ready = default_network(record) is not None
            return {
                "native_network": mode,
                "state": "ready" if ready else "preparing" if mode == "default" else "unselected",
                "allowed_actions": (
                    ["status", "use_default", "configure"] if mode == "unselected" else ["status"]
                ),
                "credential_reserved": False,
                "solver_exec": ready,
            }
        phase = state.get("state", "idle")
        if phase == "ready" and vpn_applied(record, "solver") is None:
            phase = "unconfirmed"
        actions = ["status"]
        if phase == "idle":
            actions.append("configure")
        elif phase in {
            "source_pending",
            "acquiring",
            "awaiting_policy",
            "requested",
            "starting",
            "ready",
            "unconfirmed",
            "unknown",
            "failed",
        }:
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

    def status_projection(self, record):
        if not record or not dynamic_vpn(record):
            return None
        mode = record["data"]["native_network"]
        if record["state"] in {"stopped", "exited", "retired"}:
            state = "stopped"
        elif record["data"].get("recovery"):
            state = "failed"
        else:
            phase = self._status(record, vpn_state(record))["state"]
            if phase in {"unselected", "ready", "disconnected"}:
                state = phase
            elif phase in {"failed", "unknown", "unconfirmed"}:
                state = "failed"
            else:
                state = "preparing"
        return NativeTaskNetworkStatus(mode=mode, state=state)

    async def tool(self, conn, tid, owner, binding, name, arguments):
        record = await self.bootstrap._get(conn, tid)
        if (
            not record
            or not vpn_enabled(record)
            or record["data"].get("execution_contract") == "exploration-board-v1"
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
            if name == NETWORK_USE_DEFAULT:
                NativeVpnStatus.model_validate(arguments)
                if not dynamic_vpn(record) or vpn_selected(record):
                    return vpn_result(
                        {"error": "The Task network cannot switch to default"}, error=True
                    )
                if record["data"].get("native_network") == "unselected":
                    fields = self.default_route_fields(record)
                    await self._write(conn, tid, record, state, native_network="default", **fields)
                    record = {
                        **record,
                        "data": {**record["data"], **fields, "native_network": "default"},
                    }
            elif name == VPN_STATUS:
                NativeVpnStatus.model_validate(arguments)
            elif name == VPN_CONFIGURE:
                if dynamic_vpn(record):
                    body = NativeTaskVpnConfigure.model_validate(arguments)
                    profile = NativeTaskVpnProfile.model_validate(record["config"]["vpn"])
                    if record["data"].get("native_network") == "default":
                        return vpn_result(
                            {"error": "The Task already selected the default network"}, error=True
                        )
                    checked_address(
                        body.preflight.ipv4, profile.target_cidrs, _dynamic_denied(record, profile)
                    )
                    if body.https_url is not None:
                        require_source_origin(body.https_url, profile)
                    source_request = body.model_dump(mode="json", exclude_none=True)
                    intent_digest = management_document_digest(source_request)
                    if (
                        state.get("state", "idle") != "idle"
                        and state.get("source_intent_digest") != intent_digest
                    ):
                        return vpn_result(
                            {"error": "Task VPN source and targets are already frozen"}, error=True
                        )
                    if (
                        body.source_id is not None
                        and not (
                            await conn.execute(
                                select(s.native_vpn_assets.c.id).where(
                                    s.native_vpn_assets.c.id == body.source_id,
                                    s.native_vpn_assets.c.task_id == tid,
                                    s.native_vpn_assets.c.group_id.is_not(None),
                                )
                            )
                        ).first()
                    ):
                        return vpn_result(
                            {"error": "Private VPN source is not available for this Task"},
                            error=True,
                        )
                else:
                    body = NativeVpnConfigure.model_validate(arguments)
                    if body.profile_id != record["config"]["vpn"]["id"]:
                        raise ValueError("VPN profile is not published")
                    source_request, intent_digest = None, None
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
                    operation_id = uuid5(
                        NAMESPACE_URL,
                        f"bbx-task-vpn:{record['operation_id']}:{scope['resource_generation']}",
                    )
                    state = {
                        **state,
                        "state": "requested" if source_request is None else "source_pending",
                        "credential_reserved": True,
                        "operation_id": str(operation_id),
                        "owner_epoch": owner["epoch"],
                    }
                    if source_request is not None:
                        state["source_intent_digest"] = intent_digest
                        await conn.execute(
                            insert(s.native_vpn_assets).values(
                                id=operation_id,
                                task_id=tid,
                                digest=intent_digest,
                                ciphertext=self.asset_cipher.encrypt(
                                    runner_json_text(source_request).encode()
                                ).decode(),
                            )
                        )
                    mode = {"native_network": "vpn"} if dynamic_vpn(record) else {}
                    await self._write(conn, tid, record, state, **mode)
                    if mode:
                        record = {**record, "data": {**record["data"], **mode}}
            elif name == VPN_DISCONNECT:
                if not vpn_selected(record):
                    return vpn_result({"error": "The Task has no selected VPN"}, error=True)
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

    async def _asset(self, conn, tid, asset_id, *, upload=False):
        table = s.native_vpn_assets
        row = (
            await conn.execute(
                select(table.c.digest, table.c.ciphertext).where(
                    table.c.id == UUID(str(asset_id)),
                    table.c.task_id == tid,
                    table.c.group_id.is_not(None) if upload else table.c.group_id.is_(None),
                )
            )
        ).first()
        if row is None:
            raise ValueError("Original VPN source is unavailable")
        return row.digest, self.asset_cipher.decrypt(row.ciphertext.encode())

    async def prepare_source(self, tid, action):
        async with self.repo.engine.begin() as conn:
            record, task = await self.bootstrap._checked(conn, tid, action, live=True)
            state = vpn_state(record)
            if not dynamic_vpn(record) or action.proof != {
                "vpn_operation_id": state.get("operation_id")
            }:
                raise HTTPException(409, "Original VPN acquisition identity changed")
            if state.get("state") == "acquiring":
                acquisition = state.get("source_acquisition", {})
                if (
                    acquisition.get("controller_generation") != record["controller_generation"]
                    or acquisition.get("deadline_ms", 0) <= millis()
                ):
                    await self._write(conn, tid, record, {**state, "state": "unknown"})
                    return self.bootstrap._public(await self.bootstrap._get(conn, tid))
                return self.bootstrap._public(record)
            if state.get("state") != "source_pending":
                return self.bootstrap._public(record)
            await self._live(conn, tid, record, task, state)
            await self._write(
                conn,
                tid,
                record,
                {
                    **state,
                    "state": "acquiring",
                    "source_acquisition": {
                        "controller_generation": record["controller_generation"],
                        "deadline_ms": min(
                            millis() + SOURCE_ACQUIRE_MS,
                            int(record["hard_deadline"].timestamp() * 1000),
                        ),
                    },
                },
            )
            pending = self.bootstrap._public(await self.bootstrap._get(conn, tid))
        job = asyncio.create_task(self._acquire_source(tid, action))
        self.source_jobs.add(job)
        job.add_done_callback(self.source_jobs.discard)
        return pending

    async def _acquiring(self, conn, tid, action):
        record, task = await self.bootstrap._checked(conn, tid, action, live=True)
        state = vpn_state(record)
        acquisition = state.get("source_acquisition", {})
        if (
            state.get("state") != "acquiring"
            or action.proof != {"vpn_operation_id": state.get("operation_id")}
            or acquisition.get("controller_generation") != record["controller_generation"]
            or acquisition.get("deadline_ms", 0) <= millis()
        ):
            raise HTTPException(409, "Original VPN acquisition is closed")
        await self._live(conn, tid, record, task, state)
        return record, state

    async def _acquire_source(self, tid, action):
        operation_id = UUID(action.proof["vpn_operation_id"])
        result = None
        failure = "unknown"
        try:
            async with self.repo.engine.begin() as conn:
                record, state = await self._acquiring(conn, tid, action)
                digest, document = await self._asset(conn, tid, operation_id)
                request = NativeTaskVpnConfigure.model_validate_json(document)
                if digest != state.get(
                    "source_intent_digest"
                ) or digest != management_document_digest(
                    request.model_dump(mode="json", exclude_none=True)
                ):
                    raise ValueError("Original VPN source request changed")
                upload = (
                    await self._asset(conn, tid, request.source_id, upload=True)
                    if request.source_id is not None
                    else None
                )
                profile = NativeTaskVpnProfile.model_validate(record["config"]["vpn"])
                denied = _dynamic_denied(record, profile)
            if upload:
                payload = upload[1]
                if hashlib.sha256(payload).hexdigest() != upload[0]:
                    raise ValueError("Original private VPN source changed")
            else:
                assert request.https_url is not None
                payload = await fetch_vpn_configuration(
                    request.https_url,
                    profile,
                    denied,
                    resolver=self.resolve_source,
                    tls=self.source_tls,
                )
            (host, port), server_name, material = await self.parse_source(payload)
            async with asyncio.timeout(4):
                ip = await pin_address(host, port, profile.peer_cidrs, denied, self.resolve_source)
            policy = _route_policy(record, profile, profile.target_cidrs, [f"{ip}/32"])
            checked_address(request.preflight.ipv4, policy.allow_cidrs, policy.deny_cidrs)
            material_json = runner_json_text(material)
            material_sha = hashlib.sha256(material_json.encode()).hexdigest()
            document = {
                "peer": {"ipv4": ip, "port": port, "server_name": server_name},
                "route_policy": policy.model_dump(mode="json"),
                "preflight": request.preflight.model_dump(mode="json"),
            }
            network_digest = management_document_digest(
                {**document, "material_sha256": material_sha}
            )
            network = NativeTaskVpnNetwork.model_validate(
                {**document, "network_digest": network_digest}
            ).model_dump(mode="json")
            result = (
                network,
                material_sha,
                self.asset_cipher.encrypt(material_json.encode()).decode(),
            )
        except HTTPException:
            return
        except (ValueError, TypeError, InvalidToken):
            failure = "failed"
        except (BrokenWorkerProcess, TimeoutError, OSError, HTTPError):
            pass
        try:
            async with self.repo.engine.begin() as conn:
                record, state = await self._acquiring(conn, tid, action)
                if result is None:
                    await self._write(conn, tid, record, {**state, "state": failure, "applied": {}})
                    return
                network, material_sha, ciphertext = result
                await conn.execute(
                    update(s.native_vpn_assets)
                    .where(
                        s.native_vpn_assets.c.id == operation_id,
                        s.native_vpn_assets.c.task_id == tid,
                    )
                    .values(digest=material_sha, ciphertext=ciphertext)
                )
                await self._write(
                    conn, tid, record, {**state, "state": "awaiting_policy", "network": network}
                )
        except HTTPException:
            return

    async def peer_ready(self, conn, tid, record, task, proof):
        state = vpn_state(record)
        network = vpn_network(record)
        try:
            valid = (
                dynamic_vpn(record)
                and set(proof)
                == {"network_digest", "policy_name", "policy_uid", "vpn_operation_id"}
                and proof["vpn_operation_id"] == state.get("operation_id")
                and proof["network_digest"] == network.get("network_digest")
                and proof["policy_name"] == "bbx-vpn-peer"
                and str(UUID(proof["policy_uid"])) == proof["policy_uid"]
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid or not network:
            raise HTTPException(409, "Original VPN policy receipt changed")
        if state.get("policy") == proof:
            return self.bootstrap._public(record)
        if state.get("state") != "awaiting_policy":
            raise HTTPException(409, "Original VPN policy cannot reopen")
        await self._live(conn, tid, record, task, state)
        await self._write(conn, tid, record, {**state, "state": "requested", "policy": proof})
        return self.bootstrap._public(await self.bootstrap._get(conn, tid))

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
            or body.network_digest != vpn_network(record).get("network_digest")
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
            if dynamic_vpn(record) and not vpn_selected(record):
                result = {
                    "state": "idle",
                    "scope": state["material"]["scope"],
                    "vpn_operation_id": None,
                    "remote_public_key": None,
                    "can_probe": False,
                    "tunnel": None,
                }
                if host:
                    result["native_network"] = record["data"]["native_network"]
                    if network := default_network(record):
                        result["default_network"] = network
                return result
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
            board = board_protected_policy(record) if host else None
            return {
                "state": phase,
                "scope": state["material"]["scope"],
                "vpn_operation_id": state.get("operation_id"),
                "remote_public_key": peer.get("public_key") if peer else None,
                "can_probe": phase == "starting" and vpn_applied(record, "probe") is not None,
                "tunnel": state.get("tunnel"),
                **({"network": state["network"]} if state.get("network") else {}),
                **(
                    {"legacy_probe": board.gateway.probe.model_dump(mode="json")}
                    if board and board.gateway
                    else {}
                ),
                **({"native_network": "vpn"} if host and dynamic_vpn(record) else {}),
                **({"pause_id": pause_id} if pause_id else {}),
                **({"resume_pause_id": resume_pause_id} if resume_pause_id else {}),
            }

    async def claim(self, tid, key, body):
        async with self.repo.engine.begin() as conn:
            record, task, state = await self._authenticated(conn, tid, key)
            self._operation(record, state, body)
            if not vpn_selected(record) or state.get("state") != "requested":
                return {"launch_allowed": False, "state": state["state"]}
            await self._live(conn, tid, record, task, state)
            result = {"launch_allowed": True, "state": "starting"}
            if dynamic_vpn(record):
                network = vpn_network(record)
                if not state.get("policy") or not network:
                    raise HTTPException(409, "Original VPN policy is not confirmed")
                if board := board_protected_policy(record):
                    if network != self._board_edge_network(record, board):
                        raise HTTPException(409, "Original Blackboard protected policy changed")
                else:
                    digest, material = await self._asset(conn, tid, state["operation_id"])
                    safe = {key: value for key, value in network.items() if key != "network_digest"}
                    if (
                        hashlib.sha256(material).hexdigest() != digest
                        or management_document_digest({**safe, "material_sha256": digest})
                        != body.network_digest
                    ):
                        raise HTTPException(409, "Original VPN material changed")
                    result["material"] = json.loads(material)
            await self._write(conn, tid, record, {**state, "state": "starting"})
            return result

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
                if phase in {"starting", "ready"} or (
                    phase == "requested" and dynamic_vpn(record) and body.phase == "unknown"
                ):
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
                        or not self._valid_edge(record, body.tunnel)
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

    @staticmethod
    def _valid_edge(record, edge):
        board = board_protected_policy(record)
        if isinstance(edge, NativeSocksEdge):
            return board is not None and board.mode == "socks5"
        if board and board.mode == "socks5":
            return False
        return NativeVpn._valid_tunnel_address(record, edge.tun_ipv4)

    @staticmethod
    def _valid_tunnel_address(record, value):
        if not dynamic_vpn(record):
            return value == record["config"]["vpn"]["tun_ipv4"]
        profile = NativeTaskVpnProfile.model_validate(record["config"]["vpn"])
        network = vpn_network(record)
        denied = _dynamic_denied(record, profile)
        denied += [f"{network[key]['ipv4']}/32" for key in ("peer", "preflight")]
        try:
            checked_address(value, ["0.0.0.0/0"], denied)
        except ValueError:
            return False
        return True

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
            target = (
                vpn_network(record)["preflight"]
                if dynamic_vpn(record)
                else record["config"]["traffic_targets"][0]
            )
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
async def vpn_egress_claim(request: Request, response: Response, task_id: UUID):
    response.headers["Cache-Control"] = "no-store"
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
