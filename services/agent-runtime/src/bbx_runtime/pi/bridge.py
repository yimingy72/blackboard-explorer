"""Bounded subprocess transport for Pi's native JavaScript runtime.

This is only a language boundary. It neither schedules model/tool iterations nor
retries unknown work; those are Pi and platform responsibilities respectively.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

MAX_FRAME_BYTES = 64 * 1024 * 1024
Callback = Callable[[str, dict[str, Any]], Awaitable[Any]]


class PiBridgeError(RuntimeError):
    pass


def worker_path() -> Path:
    configured = os.environ.get("BBX_PI_WORKER")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "pi" / "worker.mjs"


class PiBridge:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._callbacks: dict[str, Callback] = {}
        self._events: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._errors: dict[str, BaseException] = {}
        self._contexts: dict[str, contextvars.Context] = {}
        self._task_names: dict[str, str] = {}
        self._handlers: set[asyncio.Task] = set()
        self._callback_tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        async with self._start_lock:
            if self.process is not None and self.process.returncode is None:
                return
            path = worker_path()
            if not path.is_file():
                raise PiBridgeError("Pi worker is not installed; install the locked Pi runtime")
            # Never hand model credentials or the platform's service environment
            # to Node as ambient provider auth or user configuration.
            allowed = {"PATH", "SYSTEMROOT", "TMPDIR", "LANG", "NODE_EXTRA_CA_CERTS"}
            environment = {key: value for key, value in os.environ.items() if key in allowed}
            environment.update(
                {
                    key: value
                    for key, value in os.environ.items()
                    if key.lower() in {"http_proxy", "https_proxy", "no_proxy"}
                }
            )
            environment["NODE_USE_ENV_PROXY"] = "1"
            try:
                self.process = await asyncio.create_subprocess_exec(
                    os.environ.get("BBX_PI_NODE", "node"),
                    str(path),
                    cwd=path.parent,
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_FRAME_BYTES,
                )
            except OSError as error:
                raise PiBridgeError(
                    "Pi requires Node.js >=22.21 and locked dependencies"
                ) from error
            self._reader = asyncio.create_task(self._read())
            self._stderr = asyncio.create_task(self._drain_stderr())
            request_id = "startup-" + str(uuid4())
            ready = asyncio.get_running_loop().create_future()
            self._pending[request_id] = ready
            try:
                await self._send(
                    {"type": "call", "id": request_id, "method": "health", "params": {}}
                )
                async with asyncio.timeout(10):
                    health = await asyncio.shield(ready)
                version = tuple(int(part) for part in health["node"].split(".")[:2])
                if (
                    health.get("engine") != "pi"
                    or version < (22, 21)
                    or health.get("versions")
                    != {
                        "pi-agent-core": "1.4.0",
                        "pi-ai": "1.4.0",
                        "pi-telemetry": "1.4.0",
                    }
                ):
                    raise PiBridgeError("Pi runtime version differs from the locked dependency set")
            except BaseException:
                await self.close()
                if ready.done() and not ready.cancelled():
                    ready.exception()
                else:
                    ready.cancel()
                raise
            finally:
                self._pending.pop(request_id, None)

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while await self.process.stderr.read(8192):
            # Module/SDK error messages can embed URLs, arguments and credentials.
            # Expose the fixed startup/failure diagnostic, never raw stderr.
            pass

    async def _send(self, frame: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        data = json.dumps(frame, ensure_ascii=False, default=str).encode() + b"\n"
        if len(data) > MAX_FRAME_BYTES:
            raise PiBridgeError("Pi transport frame exceeds the supported context size")
        async with self._write_lock:
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def _callback(self, frame: dict) -> None:
        run_id = frame["run_id"]
        handler = self._callbacks.get(run_id)
        try:
            if handler is None:
                raise PiBridgeError("Pi run has no active platform callback")
            result = await handler(frame["method"], frame.get("params", {}))
            await self._send({"type": "reply", "id": frame["id"], "result": result})
        except BaseException as error:
            token = str(uuid4())
            self._errors[token] = error
            try:
                await self._send(
                    {
                        "type": "reply",
                        "id": frame["id"],
                        "error": {"code": "platform_callback", "token": token},
                    }
                )
            except (OSError, PiBridgeError):
                pass

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                frame = json.loads(line)
                kind = frame.get("type")
                if kind == "callback":
                    context = self._contexts.get(frame["run_id"])
                    task = asyncio.create_task(
                        self._callback(frame),
                        context=context.copy() if context else None,
                        name=self._task_names.get(frame["run_id"]),
                    )
                    self._handlers.add(task)
                    task.add_done_callback(self._handlers.discard)
                    self._callback_tasks[frame["id"]] = task
                    task.add_done_callback(
                        lambda _task, key=frame["id"]: self._callback_tasks.pop(key, None)
                    )
                elif kind == "cancel_callback":
                    task = self._callback_tasks.get(frame["id"])
                    if task is not None:
                        task.cancel()
                elif kind == "event":
                    handler = self._events.get(frame["id"])
                    if handler is not None:
                        await handler(frame["event"])
                elif kind == "result":
                    future = self._pending.get(frame["id"])
                    if future is None or future.done():
                        continue
                    error = frame.get("error")
                    if error:
                        original = self._errors.pop(error.get("token"), None)
                        future.set_exception(
                            original
                            or PiBridgeError(
                                f"Pi operation failed ({error.get('code', 'unknown')})"
                            )
                        )
                    else:
                        future.set_result(frame.get("result"))
        except (OSError, ValueError, asyncio.LimitOverrunError) as error:
            failure = PiBridgeError("Pi transport failed; in-flight work will not be replayed")
            failure.__cause__ = error
        else:
            failure = PiBridgeError("Pi process exited; in-flight work will not be replayed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        callback: Callback | None = None,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ) -> Any:
        await self.start()
        request_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._contexts[request_id] = contextvars.copy_context()
        current = asyncio.current_task()
        if current:
            self._task_names[request_id] = current.get_name()
        if callback is not None:
            self._callbacks[request_id] = callback
        if on_event is not None:
            self._events[request_id] = on_event
        try:
            await self._send({"type": "call", "id": request_id, "method": method, "params": params})
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await self._send({"type": "cancel", "id": request_id})
            # Cancellation stops the native loop before returning to a caller
            # that may drain envd. A lost worker never authorizes a retry.
            for task in tuple(self._handlers):
                if task is not asyncio.current_task():
                    task.cancel()
            try:
                async with asyncio.timeout(10):
                    await asyncio.shield(future)
            except (Exception, asyncio.CancelledError):
                await self.close()
            raise
        finally:
            self._pending.pop(request_id, None)
            self._callbacks.pop(request_id, None)
            self._events.pop(request_id, None)
            self._contexts.pop(request_id, None)
            self._task_names.pop(request_id, None)

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            async with asyncio.timeout(2):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
        for task in (*tuple(self._handlers), self._reader, self._stderr):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(
            *[
                task
                for task in (*tuple(self._handlers), self._reader, self._stderr)
                if task is not None and task is not asyncio.current_task()
            ],
            return_exceptions=True,
        )
        self.process = None
        self._errors.clear()


async def ensure_pi_runtime() -> None:
    bridge = PiBridge()
    try:
        await bridge.start()
    finally:
        await bridge.close()
