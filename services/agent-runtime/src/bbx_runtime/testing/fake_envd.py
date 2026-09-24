"""In-process envd double with real MCP transport and scripted commands."""

from __future__ import annotations

import asyncio
import fnmatch
import hmac
import io
import json
import re
import shlex
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_AGENT_ID = re.compile(r"agent-[0-9]{1,6}\Z")
_OUTPUT_LIMIT = 64 * 1024
_ARCHIVE_EXCLUDE = "**/.git/objects/,**/node_modules/,**/__pycache__/,**/target/,**/*.o"


@dataclass
class CommandOutput:
    stdout: str | bytes = ""
    stderr: str | bytes = ""
    exit_code: int = 0
    delay: float = 0
    files: dict[str, bytes] = field(default_factory=dict)


class _TokenAuth:
    def __init__(self, app: Starlette, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            actual = dict(scope["headers"]).get(b"authorization", b"")
            expected = b"Bearer " + self.token.encode()
            if not hmac.compare_digest(actual, expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class FakeEnvd:
    """Use `async with fake` before httpx.ASGITransport or MCP sessions."""

    def __init__(
        self,
        root: Path,
        *,
        token: str = "test-envd-token",
        command_outputs: dict[str | tuple[str, str], CommandOutput | dict[str, Any]] | None = None,
        evidence_max_bytes: int = 50 * 1024 * 1024,
        archive_max_bytes: int = 2 * 1024 * 1024 * 1024,
        archive_exclude: str = _ARCHIVE_EXCLUDE,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "agents").mkdir(exist_ok=True)
        (self.root / "shared").mkdir(exist_ok=True)
        self.token = token
        self.command_outputs = {
            key: value if isinstance(value, CommandOutput) else CommandOutput(**value)
            for key, value in (command_outputs or {}).items()
        }
        self.evidence_max_bytes = evidence_max_bytes
        self.archive_max_bytes = archive_max_bytes
        self.archive_exclude = archive_exclude
        self.calls: list[dict[str, object]] = []
        self.commands: list[str] = []
        self.max_concurrent_commands = 0
        self._active_commands = 0
        self._output_sequence = 0
        self._session = None
        self.mcp = FastMCP("fake-envd", host="0.0.0.0")

        @self.mcp.tool(name="execute_command")
        async def execute_command(
            command: str,
            cwd: str | None = None,
            timeout_sec: int = 120,
            privileged: bool = False,
            *,
            ctx: Context,
        ) -> dict[str, object]:
            request = ctx.request_context.request
            agent_id = request.headers.get("X-Agent-Id", "") if request is not None else ""
            return await self.run_command(agent_id, command, cwd, timeout_sec, privileged)

        async def health(request) -> Response:
            return JSONResponse({"status": "ok"})

        async def users(request) -> Response:
            try:
                agent_id = (await request.json())["agent_id"]
                if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
                    raise ValueError("invalid agent id")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            (self.root / "agents" / agent_id).mkdir(exist_ok=True)
            return JSONResponse({"agent_id": agent_id, "home": f"/workspace/agents/{agent_id}"})

        async def files(request) -> Response:
            try:
                path = self._path(request.query_params["path"])
            except (KeyError, ValueError, OSError):
                return JSONResponse({"error": "invalid path"}, status_code=400)
            if not path.is_file():
                return JSONResponse({"error": "not a file"}, status_code=400)
            data = path.read_bytes()
            if len(data) > self.evidence_max_bytes:
                return JSONResponse({"error": "file too large"}, status_code=413)
            if request.method == "HEAD":
                return Response(
                    media_type="application/octet-stream",
                    headers={"Content-Length": str(len(data))},
                )
            return Response(data, media_type="application/octet-stream")

        async def stat(request) -> Response:
            try:
                path = self._path(request.query_params["path"])
            except (KeyError, ValueError, OSError):
                return JSONResponse({"error": "invalid path"}, status_code=400)
            if not path.exists():
                return JSONResponse({"exists": False})
            info = path.stat()
            return JSONResponse(
                {
                    "exists": True,
                    "size": info.st_size,
                    "is_file": path.is_file(),
                    "is_dir": path.is_dir(),
                }
            )

        async def archive(request) -> Response:
            data = self._archive(False)
            fallback = len(data) > self.archive_max_bytes
            if fallback:
                data = self._archive(True)
            return Response(
                data,
                media_type="application/zstd",
                headers={"X-Archive-Fallback": "agents-only" if fallback else "none"},
            )

        self.app = _TokenAuth(
            Starlette(
                routes=[
                    Route("/health", health),
                    Route("/users", users, methods=["POST"]),
                    Route("/files", files),
                    Route("/stat", stat),
                    Route("/archive", archive, methods=["POST"]),
                    *self.mcp.streamable_http_app().routes,
                ]
            ),
            token,
        )

    async def __aenter__(self) -> FakeEnvd:
        self._session = self.mcp.session_manager.run()
        await self._session.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        assert self._session is not None
        await self._session.__aexit__(exc_type, exc, traceback)
        self._session = None

    def _path(self, value: str) -> Path:
        raw = PurePosixPath(value)
        if not raw.is_absolute() or not raw.parts[:2] == ("/", "workspace") or ".." in raw.parts:
            raise ValueError("invalid path")
        path = (self.root / Path(*raw.parts[2:])).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("path outside workspace")
        return path

    def put_file(self, path: str, data: bytes | str) -> None:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)

    def set_command(
        self,
        command: str,
        output: CommandOutput,
        *,
        agent_id: str | None = None,
    ) -> None:
        self.command_outputs[(agent_id, command) if agent_id else command] = output

    async def run_command(
        self,
        agent_id: str,
        command: str,
        cwd: str | None,
        timeout_sec: int,
        privileged: bool,
    ) -> dict[str, object]:
        home = self.root / "agents" / agent_id
        if not _AGENT_ID.fullmatch(agent_id) or not home.is_dir():
            raise ValueError("unknown agent")
        if not 1 <= timeout_sec <= 1200:
            raise ValueError("invalid timeout_sec")
        directory = self._path(cwd) if cwd is not None else home
        if not directory.is_dir():
            raise ValueError("cwd is not a directory")
        if privileged:
            if re.search(r"[;&|`$<>\\\n\r'\"(){}]", command):
                raise ValueError("privileged command is not allowed")
            words = shlex.split(command)
            if not any(
                words[: len(prefix)] == prefix
                for prefix in (
                    ["apt-get", "install"],
                    ["apt-get", "update"],
                    ["pip", "install"],
                    ["npm", "install", "-g"],
                )
            ):
                raise ValueError("privileged command is not allowed")
        self.calls.append(
            {
                "agent_id": agent_id,
                "command": command,
                "cwd": cwd or f"/workspace/agents/{agent_id}",
                "timeout_sec": timeout_sec,
                "privileged": privileged,
            }
        )
        output = self.command_outputs.get((agent_id, command), self.command_outputs.get(command))
        if output is None:
            raise ValueError(f"command is not scripted: {command}")
        self.commands.append(command)
        self._active_commands += 1
        self.max_concurrent_commands = max(self.max_concurrent_commands, self._active_commands)
        try:
            if output.delay:
                await asyncio.sleep(output.delay)
            for path, data in output.files.items():
                self.put_file(path, data)
            stdout = output.stdout.encode() if isinstance(output.stdout, str) else output.stdout
            stderr = output.stderr.encode() if isinstance(output.stderr, str) else output.stderr
            total = len(stdout) + len(stderr)
            truncated = total > _OUTPUT_LIMIT
            full_output_path: str | None = None
            if truncated:
                head = _OUTPUT_LIMIT // 2
                tail = _OUTPUT_LIMIT // 2
                shown_out = stdout[:head] + (
                    stdout[-max(0, tail - len(stderr)) :] if len(stderr) < tail else b""
                )
                shown_err = stderr[: max(0, head - len(stdout))] + stderr[-tail:]
                self._output_sequence += 1
                full_output_path = (
                    f"/workspace/agents/{agent_id}/.outputs/{self._output_sequence}.txt"
                )
                self.put_file(full_output_path, stdout + stderr)
            else:
                shown_out, shown_err = stdout, stderr

            def wrap(value: bytes) -> str:
                return f"<command_output>{value.decode('utf-8', errors='replace')}</command_output>"

            return {
                "exit_code": output.exit_code,
                "stdout": wrap(shown_out),
                "stderr": wrap(shown_err),
                "truncated": truncated,
                "full_output_path": full_output_path,
            }
        finally:
            self._active_commands -= 1

    def _archive(self, agents_only: bool) -> bytes:
        import zstandard

        root = "agents" if agents_only else "."
        rules = [
            item.strip().rstrip("/") for item in self.archive_exclude.split(",") if item.strip()
        ]

        def exclude(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            return None if any(fnmatch.fnmatch(info.name, rule) for rule in rules) else info

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            tar.add(self.root / root, arcname=root, filter=exclude)
        return zstandard.ZstdCompressor().compress(buffer.getvalue())
