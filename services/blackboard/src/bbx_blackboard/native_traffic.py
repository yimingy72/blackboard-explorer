"""Task-bound native capture registration, immutable archive writes, and read projection."""

from __future__ import annotations

import hashlib
import ipaddress
import struct
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from bbx_contracts.models import ContractModel
from bbx_contracts.traffic import (
    TrafficBindingScope,
    TrafficFile,
    TrafficManifest,
    TrafficNativePart,
    TrafficNativeSourceIndex,
    TrafficNativeSourceRef,
)
from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select, text, update

from bbx_blackboard.native import NativeUnavailable, object_bytes
from bbx_blackboard.native_vpn import (
    board_direct_policy,
    default_network,
    dynamic_vpn,
    vpn_network,
    vpn_retired,
    vpn_selected,
    vpn_solver_deadline,
    vpn_state,
)
from bbx_blackboard.runner_control import row
from bbx_blackboard.store import schema as s

PROFILE = "binding-pcap-v1"
PART_LIMIT = 4 * 1024 * 1024
PACKET_LIMIT = 7943
SCOPE_LIMIT = 256
ARCHIVE_LOCK_SALT = 0x424258434150


def opted_in(record):
    return bool(record and record["request"].get("traffic_profile") == PROFILE)


def network_scopes(record):
    return record["data"].get("network_scopes", {}) if opted_in(record) else {}


def scope_done(entry):
    return entry["state"] == "archived"


def binding_capture_pending(record, binding_id):
    return any(
        entry["scope"]["binding_id"] == binding_id and not scope_done(entry)
        for entry in network_scopes(record).values()
    )


def epoch_capture_pending(record, epoch=None):
    return any(
        (epoch is None or entry["scope"]["epoch"] == epoch) and not scope_done(entry)
        for entry in network_scopes(record).values()
    )


def lost_captures(record):
    return {
        source: (
            entry
            if entry["state"] == "archived"
            else {**entry, "loss": "Original Sandbox exited before capture archive confirmation"}
        )
        for source, entry in network_scopes(record).items()
    }


class CaptureReady(ContractModel):
    netns_inode: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    host_ifindex: int = Field(ge=1, le=2**31 - 1, strict=True)
    capture_pid: int = Field(ge=1, le=2**31 - 1, strict=True)
    capture_start_ticks: str = Field(pattern=r"^[1-9][0-9]{0,19}$")


class CaptureSeal(CaptureReady):
    size_bytes: int = Field(ge=24, le=PART_LIMIT, strict=True)
    packet_count: int = Field(ge=0, le=PACKET_LIMIT, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    complete: bool = Field(strict=True)
    error: str | None = Field(default=None, max_length=512)
    dropped_packets: int | None = Field(default=None, ge=0, strict=True)
    truncated_packets: int = Field(ge=0, le=PACKET_LIMIT, strict=True)
    discarded_tail_bytes: int = Field(ge=0, le=PART_LIMIT, strict=True)

    @model_validator(mode="after")
    def coherent_coverage(self):
        if self.truncated_packets > self.packet_count:
            raise ValueError("Truncated packets exceed the captured packet count")
        if self.complete and (
            self.error
            or self.dropped_packets != 0
            or self.discarded_tail_bytes
            or self.truncated_packets
        ):
            raise ValueError("Complete capture requires exact final coverage")
        if not self.complete and not self.error:
            raise ValueError("Partial capture requires its observed coverage gap")
        return self


def pcap_counts(data):
    byte_order = {
        b"\xd4\xc3\xb2\xa1": "<",
        b"\xa1\xb2\xc3\xd4": ">",
        b"\x4d\x3c\xb2\xa1": "<",
        b"\xa1\xb2\x3c\x4d": ">",
    }.get(data[:4])
    if not byte_order or len(data) < 24 or len(data) > PART_LIMIT:
        raise ValueError("Capture is not a bounded classic PCAP")
    major, minor, _, _, snaplen, link = struct.unpack_from(byte_order + "HHIIII", data, 4)
    if (major, minor, snaplen, link) != (2, 4, 512, 1):
        raise ValueError("Unexpected native Ethernet capture format")
    fraction_limit = (
        1_000_000_000 if data[:4] in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d") else 1_000_000
    )
    packet, truncated, offset = 0, 0, 24
    while offset < len(data):
        if offset + 16 > len(data) or packet >= PACKET_LIMIT:
            raise ValueError("Native capture has an incomplete packet header")
        _, fraction, size, original = struct.unpack_from(byte_order + "IIII", data, offset)
        if fraction >= fraction_limit or not 0 < size <= min(512, original):
            raise ValueError("Native capture packet length is invalid")
        offset += 16 + size
        if offset > len(data):
            raise ValueError("Native capture has an incomplete packet body")
        packet += 1
        truncated += size < original
    return packet, truncated


async def _set(conn, record, data):
    await conn.execute(
        update(s.task_runner_resources)
        .where(s.task_runner_resources.c.task_id == record["task_id"])
        .values(data=data, revision=s.task_runner_resources.c.revision + 1)
    )


async def _put_scope(conn, record, entry):
    source = entry["scope"]["source_id"]
    await _set(
        conn,
        record,
        {
            **record["data"],
            "network_scopes": {**network_scopes(record), source: entry},
        },
    )


def _same_host(record, entry):
    scope = entry["scope"]
    context = record["sandbox_host"].get("context", {})
    return all(
        scope[key] == context.get(key)
        for key in ("sandbox_id", "sandbox_pod_uid", "runner_pod_uid", "execution_context_id")
    )


def _scope(record, source):
    entry = network_scopes(record).get(source)
    if not entry or not _same_host(record, entry):
        raise HTTPException(403, "Original capture source is outside this host")
    return entry


async def _lock_archive(conn, tid):
    if not (
        await conn.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:tid, :salt))"),
            {"tid": str(tid), "salt": ARCHIVE_LOCK_SALT},
        )
    ).scalar_one():
        raise HTTPException(409, "Original capture archive writer is busy")


def _uncertain_writes(record):
    return any(
        state == "claimed"
        for item in network_scopes(record).values()
        for state in item.get("writes", {}).values()
    )


async def _resource(conn, tid):
    return (
        (
            await conn.execute(
                select(s.task_runner_resources).where(s.task_runner_resources.c.task_id == tid)
            )
        )
        .mappings()
        .first()
    )


async def close_archive_for_purge(conn, record):
    """Only called after the original Task and resource retirement checks under Task lock."""
    if not opted_in(record):
        return False
    if not record["data"].get("traffic_archive_closed"):
        await _set(conn, record, {**record["data"], "traffic_archive_closed": True})
    return True


@asynccontextmanager
async def traffic_purge_guard(repo, tid, *, protect):
    if not protect:
        yield
        return
    async with repo.engine.begin() as conn:
        await _lock_archive(conn, tid)
        record = await _resource(conn, tid)
        if _uncertain_writes(record):
            raise HTTPException(409, "Original capture object write is not confirmed")
        yield


class NativeTraffic:
    def __init__(self, sandbox):
        self.sandbox, self.repo, self.control = sandbox, sandbox.repo, sandbox.gateway.control

    @staticmethod
    def binding(record, bid):
        for entry in network_scopes(record).values():
            if entry["scope"]["binding_id"] == bid and _same_host(record, entry):
                return entry
        return None

    async def allocate(self, conn, tid, binding, runner_binding):
        record = await self.sandbox.resource(conn, tid)
        if not opted_in(record):
            return binding, None
        entry = self.binding(record, runner_binding["id"])
        allowed = False
        if entry is None:
            active = {
                source: item
                for source, item in network_scopes(record).items()
                if _same_host(record, item)
            }
            if len(active) >= SCOPE_LIMIT:
                raise HTTPException(409, "Native capture scope capacity reached")
            context = record["sandbox_host"]["context"]
            ordinal = (
                max(
                    (item["ordinal"] for item in active.values() if _same_host(record, item)),
                    default=0,
                )
                + 1
            )
            if ordinal == 64:  # The original optional worker-a bootstrap reserves this /30.
                ordinal += 1
            if ordinal > SCOPE_LIMIT:
                raise HTTPException(409, "Original Sandbox network pool is exhausted")
            source = str(
                uuid5(
                    NAMESPACE_URL,
                    f"bbx-capture:{tid}:{context['sandbox_pod_uid']}:{runner_binding['id']}",
                )
            )
            gateway = ipaddress.IPv4Address("198.19.0.0") + ordinal * 4 + 1
            bound = {
                **binding,
                "network_identity": source,
                "namespace": f"bbx{ordinal:03x}",
                "ipv4": str(gateway + 1),
            }
            if dynamic_vpn(record) or board_direct_policy(record):
                vpn_solver_deadline(record)
                if vpn_selected(record):
                    network = vpn_network(record)
                    path = {
                        "route_policy": network["route_policy"],
                        "vpn_operation_id": vpn_state(record)["operation_id"],
                        "vpn_network_digest": network["network_digest"],
                    }
                else:
                    path = default_network(record)
                    if path is None:
                        raise HTTPException(409, "Original default network policy is not confirmed")
            else:
                path = {
                    "targets": context["traffic_targets"],
                    **(
                        {"vpn_operation_id": vpn_state(record)["operation_id"]}
                        if vpn_selected(record)
                        else {}
                    ),
                }
            scope = {
                **{
                    key: bound[key]
                    for key in (
                        "task_id",
                        "agent_id",
                        "role",
                        "parent_agent_id",
                        "assignment_id",
                        "generation",
                        "epoch",
                        "sandbox_id",
                        "execution_context_id",
                        "namespace",
                        "ipv4",
                    )
                },
                "source_id": source,
                "binding_id": runner_binding["id"],
                "sandbox_pod_uid": context["sandbox_pod_uid"],
                "runner_pod_uid": context["runner_pod_uid"],
                "host_interface": f"bv{ordinal:03x}",
                "host_ipv4": str(gateway),
                **path,
            }
            task = await row(conn, s.tasks, tid)
            entry = {
                "scope": scope,
                "binding": bound,
                "run": task["run_number"],
                "ordinal": ordinal,
                "state": "claimed",
                "created_at": datetime.now(UTC).isoformat(),
            }
            await _put_scope(conn, record, entry)
            allowed = True
        elif entry["state"] != "ready" or entry.get("loss"):
            raise HTTPException(409, "Original network preparation or closure is unconfirmed")
        return entry["binding"], {**self._view(entry), "prepare_allowed": allowed}

    @staticmethod
    def _view(entry, *, retire=False):
        return {
            **{
                name: entry[name]
                for name in ("scope", "binding", "state", "ready", "seal")
                if name in entry
            },
            "retire": retire,
            "archive_confirmed": entry["state"] == "archived",
        }

    async def _live(self, conn, tid, record, entry):
        scope = entry["scope"]
        await self.control._task(conn, tid, live=True)
        owner = await self.control._current_owner(conn, tid)
        if owner["epoch"] != scope["epoch"] or owner["pod_uid"] != scope["runner_pod_uid"]:
            raise HTTPException(409, "Original capture owner changed")
        binding = await self.control._binding(
            conn, tid, scope["binding_id"], owner, live=True, permit=True
        )
        task = await row(conn, s.tasks, tid)
        if (task.get("ctf_control") or {}).get("failure_hold") or record["desired"] != "active":
            raise HTTPException(409, "Native network admission is held")
        return owner, binding

    async def list(self, tid, key):
        async with self.repo.engine.connect() as conn:
            record, _ = await self.sandbox.host(conn, tid, key)
            views = []
            for entry in network_scopes(record).values():
                if not _same_host(record, entry):
                    continue
                retire = (
                    entry["state"] in {"sealed", "archived"}
                    or bool(entry.get("loss"))
                    or vpn_retired(record)
                )
                if not retire:
                    try:
                        await self._live(conn, tid, record, entry)
                    except HTTPException as error:
                        if error.status_code not in {403, 404, 409}:
                            raise
                        retire = True
                views.append(
                    {
                        **self._view(entry, retire=retire),
                        "can_seal": retire and bool(entry.get("ready")),
                    }
                )
            return {
                "scopes": views,
                "archive_closed": bool(record["data"].get("traffic_archive_closed")),
            }

    async def observe(self, tid, key, source, body):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            record, _ = await self.sandbox.host(conn, tid, key)
            entry = _scope(record, source)
            ready = body.model_dump(mode="json")
            if entry.get("ready") not in (None, ready) or entry.get("loss"):
                raise HTTPException(409, "Original capture process identity changed")
            if "ready" not in entry:
                entry = {**entry, "state": "ready", "ready": ready}
                await _put_scope(conn, record, entry)
            return self._view(entry)

    async def permit(self, tid, key, source):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            record, _ = await self.sandbox.host(conn, tid, key)
            entry = _scope(record, source)
            if entry["state"] != "ready" or entry.get("loss"):
                raise HTTPException(409, "Original capture is not ready for forwarding")
            owner, binding = await self._live(conn, tid, record, entry)
            now = int(datetime.now(UTC).timestamp() * 1000)
            vpn_deadline = vpn_solver_deadline(record)
            if vpn_deadline is not None and (
                entry["scope"].get("vpn_operation_id") != vpn_state(record).get("operation_id")
            ):
                raise HTTPException(409, "Original capture source VPN identity changed")
            if dynamic_vpn(record) or board_direct_policy(record):
                path = vpn_network(record) if vpn_selected(record) else default_network(record)
                if path is None or (
                    entry["scope"].get("route_policy") != path.get("route_policy")
                    or entry["scope"].get("dns_ipv4") != path.get("dns_ipv4")
                    or entry["scope"].get(
                        "vpn_network_digest" if vpn_selected(record) else "network_digest"
                    )
                    != path.get("network_digest")
                ):
                    raise HTTPException(409, "Original capture source route policy changed")
            until = min(
                vpn_deadline or now + 12000,
                now + 12000,
                int(owner["expires_at"].timestamp() * 1000),
                int(binding["permit_expires_at"].timestamp() * 1000),
                int(record["hard_deadline"].timestamp() * 1000),
                binding["business"].get("child_deadline_ms", now + 12000),
            )
            if parent_id := binding["business"].get("parent_binding_id"):
                parent = await row(conn, s.runner_bindings, tid, parent_id)
                until = min(until, int(parent["permit_expires_at"].timestamp() * 1000))
            if until - now < 8000:
                raise HTTPException(409, "Original network permission is about to expire")
            grant = {
                "source_id": source,
                "grant_revision": entry.get("grant", {}).get("grant_revision", 0) + 1,
                "issued_at_ms": now,
                "authority_until_ms": until,
            }
            await _put_scope(conn, record, {**entry, "grant": grant})
            return grant

    async def seal(self, tid, key, source, body):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            record, _ = await self.sandbox.host(conn, tid, key)
            entry = _scope(record, source)
            proof = body.model_dump(mode="json")
            if entry.get("seal") == proof:
                return self._view(entry, retire=True)
            if (
                entry["state"] != "ready"
                or record["data"].get("traffic_archive_closed")
                or entry.get("loss")
                or entry.get("ready") != {name: proof[name] for name in CaptureReady.model_fields}
            ):
                raise HTTPException(409, "Original capture seal identity changed")
            entry = {**entry, "state": "sealed", "seal": proof}
            await _put_scope(conn, record, entry)
            # The sealed source cannot reopen SDK admission. Settle an interrupted
            # pre-admit host only when the exact durable send gate was never crossed.
            calls = s.runner_calls
            pending = (
                await conn.execute(
                    select(calls).where(
                        calls.c.task_id == tid,
                        calls.c.binding_id == entry["scope"]["binding_id"],
                        calls.c.owner_epoch == entry["scope"]["epoch"],
                        calls.c.provider_key == "sandbox",
                        calls.c.state.in_(["dispatched", "unknown"]),
                        calls.c.data["network_source_id"].astext == source,
                        calls.c.data["sdk_admitted"].astext.is_distinct_from("true"),
                    )
                )
            ).mappings()
            for call in pending:
                await self.sandbox.settle_unsent(conn, tid, call)
            return self._view(entry, retire=True)

    async def _read_entry(self, tid, key, source):
        async with self.repo.engine.connect() as conn:
            record, _ = await self.sandbox.host(conn, tid, key)
            return _scope(record, source), record

    async def _write_state(self, tid, key, source, name, state):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            record, _ = await self.sandbox.host(conn, tid, key)
            entry = _scope(record, source)
            writes = dict(entry.get("writes", {}))
            if state == "claimed":
                if record["data"].get("traffic_archive_closed") or writes.get(name):
                    raise HTTPException(409, "Original capture write is closed or already claimed")
            elif (
                writes.get(name) != {"put_confirmed": "claimed", "verified": "put_confirmed"}[state]
            ):
                raise HTTPException(409, "Original capture write has no matching claim")
            writes[name] = state
            await _put_scope(conn, record, {**entry, "writes": writes})

    async def upload(self, tid, key, source, data, objects):
        async with self.repo.engine.begin() as guard:
            await _lock_archive(guard, tid)
            entry, _ = await self._read_entry(tid, key, source)
            seal = entry.get("seal")
            if (
                not seal
                or len(data) != seal["size_bytes"]
                or hashlib.sha256(data).hexdigest() != seal["sha256"]
            ):
                raise HTTPException(409, "Capture bytes do not match the original seal")
            try:
                counts = pcap_counts(data)
            except ValueError as error:
                raise HTTPException(422, "Original capture has invalid PCAP framing") from error
            if counts != (seal["packet_count"], seal["truncated_packets"]):
                raise HTTPException(409, "Capture packets do not match the original seal")
            index, ref, encoded = _index(entry)
            prefix = f"traffic/{tid}/run-{entry['run']}/native/{source}/"
            for name, target, value in (
                ("part", prefix + f"parts/{index.parts[0].id}.bin", data),
                ("index", prefix + f"indexes/{ref.revision}.json", encoded),
            ):
                state = entry.get("writes", {}).get(name)
                if state == "verified":
                    continue
                if state is None:
                    await self._write_state(tid, key, source, name, "claimed")
                    await objects.put(target, value, content_type="application/octet-stream")
                    # An equal GET alone never proves a previous ambiguous PUT stopped.
                    await self._write_state(tid, key, source, name, "put_confirmed")
                elif state != "put_confirmed":
                    raise HTTPException(409, "Original capture object writer is unconfirmed")
                try:
                    observed = await object_bytes(objects, target, len(value))
                except NativeUnavailable as error:
                    raise HTTPException(
                        409, "Original capture object write is unconfirmed"
                    ) from error
                if observed != value:
                    raise HTTPException(409, "Original capture object bytes changed")
                await self._write_state(tid, key, source, name, "verified")
            async with self.repo.engine.begin() as conn:
                await self.repo.lock(conn, tid)
                record, _ = await self.sandbox.host(conn, tid, key)
                current = _scope(record, source)
                if record["data"].get("traffic_archive_closed") and current["state"] != "archived":
                    raise HTTPException(409, "Original capture archive publication is closed")
                if current["state"] != "archived":
                    await _put_scope(
                        conn,
                        record,
                        {
                            **current,
                            "state": "archived",
                            "ref": ref.model_dump(mode="json"),
                        },
                    )
                    await self.repo.notify(tid, 0, connection=conn)
                return {"archive_confirmed": True}


def _index(entry):
    scope, seal = entry["scope"], entry["seal"]
    details = TrafficBindingScope(
        **{
            name: scope[name]
            for name in (
                "binding_id",
                "assignment_id",
                "generation",
                "epoch",
                "role",
                "parent_agent_id",
                "sandbox_pod_uid",
                "ipv4",
            )
        },
        interface=scope["host_interface"],
        **{
            name: seal[name]
            for name in (
                "netns_inode",
                "host_ifindex",
                "truncated_packets",
                "discarded_tail_bytes",
                "dropped_packets",
            )
        },
    )
    count = seal["packet_count"]
    source = scope["source_id"]
    part = TrafficNativePart(
        id=seal["sha256"],
        segment_id=source,
        sequence=1,
        offset=0,
        size_bytes=seal["size_bytes"],
        sha256=seal["sha256"],
        first_packet=1 if count else None,
        last_packet=count or None,
    )
    index = TrafficNativeSourceIndex(
        task_id=scope["task_id"],
        run=entry["run"],
        source_id=source,
        agent_id=scope["agent_id"],
        scope=details,
        complete=seal["complete"],
        error=seal["error"],
        packet_count=count,
        parts=[part],
    )
    encoded = index.model_dump_json().encode()
    ref = TrafficNativeSourceRef(
        source_id=source,
        agent_id=scope["agent_id"],
        scope=details,
        revision=hashlib.sha256(encoded).hexdigest(),
        available_bytes=part.size_bytes,
        packet_count=count,
        complete=index.complete,
    )
    return index, ref, encoded


def capture_manifest(record, task_id, run):
    entries = [entry for entry in network_scopes(record).values() if entry["run"] == run]
    refs = [
        TrafficNativeSourceRef.model_validate(entry["ref"])
        for entry in entries
        if scope_done(entry)
    ]
    partial = any(
        not entry["seal"]["complete"] if scope_done(entry) else bool(entry.get("loss"))
        for entry in entries
    )
    stopped = record["state"] == "stopped"
    partial |= stopped and any(not scope_done(entry) for entry in entries)
    status: Literal["partial", "complete", "collecting"] = (
        "partial" if partial else "complete" if stopped else "collecting"
    )
    return TrafficManifest(
        task_id=task_id,
        run=run,
        status=status,
        https="unavailable",
        started_at=record["created_at"],
        updated_at=datetime.now(UTC),
        finished_at=datetime.now(UTC) if stopped else None,
        captured_bytes=sum(ref.available_bytes for ref in refs),
        dropped_packets=sum((ref.scope.dropped_packets or 0) if ref.scope else 0 for ref in refs),
        pending_chunks=sum(not scope_done(entry) and not entry.get("loss") for entry in entries),
        error="Some original capture sources are incomplete or unconfirmed" if partial else None,
        native=refs,
        files=[
            TrafficFile(
                id=ref.source_id,
                name=f"{ref.source_id}.pcap",
                agent_id=ref.agent_id,
                source_id=ref.source_id,
                size_bytes=ref.available_bytes,
            )
            for ref in refs
        ],
        native_bound=True,
    )
