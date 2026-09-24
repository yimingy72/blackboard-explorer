"""Authenticated internal API and MCP command server."""

import fnmatch
import hmac
import json
import os
import pwd
import stat as stat_mode
import subprocess
import tarfile
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import BinaryIO

from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from bbx_envd.core import (
    WORKSPACE,
    execute_command,
    prepare_workspace,
    valid_agent_id,
    workspace_path,
)
from bbx_envd.settings import Settings

settings = Settings()  # pyright: ignore[reportCallIssue]
mcp = FastMCP("envd", host="0.0.0.0")


@mcp.tool(name="execute_command")
async def execute_command_tool(
    command: str,
    cwd: str | None = None,
    timeout_sec: int = 120,
    privileged: bool = False,
    *,
    ctx: Context,
) -> dict[str, object]:
    """Run a command in the current agent's workspace."""
    request = ctx.request_context.request
    agent_id = request.headers.get("X-Agent-Id", "") if request is not None else ""
    return await execute_command(agent_id, command, cwd, timeout_sec, privileged, settings)


async def health(request) -> Response:
    return JSONResponse({"status": "ok"})


async def users(request) -> Response:
    try:
        agent_id = (await request.json())["agent_id"]
        if not isinstance(agent_id, str) or not valid_agent_id(agent_id):
            raise ValueError("invalid agent id")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    home = WORKSPACE / "agents" / agent_id
    try:
        pwd.getpwnam(agent_id)
    except KeyError:
        try:
            subprocess.run(
                ["useradd", "-m", "-d", str(home), "-s", "/bin/bash", agent_id],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            return JSONResponse({"error": exc.stderr.decode(errors="replace")}, status_code=500)
    home.mkdir(mode=0o755, exist_ok=True)
    owner = pwd.getpwnam(agent_id)
    os.chown(home, owner.pw_uid, owner.pw_gid)
    home.chmod(0o755)
    return JSONResponse({"agent_id": agent_id, "home": str(home)})


def requested_path(request) -> Path | Response:
    try:
        return workspace_path(request.query_params["path"])
    except KeyError:
        return JSONResponse({"error": "missing path"}, status_code=400)
    except (ValueError, FileNotFoundError, OSError):
        return JSONResponse({"error": "invalid path"}, status_code=400)


def open_workspace_path(path: Path) -> int:
    """Open a resolved workspace path without following any later symlink changes."""
    root = WORKSPACE.resolve(strict=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.relative_to(root).parts
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


class SnapshotResponse(StreamingResponse):
    def __init__(self, snapshot: BinaryIO, size: int) -> None:
        self.snapshot = snapshot
        super().__init__(
            iter(lambda: snapshot.read(65536), b""),
            media_type="application/octet-stream",
            headers={"Content-Length": str(size)},
        )

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.snapshot.close()


async def files(request) -> Response:
    path = requested_path(request)
    if isinstance(path, Response):
        return path
    try:
        fd = open_workspace_path(path)
    except OSError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    try:
        info = os.fstat(fd)
        if not stat_mode.S_ISREG(info.st_mode):
            return JSONResponse({"error": "not a file"}, status_code=400)
        if info.st_size > settings.evidence_max_bytes:
            return JSONResponse({"error": "file too large"}, status_code=413)
        if request.method == "HEAD":
            return Response(
                media_type="application/octet-stream", headers={"Content-Length": str(info.st_size)}
            )
        with os.fdopen(fd, "rb", closefd=False) as source:
            snapshot = tempfile.TemporaryFile()
            try:
                remaining = settings.evidence_max_bytes + 1
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        break
                    snapshot.write(chunk)
                    remaining -= len(chunk)
                if remaining == 0:
                    snapshot.close()
                    return JSONResponse({"error": "file too large"}, status_code=413)
                size = snapshot.tell()
                snapshot.seek(0)
                return SnapshotResponse(snapshot, size)
            except Exception:
                snapshot.close()
                raise
    finally:
        os.close(fd)


async def stat(request) -> Response:
    try:
        path = workspace_path(request.query_params["path"], strict=False)
    except (KeyError, ValueError, OSError):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    try:
        fd = open_workspace_path(path)
    except FileNotFoundError:
        return JSONResponse({"exists": False})
    except OSError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    return JSONResponse(
        {
            "exists": True,
            "size": info.st_size,
            "is_file": stat_mode.S_ISREG(info.st_mode),
            "is_dir": stat_mode.S_ISDIR(info.st_mode),
        }
    )


def make_archive(agents_only: bool = False) -> str:
    rules = [
        item.strip().rstrip("/") for item in settings.archive_exclude.split(",") if item.strip()
    ]

    def exclude(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if any(fnmatch.fnmatch(info.name, rule) for rule in rules) else info

    root = "agents" if agents_only else "."
    with tempfile.NamedTemporaryFile(
        prefix="bbx-m2env-", suffix=".tar.zst", delete=False
    ) as target:
        name = target.name
        process = subprocess.Popen(["zstd", "-q", "-c"], stdin=subprocess.PIPE, stdout=target)
        try:
            assert process.stdin is not None
            with tarfile.open(fileobj=process.stdin, mode="w|") as tar:
                tar.add(WORKSPACE / root, arcname=root, filter=exclude)
            process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError("zstd failed")
        except Exception:
            if process.poll() is None:
                process.kill()
            process.wait()
            os.unlink(name)
            raise
    return name


async def archive(request) -> Response:
    name = make_archive()
    fallback = os.path.getsize(name) > settings.archive_max_bytes
    if fallback:
        os.unlink(name)
        name = make_archive(agents_only=True)
    return FileResponse(
        name,
        media_type="application/zstd",
        filename="workspace.tar.zst",
        headers={"X-Archive-Fallback": "agents-only" if fallback else "none"},
        background=BackgroundTask(os.unlink, name),
    )


class TokenAuth:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        expected = b"Bearer " + settings.envd_token.get_secret_value().encode()
        if not hmac.compare_digest(headers.get(b"authorization", b""), expected):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app):
    prepare_workspace()
    async with mcp.session_manager.run():
        yield


app = TokenAuth(
    Starlette(
        routes=[
            Route("/health", health),
            Route("/users", users, methods=["POST"]),
            Route("/files", files),
            Route("/stat", stat),
            Route("/archive", archive, methods=["POST"]),
            *mcp.streamable_http_app().routes,
        ],
        lifespan=lifespan,
    )
)
