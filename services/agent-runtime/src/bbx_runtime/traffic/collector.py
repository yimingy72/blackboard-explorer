"""Upload sealed capture fragments before publishing their task/run manifest."""

from __future__ import annotations

import asyncio
import copy
import gzip
import hashlib
import json
import logging
import re
import tempfile
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import uuid4

from bbx_contracts.traffic import TrafficManifest, TrafficRecordDetail, TrafficRecordSummary

from bbx_runtime.clients.envd import EnvdClient
from bbx_runtime.traffic import redaction
from bbx_runtime.traffic.native import NativeArchive, NativeUpdate

LOGGER = logging.getLogger(__name__)
BATCH_BYTES = 1024 * 1024
TICK_BATCHES = 8
TICK_BYTES = 8 * BATCH_BYTES
IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,80}")


def now() -> str:
    return datetime.now(UTC).isoformat()


def prepare_records(records: list[dict[str, Any]], seq: int) -> list[dict[str, Any]]:
    details = []
    for raw in records:
        seq += 1
        record = dict(raw)
        record.update(id=f"r-{seq}", seq=seq)
        record["url"] = redaction.url(str(record["url"])) if record.get("url") else None
        record["summary"] = redaction.text(str(record.get("summary", "")))
        record["request"] = redaction.side(record.get("request"))
        record["response"] = redaction.side(record.get("response"))
        record = TrafficRecordDetail.model_validate(record).model_dump(mode="json")
        details.append(record)
    return details


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def encode_bundle(details: list[dict[str, Any]]) -> bytes:
    # Optional tree fields can add many nulls; readers restore their schema defaults.
    return encode(
        [
            TrafficRecordDetail.model_validate(record).model_dump(
                mode="json", exclude_defaults=True
            )
            for record in details
        ]
    )


class TrafficCollector:
    def __init__(
        self,
        envd: EnvdClient,
        objects: Any,
        task_id: str,
        run: int,
        *,
        poll_seconds: float = 1,
        max_bytes: int = 2 * 1024**3,
    ) -> None:
        self.envd, self.objects, self.task_id, self.run = envd, objects, task_id, run
        self.prefix = f"traffic/{task_id}/run-{run}/"
        self.poll_seconds, self.max_bytes = poll_seconds, max_bytes
        self.manifest: dict[str, Any] = {}
        self.lock = asyncio.Lock()
        self.artifact_lock = asyncio.Lock()
        self.native_lock = asyncio.Lock()
        self.worker: asyncio.Task | None = None
        self.pcap_worker: asyncio.Task | None = None
        self.native_worker: asyncio.Task | None = None
        self.native_archive: NativeArchive | None = None
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self.pending: list[dict[str, Any]] = []
        self.dropped_observations = 0
        self.source_id = ""
        self.sources: dict[str, dict[str, int]] = {}
        self.stopped = False
        self.limited = False
        self.limit_attempts = 0
        self.discarded = False
        self.error: str | None = None

    async def start(self) -> None:
        status = await self.envd.traffic("start", task_id=self.task_id, run_number=self.run)
        self.source_id = str(status.get("source_id", ""))
        if not IDENTIFIER.fullmatch(self.source_id):
            raise ValueError("Capture lacks a valid source identity")
        if await self.objects.exists(self.prefix + "manifest.json"):
            self.manifest = json.loads(await self.objects.get(self.prefix + "manifest.json"))
            TrafficManifest.model_validate(self.manifest)
            self.error = self.manifest.get("error")
            if self.manifest["status"] == "collecting":
                self.error = "Runtime restarted during capture; logical observations may be missing"
            self.manifest["finished_at"] = None
            if await self.objects.exists(self.prefix + "checkpoint.json"):
                checkpoint = json.loads(await self.objects.get(self.prefix + "checkpoint.json"))
                self.sources = checkpoint.get("sources", {})
                self.dropped_observations = int(checkpoint.get("logical_dropped", 0))
            if self.manifest.get("task_id") != self.task_id or self.manifest.get("run") != self.run:
                raise ValueError("Traffic manifest identity mismatch")
        else:
            self.manifest = {
                "schema_version": 1,
                "task_id": self.task_id,
                "run": self.run,
                "status": "collecting",
                "https": status.get("https", "unavailable"),
                "started_at": status.get("started_at") or now(),
                "updated_at": now(),
                "finished_at": None,
                "record_count": 0,
                "captured_bytes": 0,
                "dropped_packets": 0,
                "error": None,
                "chunks": [],
                "files": [],
            }
        if status.get("native_enabled") is True:
            self.native_archive = NativeArchive(self.envd, self.objects, self.task_id, self.run)
            await self.native_archive.restore(self.manifest.get("native", []))
        self._status(status)
        if self.manifest["captured_bytes"] >= self.max_bytes:
            await self._limit()
        await self._publish()
        from bbx_runtime.traffic.observation import register

        register(self)
        self.worker = asyncio.create_task(self._background(), name=f"traffic:{self.task_id}")
        self.pcap_worker = asyncio.create_task(
            self._background(channel="pcap"), name=f"traffic-pcap:{self.task_id}"
        )
        if self.native_archive is not None:
            self.native_worker = asyncio.create_task(
                self._background(channel="native"), name=f"traffic-native:{self.task_id}"
            )

    def enqueue(self, record: dict[str, Any]) -> None:
        if self.discarded:
            return
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped_observations += 1
            self.error = self.error or "Logical observation queue capacity exceeded"

    def _status(self, status: dict[str, Any]) -> None:
        if (
            status.get("source_id", self.source_id) != self.source_id
            or status.get("task_id", self.task_id) != self.task_id
            or status.get("run", status.get("run_number", self.run)) != self.run
        ):
            raise ValueError("Traffic source ownership changed")
        if status.get("capturing") is False:
            self.limited = True
        self.manifest["https"] = status.get("https", self.manifest.get("https", "unavailable"))
        # Source counters can reset after a container replacement; preserve each source total.
        sources = self.sources
        sources[self.source_id] = {
            "captured_bytes": int(status.get("captured_bytes", 0)),
            "dropped_packets": int(status.get("dropped_packets", 0)),
            "preview_dropped": int(status.get("preview_dropped", 0)),
        }
        self.manifest["captured_bytes"] = sum(s["captured_bytes"] for s in sources.values())
        self.manifest["dropped_packets"] = sum(s["dropped_packets"] for s in sources.values())
        self.manifest["preview_dropped"] = self.dropped_observations + sum(
            s.get("preview_dropped", 0) for s in sources.values()
        )
        if status.get("last_packet_at"):
            times = [
                stamp
                for stamp in (self.manifest.get("last_packet_at"), status["last_packet_at"])
                if stamp
            ]
            self.manifest["last_packet_at"] = max(
                times, key=lambda stamp: datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            )
        if (
            status.get("error")
            or status.get("status") == "error"
            or (
                status.get("status") == "partial"
                and not status.get("preview_dropped")
                and not status.get("dropped_packets")
            )
        ):
            self.error = redaction.text(str(status.get("error") or "Capture incomplete"))
        if self.manifest["captured_bytes"] >= self.max_bytes and not self.error:
            self.error = "Traffic storage capacity exceeded"
        self.manifest["error"] = self.error
        self.manifest["status"] = (
            "partial"
            if (
                self.error
                or self.manifest["preview_dropped"]
                or self.manifest["dropped_packets"]
                or self.manifest["https"] != "enabled"
            )
            else "collecting"
        )

    async def _limit(self) -> None:
        if self.limited:
            return
        if self.limit_attempts >= 3:
            self.error = "Traffic capacity limit unconfirmed; source retained"
            self.manifest.update(status="error", error=self.error)
            raise RuntimeError("Traffic capacity limit could not be confirmed")
        self.limit_attempts += 1
        try:
            status = await self.envd.traffic("limit")
            self._status(status)
            if status.get("capturing") is not False:
                raise ValueError("Traffic capacity stop is unconfirmed")
        except Exception:
            self.error = "Traffic capacity limit unconfirmed; source retained"
            self.manifest.update(status="error", error=self.error)
            raise
        self.limited = True

    async def _publish(self) -> None:
        self.manifest["updated_at"] = now()
        validated = TrafficManifest.model_validate(self.manifest)
        self.manifest = validated.model_dump(mode="json")
        await self.objects.put(
            self.prefix + "checkpoint.json",
            json.dumps(
                {"sources": self.sources, "logical_dropped": self.dropped_observations}
            ).encode(),
            content_type="application/json",
        )
        await self.objects.put(
            self.prefix + "manifest.json",
            json.dumps(self.manifest, ensure_ascii=False).encode(),
            content_type="application/json",
        )

    def _last_seq(self) -> int:
        return max(
            int(self.manifest.get("last_seq", 0)),
            max((chunk["last_seq"] for chunk in self.manifest["chunks"]), default=0),
        )

    async def _records(
        self, records: list[dict[str, Any]], source: str, local: str, *, prepared: bool = False
    ) -> None:
        """Stage immutable dependencies; the caller commits the whole tick before ACK."""
        if not records:
            return
        seq = self._last_seq()
        bundled = any(record.get("record_kind") == "packet" for record in records)
        if bundled and len(records) > 128:
            raise ValueError("Packet batch exceeds its record limit")
        details = records if prepared else await asyncio.to_thread(prepare_records, records, seq)
        if any(record["attribution"] == "unknown" for record in details):
            self.error = self.error or "Some captured traffic has no exact Agent attribution"
        if self.error:
            self.manifest.update(status="partial", error=self.error)
        seq = details[-1]["seq"]
        identifier = hashlib.sha256(f"{source}:{local}".encode()).hexdigest()[:32]
        if bundled:
            encoded = await asyncio.to_thread(encode_bundle, details)
            if len(encoded) > BATCH_BYTES:
                raise ValueError("Packet detail bundle exceeds 1 MiB")
            await self.objects.put(
                self.prefix + f"detail-chunks/{identifier}.json.gz",
                await asyncio.to_thread(gzip.compress, encoded),
                content_type="application/gzip",
            )
        else:
            for record in details:
                await self.objects.put(
                    self.prefix + f"records/{record['id']}.json.gz",
                    gzip.compress(encode(record)),
                    content_type="application/gzip",
                )
        summaries = [
            {key: record[key] for key in TrafficRecordSummary.model_fields} for record in details
        ]
        encoded = await asyncio.to_thread(encode, summaries)
        await self.objects.put(
            self.prefix + f"chunks/{identifier}.json.gz",
            await asyncio.to_thread(gzip.compress, encoded),
            content_type="application/gzip",
        )
        self.manifest["chunks"].append(
            {
                "id": identifier,
                "first_seq": summaries[0]["seq"],
                "last_seq": seq,
                "count": len(summaries),
                "source_id": source,
                "local_chunk_id": local,
                "detail_storage": "chunk" if bundled else "record",
                "agent_ids": sorted(
                    {record.get("agent_id") for record in summaries}, key=lambda value: value or ""
                ),
            }
        )
        self.manifest["record_count"] += len(summaries)
        self.manifest["last_seq"] = seq

    async def _chunk(self, item: dict[str, Any]) -> int:
        identifier = str(item["id"])
        if not IDENTIFIER.fullmatch(identifier):
            raise ValueError("Unsafe capture chunk identity")
        if any(
            c.get("source_id") == self.source_id and c.get("local_chunk_id") == identifier
            for c in self.manifest["chunks"]
        ):
            return 0
        content = bytearray()
        async with self.envd.traffic_stream("chunks", identifier) as response:
            async for part in response.aiter_bytes(chunk_size=65536):
                content.extend(part)
                if len(content) > BATCH_BYTES:
                    raise ValueError("Capture metadata chunk exceeds 1 MiB")
        if item.get("sha256") and hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("Capture metadata chunk digest mismatch")
        records = await asyncio.to_thread(
            lambda: [json.loads(line) for line in content.splitlines() if line]
        )
        if len(records) > 128:
            raise ValueError("Capture metadata chunk exceeds 128 records")
        if item.get("count") is not None and len(records) != item["count"]:
            raise ValueError("Capture metadata chunk count mismatch")
        await self._records(records, self.source_id, identifier)
        return len(content)

    async def _artifact(self, item: dict[str, Any]) -> None:
        local = str(item["id"])
        if not IDENTIFIER.fullmatch(local):
            raise ValueError("Unsafe capture artifact identity")
        identifier = hashlib.sha256(f"{self.source_id}:{local}".encode()).hexdigest()[:32]
        if not any(file["id"] == identifier for file in self.manifest["files"]):
            size, digest = 0, hashlib.sha256()
            with tempfile.TemporaryFile() as output:
                async with self.envd.traffic_stream("artifacts", local) as response:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise ValueError("Capture packet fragment exceeds limit")
                        digest.update(chunk)
                        await asyncio.to_thread(output.write, chunk)
                if size != int(item["size_bytes"]):
                    raise ValueError("Capture packet fragment size mismatch")
                if item.get("sha256") and digest.hexdigest() != item["sha256"]:
                    raise ValueError("Capture packet fragment digest mismatch")
                await asyncio.to_thread(output.seek, 0)
                await self.objects.put(
                    self.prefix + f"pcap/{identifier}",
                    output,
                    length=size,
                    content_type="application/vnd.tcpdump.pcap",
                )
            async with self.lock:
                if not any(file["id"] == identifier for file in self.manifest["files"]):
                    self.manifest["files"].append(
                        {
                            "id": identifier,
                            "kind": "pcap",
                            "name": str(item.get("name", local))[:255],
                            "size_bytes": size,
                            "content_type": "application/vnd.tcpdump.pcap",
                            "agent_id": item.get("agent_id"),
                            "source_id": item.get("source_id") or self.source_id,
                        }
                    )
                await self._publish()
        else:
            async with self.lock:
                await self._publish()
        await self.envd.traffic("ack", chunks=[], artifacts=[local])

    async def _observations(self) -> list[dict[str, Any]]:
        while not self.queue.empty() and len(self.pending) < 128:
            self.pending.append(self.queue.get_nowait())
        normalized = await asyncio.to_thread(prepare_records, list(self.pending), self._last_seq())
        size, selected = 2, []
        for record in normalized:
            length = len(encode(record)) + 1
            if length > BATCH_BYTES - 2:
                raise ValueError("Logical observation exceeds batch capacity")
            if size + length > BATCH_BYTES:
                break
            selected.append(record)
            size += length
        return selected

    async def _metadata_tick(self, *, stop: bool = False) -> int:
        async with self.lock:
            status = await self.envd.traffic("stop" if stop else "seal")
            if stop:
                self.stopped = True
            self._status(status)
            if not self.stopped and self.manifest["captured_bytes"] >= self.max_bytes:
                await self._limit()
            listed = await self.envd.traffic("chunks", limit=256)
            items = sorted(listed.get("items", []), key=lambda item: item.get("first_seq", 0))
            previous = copy.deepcopy(self.manifest)
            acknowledged: list[str] = []
            used = 0
            has_observations = bool(self.pending or not self.queue.empty())
            observations: list[dict[str, Any]] = []
            slots = TICK_BATCHES - has_observations
            try:
                for item in items[:slots]:
                    advertised = int(item.get("size_bytes", 0))
                    if advertised > BATCH_BYTES:
                        raise ValueError("Capture metadata chunk exceeds 1 MiB")
                    if used + advertised > TICK_BYTES - (BATCH_BYTES if has_observations else 0):
                        break
                    used += await self._chunk(item)
                    acknowledged.append(str(item["id"]))
                if has_observations:
                    observations = await self._observations()
                    await self._records(observations, "runtime", uuid4().hex, prepared=True)
                remaining = max(0, int(listed.get("total_items", len(items))) - len(acknowledged))
                self.manifest["pending_chunks"] = remaining
                await self._publish()
            except BaseException:
                self.manifest = previous
                raise
            # Source deletion and queue advancement happen only after a confirmed commit.
            del self.pending[: len(observations)]
            if acknowledged:
                await self.envd.traffic("ack", chunks=acknowledged, artifacts=[])
            return remaining

    async def _artifacts_tick(self) -> None:
        async with self.artifact_lock:
            artifacts = await self.envd.traffic("artifacts", limit=256)
            for item in artifacts.get("items", []):
                await self._artifact(item)

    async def _native_error(self, reason: str) -> None:
        self.error = reason
        async with self.lock:
            self.manifest.update(status="error", error=reason)
            try:
                await self._publish()
            except Exception:
                pass

    async def _native_tick(self) -> NativeUpdate | None:
        try:
            return await self._native_tick_inner()
        except Exception as error:
            await self._native_error(
                f"Native traffic persistence failed ({type(error).__name__}); retry pending"
            )
            raise

    async def _native_tick_inner(self) -> NativeUpdate | None:
        if self.native_archive is None:
            return None
        async with self.native_lock:
            update = await self.native_archive.stage()
            async with self.lock:
                previous = self.manifest.get("native", [])
                self.manifest["native"] = update.refs
                if any(index.error for index in update.indices.values()):
                    self.error = "Native capture source reported incomplete input"
                    self.manifest.update(status="partial", error=self.error)
                try:
                    await self._publish()
                except BaseException:
                    self.manifest["native"] = previous
                    raise
                self.native_archive.confirm(update)
            if update.acknowledgements:
                await self.envd.native_ack(update.acknowledgements)
            await self.native_archive.collect_indices()
            return update

    async def _drain_native(self) -> None:
        update = await self._native_tick()
        while update is not None and update.pending:
            if not update.progress:
                await self._native_error(
                    "Native capture prefix cannot advance; environment retained"
                )
                raise RuntimeError("Native capture prefix cannot advance; environment retained")
            update = await self._native_tick()
        if update is not None and not update.complete:
            await self._native_error(
                "Native capture final input is unconfirmed; environment retained"
            )
            raise RuntimeError("Native capture final input is unconfirmed; environment retained")

    async def flush(self, *, stop: bool = False) -> None:
        remaining = await self._metadata_tick(stop=stop)
        if stop:
            while remaining or self.pending or not self.queue.empty():
                remaining = await self._metadata_tick()
        if stop:
            await self._drain_native()
        elif self.native_archive is not None:
            await self._native_tick()
        await self._artifacts_tick()
        if stop:
            async with self.lock:
                self.manifest["finished_at"] = now()
                self.manifest["status"] = (
                    "partial"
                    if self.error
                    or self.manifest["dropped_packets"]
                    or self.manifest.get("preview_dropped")
                    or self.manifest["https"] != "enabled"
                    else "complete"
                )
                await self._publish()

    async def _background(self, *, channel: str = "metadata") -> None:
        await asyncio.sleep(self.poll_seconds)
        while True:
            if self.limit_attempts >= 3 and not self.limited:
                return
            started = monotonic()
            await asyncio.sleep(0)
            try:
                if channel == "pcap":
                    await self._artifacts_tick()
                elif channel == "native":
                    await self._native_tick()
                else:
                    await self._metadata_tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                async with self.lock:
                    limit_failed = self.limit_attempts > 0 and not self.limited
                    if not limit_failed:
                        self.error = (
                            f"Traffic upload failed ({type(error).__name__}); retry pending"
                        )
                    self.manifest.update(
                        status="error" if limit_failed else "partial", error=self.error
                    )
                    LOGGER.warning(
                        "Traffic upload deferred task=%s error=%s",
                        self.task_id,
                        type(error).__name__,
                    )
                    try:
                        await self._publish()
                    except Exception:
                        pass
                    if self.limit_attempts >= 3 and not self.limited:
                        return
            await asyncio.sleep(max(0.01, self.poll_seconds - (monotonic() - started)))

    async def _cancel_workers(self) -> None:
        workers = [
            task for task in (self.worker, self.pcap_worker, self.native_worker) if task is not None
        ]
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.worker = self.pcap_worker = self.native_worker = None

    async def close(self, *, discard: bool = False) -> None:
        await self._cancel_workers()
        if discard:
            self.discarded = True
            try:
                await self.envd.traffic("stop")
            except Exception as error:
                # Authorized task deletion can force-remove an unreachable execution container.
                LOGGER.warning("Traffic discard stop unconfirmed (%s)", type(error).__name__)
        else:
            await self.flush(stop=True)
        from bbx_runtime.traffic.observation import unregister

        unregister(self)
        try:
            await self.envd.close()
        except Exception as error:
            LOGGER.warning("Traffic client cleanup failed (%s)", type(error).__name__)

    async def suspend(self) -> None:
        """Flush runtime observations while envd continues capture across runtime restarts."""
        await self._cancel_workers()
        try:
            await self.flush()
            while self.pending or not self.queue.empty():
                await self._metadata_tick()
        finally:
            from bbx_runtime.traffic.observation import unregister

            unregister(self)
            await self.envd.close()
