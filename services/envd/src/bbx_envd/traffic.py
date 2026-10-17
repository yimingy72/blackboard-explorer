"""Task-local official capture processes and acknowledged immutable segments."""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bbx_envd.native import NativeParts
from bbx_envd.networks import AgentNetworks
from bbx_envd.packets import (
    packet_ifindex,
    packet_record,
)
from bbx_envd.traffic_format import complete_wire_detail

SAFE_ID = re.compile(r"[a-zA-Z0-9_-]{1,80}\Z")


def now() -> str:
    return datetime.now(UTC).isoformat()


def control_capture_filter(peer: str, local: str) -> str:
    peer = str(ipaddress.ip_address(peer))
    local = str(ipaddress.ip_address(local))
    if ipaddress.ip_address(local).is_unspecified:
        raise ValueError("A concrete local control address is required")
    return (
        f"not (tcp and ((src host {peer} and dst host {local} and dst port 8080) "
        f"or (src host {local} and src port 8080 and dst host {peer})))"
    )


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as target:
        json.dump(value, target, separators=(",", ":"))
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)


class TrafficCapture:
    def __init__(self, root: Path, max_bytes: int, max_records: int) -> None:
        self.root, self.max_bytes, self.max_records = root, max_bytes, max_records
        self.state: dict[str, Any] | None = None
        self.directory: Path | None = None
        self.credentials: dict[str, Any] = {}
        self.identities: dict[str, str] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.readers: list[asyncio.Task] = []
        self.poller: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.current: list[dict[str, Any]] = []
        self.details: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[str, Any]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.paths: dict[str, str] = {}
        self.proxy_port: int | None = None
        self.ca_bundle: str | None = None
        self.reasons: list[str] = []
        self.stopping = False
        self.drop_statistics_seen = False
        self.networks: AgentNetworks | None = None
        self.proxy_host = "127.0.0.1"
        self.raw_owners: dict[str, str | None] = {}
        self.drop_counts: dict[str, int] = {}
        self.limited = False
        self.recovered_raw: set[str] = set()
        self.control_filter: str | None = None
        self.raw_ready: set[str] = set()
        self.raw_readiness: dict[str, asyncio.Event] = {}
        self.packet_numbers: dict[str, int] = {}
        self.raw_ranges: dict[str, list[dict[str, Any]]] = {}
        self.active_raw: dict[str, str] = {}
        self.interface_names: dict[str, dict[str, str]] = {}
        self.packet_queue: asyncio.Queue[tuple[str, int, bytes]] = asyncio.Queue(maxsize=1024)
        self.packet_queue_bytes = 0
        self.packet_worker: asyncio.Task | None = None
        self.current_bytes = 0
        self.current_lines: list[bytes] = []
        self.active_fd: int | None = None
        self.last_seal = 0.0
        self.native: NativeParts | None = None
        self.last_native = 0.0
        self.packet_times: dict[str, tuple[int, str]] = {}

    def restore(self) -> None:
        if not self.root.exists():
            return
        try:
            fd = os.open(self.root / "current.json", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as source:
                saved = json.load(source)
            self.state = saved["state"]
            assert self.state is not None
            identifier = self.state["source_id"]
            if not SAFE_ID.fullmatch(identifier):
                raise ValueError("Invalid capture source")
            directory = self.root / str(identifier)
            self.directory = directory
            self.identities = saved.get("identities", {})
            if self.state.get("native_enabled"):
                self.native = NativeParts(
                    directory, self.state["task_id"], self.state["run"], self.max_bytes
                )
                # Rotation may reach disk just before its stderr event or a crash.
                for path in sorted(directory.glob("raw_*.pcapng")):
                    match = re.fullmatch(
                        r"raw_(?:(agent-[0-9]+)_)?[0-9]+_[0-9]+\.pcapng", path.name
                    )
                    if match and path.name not in self.native.segments:
                        linux_agent = match[1]
                        self.native.add_segment(
                            identifier + "-raw" + ("-" + linux_agent if linux_agent else ""),
                            self.identities.get(linux_agent) if linux_agent else None,
                            path.name,
                        )
                interrupted = self.state["status"] == "collecting" or self.state.get(
                    "proxy_serving"
                )
                self.native.drain(
                    final=True,
                    error="envd restarted; capture epoch ended" if interrupted else None,
                )
            self.chunks, self.artifacts = saved["chunks"], saved["artifacts"]
            self.paths, self.reasons = saved["paths"], saved["reasons"]
            self.identities = saved.get("identities", {})
            self.recovered_raw = set(saved.get("recovered_raw", []))
            self.packet_numbers = saved.get("packet_numbers", {})
            self.raw_ranges = saved.get("raw_ranges", {})
            if self.state["status"] == "collecting" or self.state.get("proxy_serving"):
                self.partial("envd restarted; previous capture process state is unknown")
                self.state["status"] = "partial"
                self.state["https"] = "unavailable"
                self.state.update(capturing=False, proxy_serving=False)
            self.recover_segments()
            try:
                fd = self.open_name("active.jsonl")
                with os.fdopen(fd) as active:
                    for line in active:
                        try:
                            detail = json.loads(line)
                        except ValueError:
                            self.partial("An interrupted decoded record was incomplete")
                            break
                        if any(
                            chunk["first_seq"] <= detail["seq"] <= chunk["last_seq"]
                            for chunk in self.chunks.values()
                        ):
                            continue
                        self.current.append(detail)
                        encoded = (
                            json.dumps(detail, ensure_ascii=False, separators=(",", ":")) + "\n"
                        ).encode()
                        self.current_lines.append(encoded)
                        self.current_bytes += len(encoded)
                        self.details[detail["id"]] = detail
                        self.state["record_count"] = max(self.state["record_count"], detail["seq"])
                self.seal_records()
            except FileNotFoundError:
                pass
            self.persist()
        except FileNotFoundError:
            return

    def recover_segments(self) -> None:
        """Keep immutable snapshots of files interrupted before their index was published."""
        assert self.directory is not None and self.state is not None
        for path in sorted(self.directory.glob("c-*.jsonl")):
            if path.name in self.paths.values() or not SAFE_ID.fullmatch(path.stem):
                continue
            fd = self.open_name(path.name)
            with os.fdopen(fd, "rb") as source:
                content = source.read(8 * 1024 * 1024 + 1)
            if len(content) > 8 * 1024 * 1024:
                self.partial("Interrupted metadata segment exceeds recovery limit")
                continue
            try:
                items = [json.loads(line) for line in content.splitlines()]
            except ValueError:
                self.partial("An interrupted metadata segment could not be recovered")
                continue
            if not items or len(items) > 128:
                self.partial("Interrupted metadata segment has an invalid record count")
                continue
            identifier = path.stem
            self.paths[identifier] = path.name
            self.chunks[identifier] = {
                "id": identifier,
                "first_seq": items[0]["seq"],
                "last_seq": items[-1]["seq"],
                "count": len(items),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            self.state["record_count"] = max(self.state["record_count"], items[-1]["seq"])
        for path in sorted(self.directory.glob("raw_*.pcapng")):
            if path.name in self.paths.values() or path.name in self.recovered_raw:
                continue
            match = re.fullmatch(r"raw_(?:(agent-[0-9]+)_)?[0-9]+_[0-9]+\.pcapng", path.name)
            if match is None:
                continue
            identifier = "p-" + uuid.uuid4().hex
            snapshot = self.directory / ("recovered-" + identifier + ".pcapng")
            fd = self.open_name(path.name)
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb") as source, snapshot.open("xb") as output:
                os.chmod(snapshot, 0o600)
                remaining = min(os.fstat(source.fileno()).st_size, self.max_bytes)
                while remaining:
                    block = source.read(min(remaining, 65536))
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
            owner = self.identities.get(match[1]) if match[1] else None
            self.paths[identifier] = snapshot.name
            self.artifacts[identifier] = {
                "id": identifier,
                "kind": "pcap",
                "name": snapshot.name,
                "size_bytes": snapshot.stat().st_size,
                "sha256": digest.hexdigest(),
                "content_type": "application/octet-stream",
                "sealed": True,
                "agent_id": owner,
                "source_id": self.state["source_id"]
                + "-raw"
                + (f"-{match[1]}" if match[1] else ""),
            }
            self.state["captured_bytes"] += snapshot.stat().st_size
            self.recovered_raw.add(path.name)
            self.partial("Interrupted raw segment recovered as a bounded snapshot")

    def persist(self) -> None:
        if self.state is None:
            return
        atomic_json(
            self.root / "current.json",
            {
                "state": self.state,
                "chunks": self.chunks,
                "artifacts": self.artifacts,
                "paths": self.paths,
                "reasons": self.reasons,
                "identities": self.identities,
                "recovered_raw": sorted(self.recovered_raw),
                "packet_numbers": self.packet_numbers,
                "raw_ranges": self.raw_ranges,
            },
        )

    def partial(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason[:512])
        if self.state is not None:
            self.state["error"] = "; ".join(self.reasons)[:4096]

    def status(self, *, include_items: bool = False) -> dict[str, Any]:
        if self.state is None:
            return {"status": "error", "https": "unavailable", "error": "Capture not started"}
        result = {
            **self.state,
            "updated_at": now(),
            "pending_chunks": len(self.chunks),
        }
        if include_items:
            result.update(chunks=list(self.chunks.values()), files=list(self.artifacts.values()))
        return result

    async def start(self, task_id: str, run_number: int) -> dict[str, Any]:
        task_id = str(uuid.UUID(task_id))
        if type(run_number) is not int or run_number < 1:
            raise ValueError("Invalid run_number")
        async with self.lock:
            if self.state is not None:
                if (self.state["task_id"], self.state["run"]) == (task_id, run_number):
                    return self.status()
                if (
                    self.state["status"] == "collecting"
                    or self.paths
                    or self.limited
                    or (self.native is not None and self.native.pending)
                ):
                    raise ValueError("Previous capture must be stopped and acknowledged")
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.root.chmod(0o700)
            identifier = uuid.uuid4().hex
            self.directory = self.root / identifier
            self.directory.mkdir(mode=0o700)
            self.native = NativeParts(self.directory, task_id, run_number, self.max_bytes)
            fd = os.open(self.directory / "tls.keys", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            control = self.directory / "control"
            control.mkdir(mode=0o700)
            (control / "inbox").mkdir(mode=0o700)
            self.reasons, self.credentials, self.paths = [], {}, {}
            self.stopping, self.drop_statistics_seen = False, False
            self.limited = False
            self.recovered_raw = set()
            self.processes, self.readers = {}, []
            self.raw_owners, self.drop_counts = {}, {}
            self.raw_ready, self.raw_readiness = set(), {}
            self.packet_numbers, self.raw_ranges, self.active_raw = {}, {}, {}
            self.packet_times = {}
            self.interface_names = {}
            self.packet_queue = asyncio.Queue(maxsize=1024)
            self.packet_queue_bytes = self.current_bytes = 0
            self.current_lines = []
            self.last_seal = asyncio.get_running_loop().time()
            self.packet_worker = asyncio.create_task(self.consume_packets())
            self.chunks, self.artifacts, self.details, self.current = {}, {}, {}, []
            atomic_json(control / "credentials.json", {})
            self.state = {
                "schema_version": 1,
                "task_id": task_id,
                "run": run_number,
                "source_id": identifier,
                "status": "collecting",
                "https": "unavailable",
                "started_at": now(),
                "updated_at": now(),
                "record_count": 0,
                "capturing": True,
                "proxy_serving": False,
                "captured_bytes": 0,
                "dropped_packets": 0,
                "preview_dropped": 0,
                "pending_chunks": 0,
                "last_packet_at": None,
                "native_enabled": True,
            }
            self.persist()
            if self.networks is not None:
                try:
                    await self.networks.initialize(task_id)
                except (OSError, ValueError):
                    self.partial("Task network gateway could not be initialized")
                    self.state["status"] = "error"
                    self.persist()
                    raise
                self.proxy_host = self.networks.gateway
            await self.start_raw()
            if self.networks is not None:
                for linux_agent in self.networks.agents:
                    await self.start_raw(linux_agent)
            await self.start_proxy()
            self.poller = asyncio.create_task(self.poll())
            self.persist()
            return self.status()

    async def start_raw(self, linux_agent: str | None = None) -> None:
        assert self.directory is not None
        if not shutil.which("dumpcap") or not shutil.which("tshark"):
            self.partial("dumpcap/tshark unavailable; raw capture was not started")
            return
        if linux_agent is None and self.control_filter is None:
            self.partial("Verified control endpoint unavailable; shared capture disabled")
            return
        role = "raw" if linux_agent is None else f"raw:{linux_agent}"
        try:
            if role in self.processes:
                await asyncio.wait_for(self.raw_readiness[role].wait(), 5)
                return
            self.raw_readiness[role] = asyncio.Event()
            prefix = (
                []
                if linux_agent is None
                else self.networks.prefix(linux_agent)
                if self.networks
                else []
            )
            stem = "raw" if linux_agent is None else f"raw_{linux_agent}"
            owner = (
                self.identities.get(linux_agent, linux_agent) if linux_agent is not None else None
            )
            self.raw_owners[role] = owner
            self.packet_numbers[role] = 0
            self.raw_ranges[role] = []
            await self.refresh_interfaces(role)
            process = await asyncio.create_subprocess_exec(
                *prefix,
                "tshark",
                "-l",
                "-n",
                "-P",
                "-V",
                "-T",
                "ek",
                "-x",
                "--log-level",
                "info",
                "-y",
                "LINUX_SLL2",
                "-o",
                f"tls.keylog_file:{self.directory / 'tls.keys'}",
                "-i",
                "any",
                "-s",
                "0",
                *(
                    ["-f", self.control_filter]
                    if linux_agent is None and self.control_filter
                    else []
                ),
                "-w",
                str(self.directory / (stem + ".pcapng")),
                "-b",
                "duration:10",
                "-b",
                "filesize:16384",
                "-a",
                "files:100000",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=128 * 1024,
                env={
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if key
                        not in {
                            "ENVD_TOKEN",
                            "ENVD_TOKEN_SECRET",
                            "SSLKEYLOGFILE",
                            "MITMPROXY_SSLKEYLOGFILE",
                        }
                    },
                    "LC_ALL": "C",
                },
            )
            self.processes[role] = process
            self.readers.extend(
                [
                    asyncio.create_task(self.raw_files(process, owner, role)),
                    asyncio.create_task(self.raw_errors(process, role)),
                ]
            )
            await asyncio.wait_for(self.raw_readiness[role].wait(), 5)
        except (OSError, TimeoutError):
            self.partial("Raw capture startup could not be confirmed")
            if role in self.processes:
                await self.stop_process(self.processes[role])

    async def start_proxy(self) -> None:
        assert self.directory is not None and self.state is not None
        executable = "/opt/traffic/venv/bin/mitmdump"
        if not Path(executable).is_file():
            self.partial("mitmdump unavailable; HTTPS decoding unavailable")
            return
        control = self.directory / "control"
        with socket.socket() as probe:
            probe.bind((self.proxy_host, 0))
            port = probe.getsockname()[1]
        upstream = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        mode = f"upstream:{upstream}" if upstream else "regular"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {"ENVD_TOKEN", "ENVD_TOKEN_SECRET", "SSLKEYLOGFILE", "MITMPROXY_SSLKEYLOGFILE"}
        }
        environment.update(
            BBX_TRAFFIC_CONTROL=str(control),
            PYTHONPATH=str(Path(__file__).resolve().parent.parent),
            MITMPROXY_SSLKEYLOGFILE=str(self.directory / "tls.keys"),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--quiet",
                "--mode",
                mode,
                "--listen-host",
                self.proxy_host,
                "--listen-port",
                str(port),
                "--set",
                "proxyauth=any",
                "--set",
                f"confdir={control / 'ca'}",
                "--set",
                "ssl_insecure=false",
                "--set",
                "ssl_verify_upstream_trusted_ca=/etc/ssl/certs/ca-certificates.crt",
                "--set",
                "flow_detail=0",
                "--scripts",
                str(Path(__file__).with_name("traffic_addon.py")),
                env=environment,
                start_new_session=True,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.processes["proxy"] = process
            for _ in range(100):
                if process.returncode is not None:
                    raise RuntimeError("mitmdump startup failed")
                if (control / "ready").is_file():
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("mitmdump startup timed out")
            certificate = control / "ca" / "mitmproxy-ca-cert.pem"
            shutil.copyfile(certificate, "/usr/local/share/ca-certificates/bbx-traffic.crt")
            trust = await asyncio.create_subprocess_exec(
                "update-ca-certificates",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await trust.wait() != 0:
                raise RuntimeError("Task CA trust installation failed")
            public = self.root.parent / "traffic-ca"
            public.mkdir(mode=0o755, exist_ok=True)
            public.chmod(0o755)
            bundle = public / "bundle.pem"
            bundle.write_bytes(
                Path("/etc/ssl/certs/ca-certificates.crt").read_bytes()
                + b"\n"
                + certificate.read_bytes()
            )
            bundle.chmod(0o644)
            self.ca_bundle = str(bundle)
            self.proxy_port = port
            self.state["https"] = "enabled"
            self.state["proxy_serving"] = True
        except (OSError, RuntimeError):
            self.partial("HTTPS proxy or task CA could not be initialized")
            if "proxy" in self.processes:
                await self.stop_process(self.processes["proxy"])

    def command_environment(self, agent_id: str, command_id: str) -> dict[str, str]:
        if (
            self.state is None
            or (self.state["status"] != "collecting" and not self.limited)
            or self.proxy_port is None
        ):
            return {}
        assert self.directory is not None and self.ca_bundle is not None
        password = secrets.token_urlsafe(24)
        self.credentials[command_id] = {
            "agent_id": self.identities.get(agent_id, agent_id),
            "command_id": command_id,
            "password": password,
        }
        atomic_json(self.directory / "control" / "credentials.json", self.credentials)
        proxy = f"http://{command_id}:{password}@{self.proxy_host}:{self.proxy_port}"
        bypass = ",".join(
            filter(
                None,
                (
                    os.environ.get("NO_PROXY", os.environ.get("no_proxy", "")),
                    "localhost,127.0.0.1,::1",
                ),
            )
        )
        return {
            **dict.fromkeys(
                (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                ),
                proxy,
            ),
            **dict.fromkeys(
                (
                    "SSL_CERT_FILE",
                    "REQUESTS_CA_BUNDLE",
                    "CURL_CA_BUNDLE",
                    "NODE_EXTRA_CA_CERTS",
                    "GIT_SSL_CAINFO",
                ),
                self.ca_bundle,
            ),
            "NO_PROXY": bypass,
            "no_proxy": bypass,
        }

    def register_agent(self, linux_agent_id: str, canonical_agent_id: str) -> None:
        if not re.fullmatch(r"agent-[0-9]{1,6}", linux_agent_id):
            raise ValueError("Invalid Linux agent identity")
        if not SAFE_ID.fullmatch(canonical_agent_id):
            raise ValueError("Invalid canonical agent identity")
        previous = self.identities.get(linux_agent_id)
        if previous is not None and previous != canonical_agent_id:
            raise ValueError("Execution identity belongs to another Agent")
        self.identities[linux_agent_id] = canonical_agent_id
        self.persist()

    async def prepare_agent(
        self, linux_agent_id: str, canonical_agent_id: str, task_id: str | None
    ) -> None:
        self.register_agent(linux_agent_id, canonical_agent_id)
        if self.networks is not None:
            if task_id is None:
                raise ValueError("Traffic networking requires a fixed task identity")
            try:
                await self.networks.register(task_id, linux_agent_id, canonical_agent_id)
            except (OSError, ValueError):
                self.partial("Agent network namespace could not be initialized")
                self.persist()
                raise
            if self.state is not None and self.state["status"] == "collecting":
                await self.start_raw(linux_agent_id)

    def redact_output(self, value: bytes) -> bytes:
        for identity in self.credentials.values():
            value = value.replace(identity["password"].encode(), b"[redacted]")
        return re.sub(rb"(?i)(https?://[^\s:@/]+:)[^\s@/]+@", rb"\1[redacted]@", value)

    def preview_lost(self, reason: str, count: int = 1) -> None:
        assert self.state is not None
        self.state["preview_dropped"] = self.state.get("preview_dropped", 0) + count
        # Preview loss affects the live packet list only. The raw capture remains
        # available and must not turn the entire capture into an error.

    async def refresh_interfaces(self, role: str) -> None:
        args = ["ip", "-j", "link", "show"]
        if role.startswith("raw:") and self.networks is not None:
            identity = self.networks.agents[role[4:]]
            args = ["ip", "-n", identity["namespace"], "-j", "link", "show"]
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await process.communicate()
            if process.returncode == 0:
                self.interface_names.setdefault(role, {}).update(
                    {str(item["ifindex"]): item["ifname"] for item in json.loads(output)}
                )
        except (OSError, ValueError, KeyError):
            self.interface_names.setdefault(role, {})

    async def raw_files(
        self, process: asyncio.subprocess.Process, owner: str | None = None, role: str = "raw"
    ) -> None:
        """Continuously drain EK; slow previews cannot apply MinIO backpressure to capture."""
        assert process.stdout is not None and self.state is not None
        buffer = bytearray()
        oversized = False
        while block := await process.stdout.read(65536):
            buffer.extend(block)
            while True:
                end = buffer.find(b"\n")
                if end < 0:
                    if len(buffer) > 2 * 1024 * 1024:
                        oversized = True
                        buffer.clear()
                    break
                line = bytes(buffer[:end])
                del buffer[: end + 1]
                if oversized:
                    oversized = False
                    self.packet_numbers[role] += 1
                    self.preview_lost("Oversized packet preview discarded; raw capture retained")
                    continue
                if not line or line.lstrip().startswith(b'{"index"'):
                    continue
                self.packet_numbers[role] += 1
                number = self.packet_numbers[role]
                self.state["last_packet_at"] = now()
                if (
                    self.packet_queue.full()
                    or self.packet_queue_bytes + len(line) > 16 * 1024 * 1024
                    or len(line) > 2 * 1024 * 1024
                ):
                    self.preview_lost("Live packet preview queue overflow; raw capture retained")
                    continue
                self.packet_queue.put_nowait((role, number, line))
                self.packet_queue_bytes += len(line)
        if buffer or oversized:
            self.preview_lost("Interrupted packet preview; raw capture retained")

    async def consume_packets(self) -> None:
        processed = 0
        while True:
            role, number, line = await self.packet_queue.get()
            self.packet_queue_bytes -= len(line)
            try:
                document = json.loads(line)
                index = packet_ifindex(document)
                names = self.interface_names.get(role, {})
                if index and index not in names:
                    await self.refresh_interfaces(role)
                    names = self.interface_names.get(role, {})
                    names.setdefault(index, f"ifindex:{index}")
                interface = names.get(index, f"ifindex:{index}" if index else None)
                assert self.state is not None
                source_id = self.state["source_id"] + "-" + role.replace(":", "-")
                detail = packet_record(
                    document, source_id, number, self.raw_owners.get(role), interface
                )
                self.state["last_packet_at"] = detail["timestamp"]
                self.packet_times[source_id] = (number, detail["timestamp"])
                self.record(detail)
            except (ValueError, TypeError, KeyError, OSError):
                self.preview_lost("Packet preview could not be decoded; raw capture retained")
            finally:
                self.packet_queue.task_done()
            processed += 1
            if processed >= 32 or self.packet_queue.empty():
                processed = 0
                await asyncio.sleep(0)

    async def raw_errors(self, process: asyncio.subprocess.Process, role: str = "raw") -> None:
        assert process.stderr is not None and self.state is not None
        while line := await process.stderr.readline():
            text = line.decode(errors="replace")
            match = re.search(r'File: "?(/[^"\n]+)"?', text)
            if match:
                name = Path(match[1]).name
                if self.native:
                    self.native.add_segment(
                        self.state["source_id"] + "-" + role.replace(":", "-"),
                        self.raw_owners.get(role),
                        name,
                    )
                previous = self.active_raw.get(role)
                self.active_raw[role] = name
                self.raw_ready.add(role)
                self.raw_readiness[role].set()
                if previous and previous != name:
                    try:
                        await self.register_raw(previous, self.raw_owners.get(role), role)
                    except (OSError, ValueError):
                        self.partial("A raw capture segment could not be sealed")
            legacy = re.search(r"received/dropped.*?:\s*\d+/(\d+)", text, re.I)
            dropped = re.search(r"(\d+) packets? dropped(?: from .*)?", text)
            if legacy or dropped:
                report = legacy or dropped
                assert report is not None
                count = int(report[1])
                self.drop_counts[role] = count
                self.state["dropped_packets"] = sum(self.drop_counts.values())
                if count:
                    self.partial("Raw capture dropped packets")
            if re.search(r"\d+ packets? captured", text):
                self.drop_counts.setdefault(role, 0)
            if (
                "error" in text.lower() or "permission denied" in text.lower()
            ) and " INFO]" not in text:
                self.partial("Raw capture reported an error")
        previous = self.active_raw.pop(role, None)
        if previous:
            try:
                await self.register_raw(previous, self.raw_owners.get(role), role)
            except (OSError, ValueError):
                self.partial("A raw capture segment could not be sealed")

    async def register_raw(self, name: str, owner: str | None = None, role: str = "raw") -> None:
        assert self.directory is not None and self.state is not None
        if name in self.paths.values():
            return
        if self.native and name in self.native.segments:
            try:
                self.native.drain_segment(name, final=True)
            except (OSError, ValueError) as exc:
                self.native.error = str(exc)[:512]
                self.partial("Native raw input could not be completely sealed")
                self.native.save()
        fd = self.open_name(name)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb") as source:
            while chunk := source.read(65536):
                digest.update(chunk)
                size += len(chunk)
        process = await asyncio.create_subprocess_exec(
            "capinfos",
            "-c",
            "-M",
            str(self.directory / name),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate()
        match = re.search(rb"Number of packets:\s*([0-9]+)", output)
        if process.returncode != 0 or match is None:
            self.partial("Raw segment packet count unavailable")
            packet_count = 0
        else:
            packet_count = int(match[1])
            if not packet_count:
                (self.directory / name).unlink(missing_ok=True)
                return
        ranges = self.raw_ranges.setdefault(role, [])
        first = ranges[-1]["last_packet"] + 1 if ranges else 1
        ranges.append(
            {
                "name": name,
                "first_packet": first,
                "last_packet": first + packet_count - 1,
                "packet_count": packet_count,
            }
        )
        identifier = "p-" + uuid.uuid4().hex
        self.paths[identifier] = name
        self.artifacts[identifier] = {
            "id": identifier,
            "kind": "pcap",
            "name": name,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "content_type": "application/octet-stream",
            "sealed": True,
            "agent_id": owner,
            "source_id": self.state["source_id"] + "-" + role.replace(":", "-"),
        }
        self.state["captured_bytes"] += size
        self.persist()

    def record(self, detail: dict[str, Any], *, priority: bool = False) -> None:
        assert self.state is not None
        if self.limited:
            return
        reserve = min(10000, self.max_records // 10) if self.max_records >= 100 else 0
        limit = self.max_records if priority else self.max_records - reserve
        if self.state["record_count"] >= limit:
            if detail.get("record_kind") == "packet":
                self.preview_lost("Traffic record capacity reached; raw capture continues")
            else:
                self.partial("Traffic record capacity reached; raw capture continues")
            return
        sequence = self.state["record_count"] + 1
        detail.update(id=f"r-{sequence}", seq=sequence)
        complete_wire_detail(detail)
        encoded = (json.dumps(detail, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(encoded) > 120 * 1024:
            self.preview_lost("Traffic detail exceeds its preview limit; raw capture retained")
            return
        if self.current and self.current_bytes + len(encoded) > 900 * 1024:
            self.seal_records()
        self.state["record_count"] = sequence
        self.current.append(detail)
        self.current_lines.append(encoded)
        self.current_bytes += len(encoded)
        self.details[detail["id"]] = detail
        assert self.directory is not None
        if self.active_fd is None:
            self.active_fd = os.open(
                self.directory / "active.jsonl",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
            )
        if os.write(self.active_fd, encoded) != len(encoded):
            raise OSError("Incomplete packet metadata write")
        if len(self.details) > 256:
            self.details.pop(next(iter(self.details)))
        if len(self.current) >= 128:
            self.seal_records()

    def ingest_proxy(self) -> None:
        assert self.directory is not None
        control = self.directory / "control"
        if (control / "failure").is_file():
            self.partial((control / "failure").read_text()[:512])
        for path in sorted((control / "inbox").glob("*.json")):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as source:
                if os.fstat(source.fileno()).st_size > 128 * 1024:
                    self.partial("Decoded traffic record exceeded its size limit")
                else:
                    self.record(json.load(source), priority=True)
            path.unlink()

    def seal_records(self) -> None:
        if self.active_fd is not None:
            os.close(self.active_fd)
            self.active_fd = None
        if not self.current:
            return
        assert self.directory is not None
        identifier = "c-" + uuid.uuid4().hex
        name = identifier + ".jsonl"
        content = b"".join(self.current_lines)
        fd = os.open(
            self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as target:
            target.write(content)
        self.paths[identifier] = name
        self.chunks[identifier] = {
            "id": identifier,
            "first_seq": self.current[0]["seq"],
            "last_seq": self.current[-1]["seq"],
            "count": len(self.current),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        self.current = []
        self.current_lines = []
        self.current_bytes = 0
        self.last_seal = asyncio.get_running_loop().time()
        (self.directory / "active.jsonl").unlink(missing_ok=True)
        self.persist()

    async def poll(self) -> None:
        assert self.directory is not None and self.state is not None
        while self.state["status"] == "collecting":
            try:
                self.ingest_proxy()
                if self.current and asyncio.get_running_loop().time() - self.last_seal >= 0.5:
                    self.seal_records()
                if asyncio.get_running_loop().time() - self.last_native >= 0.5:
                    self.drain_native()
                if (self.directory / "control" / "keylog-full").exists():
                    self.partial(
                        "Private TLS keylog capacity reached; later TLS secrets unavailable"
                    )
                    if self.native:
                        self.native.error = "Private TLS keylog capacity reached"
                if self.networks is not None and (
                    self.networks.dns is None or self.networks.dns.returncode is not None
                ):
                    self.partial("Task gateway DNS is unavailable; command execution disabled")
                for role, process in self.processes.items() if not self.stopping else []:
                    if process.returncode is not None:
                        self.partial(f"{role} capture process exited unexpectedly")
                        if role == "proxy":
                            self.state["https"] = "unavailable"
                            self.proxy_port = None
                total = sum(
                    path.stat().st_size for path in self.directory.rglob("*") if path.is_file()
                )
                if total > self.max_bytes or self.state["captured_bytes"] >= self.max_bytes:
                    await self.limit()
                    return
                self.persist()
            except (OSError, ValueError, TypeError):
                self.partial("Traffic data could not be persisted or read")
            await asyncio.sleep(0.1)

    async def seal(self) -> dict[str, Any]:
        async with self.lock:
            if self.state is None:
                raise ValueError("Capture not started")
            self.ingest_proxy()
            self.seal_records()
            self.drain_native()
            self.persist()
            return self.status()

    def detail(self, identifier: str) -> dict[str, Any] | None:
        cached = self.details.get(identifier)
        if cached is not None:
            return cached
        if not re.fullmatch(r"r-[1-9][0-9]*", identifier):
            return None
        sequence = int(identifier[2:])
        for chunk_id, chunk in self.chunks.items():
            if chunk["first_seq"] <= sequence <= chunk["last_seq"]:
                with os.fdopen(self.open_segment(chunk_id, "chunks")) as source:
                    for line in source:
                        item = json.loads(line)
                        if item["id"] == identifier:
                            return item
        return None

    @staticmethod
    async def stop_processes(processes: list[asyncio.subprocess.Process]) -> bool:
        """Stop capture process groups together so one slow child cannot stall cleanup."""
        running = [process for process in processes if process.returncode is None]
        for process in running:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        if not running:
            return False
        waits = {asyncio.create_task(process.wait()): process for process in running}
        done, pending = await asyncio.wait(waits, timeout=5)
        await asyncio.gather(*done, return_exceptions=True)
        for waiter in pending:
            process = waits[waiter]
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return bool(pending)

    @classmethod
    async def stop_process(cls, process: asyncio.subprocess.Process) -> bool:
        return await cls.stop_processes([process])

    async def limit(self) -> dict[str, Any]:
        async with self.lock:
            if self.state is None:
                raise ValueError("Capture not started")
            if self.limited or self.state["status"] != "collecting":
                return self.status()
            self.partial("Traffic capacity reached; forwarding remains available")
            assert self.directory is not None
            (self.directory / "control" / "disabled").touch(mode=0o600)
            self.stopping = True
            # dumpcap/libpcap batches Linux packets for CAP_READ_TIMEOUT=250ms.
            await asyncio.sleep(0.3)
            capture_processes = [
                process for role, process in self.processes.items() if role != "proxy"
            ]
            if await self.stop_processes(capture_processes):
                self.partial(
                    "Capture processes did not stop gracefully; packet archive may end early"
                )
            if self.readers:
                await asyncio.gather(*self.readers)
            await self.packet_queue.join()
            if self.packet_worker is not None:
                self.packet_worker.cancel()
                await asyncio.gather(self.packet_worker, return_exceptions=True)
            self.ingest_proxy()
            self.seal_records()
            self.recover_segments()
            self.drain_native(final=True)
            self.limited = True
            self.state.update(status="partial", capturing=False)
            self.persist()
            return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self.lock:
            if self.state is None:
                raise ValueError("Capture not started")
            if self.state["status"] != "collecting" and not self.limited:
                return self.status()
            self.stopping = True
            if self.poller is not None and self.poller is not asyncio.current_task():
                self.poller.cancel()
                await asyncio.gather(self.poller, return_exceptions=True)
            # Let the final kernel capture batch reach dumpcap before its SIGINT.
            await asyncio.sleep(0.3)
            if await self.stop_processes(list(self.processes.values())):
                self.partial(
                    "Capture processes did not stop gracefully; packet archive may end early"
                )
            if self.readers:
                await asyncio.gather(*self.readers)
            if any(role not in self.drop_counts for role in self.raw_owners):
                self.partial("Raw packet drop statistics unavailable")
            await self.packet_queue.join()
            if self.packet_worker is not None:
                self.packet_worker.cancel()
                await asyncio.gather(self.packet_worker, return_exceptions=True)
            self.ingest_proxy()
            self.seal_records()
            self.recover_segments()
            self.drain_native(final=True)
            self.proxy_port = None
            self.credentials = {}
            self.limited = False
            assert self.directory is not None
            shutil.rmtree(self.directory / "control", ignore_errors=True)
            self.state.update(
                status=(
                    "partial"
                    if self.reasons or int(self.state.get("preview_dropped", 0))
                    else "complete"
                ),
                finished_at=now(),
                capturing=False,
                proxy_serving=False,
            )
            self.persist()
            return self.status()

    def open_name(self, name: str) -> int:
        if Path(name).name != name:
            raise ValueError("Invalid local segment")
        assert self.directory is not None
        parent = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("Segment is not a regular file")
        return fd

    def drain_native(self, *, final: bool = False) -> None:
        if self.native is None:
            return
        self.native.drain(final=final)
        for source_id, (number, timestamp) in self.packet_times.items():
            source = self.native.sources.get(source_id)
            if source is not None and source["packet_count"] >= number:
                source["last_packet_at"] = timestamp
        self.native.save()
        self.last_native = asyncio.get_running_loop().time()
        if self.native.error:
            self.partial("Native input incomplete: " + self.native.error)

    def native_listing(self) -> dict[str, Any]:
        if self.native is None:
            raise ValueError("Native input unavailable in this capture epoch")
        if self.state and self.state["status"] == "collecting":
            self.drain_native()
        return self.native.listing()

    def open_segment(self, identifier: str, kind: str) -> int:
        collection = self.chunks if kind == "chunks" else self.artifacts
        if not SAFE_ID.fullmatch(identifier) or identifier not in collection:
            raise FileNotFoundError("Unknown sealed segment")
        return self.open_name(self.paths[identifier])

    def ack(self, chunks: list[str], artifacts: list[str]) -> dict[str, Any]:
        assert self.directory is not None
        for values, collection in ((chunks, self.chunks), (artifacts, self.artifacts)):
            for identifier in values:
                if not isinstance(identifier, str) or not SAFE_ID.fullmatch(identifier):
                    raise ValueError("Invalid segment ID")
                if identifier not in collection:
                    continue
                if collection is self.chunks:
                    first, last = (
                        collection[identifier]["first_seq"],
                        collection[identifier]["last_seq"],
                    )
                    for sequence in range(first, last + 1):
                        self.details.pop(f"r-{sequence}", None)
                elif self.native:
                    name = self.paths[identifier]
                    if name in self.native.segments and not self.native.segments[name]["closed"]:
                        self.native.drain_segment(name, final=True)
                (self.directory / self.paths[identifier]).unlink(missing_ok=True)
                del self.paths[identifier]
                del collection[identifier]
        self.persist()
        return self.status()
