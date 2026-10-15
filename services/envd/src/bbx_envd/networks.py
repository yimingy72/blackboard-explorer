"""Per-Agent network namespaces with a shared task gateway and filesystem."""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path


class AgentNetworks:
    def __init__(self, root: Path, shared: Path) -> None:
        self.root, self.shared = root, shared
        self.task_id: str | None = None
        self.gateway = "198.18.0.1"
        self.gateway_ipv6 = ""
        self.bridge = "bbxtr0"
        self.agents: dict[str, dict[str, str]] = {}
        self.dns: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def run(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            raise ValueError(f"Agent network setup failed: {args[0]} {args[1]}")
        return output.decode()

    async def initialize(self, task_id: str) -> None:
        if self.task_id is not None:
            if self.task_id != task_id:
                raise ValueError("Network belongs to another task")
            return
        if (self.root / "identity.json").exists():
            raise ValueError("Previous network process state unknown; replace the container")
        routes = json.loads(await self.run("ip", "-j", "route", "show", "table", "all"))
        subnet = ipaddress.ip_network("198.18.0.0/24")
        for route in routes:
            destination = route.get("dst", "default")
            if destination == "default":
                continue
            try:
                if ipaddress.ip_network(destination, strict=False).overlaps(subnet):
                    raise ValueError("Task network subnet conflicts with an existing route")
            except TypeError:
                continue
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        suffix = hashlib.sha256(task_id.encode()).hexdigest()[:4]
        self.gateway_ipv6 = f"fd42:6262:{suffix}::1"
        await self.run("ip", "link", "add", self.bridge, "type", "bridge")
        try:
            await self.run("ip", "addr", "add", self.gateway + "/24", "dev", self.bridge)
            await self.run(
                "ip", "-6", "addr", "add", self.gateway_ipv6 + "/64", "dev", self.bridge, "nodad"
            )
            await self.run("ip", "link", "set", self.bridge, "up")
            if Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() != "1":
                raise ValueError("Task gateway IPv4 forwarding is disabled")
            await self.run(
                "iptables",
                "-t",
                "nat",
                "-A",
                "POSTROUTING",
                "-s",
                "198.18.0.0/24",
                "!",
                "-d",
                "198.18.0.0/24",
                "-j",
                "MASQUERADE",
            )
            await self.run("iptables", "-A", "FORWARD", "-i", self.bridge, "-j", "ACCEPT")
            await self.run(
                "iptables",
                "-A",
                "FORWARD",
                "-o",
                self.bridge,
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "ACCEPT",
            )
            await self.run(
                "ip6tables",
                "-t",
                "nat",
                "-A",
                "POSTROUTING",
                "-s",
                self.gateway_ipv6.removesuffix("1") + "/64",
                "-j",
                "MASQUERADE",
            )
            await self.run("ip6tables", "-A", "FORWARD", "-i", self.bridge, "-j", "ACCEPT")
            await self.run(
                "ip6tables",
                "-A",
                "FORWARD",
                "-o",
                self.bridge,
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "ACCEPT",
            )
            resolvers = [
                line.split()[1]
                for line in Path("/etc/resolv.conf").read_text().splitlines()
                if line.strip().startswith("nameserver ")
            ]
            if not resolvers:
                raise ValueError("Task DNS resolver unavailable")
            args = [
                "dnsmasq",
                "--keep-in-foreground",
                "--bind-interfaces",
                "--no-resolv",
                "--conf-file=/dev/null",
                f"--listen-address={self.gateway}",
                "--user=root",
                "--group=root",
                f"--pid-file={self.root / 'dns.pid'}",
            ]
            args += [f"--server={address}" for address in resolvers]
            self.dns = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            await asyncio.sleep(0.1)
            if self.dns.returncode is not None:
                raise ValueError("Task gateway DNS failed to start")
            self.task_id = task_id
            identity = self.root / "identity.json"
            identity.write_text(json.dumps({"task_id": task_id, "bridge": self.bridge}))
            identity.chmod(0o600)
        except BaseException:
            try:
                await self.run("ip", "link", "del", self.bridge)
            except ValueError:
                pass
            raise

    async def register(self, task_id: str, linux_agent: str, canonical: str) -> dict[str, str]:
        async with self.lock:
            await self.initialize(task_id)
            existing = self.agents.get(linux_agent)
            if existing is not None:
                if existing["agent_id"] != canonical:
                    raise ValueError("Network identity belongs to another Agent")
                return existing
            if not re.fullmatch(r"agent-[0-9]{1,6}", linux_agent) or len(self.agents) >= 240:
                raise ValueError("Invalid or exhausted Agent network identity")
            suffix = hashlib.sha256((task_id + ":" + linux_agent).encode()).hexdigest()[:10]
            namespace, host, peer = "bbx-" + suffix, "bh" + suffix, "bn" + suffix
            index = 10 + len(self.agents)
            ipv4 = f"198.18.0.{index}"
            ipv6 = self.gateway_ipv6.removesuffix("1") + f"{index:x}"
            await self.run("ip", "netns", "add", namespace)
            try:
                await self.run("ip", "link", "add", host, "type", "veth", "peer", "name", peer)
                await self.run("ip", "link", "set", host, "master", self.bridge)
                await self.run("ip", "link", "set", host, "up")
                await self.run("ip", "link", "set", peer, "netns", namespace)
                prefix = ["ip", "-n", namespace]
                await self.run(*prefix, "link", "set", peer, "name", "eth0")
                await self.run(*prefix, "addr", "add", ipv4 + "/24", "dev", "eth0")
                await self.run(*prefix, "-6", "addr", "add", ipv6 + "/64", "dev", "eth0", "nodad")
                await self.run(*prefix, "link", "set", "lo", "up")
                await self.run(*prefix, "link", "set", "eth0", "up")
                await self.run(*prefix, "route", "add", "default", "via", self.gateway)
                await self.run(*prefix, "-6", "route", "add", "default", "via", self.gateway_ipv6)
                directory = Path("/etc/netns") / namespace
                directory.mkdir(parents=True, mode=0o755)
                (directory / "resolv.conf").write_text(f"nameserver {self.gateway}\n")
                identity = {
                    "agent_id": canonical,
                    "linux_agent_id": linux_agent,
                    "namespace": namespace,
                    "ipv4": ipv4,
                    "ipv6": ipv6,
                    "gateway": self.gateway,
                    "gateway_ipv6": self.gateway_ipv6,
                }
                self.agents[linux_agent] = identity
                self.publish()
                return identity
            except BaseException:
                try:
                    await self.run("ip", "link", "del", host)
                except ValueError:
                    pass
                await self.run("ip", "netns", "del", namespace)
                raise

    def publish(self) -> None:
        self.shared.mkdir(parents=True, exist_ok=True)
        target = self.shared / "network.json"
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix=".network-",
            dir=self.shared,
            delete=False,
        ) as output:
            os.fchmod(output.fileno(), 0o644)
            json.dump(
                {
                    "task_id": self.task_id,
                    "gateway": self.gateway,
                    "gateway_ipv6": self.gateway_ipv6,
                    "agents": {item["agent_id"]: item for item in self.agents.values()},
                },
                output,
            )
            temporary = Path(output.name)
        temporary.replace(target)

    def prefix(self, linux_agent: str) -> list[str]:
        identity = self.agents.get(linux_agent)
        if identity is None or self.dns is None or self.dns.returncode is not None:
            raise ValueError("Agent network unavailable; command was not executed")
        return [
            "ip",
            "netns",
            "exec",
            identity["namespace"],
            "setpriv",
            "--bounding-set=-sys_admin",
            "--",
        ]

    def environment(self, linux_agent: str) -> dict[str, str]:
        self.prefix(linux_agent)
        identity = self.agents[linux_agent]
        return {
            "BBX_AGENT_IP": identity["ipv4"],
            "BBX_AGENT_IPV6": identity["ipv6"],
            "BBX_TASK_GATEWAY": self.gateway,
        }

    async def close(self) -> None:
        if self.dns is not None and self.dns.returncode is None:
            self.dns.terminate()
            await self.dns.wait()
