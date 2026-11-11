"""Admission evidence around the official SDK; execution remains owned by execd."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from opensandbox import Sandbox
from opensandbox.models.execd import (
    Execution,
    ExecutionHandlers,
    ExecutionInit,
    OutputMessage,
    RunCommandOpts,
)
from opensandbox.models.filesystem import SetPermissionEntry
from opensandbox.services.filesystem import Filesystem

SOLVER_UID = SOLVER_GID = 10000
SOLVER_USER = SOLVER_GROUP = "bbx-solver"
LAUNCHER = "/opt/bbx/control/launcher"
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


class AlreadyAdmitted(RuntimeError):
    """A durable admission forbids sending the same invocation twice."""


class UnknownCommandOutcome(RuntimeError):
    """The response ended without the execution evidence required for its mode."""


class UnknownAssignmentCleanup(RuntimeError):
    """An Assignment has no verified, durable observation of its sealed empty scope."""


@dataclass(frozen=True)
class AssignmentBinding:
    task_id: str
    agent_id: str
    role: str
    assignment_id: str
    generation: int
    epoch: int
    sandbox_id: str
    pod_uid: str
    environment_revision: str
    network_identity: str
    namespace: str
    ipv4: str
    challenge_id: str | None = None
    parent_agent_id: str | None = None
    execution_profile: str = "basic"
    process_accounting: str = "legacy"
    execution_context_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not all(
                IDENTIFIER.fullmatch(value)
                for key, value in asdict(self).items()
                if isinstance(value, str) and key != "ipv4"
            )
            or self.role not in {"lead", "worker", "helper", "explore"}
            or type(self.generation) is not int
            or self.generation < 0
            or type(self.epoch) is not int
            or self.epoch < 0
            or (self.role == "helper" and not self.parent_agent_id)
            or self.execution_profile not in {"basic", "net-raw"}
            or self.process_accounting not in {"legacy", "cgroup-v2"}
            or (
                self.process_accounting == "cgroup-v2"
                and (
                    not isinstance(self.execution_context_id, str)
                    or re.fullmatch(r"[0-9a-f]{64}", self.execution_context_id) is None
                )
            )
        ):
            raise ValueError("Invalid trusted assignment binding")
        ipaddress.IPv4Address(self.ipv4)

    def scope_request(self) -> tuple[str, str]:
        if self.process_accounting != "cgroup-v2":
            raise ValueError("Assignment cleanup requires the trusted cgroup-v2 profile")
        binding = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(binding.encode()).hexdigest(), binding


@dataclass(frozen=True)
class CommandRequest:
    command_id: str
    invocation_id: str
    command: str
    cwd: str = "/workspace"
    background: bool = False
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        directory = PurePosixPath(self.cwd)
        if (
            not IDENTIFIER.fullmatch(self.command_id)
            or not IDENTIFIER.fullmatch(self.invocation_id)
            or not self.command.strip()
            or "\x00" in self.command
            or len(self.command.encode()) > 1024 * 1024
            or not directory.is_absolute()
            or not directory.is_relative_to("/workspace")
            or ".." in directory.parts
            or "\x00" in self.cwd
            or type(self.background) is not bool
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 3600
        ):
            raise ValueError("Invalid command request")


class CommandJournal:
    """P1 single-host admission/output evidence; P3 must use the platform ledger."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_mode & 0o077:
            raise ValueError("Command journal requires a private control directory")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise ValueError("Command journal must not be shared with solver users")
        finally:
            os.close(fd)
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            "CREATE TABLE IF NOT EXISTS admissions ("
            "invocation_id TEXT PRIMARY KEY, command_id TEXT UNIQUE NOT NULL, "
            "fingerprint TEXT NOT NULL, binding TEXT NOT NULL, request TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS events ("
            "sequence INTEGER PRIMARY KEY, command_id TEXT NOT NULL, kind TEXT NOT NULL, "
            "record TEXT NOT NULL);"
        )

    def close(self) -> None:
        self.connection.close()

    def admit(self, binding: AssignmentBinding, request: CommandRequest) -> None:
        data = {"binding": asdict(binding), "request": asdict(request)}
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            previous = self.connection.execute(
                "SELECT fingerprint FROM admissions WHERE invocation_id = ? OR command_id = ?",
                (request.invocation_id, request.command_id),
            ).fetchall()
            if previous:
                if any(row[0] != fingerprint for row in previous):
                    raise ValueError(
                        "Invocation or command ID reused with different input or identity"
                    )
                raise AlreadyAdmitted("Invocation already admitted; inspect its original evidence")
            self.connection.execute(
                "INSERT INTO admissions VALUES (?, ?, ?, ?, ?)",
                (
                    request.invocation_id,
                    request.command_id,
                    fingerprint,
                    json.dumps(data["binding"]),
                    json.dumps(data["request"]),
                ),
            )
            self.append(request.command_id, "admitted", invocation_id=request.invocation_id)
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def append(self, command_id: str, kind: str, **record: Any) -> None:
        self.connection.execute(
            "INSERT INTO events(command_id, kind, record) VALUES (?, ?, ?)",
            (command_id, kind, json.dumps({"at": datetime.now(UTC).isoformat(), **record})),
        )

    def events(self, command_id: str) -> list[dict[str, Any]]:
        return [
            {"sequence": sequence, "kind": kind, **json.loads(record)}
            for sequence, kind, record in self.connection.execute(
                "SELECT sequence, kind, record FROM events WHERE command_id = ? ORDER BY sequence",
                (command_id,),
            )
        ]

    def require_binding(self, command_id: str, binding: AssignmentBinding) -> None:
        row = self.connection.execute(
            "SELECT binding FROM admissions WHERE command_id = ?", (command_id,)
        ).fetchone()
        if row is None or json.loads(row[0]) != asdict(binding):
            raise ValueError("Command does not belong to this current execution binding")

    def execution_id(self, command_id: str, binding: AssignmentBinding) -> str:
        self.require_binding(command_id, binding)
        identifiers = {
            json.loads(row[0])["execution_id"]
            for row in self.connection.execute(
                "SELECT record FROM events WHERE command_id = ? AND kind = 'execution_bound'",
                (command_id,),
            )
        }
        if len(identifiers) != 1:
            raise ValueError("Official execution ID is unknown or inconsistent; do not replay")
        return identifiers.pop()

    def claim_assignment_stop(self, command_id: str, binding: AssignmentBinding) -> bool:
        self.require_binding(command_id, binding)
        scope_id, _ = binding.scope_request()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            claimed = self.connection.execute(
                "SELECT 1 FROM events WHERE kind = 'assignment_stop_requested' "
                "AND json_extract(record, '$.scope_id') = ? LIMIT 1",
                (scope_id,),
            ).fetchone()
            if not claimed:
                self.append(
                    command_id,
                    "assignment_stop_requested",
                    scope_id=scope_id,
                    binding=asdict(binding),
                )
            self.connection.execute("COMMIT")
            return claimed is None
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def output_preview(self, command_id: str, limit: int = 65536) -> dict[str, Any]:
        if not 0 <= limit <= 1024 * 1024:
            raise ValueError("Invalid output preview limit")
        preview = bytearray()
        total = 0
        collected = False
        row = self.connection.execute(
            "SELECT request FROM admissions WHERE command_id = ?", (command_id,)
        ).fetchone()
        background = row is not None and json.loads(row[0])["background"]
        uncertain = (
            self.connection.execute(
                "SELECT 1 FROM events WHERE command_id = ? AND kind = 'unknown' LIMIT 1",
                (command_id,),
            ).fetchone()
            is not None
        )
        source_gaps: list[list[int]] = []
        text_gaps: list[list[int]] = []
        text_loss = False
        text_bytes = 0
        covered_end = 0
        if background:
            records = self.connection.execute(
                "SELECT record FROM events WHERE command_id = ? AND kind = 'background_output' "
                "ORDER BY json_extract(record, '$.from_cursor'), "
                "json_extract(record, '$.cursor') DESC, sequence DESC",
                (command_id,),
            )
        else:
            records = self.connection.execute(
                "SELECT record FROM events WHERE command_id = ? AND kind = 'output' "
                "ORDER BY sequence",
                (command_id,),
            )
        for (record,) in records:
            item = json.loads(record)
            collected = True
            if background:
                start, end = item["from_cursor"], item["cursor"]
                if end <= covered_end:
                    continue
                content = item["content"].encode("utf-8")
                overlap = max(0, covered_end - start)
                if start > covered_end:
                    source_gaps.append([covered_end, start])
                loss = len(content) != end - start or "\ufffd" in item["content"]
                text_loss |= loss
                total += end - max(covered_end, start)
                if loss and overlap:
                    # A decoded string cannot be sliced at an original byte offset after loss.
                    text_gaps.append([covered_end, end])
                    content = b""
                else:
                    content = content[overlap:]
                covered_end = end
            else:
                content = base64.b64decode(item["data"])
                text_loss |= b"\xef\xbf\xbd" in content
                total += len(content)
            text_bytes += len(content)
            preview.extend(content[: max(0, limit - len(preview))])
        gap = uncertain or bool(source_gaps) or bool(text_gaps) or text_loss
        retained = not gap and (collected or not background)
        return {
            "text": preview.decode("utf-8", errors="replace"),
            "bytes": total,
            "byte_count_basis": "server_cursor" if background else "sdk_text_utf8",
            "text_bytes": text_bytes,
            "truncated": text_bytes > limit,
            "retained": retained,
            "coverage": "partial" if gap else "collected" if retained else "not_collected",
            "source_gaps": source_gaps,
            "text_gaps": text_gaps,
            "text_integrity": "loss_possible" if text_loss else "text_only",
            "raw_bytes_retained": False,
        }


class SolverFiles:
    """Only the low-identity SDK client is reachable from these file tools."""

    def __init__(self, files: Filesystem) -> None:
        self._files = files

    @staticmethod
    def _mode(mode: int) -> int:
        if type(mode) is not int or not 0 <= mode <= 777 or any(c > "7" for c in str(mode)):
            raise ValueError(
                "File mode must use ordinary octal permission digits, without special bits"
            )
        return mode

    async def read_file(
        self, path: str, *, offset: int | None = None, limit: int | None = None
    ) -> str:
        return await self._files.read_file(path, offset=offset, limit=limit)

    async def read_bytes(self, path: str) -> bytes:
        return await self._files.read_bytes(path)

    async def write_file(self, path: str, data: str | bytes, *, mode: int = 644) -> None:
        await self._files.write_file(
            path, data, mode=self._mode(mode), owner=SOLVER_USER, group=SOLVER_GROUP
        )

    async def set_permissions(self, path: str, *, mode: int) -> None:
        await self._files.set_permissions(
            [
                SetPermissionEntry(
                    path=path, mode=self._mode(mode), owner=SOLVER_USER, group=SOLVER_GROUP
                )
            ]
        )

    async def delete_files(self, paths: list[str]) -> None:
        await self._files.delete_files(paths)


class SandboxExecution:
    def __init__(
        self, sandbox: Sandbox, binding: AssignmentBinding, journal: CommandJournal
    ) -> None:
        if sandbox.id != binding.sandbox_id:
            raise ValueError("Sandbox does not match the trusted binding")
        self.sandbox, self.binding, self.journal = sandbox, binding, journal
        # This public API intentionally raises on an unsupported identity adapter.
        self._files = SolverFiles(sandbox.files_with_identity(SOLVER_UID, SOLVER_GID))
        self._dispatch_lock = asyncio.Lock()

    @property
    def files(self) -> SolverFiles:
        return self._files

    async def run(
        self, request: CommandRequest, *, proxy_url: str = "-", ca_file: str = "-"
    ) -> Execution:
        async with self._dispatch_lock:
            self.journal.admit(self.binding, request)
        command_id = request.command_id
        offsets = {"stdout": 0, "stderr": 0}

        async def on_init(event: ExecutionInit) -> None:
            if not event.id:
                raise ValueError("Official execution ID was empty")
            self.journal.append(command_id, "execution_bound", execution_id=event.id)

        async def output(stream: str, event: OutputMessage) -> None:
            data = event.text.encode("utf-8")
            self.journal.append(
                command_id,
                "output",
                stream=stream,
                offset=offsets[stream],
                size=len(data),
                data=base64.b64encode(data).decode("ascii"),
            )
            offsets[stream] += len(data)

        handlers = ExecutionHandlers(
            on_init=on_init,
            on_stdout=lambda event: output("stdout", event),
            on_stderr=lambda event: output("stderr", event),
            skip_accumulation=True,
        )
        argv = [
            LAUNCHER,
            self.binding.namespace,
            command_id,
            self.binding.network_identity,
            self.binding.ipv4,
            request.cwd,
            proxy_url,
            ca_file,
            request.command,
            self.binding.execution_profile,
        ]
        if self.binding.process_accounting == "cgroup-v2":
            argv.extend(self.binding.scope_request())
            argv.extend([self.binding.pod_uid, self.binding.environment_revision])
            assert self.binding.execution_context_id is not None
            argv.append(self.binding.execution_context_id)
        self.journal.append(command_id, "dispatching")
        try:
            result = await self.sandbox.commands.run(
                argv,
                opts=RunCommandOpts(
                    working_directory="/opt/bbx/control",
                    background=request.background,
                    timeout=timedelta(seconds=request.timeout_seconds),
                ),
                handlers=handlers,
            )
            official_id = self.journal.execution_id(command_id, self.binding)
            if result.id != official_id:
                raise ValueError("Official execution ID changed after initialization")
            terminal_error = (
                result.error is not None
                and result.error.exit_code is not None
                and result.error.exit_code == result.exit_code
            )
            evidence_available = result.complete is not None
            if not request.background:
                evidence_available = result.exit_code is not None and (
                    result.complete is not None or terminal_error
                )
            if not evidence_available:
                mode = (
                    "background start acknowledgement"
                    if request.background
                    else "foreground terminal result"
                )
                raise UnknownCommandOutcome(f"Official {mode} was not observed before response EOF")
            evidence = "foreground_terminal"
            if request.background:
                evidence = "background_start_error" if result.error else "background_start_ack"
            self.journal.append(
                command_id,
                "returned",
                execution_id=official_id,
                background=request.background,
                evidence=evidence,
                response_event_at=result.complete.timestamp
                if result.complete is not None
                else result.error.timestamp
                if result.error
                else None,
                exit_code=result.exit_code,
                error=result.error.model_dump() if result.error else None,
            )
            return result
        except BaseException as error:
            self.journal.append(command_id, "unknown", reason=type(error).__name__)
            raise

    async def status(self, command_id: str) -> dict[str, Any]:
        execution_id = self.journal.execution_id(command_id, self.binding)
        status = await self.sandbox.commands.get_command_status(execution_id)
        if status.id != execution_id:
            raise ValueError("Official status belongs to another execution")
        # Execd content contains launcher arguments; never expose it as an Agent result.
        result = status.model_dump(mode="json", exclude={"content"})
        self.journal.append(command_id, "status", **result)
        return result

    async def background_logs(self, command_id: str, cursor: int | None = None) -> dict[str, Any]:
        if cursor is not None and (type(cursor) is not int or cursor < 0):
            raise ValueError("Background cursor must be a nonnegative byte offset")
        execution_id = self.journal.execution_id(command_id, self.binding)
        logs = await self.sandbox.commands.get_background_command_logs(execution_id, cursor)
        if logs.cursor is None or logs.cursor < 0 or logs.cursor < (cursor or 0):
            raise ValueError("Official background cursor moved backwards")
        source_bytes = logs.cursor - (cursor or 0)
        text_bytes = len(logs.content.encode("utf-8"))
        result = {
            "content": logs.content,
            "cursor": logs.cursor,
            "source_bytes": source_bytes,
            "text_bytes": text_bytes,
            "text_integrity": "loss_possible"
            if source_bytes != text_bytes or "\ufffd" in logs.content
            else "text_only",
            "raw_bytes_retained": False,
        }
        self.journal.append(command_id, "background_output", from_cursor=cursor or 0, **result)
        return result

    async def interrupt(self, command_id: str) -> dict[str, Any]:
        execution_id = self.journal.execution_id(command_id, self.binding)
        self.journal.append(command_id, "cancel_requested", execution_id=execution_id)
        try:
            await self.sandbox.commands.interrupt(execution_id)
        except BaseException as error:
            self.journal.append(command_id, "cancel_unknown", reason=type(error).__name__)
            raise
        # Successful interrupt is a request acknowledgement, not a descendant drain proof.
        return await self.status(command_id)

    async def stop_assignment(self, command_id: str) -> dict[str, Any]:
        """Request a stop once; repeated calls only inspect the original sealed scope."""
        first_request = self.journal.claim_assignment_stop(command_id, self.binding)
        return await self._assignment_control(command_id, stop=first_request)

    async def assignment_cleanup_status(self, command_id: str) -> dict[str, Any]:
        """Reconcile the original scope without redispatching a command or kill request."""
        self.journal.require_binding(command_id, self.binding)
        self.binding.scope_request()
        return await self._assignment_control(command_id, stop=False)

    async def _assignment_control(self, command_id: str, *, stop: bool) -> dict[str, Any]:
        self.journal.require_binding(command_id, self.binding)
        scope_id, binding = self.binding.scope_request()
        observed_id = None

        async def on_init(event: ExecutionInit) -> None:
            nonlocal observed_id
            if not event.id:
                raise ValueError("Assignment control execution ID is empty")
            observed_id = event.id
            self.journal.append(
                command_id,
                "assignment_control_bound",
                scope_id=scope_id,
                execution_id=event.id,
                operation="stop" if stop else "status",
            )

        try:
            result = await self.sandbox.commands.run(
                [
                    LAUNCHER,
                    "--assignment-stop" if stop else "--assignment-status",
                    scope_id,
                    binding,
                    self.binding.pod_uid,
                    self.binding.environment_revision,
                    self.binding.execution_context_id or "",
                ],
                opts=RunCommandOpts(
                    working_directory="/opt/bbx/control",
                    timeout=timedelta(seconds=10),
                ),
                handlers=ExecutionHandlers(on_init=on_init),
            )
            if (
                not observed_id
                or result.id != observed_id
                or result.complete is None
                or result.exit_code != 0
                or result.error is not None
            ):
                raise UnknownAssignmentCleanup("Assignment control terminal evidence is missing")
            receipt = json.loads("".join(item.text for item in result.logs.stdout))
            self.journal.append(
                command_id,
                "assignment_scope_observed",
                scope_id=scope_id,
                execution_id=observed_id,
                receipt=receipt,
            )
            if (
                receipt.get("scope_id") != scope_id
                or receipt.get("execution_context_id") != self.binding.execution_context_id
                or receipt.get("state") != "confirmed"
                or receipt.get("reason") != "observed_empty"
                or receipt.get("sealed") is not True
                or type(receipt.get("populated")) is not int
                or receipt["populated"] != 0
                or any(
                    type(receipt.get(key)) is not int or receipt[key] <= 0
                    for key in (
                        "cgroup_namespace",
                        "mount_namespace",
                        "mount_id",
                        "mount_device",
                        "mount_inode",
                        "scope_device",
                        "scope_inode",
                    )
                )
                or not isinstance(receipt.get("boot_id"), str)
                or not receipt["boot_id"]
            ):
                raise UnknownAssignmentCleanup("Original Assignment scope is not confirmed empty")
            self.journal.append(command_id, "assignment_cleanup_confirmed", **receipt)
            return receipt
        except BaseException as error:
            self.journal.append(
                command_id,
                "assignment_cleanup_unknown",
                scope_id=scope_id,
                reason=type(error).__name__,
            )
            raise
