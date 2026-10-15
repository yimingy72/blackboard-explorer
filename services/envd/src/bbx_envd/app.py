"""Authenticated internal API and MCP command server."""

import asyncio
import fcntl
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
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast

from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from bbx_envd.core import (
    COMMAND_LOG,
    SUDO_LOG,
    WORKSPACE,
    CtfCommandRegistry,
    execute_command,
    prepare_audit_logs,
    prepare_workspace,
    valid_agent_id,
    workspace_path,
)
from bbx_envd.networks import AgentNetworks
from bbx_envd.settings import Settings
from bbx_envd.traffic import TrafficCapture, control_capture_filter

settings = Settings()  # pyright: ignore[reportCallIssue]
mcp = FastMCP("envd", host="0.0.0.0")
RESTORE_MARKER = Path("/var/lib/bbx/restore-ready")
AUDIT_MAX_BYTES = 32 * 1024 * 1024
CTF_BOOT_MARKER = Path("/var/lib/bbx/ctf-boot")
ctf_registry: CtfCommandRegistry | None = None
traffic_capture: TrafficCapture | None = None


@mcp.tool(name="execute_command")
async def execute_command_tool(
    command: str,
    cwd: str | None = None,
    timeout_sec: int = 120,
    privileged: bool = False,
    *,
    ctx: Context,
) -> dict[str, object]:
    """Run a command in the current agent's workspace and network namespace.

    Localhost belongs to this Agent. BBX_AGENT_IP and BBX_TASK_GATEWAY identify its
    task network. Shared services should listen on 0.0.0.0; find other Agents at
    /workspace/shared/network.json. Files and installed tools remain shared.
    """
    request = ctx.request_context.request
    agent_id = request.headers.get("X-Agent-Id", "") if request is not None else ""
    if settings.envd_mode == "ctf":
        if ctf_registry is None:
            raise ValueError("CTF registry is not initialized")
        meta = ctx.request_context.meta
        metadata = meta.model_dump().get("ctf") if meta is not None else None
        if not isinstance(metadata, dict):
            raise ValueError("CTF command metadata required")
        return await ctf_registry.execute(
            agent_id, metadata, command, cwd, timeout_sec, privileged, settings, traffic_capture
        )
    return await execute_command(
        agent_id, command, cwd, timeout_sec, privileged, settings, traffic=traffic_capture
    )


async def ctf_control(request) -> Response:
    if settings.envd_mode != "ctf" or ctf_registry is None:
        return JSONResponse({"error": "CTF mode unavailable"}, status_code=409)
    try:
        if request.method == "GET":
            agent_id = request.query_params.get("agent_id")
            result = ctf_registry.status(agent_id)
            if agent_id is not None and "generation" in request.query_params:
                if result.get("generation") != int(request.query_params["generation"]):
                    raise ValueError("Stale generation")
            return JSONResponse(result)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Object body required")
        if request.url.path.endswith("/register"):
            result = ctf_registry.register(body)
            if traffic_capture is not None:
                await traffic_capture.prepare_agent(
                    body["agent_id"], body["member_id"], str(settings.envd_task_id)
                )
        else:
            result = await ctf_registry.stop(body)
        return JSONResponse(result)
    except (ValueError, TypeError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


async def health(request) -> Response:
    result = {"status": "ok", "runtime_audit": True}
    if settings.envd_traffic_enabled:
        result["traffic"] = True
    return JSONResponse(result)


async def traffic_control(request) -> Response:
    capture = traffic_capture
    if capture is None:
        return JSONResponse({"error": "Traffic capture unavailable"}, status_code=409)
    action = request.path_params.get("action", "status")
    identifier = request.path_params.get("identifier")
    try:
        if action == "native":
            if identifier is not None:
                if capture.native is None:
                    raise FileNotFoundError("Native input unavailable")
                fd = capture.native.open_part(identifier)
                source = os.fdopen(fd, "rb")
                return StreamingResponse(
                    iter(lambda: source.read(65536), b""),
                    media_type="application/octet-stream",
                    headers={"Content-Length": str(os.fstat(fd).st_size)},
                    background=BackgroundTask(source.close),
                )
            if request.method == "POST":
                body = await request.json()
                if not isinstance(body, dict) or not isinstance(body.get("parts"), list):
                    raise ValueError("Native part array required")
                if capture.native is None:
                    raise ValueError("Native input unavailable")
                capture.native.ack(body["parts"])
            return JSONResponse(capture.native_listing())
        if identifier is not None:
            if action == "records":
                detail = capture.detail(identifier)
                if detail is None:
                    return JSONResponse({"error": "Record unavailable"}, status_code=404)
                return JSONResponse(detail)
            fd = capture.open_segment(identifier, action)
            source = os.fdopen(fd, "rb")
            media = "application/x-ndjson" if action == "chunks" else "application/octet-stream"
            return StreamingResponse(
                iter(lambda: source.read(65536), b""),
                media_type=media,
                headers={"Content-Length": str(os.fstat(fd).st_size)},
                background=BackgroundTask(source.close),
            )
        if request.method == "POST":
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Object body required")
            if action == "start":
                task_id = str(body["task_id"])
                if settings.envd_task_id is not None and task_id != str(settings.envd_task_id):
                    raise ValueError("Wrong task identity")
                peer, local = request.scope.get("client"), request.scope.get("server")
                if peer is None or local is None or local[1] != 8080:
                    raise ValueError("Verified control endpoint required")
                capture.control_filter = control_capture_filter(peer[0], local[0])
                result = await capture.start(task_id, body.get("run_number", body.get("run", 1)))
            elif action == "seal":
                result = await capture.seal()
            elif action == "stop":
                result = await capture.stop()
            elif action == "limit":
                result = await capture.limit()
            elif action == "ack":
                chunks, artifacts = body.get("chunks", []), body.get("artifacts", [])
                if not isinstance(chunks, list) or not isinstance(artifacts, list):
                    raise ValueError("Segment arrays required")
                result = capture.ack(chunks, artifacts)
            else:
                raise ValueError("Unknown traffic operation")
            return JSONResponse(result)
        if action in {"chunks", "artifacts"}:
            collection = capture.chunks if action == "chunks" else capture.artifacts
            return JSONResponse(
                {
                    "source_id": capture.status().get("source_id"),
                    "items": list(collection.values()),
                    "status": capture.status(),
                }
            )
        if action == "records":
            after = int(request.query_params.get("after_sequence", "0"))
            limit = int(request.query_params.get("limit", "100"))
            if after < 0 or not 1 <= limit <= 1000:
                raise ValueError("Invalid pagination")
            agent = request.query_params.get("agent_id")
            records = [
                item
                for item in capture.details.values()
                if item["seq"] > after and (agent is None or item.get("agent_id") == agent)
            ]
            records.sort(key=lambda item: item["seq"])
            items = [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"request", "response", "observation", "packet_detail"}
                }
                for item in records[:limit]
            ]
            return JSONResponse(
                {
                    "items": items,
                    "next_sequence": items[-1]["seq"] if items else after,
                    "has_more": len(records) > limit,
                    "status": capture.status(),
                }
            )
        return JSONResponse(capture.status())
    except FileNotFoundError:
        return JSONResponse({"error": "Sealed segment unavailable"}, status_code=404)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return JSONResponse({"error": "Invalid traffic request or capture state"}, status_code=409)
    except OSError:
        return JSONResponse({"error": "Traffic data unavailable"}, status_code=503)


async def audit(request) -> Response:
    try:
        contents: list[str] = []
        remaining = AUDIT_MAX_BYTES
        for path in (COMMAND_LOG, SUDO_LOG):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat_mode.S_ISREG(os.fstat(fd).st_mode):
                    return JSONResponse({"error": "audit log is not a file"}, status_code=503)
                with os.fdopen(fd, "rb", closefd=False) as source:
                    data = source.read(remaining + 1)
                if len(data) > remaining:
                    return JSONResponse({"error": "audit logs too large"}, status_code=413)
                remaining -= len(data)
                if traffic_capture is not None:
                    data = traffic_capture.redact_output(data)
                contents.append(data.decode("utf-8", errors="replace"))
            finally:
                os.close(fd)
    except FileNotFoundError:
        return JSONResponse({"error": "audit log missing"}, status_code=503)
    except OSError:
        return JSONResponse({"error": "audit log unavailable"}, status_code=503)
    return JSONResponse(
        {"format": "bbx.runtime-audit.v1", "commands": contents[0], "sudo": contents[1]}
    )


async def users(request) -> Response:
    try:
        body = await request.json()
        agent_id = body["agent_id"]
        if not isinstance(agent_id, str) or not valid_agent_id(agent_id):
            raise ValueError("invalid agent id")
        canonical_agent_id = body.get("canonical_agent_id", agent_id)
        if not isinstance(canonical_agent_id, str):
            raise ValueError("invalid canonical agent id")
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
    try:
        subprocess.run(
            ["usermod", "-a", "-G", "bbx-agents", agent_id],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        return JSONResponse({"error": exc.stderr.decode(errors="replace")}, status_code=500)
    owner = pwd.getpwnam(agent_id)
    os.chown(home, owner.pw_uid, owner.pw_gid)
    home.chmod(0o755)
    if traffic_capture is not None and settings.envd_mode != "ctf":
        try:
            await traffic_capture.prepare_agent(
                agent_id,
                canonical_agent_id,
                str(settings.envd_task_id) if settings.envd_task_id is not None else None,
            )
        except (OSError, ValueError):
            return JSONResponse(
                {"error": "Agent network unavailable; command execution disabled"}, status_code=409
            )
    return JSONResponse({"agent_id": agent_id, "home": str(home)})


def requested_path(request) -> Path | Response:
    try:
        path = workspace_path(request.query_params["path"])
        check_file_scope(request, path)
        return path
    except KeyError:
        return JSONResponse({"error": "missing path"}, status_code=400)
    except (ValueError, FileNotFoundError, OSError):
        return JSONResponse({"error": "invalid path"}, status_code=400)


def check_file_scope(request, path: Path) -> None:
    """Apply a trusted caller's optional member scope after resolving symlinks."""
    agent_id = request.query_params.get("scope_agent_id")
    if agent_id is None:
        return
    if not valid_agent_id(agent_id):
        raise ValueError("Invalid file scope")
    root = WORKSPACE.resolve(strict=True)
    if not any(
        path.is_relative_to(directory)
        for directory in (root / "agents" / agent_id, root / "shared")
    ):
        raise ValueError("File is outside the member's permitted workspace")


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
        check_file_scope(request, path)
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


class ArchiveTooLarge(ValueError):
    pass


class ArchiveWriter:
    """Apply the restore size limit to the exact uncompressed tar stream."""

    def __init__(self, output: BinaryIO, limit: int) -> None:
        self.output, self.limit, self.size = output, limit, 0

    def write(self, data: bytes) -> int:
        self.size += len(data)
        if self.size > self.limit:
            raise ArchiveTooLarge("expanded archive too large")
        return self.output.write(data)


def make_archive(agents_only: bool = False) -> str:
    rules = [
        item.strip().rstrip("/") for item in settings.archive_exclude.split(",") if item.strip()
    ]

    entries = 0

    def exclude(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        nonlocal entries
        if any(fnmatch.fnmatch(info.name, rule) for rule in rules):
            return None
        entries += 1
        if entries > 100000:
            raise ArchiveTooLarge("too many archive entries")
        return info

    root = "agents" if agents_only else "."
    with tempfile.NamedTemporaryFile(
        prefix="bbx-m2env-", suffix=".tar.zst", delete=False
    ) as target:
        name = target.name
        process = subprocess.Popen(["zstd", "-q", "-c"], stdin=subprocess.PIPE, stdout=target)
        try:
            assert process.stdin is not None
            writer = ArchiveWriter(cast(BinaryIO, process.stdin), settings.archive_max_bytes * 4)
            with tarfile.open(fileobj=cast(BinaryIO, writer), mode="w|") as tar:
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
    name = None
    try:
        name = await asyncio.to_thread(make_archive)
        fallback = os.path.getsize(name) > settings.archive_max_bytes
    except ArchiveTooLarge:
        fallback = True
    if fallback:
        if name is not None:
            os.unlink(name)
        try:
            name = await asyncio.to_thread(make_archive, agents_only=True)
        except ArchiveTooLarge:
            return JSONResponse({"error": "expanded agent archive too large"}, status_code=413)
        if os.path.getsize(name) > settings.archive_max_bytes:
            os.unlink(name)
            return JSONResponse({"error": "agent archive too large"}, status_code=413)
    assert name is not None
    return FileResponse(
        name,
        media_type="application/zstd",
        filename="workspace.tar.zst",
        headers={"X-Archive-Fallback": "agents-only" if fallback else "none"},
        background=BackgroundTask(os.unlink, name),
    )


def restore_archive(compressed: Path, unpacked: Path) -> int:
    RESTORE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    with (RESTORE_MARKER.parent / "restore.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if RESTORE_MARKER.exists():
            return int(RESTORE_MARKER.read_text())
        return _restore_archive(compressed, unpacked)


def _restore_archive(compressed: Path, unpacked: Path) -> int:
    """Validate every member before extraction; restore only regular files and directories."""
    with unpacked.open("wb") as output:
        process = subprocess.Popen(["zstd", "-dc", str(compressed)], stdout=subprocess.PIPE)
        try:
            assert process.stdout is not None
            expanded = 0
            while chunk := process.stdout.read(65536):
                expanded += len(chunk)
                if expanded > settings.archive_max_bytes * 4:
                    raise ValueError("expanded archive too large")
                output.write(chunk)
            if process.wait() != 0:
                raise ValueError("invalid compressed archive")
        except Exception:
            process.kill()
            process.wait()
            raise
    with tarfile.open(unpacked) as tar:
        members = []
        for member in tar:
            if len(members) >= 100000:
                raise ValueError("too many archive entries")
            members.append(member)
        total = 0
        skipped_links = 0
        owners: set[str] = set()
        for member in members:
            path = PurePosixPath(member.name)
            if member.name.startswith("/") or ".." in path.parts:
                raise ValueError("unsafe archive path")
            if member.issym() or member.islnk():
                skipped_links += 1
                continue
            if not (member.isdir() or member.isfile()):
                raise ValueError("unsafe archive entry")
            total += member.size
            if total > settings.archive_max_bytes * 4:
                raise ValueError("expanded archive too large")
            if len(path.parts) >= 2 and path.parts[0] == "agents":
                if not valid_agent_id(path.parts[1]):
                    raise ValueError("invalid agent directory")
                owners.add(path.parts[1])
            if valid_agent_id(member.uname):
                owners.add(member.uname)
        for agent_id in owners:
            try:
                pwd.getpwnam(agent_id)
            except KeyError:
                subprocess.run(
                    [
                        "useradd",
                        "-M",
                        "-d",
                        str(WORKSPACE / "agents" / agent_id),
                        "-s",
                        "/bin/bash",
                        agent_id,
                    ],
                    check=True,
                    capture_output=True,
                )
            subprocess.run(
                ["usermod", "-a", "-G", "bbx-agents", agent_id],
                check=True,
                capture_output=True,
            )
        for member in members:
            if not (member.isdir() or member.isfile()):
                continue
            tar.extract(member, WORKSPACE, filter="data")
            path = PurePosixPath(member.name)
            owner_name = (
                path.parts[1]
                if len(path.parts) >= 2 and path.parts[0] == "agents"
                else member.uname
                if valid_agent_id(member.uname)
                else None
            )
            if owner_name:
                owner = pwd.getpwnam(owner_name)
                os.chown(WORKSPACE / path, owner.pw_uid, owner.pw_gid)
        (WORKSPACE / "shared").chmod(0o1777)
    RESTORE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    RESTORE_MARKER.write_text(str(skipped_links))
    return skipped_links


async def restore_status(request) -> Response:
    return JSONResponse(
        {
            "restored": RESTORE_MARKER.exists(),
            "skipped_links": int(RESTORE_MARKER.read_text()) if RESTORE_MARKER.exists() else 0,
        }
    )


async def restore(request) -> Response:
    if RESTORE_MARKER.exists():
        return JSONResponse({"restored": True, "skipped_links": int(RESTORE_MARKER.read_text())})
    with tempfile.TemporaryDirectory(prefix="bbx-restore-") as temporary:
        compressed = Path(temporary) / "workspace.tar.zst"
        unpacked = Path(temporary) / "workspace.tar"
        size = 0
        with compressed.open("wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.archive_max_bytes:
                    return JSONResponse({"error": "archive too large"}, status_code=413)
                output.write(chunk)
        work = asyncio.create_task(asyncio.to_thread(restore_archive, compressed, unpacked))
        try:
            skipped_links = await asyncio.shield(work)
        except asyncio.CancelledError:
            await work
            raise
        except (ValueError, tarfile.TarError, OSError, subprocess.CalledProcessError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"restored": True, "skipped_links": skipped_links})


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
    global ctf_registry, traffic_capture
    if settings.envd_mode == "ctf":
        ctf_registry = CtfCommandRegistry(str(settings.envd_task_id), CTF_BOOT_MARKER)
    prepare_workspace()
    prepare_audit_logs()
    if settings.envd_traffic_enabled:
        traffic_capture = TrafficCapture(
            Path("/var/lib/bbx/traffic"),
            settings.envd_traffic_max_bytes,
            settings.envd_traffic_max_records,
        )
        traffic_capture.networks = AgentNetworks(Path("/var/lib/bbx/network"), WORKSPACE / "shared")
        traffic_capture.restore()
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        if traffic_capture is not None and traffic_capture.state is not None:
            await traffic_capture.stop()
        if traffic_capture is not None and traffic_capture.networks is not None:
            await traffic_capture.networks.close()


app = TokenAuth(
    Starlette(
        routes=[
            Route("/health", health),
            Route("/traffic/{action}/parts/{identifier}", traffic_control),
            Route("/traffic/{action}/ack", traffic_control, methods=["POST"]),
            Route("/traffic/{action}", traffic_control, methods=["GET", "POST"]),
            Route("/traffic/{action}/{identifier}", traffic_control),
            Route("/ctf/status", ctf_control),
            Route("/ctf/register", ctf_control, methods=["POST"]),
            Route("/ctf/stop", ctf_control, methods=["POST"]),
            Route("/ctf/drain", ctf_control, methods=["POST"]),
            Route("/audit", audit),
            Route("/users", users, methods=["POST"]),
            Route("/files", files),
            Route("/stat", stat),
            Route("/archive", archive, methods=["POST"]),
            Route("/restore", restore, methods=["POST"]),
            Route("/restore/status", restore_status),
            *mcp.streamable_http_app().routes,
        ],
        lifespan=lifespan,
    )
)
