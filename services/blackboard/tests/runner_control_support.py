"""Own a real local HTTP server and real Runner child for one PostgreSQL scenario."""

import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn


class LostPrepareAck:
    def __init__(self, app, path):
        self.app, self.path = app, path
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.dropped = False
        self.requests = {}

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        self.requests[path] = self.requests.get(path, 0) + 1

        async def intercept(message):
            if (
                scope.get("path") == self.path
                and message["type"] == "http.response.start"
                and message["status"] == 200
                and not self.dropped
            ):
                self.dropped = True
                self.entered.set()
                await self.release.wait()
            await send(message)

        await self.app(scope, receive, intercept)


@asynccontextmanager
async def live_http(app, ack_path):
    wrapped = LostPrepareAck(app, ack_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            wrapped,
            lifespan="off",
            access_log=False,
            log_level="critical",
            timeout_graceful_shutdown=1,
        )
    )
    running = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if running.done():
                await running
                raise RuntimeError("Local Blackboard did not start")
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}", wrapped
    finally:
        wrapped.release.set()
        server.should_exit = True
        await asyncio.wait_for(running, 5)
        sock.close()


class NodeRunner:
    def __init__(self, process, journal_root):
        self.process, self.journal_root = process, Path(journal_root)

    async def command(self, action, data=None):
        self.process.stdin.write((json.dumps({"action": action, "data": data}) + "\n").encode())
        await self.process.stdin.drain()
        line = await asyncio.wait_for(self.process.stdout.readline(), 10)
        if not line:
            raise RuntimeError(
                "Runner fixture exited: " + (await self.process.stderr.read()).decode()
            )
        return json.loads(line)


@asynccontextmanager
async def node_runner(data, directory):
    path = directory / "runner-bootstrap-source.json"
    path.write_text(json.dumps(data))
    script = Path(__file__).with_name("runner_control_fixture.mjs")
    process = await asyncio.create_subprocess_exec(
        "node",
        str(script),
        str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", ""), "PI_OFFLINE": "1"},
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        if not line:
            raise RuntimeError("Runner fixture failed: " + (await process.stderr.read()).decode())
        yield NodeRunner(process, json.loads(line)["journal_root"])
    finally:
        if process.returncode is None:
            process.stdin.write(b'{"action":"exit"}\n')
            await process.stdin.drain()
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 8)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
        if process.returncode:
            raise RuntimeError("Runner fixture failed: " + (await process.stderr.read()).decode())
