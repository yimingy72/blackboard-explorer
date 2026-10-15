"""Persist native capture inputs without interpreting or logging packet/keylog contents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from time import time
from typing import Any

from bbx_contracts.traffic import (
    TrafficNativeKeylogPart,
    TrafficNativePart,
    TrafficNativeSourceIndex,
    TrafficNativeSourceRef,
)

LOGGER = logging.getLogger(__name__)
INDEX_BYTES = 32 * 1024 * 1024
PART_BYTES = 4 * 1024 * 1024
INDEX_RETENTION_SECONDS = 180


@dataclass
class NativeUpdate:
    indices: dict[str, TrafficNativeSourceIndex]
    refs: list[dict[str, Any]]
    acknowledgements: list[str]
    pending: int
    progress: bool
    complete: bool


class NativeArchive:
    def __init__(self, envd: Any, objects: Any, task_id: str, run: int) -> None:
        self.envd, self.objects, self.task_id, self.run = envd, objects, task_id, run
        self.prefix = f"traffic/{task_id}/run-{run}/native/"
        self.indices: dict[str, TrafficNativeSourceIndex] = {}
        self.refs: dict[str, dict[str, Any]] = {}
        self.uploaded: dict[str, tuple[str, int]] = {}
        self.index_digests: set[str] = set()
        self.history: dict[str, float] = {}
        self.gc_error: str | None = None
        self.last_gc = 0.0
        self.next_source = 0

    def _part_key(self, source_id: str, part: TrafficNativePart | TrafficNativeKeylogPart) -> str:
        if isinstance(part, TrafficNativeKeylogPart):
            return self.prefix + f"keylog/{part.id}.log"
        return self.prefix + f"{source_id}/parts/{part.id}.bin"

    def _index_key(self, source_id: str, revision: str) -> str:
        return self.prefix + f"{source_id}/indexes/{revision}.json"

    def _remember(self, key: str, part: TrafficNativePart | TrafficNativeKeylogPart) -> None:
        signature = (part.sha256, part.size_bytes)
        if key in self.uploaded and self.uploaded[key] != signature:
            raise ValueError("Native capture part identity changed")
        self.uploaded[key] = signature

    def _known(self, source_id: str, part: TrafficNativePart | TrafficNativeKeylogPart) -> bool:
        key = self._part_key(source_id, part)
        if key not in self.uploaded:
            return False
        self._remember(key, part)
        return True

    async def restore(self, refs: list[dict[str, Any]]) -> None:
        for raw in refs:
            ref = TrafficNativeSourceRef.model_validate(raw)
            key = self._index_key(ref.source_id, ref.revision)
            encoded = await self.objects.get(key)
            if len(encoded) > INDEX_BYTES or hashlib.sha256(encoded).hexdigest() != ref.revision:
                raise ValueError("Native source index digest mismatch")
            index = await asyncio.to_thread(TrafficNativeSourceIndex.model_validate_json, encoded)
            self._identity(index)
            if (
                index.source_id != ref.source_id
                or index.agent_id != ref.agent_id
                or index.packet_count != ref.packet_count
                or index.complete != ref.complete
                or index.last_packet_at != ref.last_packet_at
                or sum(part.size_bytes for part in index.parts) != ref.available_bytes
            ):
                raise ValueError("Native source reference does not match its index")
            self.indices[index.source_id] = index
            self.refs[index.source_id] = ref.model_dump(mode="json")
            self.index_digests.add(ref.revision)
            for part in [*index.parts, *index.keylog_parts]:
                self._remember(self._part_key(index.source_id, part), part)
        # Index history is auxiliary: failures cannot invalidate already durable capture inputs.
        try:
            ledger = self.prefix + "index-history.json"
            if await self.objects.exists(ledger):
                saved = json.loads(await self.objects.get(ledger))
                if saved.get("task_id") != self.task_id or saved.get("run") != self.run:
                    raise ValueError("Native index history ownership mismatch")
                self.history = {
                    key: float(value)
                    for key, value in saved.get("indices", {}).items()
                    if self._own_index(key)
                }
            for key in await self.objects.list(self.prefix):
                if self._own_index(key):
                    # Unknown orphan creation time is conservatively aged from discovery.
                    self.history.setdefault(key, time())
        except Exception as error:
            self.gc_error = type(error).__name__[:80]
        await self.collect_indices(force=True)

    def _identity(self, index: TrafficNativeSourceIndex) -> None:
        if str(index.task_id) != self.task_id or index.run != self.run:
            raise ValueError("Native source index belongs to another task run")
        previous = self.indices.get(index.source_id)
        if previous is not None and (
            index.agent_id != previous.agent_id
            or index.parts[: len(previous.parts)] != previous.parts
            or index.keylog_parts[: len(previous.keylog_parts)] != previous.keylog_parts
            or (previous.complete and not index.complete)
        ):
            raise ValueError("Native source prefix regressed; a new epoch requires a new source ID")

    async def _upload(
        self, source_id: str, part: TrafficNativePart | TrafficNativeKeylogPart
    ) -> None:
        key = self._part_key(source_id, part)
        if self._known(source_id, part):
            return
        data = bytearray()
        async with self.envd.native_stream(part.id) as response:
            async for block in response.aiter_bytes(chunk_size=65536):
                data.extend(block)
                if len(data) > PART_BYTES:
                    raise ValueError("Native capture part exceeds 4 MiB")
        if len(data) != part.size_bytes or hashlib.sha256(data).hexdigest() != part.sha256:
            raise ValueError("Native capture part size or digest mismatch")
        if isinstance(part, TrafficNativeKeylogPart) and not data.endswith(b"\n"):
            raise ValueError("Native keylog part contains an unfinished line")
        await self.objects.put(key, bytes(data), content_type="application/octet-stream")
        self._remember(key, part)

    async def stage(self) -> NativeUpdate:
        snapshot = await self.envd.native_traffic()
        if snapshot.get("error"):
            raise RuntimeError("Native capture input is unavailable")
        raw_sources = snapshot.get("sources", [])
        if not isinstance(raw_sources, list) or not raw_sources or len(raw_sources) > 256:
            raise ValueError("Native capture source list exceeds its limit")
        sources = await asyncio.to_thread(
            lambda: [TrafficNativeSourceIndex.model_validate(value) for value in raw_sources]
        )
        if len({index.source_id for index in sources}) != len(sources):
            raise ValueError("Duplicate native capture source")
        for index in sources:
            self._identity(index)
        budget, uploaded = 8, 0
        # TLS parts are task-private and shared by all source indices, not duplicated per Agent.
        for index in sources:
            for part in index.keylog_parts:
                if not self._known(index.source_id, part) and budget:
                    await self._upload(index.source_id, part)
                    budget -= 1
                    uploaded += 1
        if sources:
            position = self.next_source % len(sources)
            ordered = sources[position:] + sources[:position]
            self.next_source += 1
            candidates = [
                iter([part for part in index.parts if not self._known(index.source_id, part)])
                for index in ordered
            ]
            while budget:
                advanced = False
                for index, candidates_for_source in zip(ordered, candidates, strict=True):
                    part = next(candidates_for_source, None)
                    if part is not None and budget:
                        await self._upload(index.source_id, part)
                        budget -= 1
                        uploaded += 1
                        advanced = True
                if not advanced:
                    break
        indices, refs = dict(self.indices), dict(self.refs)
        referenced: set[str] = set()
        for source in sources:
            parts, keys = [], []
            for part in source.parts:
                if not self._known(source.source_id, part):
                    break
                parts.append(part)
            for part in source.keylog_parts:
                if not self._known(source.source_id, part):
                    break
                keys.append(part)
            if not parts:
                continue
            full = len(parts) == len(source.parts) and len(keys) == len(source.keylog_parts)
            count = next(
                (part.last_packet for part in reversed(parts) if part.last_packet is not None), 0
            )
            index = source.model_copy(
                update={
                    "parts": parts,
                    "keylog_parts": keys,
                    "packet_count": count,
                    "complete": source.complete and full,
                    "last_packet_at": source.last_packet_at if full else None,
                }
            )
            index = TrafficNativeSourceIndex.model_validate(index.model_dump())
            encoded = await asyncio.to_thread(lambda index=index: index.model_dump_json().encode())
            if len(encoded) > INDEX_BYTES:
                raise ValueError("Native source index exceeds 32 MiB")
            revision = hashlib.sha256(encoded).hexdigest()
            key = self._index_key(index.source_id, revision)
            previous_ref = self.refs.get(index.source_id, {})
            if revision not in self.index_digests or (
                previous_ref.get("revision") != revision and not await self.objects.exists(key)
            ):
                await self.objects.put(key, encoded, content_type="application/json")
                self.index_digests.add(revision)
                self.history.setdefault(key, time())
            indices[index.source_id] = index
            refs[index.source_id] = TrafficNativeSourceRef(
                source_id=index.source_id,
                agent_id=index.agent_id,
                revision=revision,
                available_bytes=sum(part.size_bytes for part in parts),
                packet_count=count,
                complete=index.complete,
                last_packet_at=index.last_packet_at,
            ).model_dump(mode="json")
            referenced.update(self._part_key(index.source_id, part) for part in [*parts, *keys])
        acknowledgements = []
        pending = snapshot.get("pending", [])
        if not isinstance(pending, list):
            raise ValueError("Invalid native pending part list")
        for item in pending:
            if item.get("kind") == "keylog":
                part = TrafficNativeKeylogPart.model_validate(item["descriptor"])
                key = self._part_key("", part)
            elif item.get("kind") == "pcapng":
                part = TrafficNativePart.model_validate(item["descriptor"])
                source = next(
                    (value for value in sources if value.source_id == item.get("source_id")), None
                )
                if source is None or part not in source.parts:
                    raise ValueError("Native pending part belongs to another source")
                key = self._part_key(source.source_id, part)
            else:
                raise ValueError("Unknown native capture part kind")
            if item.get("id") != part.id:
                raise ValueError("Native pending identity differs from its descriptor")
            if key in referenced:
                self._remember(key, part)
                acknowledgements.append(part.id)
        complete = bool(indices) and all(
            index.complete and not index.error for index in indices.values()
        )
        changed = refs != self.refs
        return NativeUpdate(
            indices,
            list(refs.values()),
            acknowledgements,
            len(pending) - len(acknowledgements),
            bool(uploaded or changed or acknowledgements),
            complete,
        )

    def confirm(self, update: NativeUpdate) -> None:
        self.indices = update.indices
        self.refs = {ref["source_id"]: ref for ref in update.refs}

    def _own_index(self, key: str) -> bool:
        if not key.startswith(self.prefix):
            return False
        return (
            re.fullmatch(r"[a-zA-Z0-9_-]{1,80}/indexes/[0-9a-f]{64}\.json", key[len(self.prefix) :])
            is not None
        )

    async def collect_indices(self, *, force: bool = False) -> None:
        stamp = time()
        if not force and stamp - self.last_gc < 30:
            return
        self.last_gc = stamp
        protected = {
            self._index_key(ref["source_id"], ref["revision"]) for ref in self.refs.values()
        }
        try:
            for key, created in list(self.history.items()):
                if (
                    key not in protected
                    and stamp - created >= INDEX_RETENTION_SECONDS
                    and self._own_index(key)
                ):
                    await self.objects.remove(key)
                    self.index_digests.discard(key.rsplit("/", 1)[-1].removesuffix(".json"))
                    self.history.pop(key, None)
            await self.objects.put(
                self.prefix + "index-history.json",
                json.dumps(
                    {
                        "task_id": self.task_id,
                        "run": self.run,
                        "indices": self.history,
                        "gc_error": self.gc_error,
                    }
                ).encode(),
                content_type="application/json",
            )
            self.gc_error = None
        except Exception as error:
            self.gc_error = type(error).__name__[:80]
            LOGGER.warning("Native index cleanup deferred (%s)", self.gc_error)
