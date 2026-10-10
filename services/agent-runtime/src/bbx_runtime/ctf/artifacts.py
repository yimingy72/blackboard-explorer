"""Register bounded workspace snapshots with computed, trusted object metadata."""

import asyncio
import hashlib
from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import Any
from uuid import UUID, uuid4

from bbx_contracts.ctf import execution_agent_id

from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.client import CtfClient

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024


def artifact_path(path: str, member_id: str) -> str:
    candidate = PurePosixPath(path)
    own = PurePosixPath("/workspace/agents") / execution_agent_id(member_id)
    shared = PurePosixPath("/workspace/shared")
    if (
        not candidate.is_absolute()
        or ".." in candidate.parts
        or "\x00" in path
        or not (candidate.is_relative_to(own) or candidate.is_relative_to(shared))
        or candidate in {own, shared}
    ):
        raise ValueError("Artifact must be a file in this member's workspace or shared directory")
    return str(candidate)


class ArtifactRegistrar:
    def __init__(self, service: CtfClient, envd: Any, objects_factory: Any) -> None:
        self.service, self.envd, self.objects_factory = service, envd, objects_factory

    async def register(
        self, task_id: str, member_id: str, turn: dict[str, Any], path: str, request_id: str
    ) -> dict[str, Any]:
        path = artifact_path(path, member_id)
        request_id = str(UUID(request_id))
        await self.service.runtime(
            task_id,
            "authorize_member",
            agent_id=member_id,
            turn_id=turn["id"],
            generation=turn["generation"],
        )
        previous = await self.service.runtime(
            task_id, "lookup_artifact", agent_id=member_id, request_id=request_id
        )
        if previous is not None:
            if previous["path"] != path:
                raise ValueError("Artifact request ID belongs to a different path")
            return previous
        scope = execution_agent_id(member_id)
        info = await self.envd.stat(path, scope_agent_id=scope)
        if not info.get("is_file") or info.get("size", 0) > MAX_ARTIFACT_BYTES:
            raise ValueError("Artifact is missing, not a file, or exceeds 50 MiB")
        uri = f"evidence/{task_id}/{uuid4()}/{PurePosixPath(path).name}"
        objects = self.objects_factory()
        uploaded = False
        registration_started = False
        uncertain_registration = False
        try:
            with SpooledTemporaryFile(max_size=1024 * 1024) as snapshot:
                digest = hashlib.sha256()
                size = 0
                async with self.envd.file_stream(path, scope_agent_id=scope) as response:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_ARTIFACT_BYTES:
                            raise ValueError("Artifact exceeds 50 MiB")
                        digest.update(chunk)
                        snapshot.write(chunk)
                snapshot.seek(0)
                await objects.put(uri, snapshot, length=size)
                uploaded = True
            payload = dict(
                agent_id=member_id,
                turn_id=turn["id"],
                generation=turn["generation"],
                request_id=request_id,
                path=path,
                uri=uri,
                sha256=digest.hexdigest(),
                size=size,
                filename=PurePosixPath(path).name,
            )
            registration_started = True
            # Same request and snapshot on retry; never re-read a changed workspace file.
            for attempt in range(2):
                previously_uncertain = uncertain_registration
                # Cancellation can leave a server-side transaction alive. Treat
                # dispatch as unknown until a definite rejection proves otherwise.
                uncertain_registration = True
                try:
                    registered = await self.service.runtime(task_id, "register_artifact", **payload)
                    if registered["sha256"] != payload["sha256"] or registered["path"] != path:
                        raise ValueError("Artifact request ID conflicts with this snapshot")
                    if registered["uri"] != uri:
                        await self.cleanup(objects, uri)
                    return registered
                except Exception as error:
                    if isinstance(error, RemoteError) and error.status < 500:
                        uncertain_registration = previously_uncertain
                    previous = await self.service.runtime(
                        task_id, "lookup_artifact", agent_id=member_id, request_id=request_id
                    )
                    if previous is not None:
                        if previous["sha256"] != payload["sha256"] or previous["path"] != path:
                            raise ValueError(
                                "Artifact registration conflicts with uploaded snapshot"
                            ) from error
                        if previous["uri"] != uri:
                            await self.cleanup(objects, uri)
                        return previous
                    if attempt or (isinstance(error, RemoteError) and error.status < 500):
                        raise
            raise AssertionError("Artifact registration loop did not return")
        except BaseException:
            # A committed record must never lose its object after a lost response.
            # Unknown lookup outcome leaves a bounded task-prefix orphan for cleanup.
            removable = not registration_started
            if uploaded and registration_started:
                try:
                    previous = await self.service.runtime(
                        task_id, "lookup_artifact", agent_id=member_id, request_id=request_id
                    )
                    removable = (previous is None and not uncertain_registration) or (
                        previous is not None and previous["uri"] != uri
                    )
                except Exception:
                    removable = False
            if removable:
                await self.cleanup(objects, uri)
            raise

    @staticmethod
    async def cleanup(objects: Any, uri: str) -> None:
        try:
            async with asyncio.timeout(5):
                await objects.remove(uri)
        except Exception:
            pass
