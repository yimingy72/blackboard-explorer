"""Bounded source-isolated sessions of the official Wireshark analysis engine."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from bbx_contracts.traffic import (
    TrafficNativeCoverage,
    TrafficNativeDataSource,
    TrafficNativeFrameDetail,
    TrafficNativePacket,
    TrafficNativeQueryView,
    TrafficNativeSourceIndex,
    TrafficNativeSourceRef,
    TrafficPacketNode,
    redact_traffic_text,
)

INPUT_LIMIT = 2 * 1024**3
INDEX_LIMIT = 32 * 1024**2
KEYLOG_LIMIT = 64 * 1024**2
RPC_LIMIT = 4 * 1024**2
RESULT_LIMIT = 50_000
IDLE_SECONDS = 300
FRAME_FIELDS = ("%m", "epoch", "%s", "%d", "%p", "%L", "%i")


class NativeUnavailable(Exception):
    pass


class NativeFilterError(Exception):
    pass


def own(path: Path, uid: int | None = None) -> None:
    if os.geteuid() == 0:
        owner = uid if uid is not None else 60000 + uuid4().int % 5000
        os.chown(path, owner, owner)


def process_birth(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
    except (OSError, IndexError):
        return None


def create_cache_instance() -> Path:
    base = Path(tempfile.gettempdir()) / f"bbx-native-cache-{os.geteuid()}"
    base.mkdir(mode=0o711, exist_ok=True)
    metadata = base.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise NativeUnavailable("Native cache directory ownership is invalid")
    base.chmod(0o711)
    for path in base.glob("instance-*"):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            owner = json.loads((path / "owner.json").read_text())
            pid = int(owner["pid"])
            if pid < 1:
                continue
            try:
                os.kill(pid, 0)
                alive = owner.get("birth") is None or process_birth(pid) == owner["birth"]
            except ProcessLookupError:
                alive = False
            if not alive:
                shutil.rmtree(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    instance = Path(tempfile.mkdtemp(prefix=f"instance-{os.getpid()}-", dir=base))
    instance.chmod(0o711)
    marker = instance / "owner.json"
    marker.write_text(json.dumps({"pid": os.getpid(), "birth": process_birth(os.getpid())}))
    marker.chmod(0o600)
    return instance


async def object_bytes(objects: Any, key: str, maximum: int) -> bytes:
    data = bytearray()
    source = objects.stream(key)
    try:
        try:
            async for block in source:
                data.extend(block)
                if len(data) > maximum:
                    raise NativeUnavailable("Native input exceeds its resource limit")
        finally:
            await source.aclose()
    except Exception as error:
        raise NativeUnavailable(
            "Native input is unavailable or exceeds its resource limit"
        ) from error
    return bytes(data)


class NativeWorker:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.lock = asyncio.Lock()
        self.serial = 0
        self.version = "unknown"
        self.columns: dict[str, Any] = {}
        self.expressions: set[str] = set()

    @classmethod
    async def start(cls, home: Path) -> NativeWorker:
        binary = shutil.which("sharkd")
        if binary is None or sys.platform != "linux":
            raise NativeUnavailable("Native analysis requires the Linux Wireshark worker")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "bbx_blackboard.native_worker",
            binary,
            cwd=home,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home),
                "WIRESHARK_CONFIG_DIR": str(home),
                "LC_ALL": "C",
                "TZ": "UTC",
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=RPC_LIMIT,
            start_new_session=True,
        )
        return await cls.attach(process)

    @classmethod
    async def attach(cls, process: asyncio.subprocess.Process) -> NativeWorker:
        """Initialize an already sandboxed process, also used by Docker integration fixtures."""
        worker = cls(process)
        try:
            info = await worker.rpc("info", {})
            worker.version = str(info.get("version", "unknown"))[:128]
            formats = {item["format"]: position for position, item in enumerate(info["columns"])}
            worker.columns = {
                f"column{position}": "frame.time_epoch:1" if fmt == "epoch" else formats[fmt]
                for position, fmt in enumerate(FRAME_FIELDS)
            }
            return worker
        except BaseException:
            await worker.close()
            raise

    async def rpc(self, method: str, params: dict[str, Any], timeout: float = 10) -> Any:
        # These internal setup operations never accept user paths or arbitrary RPC names.
        if method not in {"info", "status", "load", "setconf", "check", "frames", "frame"}:
            raise NativeUnavailable("Native operation is not allowed")
        async with self.lock:
            try:
                if self.process.stdin is None or self.process.stdout is None:
                    raise NativeUnavailable("Native decoder is unavailable")
                self.serial += 1
                payload = dict(jsonrpc="2.0", id=self.serial, method=method, params=params)
                self.process.stdin.write(json.dumps(payload).encode() + b"\n")
                await self.process.stdin.drain()
                response = await asyncio.wait_for(self.process.stdout.readline(), timeout)
                if not response or len(response) > RPC_LIMIT:
                    raise NativeUnavailable("Native decoder output exceeded its limit")
                decoded = json.loads(response)
                if decoded.get("id") != self.serial:
                    raise NativeUnavailable("Native decoder response is invalid")
                if "error" in decoded:
                    if method == "check":
                        raise NativeFilterError(
                            str(decoded["error"].get("message", "Invalid display filter"))[:2048]
                        )
                    code = decoded["error"].get("code")
                    raise NativeUnavailable(
                        f"Native decoder could not complete the request ({code})"
                    )
                return decoded.get("result")
            except NativeFilterError:
                raise
            except BaseException:
                await self.close()
                raise

    async def check(self, expression: str) -> None:
        await self.rpc("check", {"filter": expression})

    async def frames(self, expression: str, skip: int, limit: int) -> list[dict[str, Any]]:
        if expression not in self.expressions and len(self.expressions) >= 32:
            raise NativeUnavailable("Native filter cache limit reached; retry the query")
        self.expressions.add(expression)
        params = dict(self.columns, filter=expression, limit=limit)
        if skip:
            params["skip"] = skip
        return await self.rpc("frames", params)

    async def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
        await self.process.wait()


@dataclass
class SourceSession:
    task_id: UUID | None
    root: Path
    worker: NativeWorker
    index: TrafficNativeSourceIndex | None
    users: int = 0
    touched: float = field(default_factory=monotonic)


@dataclass
class NativeQuery:
    task_id: UUID
    run: int
    expression: str
    agent_id: str | None
    objects: Any
    id: UUID = field(default_factory=uuid4)
    status: str = "building"
    revision: str | None = None
    requested_revision: str = ""
    refs: list[TrafficNativeSourceRef] = field(default_factory=list)
    committed_refs: list[TrafficNativeSourceRef] = field(default_factory=list)
    indices: dict[str, TrafficNativeSourceIndex] = field(default_factory=dict)
    committed_indices: dict[str, TrafficNativeSourceIndex] = field(default_factory=dict)
    records: list[TrafficNativePacket] = field(default_factory=list)
    coverage: list[TrafficNativeCoverage] = field(default_factory=list)
    engine_version: str | None = None
    error: str | None = None
    truncated: bool = False
    future: asyncio.Task | None = None
    touched: float = field(default_factory=monotonic)
    last_build: float = 0
    build_seconds: float = 0
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def view(self, seen_revision: str | None = None) -> TrafficNativeQueryView:
        return TrafficNativeQueryView.model_validate(
            dict(
                id=self.id,
                run=self.run,
                status=self.status,
                revision=self.revision,
                requested_revision=self.requested_revision,
                display_filter=self.expression,
                engine_version=self.engine_version,
                coverage=self.coverage,
                matched_packets=len(self.records),
                results_truncated=self.truncated,
                reset_required=bool(
                    seen_revision and self.revision and seen_revision != self.revision
                ),
                error=self.error,
            )
        )


class NativeAnalysisCache:
    def __init__(
        self, worker_factory: Callable[[Path], Awaitable[NativeWorker]] | None = None
    ) -> None:
        self.queries: OrderedDict[UUID, NativeQuery] = OrderedDict()
        self.sessions: OrderedDict[str, SourceSession] = OrderedDict()
        self.pool_lock = asyncio.Lock()
        self.build_lock = asyncio.Lock()
        self.sweeper: asyncio.Task | None = None
        self.worker_factory = worker_factory or NativeWorker.start
        self.part_root: Path | None = None
        self.parts: OrderedDict[str, tuple[UUID | None, Path, int]] = OrderedDict()
        self.part_bytes = 0
        self.instance: Path | None = None
        self.init_lock = asyncio.Lock()

    async def initialize(self) -> Path:
        async with self.init_lock:
            if self.instance is None:
                self.instance = await asyncio.to_thread(create_cache_instance)
            return self.instance

    def query(self, query_id: UUID, task_id: UUID) -> NativeQuery:
        query = self.queries.get(query_id)
        if query is None or query.task_id != task_id:
            raise KeyError(query_id)
        query.touched = monotonic()
        self.queries.move_to_end(query_id)
        return query

    def create(
        self, task_id: UUID, run: int, expression: str, agent_id: str | None, objects: Any
    ) -> NativeQuery:
        for query in self.queries.values():
            if (query.task_id, query.run, query.expression, query.agent_id) == (
                task_id,
                run,
                expression,
                agent_id,
            ):
                query.touched = monotonic()
                return query
        if len(self.queries) >= 8:
            removable = next(
                (
                    qid
                    for qid, item in self.queries.items()
                    if item.future is None or item.future.done()
                ),
                None,
            )
            if removable is None:
                raise NativeUnavailable("Native analysis queue is full")
            del self.queries[removable]
        query = NativeQuery(task_id, run, expression, agent_id, objects)
        self.queries[query.id] = query
        if self.sweeper is None:
            self.sweeper = asyncio.create_task(self._sweep())
        return query

    async def refresh(self, query: NativeQuery, refs: list[TrafficNativeSourceRef]) -> None:
        try:
            async with query.refresh_lock:
                await self._refresh(query, refs)
        except NativeUnavailable:
            raise
        except Exception as error:
            raise NativeUnavailable("Native source index is unavailable or invalid") from error

    async def _refresh(self, query: NativeQuery, refs: list[TrafficNativeSourceRef]) -> None:
        query.touched = monotonic()
        previous_refs = {ref.source_id: ref.revision for ref in query.refs}
        selected_refs = [
            ref for ref in refs if query.agent_id is None or ref.agent_id == query.agent_id
        ]
        selected_refs.sort(key=lambda ref: ref.source_id)
        revision = hashlib.sha256(
            json.dumps(
                [ref.model_dump(mode="json") for ref in selected_refs], sort_keys=True
            ).encode()
        ).hexdigest()
        if revision != query.requested_revision:
            indices = {}
            for ref in selected_refs:
                known = query.indices.get(ref.source_id)
                previous_ref = next(
                    (
                        item
                        for item in query.committed_refs
                        if item.source_id == ref.source_id and item.revision == ref.revision
                    ),
                    None,
                )
                if previous_ref is not None:
                    indices[ref.source_id] = query.committed_indices[ref.source_id]
                elif known is not None and previous_refs.get(ref.source_id) == ref.revision:
                    indices[ref.source_id] = known
                else:
                    # Pin immediately: the writer may collect old immutable index revisions later.
                    indices[ref.source_id] = await self._source_index(
                        query.objects, query.task_id, query.run, ref
                    )
            query.indices = indices
        query.refs = selected_refs
        query.requested_revision = revision
        if revision == query.revision:
            return
        if query.future is None or query.future.done():
            query.status = "building"
            query.future = asyncio.create_task(self._build(query))

    @asynccontextmanager
    async def _lease(
        self,
        key: str,
        task_id: UUID | None,
        objects: Any = None,
        run: int = 1,
        ref: TrafficNativeSourceRef | None = None,
        pinned_index: TrafficNativeSourceIndex | None = None,
        expression: str | None = None,
    ):
        async with self.pool_lock:
            session = self.sessions.get(key)
            if (
                session is not None
                and expression is not None
                and (
                    expression not in getattr(session.worker, "expressions", set())
                    and len(getattr(session.worker, "expressions", set())) >= 32
                )
            ):
                if session.users:
                    raise NativeUnavailable("Native decoder workers are busy")
                await self._remove_session(key)
                session = None
            if session is not None and session.worker.process.returncode is not None:
                await session.worker.close()
                shutil.rmtree(session.root, ignore_errors=True)
                del self.sessions[key]
                session = None
            if session is None:
                incoming_bytes = (
                    0
                    if pinned_index is None
                    else sum(
                        part.size_bytes
                        for part in [*pinned_index.parts, *pinned_index.keylog_parts]
                    )
                )

                def disk_bytes() -> int:
                    return sum(
                        sum(
                            part.size_bytes
                            for part in [*item.index.parts, *item.index.keylog_parts]
                        )
                        for item in self.sessions.values()
                        if item.index
                    )

                while self.sessions and (
                    len(self.sessions) >= 2
                    or disk_bytes()
                    + incoming_bytes
                    + min(INPUT_LIMIT, self.part_bytes + incoming_bytes)
                    > 4 * 1024**3
                ):
                    victim = next(
                        (item for item in self.sessions if not self.sessions[item].users), None
                    )
                    if victim is None:
                        raise NativeUnavailable("Native decoder workers are busy")
                    await self._remove_session(victim)
                if (
                    incoming_bytes + min(INPUT_LIMIT, self.part_bytes + incoming_bytes)
                    > 4 * 1024**3
                ):
                    raise NativeUnavailable("Native analysis disk cache limit reached")
                instance = await self.initialize()
                root = Path(tempfile.mkdtemp(prefix="session-", dir=instance))
                own(root)
                worker = None
                try:
                    index = (
                        await self._materialize(root, objects, task_id, run, ref, pinned_index)
                        if ref
                        else None
                    )
                    worker = await self.worker_factory(root)
                    if index is not None:
                        if index.keylog_parts:
                            await worker.rpc(
                                "setconf",
                                {"name": "tls.keylog_file", "value": str(root / "keys.log")},
                            )
                        result = await worker.rpc(
                            "load", {"file": str(root / "capture.pcapng")}, timeout=60
                        )
                        if result.get("status") != "OK":
                            raise NativeUnavailable("Native capture could not be loaded completely")
                        status = await worker.rpc("status", {})
                        if status.get("frames") != index.packet_count:
                            raise NativeUnavailable(
                                "Native packet count does not match its source index"
                            )
                    session = SourceSession(task_id, root, worker, index)
                    self.sessions[key] = session
                except BaseException:
                    if worker is not None:
                        await worker.close()
                    shutil.rmtree(root, ignore_errors=True)
                    raise
            session.users += 1
            self.sessions.move_to_end(key)
        try:
            yield session
        finally:
            session.users -= 1
            session.touched = monotonic()

    async def _source_index(
        self, objects: Any, task_id: UUID | None, run: int, ref: TrafficNativeSourceRef
    ) -> TrafficNativeSourceIndex:
        prefix = f"traffic/{task_id}/run-{run}/native/"
        data = await object_bytes(
            objects, prefix + f"{ref.source_id}/indexes/{ref.revision}.json", INDEX_LIMIT
        )
        if hashlib.sha256(data).hexdigest() != ref.revision:
            raise NativeUnavailable("Native source index digest does not match")
        index = await asyncio.to_thread(TrafficNativeSourceIndex.model_validate_json, data)
        if (
            index.task_id,
            index.run,
            index.source_id,
            index.agent_id,
            index.packet_count,
            index.complete,
        ) != (task_id, run, ref.source_id, ref.agent_id, ref.packet_count, ref.complete) or sum(
            part.size_bytes for part in index.parts
        ) != ref.available_bytes:
            raise NativeUnavailable("Native source index ownership or coverage is inconsistent")
        if not index.parts or ref.available_bytes > INPUT_LIMIT:
            raise NativeUnavailable("Native source input is empty or exceeds its resource limit")
        return index

    async def _materialize(
        self,
        root: Path,
        objects: Any,
        task_id: UUID | None,
        run: int,
        ref: TrafficNativeSourceRef,
        pinned_index: TrafficNativeSourceIndex | None = None,
    ) -> TrafficNativeSourceIndex:
        index = pinned_index or await self._source_index(objects, task_id, run, ref)
        prefix = f"traffic/{task_id}/run-{run}/native/"
        for filename, parts, maximum in (
            ("capture.pcapng", index.parts, INPUT_LIMIT),
            ("keys.log", index.keylog_parts, KEYLOG_LIMIT),
        ):
            total = 0
            path = root / filename
            with path.open("xb") as target:
                path.chmod(0o600)
                for part in parts:
                    key = prefix + (
                        f"{ref.source_id}/parts/{part.id}.bin"
                        if filename == "capture.pcapng"
                        else f"keylog/{part.id}.log"
                    )
                    content = await self._part_bytes(
                        objects, key, part.sha256, part.size_bytes, task_id
                    )
                    total += len(content)
                    if (
                        total > maximum
                        or len(content) != part.size_bytes
                        or hashlib.sha256(content).hexdigest() != part.sha256
                    ):
                        raise NativeUnavailable(
                            "Native input is incomplete or exceeds its resource limit"
                        )
                    if filename == "keys.log" and not content.endswith(b"\n"):
                        raise NativeUnavailable("Native keylog input ends inside a line")
                    await asyncio.to_thread(target.write, content)
            own(path, root.stat().st_uid)
        return index

    async def _part_bytes(
        self, objects: Any, key: str, digest: str, size: int, task_id: UUID | None
    ) -> bytes:
        cache_key = key + ":" + digest
        saved = self.parts.get(cache_key)
        if saved is not None:
            self.parts.move_to_end(cache_key)
            return await asyncio.to_thread(saved[1].read_bytes)
        content = await object_bytes(objects, key, size)
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise NativeUnavailable("Native input digest does not match")
        if self.part_root is None:
            self.part_root = (await self.initialize()) / "parts"
            self.part_root.mkdir(mode=0o700)
        while self.parts and self.part_bytes + size > INPUT_LIMIT:
            _, (_, path, count) = self.parts.popitem(last=False)
            path.unlink(missing_ok=True)
            self.part_bytes -= count
        path = self.part_root / hashlib.sha256(cache_key.encode()).hexdigest()
        await asyncio.to_thread(path.write_bytes, content)
        path.chmod(0o600)
        self.parts[cache_key] = (task_id, path, size)
        self.part_bytes += size
        return content

    async def check(self, expression: str) -> str:
        async with self._lease("checker", None) as session:
            await session.worker.check(expression)
            return session.worker.version

    async def _build(self, query: NativeQuery) -> None:
        started = monotonic()
        work_started = None
        try:
            await asyncio.sleep(
                max(0, query.last_build + max(4, 2 * query.build_seconds) - started)
            )
            async with self.build_lock:
                work_started = monotonic()
                refs = list(query.refs)
                indices = dict(query.indices)
                revision = query.requested_revision
                if not refs:
                    raise NativeUnavailable(
                        "Native input was not recorded for this task run or Agent"
                    )
                if sum(ref.available_bytes for ref in refs) > INPUT_LIMIT:
                    raise NativeUnavailable("Native analysis input exceeds 2 GiB")
                records, coverage = [], []
                truncated = False
                for ref in refs:
                    key = f"{query.task_id}:{query.run}:{ref.source_id}:{ref.revision}"
                    async with self._lease(
                        key,
                        query.task_id,
                        query.objects,
                        query.run,
                        ref,
                        indices[ref.source_id],
                        query.expression,
                    ) as session:
                        await session.worker.check(query.expression)
                        query.engine_version = session.worker.version
                        source_count = 0
                        while len(records) < RESULT_LIMIT:
                            page = await session.worker.frames(
                                query.expression,
                                source_count,
                                min(200, RESULT_LIMIT - len(records)),
                            )
                            for row in page:
                                cells = [redact_traffic_text(str(cell), 4096) for cell in row["c"]]
                                if len(cells) != 7:
                                    raise NativeUnavailable("Native packet columns are unavailable")
                                records.append(
                                    TrafficNativePacket(
                                        source_id=ref.source_id,
                                        source_number=int(row["num"]),
                                        agent_id=ref.agent_id,
                                        attribution="exact" if ref.agent_id else "shared",
                                        columns=cells,
                                        background=row.get("bg"),
                                        foreground=row.get("fg"),
                                    )
                                )
                            source_count += len(page)
                            if len(page) < 200:
                                break
                        coverage.append(
                            TrafficNativeCoverage(
                                source_id=ref.source_id,
                                agent_id=ref.agent_id,
                                packet_count=ref.packet_count,
                                available_bytes=ref.available_bytes,
                                complete=ref.complete,
                                decryption="available"
                                if session.index and session.index.keylog_parts
                                else "unavailable",
                                error=session.index.error if session.index else None,
                            )
                        )
                    if len(records) >= RESULT_LIMIT:
                        truncated = True
                        break
                records.sort(
                    key=lambda row: (Decimal(row.columns[1]), row.source_id, row.source_number)
                )
                query.records, query.coverage, query.committed_refs = records, coverage, refs
                query.committed_indices = indices
                query.revision, query.truncated = revision, truncated
                query.status = (
                    "partial" if truncated or any(item.error for item in coverage) else "ready"
                )
                query.error = (
                    "Native result limit reached; narrow the display filter" if truncated else None
                )
        except asyncio.CancelledError:
            raise
        except NativeFilterError as error:
            query.status, query.error = "error", str(error)
        except Exception:
            query.status, query.error = (
                "error",
                "Native analysis input is unavailable or a resource limit was reached",
            )
        finally:
            query.last_build = monotonic()
            query.build_seconds = 0 if work_started is None else monotonic() - work_started

    async def frame(
        self, query: NativeQuery, source_id: str, number: int, revision: str
    ) -> TrafficNativeFrameDetail:
        if query.revision != revision:
            raise KeyError("revision")
        ref = next((ref for ref in query.committed_refs if ref.source_id == source_id), None)
        if ref is None or not 1 <= number <= ref.packet_count:
            raise KeyError("frame")
        key = f"{query.task_id}:{query.run}:{source_id}:{ref.revision}"
        index = query.committed_indices[source_id]
        async with self._lease(key, query.task_id, query.objects, query.run, ref, index) as session:
            native = await session.worker.rpc(
                "frame", {"frame": number, "proto": True, "bytes": True}
            )
            columns = await session.worker.frames("", number - 1, 1)
            if not columns:
                raise NativeUnavailable("Native frame columns are unavailable")
            return native_detail(native, columns[0]["c"], ref, number, revision)

    async def _remove_session(self, key: str) -> None:
        session = self.sessions.pop(key)
        await session.worker.close()
        shutil.rmtree(session.root, ignore_errors=True)

    async def invalidate_task(self, task_id: UUID) -> None:
        futures = []
        for query_id, query in list(self.queries.items()):
            if query.task_id == task_id:
                if query.future is not None:
                    query.future.cancel()
                    futures.append(query.future)
                del self.queries[query_id]
        await asyncio.gather(*futures, return_exceptions=True)
        async with self.pool_lock:
            for key in [
                key
                for key, session in self.sessions.items()
                if session.task_id == task_id or key == "checker"
            ]:
                await self._remove_session(key)
            for key, (owner, path, size) in list(self.parts.items()):
                if owner == task_id:
                    path.unlink(missing_ok=True)
                    self.part_bytes -= size
                    del self.parts[key]

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(30)
            for query_id, query in list(self.queries.items()):
                if monotonic() - query.touched > IDLE_SECONDS and (
                    query.future is None or query.future.done()
                ):
                    del self.queries[query_id]
            async with self.pool_lock:
                for key in [
                    key
                    for key, session in self.sessions.items()
                    if not session.users and monotonic() - session.touched > IDLE_SECONDS
                ]:
                    await self._remove_session(key)

    async def close(self) -> None:
        if self.sweeper is not None:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        for task_id in {query.task_id for query in self.queries.values()}:
            await self.invalidate_task(task_id)
        async with self.pool_lock:
            for key in list(self.sessions):
                await self._remove_session(key)
            if self.part_root is not None:
                shutil.rmtree(self.part_root, ignore_errors=True)
            if self.instance is not None:
                shutil.rmtree(self.instance, ignore_errors=True)
            self.parts.clear()
            self.part_bytes = 0


def native_detail(
    native: dict[str, Any],
    columns: list[Any],
    ref: TrafficNativeSourceRef,
    number: int,
    revision: str,
) -> TrafficNativeFrameDetail:
    remaining, count, truncated = 512 * 1024, 0, False

    def nodes(items: list[dict[str, Any]], depth: int = 1) -> list[TrafficPacketNode]:
        nonlocal remaining, count, truncated
        result = []
        for item in items:
            if count >= 2048 or depth > 24 or remaining < 256:
                truncated = True
                break
            name = str(item.get("fn", "field"))[:256] or "field"
            native_label = str(item.get("l", ""))
            label = redact_traffic_text(native_label, 4096)
            truncated |= len(native_label) > 4096
            remaining -= len(name.encode()) + len(label.encode())
            count += 1
            location = item.get("h", [])
            valid = len(location) == 2 and all(
                isinstance(value, int) and value >= 0 for value in location
            )
            result.append(
                TrafficPacketNode(
                    name=name,
                    label=label,
                    offset=location[0] if valid else None,
                    length=location[1] if valid else None,
                    data_source_id=int(item.get("ds", 0)),
                    filter_expr=str(item["f"])
                    if item.get("f") and len(str(item["f"])) <= 4096 and label == native_label
                    else None,
                    generated=bool(item.get("g")),
                    severity=item.get("s"),
                    children=nodes(item.get("n", []), depth + 1),
                )
            )
            if len(result) >= 512:
                truncated |= len(items) > len(result)
                break
        return result

    layers = nodes(native.get("tree", []))
    sources = []
    data = [{"name": "Frame", "bytes": native.get("bytes", "")}]
    data.extend(native.get("ds", []))
    for index, source in enumerate(data[:16]):
        raw = base64.b64decode(source.get("bytes", ""), validate=True)
        sources.append(
            TrafficNativeDataSource(
                id=index,
                name=str(source.get("name", f"Data source {index}"))[:256],
                length=len(raw),
                raw_hex=raw[:16384].hex(),
                truncated=len(raw) > 16384,
            )
        )
    result = TrafficNativeFrameDetail(
        source_id=ref.source_id,
        source_number=number,
        agent_id=ref.agent_id,
        revision=revision,
        columns=[redact_traffic_text(str(cell), 4096) for cell in columns],
        layers=layers,
        data_sources=sources,
        tree_truncated=truncated,
    )
    if len(result.model_dump_json().encode()) > 2 * 1024**2:
        raise NativeUnavailable("Native frame detail exceeds its output limit")
    return result
