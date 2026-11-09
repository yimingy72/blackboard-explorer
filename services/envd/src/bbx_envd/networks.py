"""Per-Agent network namespaces with a shared task gateway and filesystem."""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import tempfile
import time
from contextlib import suppress
from pathlib import Path


class AgentNetworks:
    def __init__(
        self, root: Path, shared: Path, *, mode=None, next_hop=None, allow=(), deny=()
    ) -> None:
        self.root, self.shared = root, shared
        self.protected = mode is not None
        self.forced = mode in {"vpn", "socks5"}
        self.next_hop = next_hop
        self.allow = tuple(str(ipaddress.IPv4Network(value)) for value in allow)
        self.deny = tuple(str(ipaddress.ip_network(value)) for value in deny)
        self.fenced = False
        self.revoking = False
        self.revoking = False
        self.deadline: float | None = None
        self.kernel_gate = False
        self.link = None
        self.link_peer = None
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
        if self.revoking or (self.forced and not self.link):
            raise ValueError("Original protected network is unavailable")
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
            if not self.forced:
                await self.run(
                    "ip",
                    "-6",
                    "addr",
                    "add",
                    self.gateway_ipv6 + "/64",
                    "dev",
                    self.bridge,
                    "nodad",
                )
            await self.run("ip", "link", "set", self.bridge, "up")
            if Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() != "1":
                raise ValueError("Task gateway IPv4 forwarding is disabled")
            if self.protected:
                await self.protect_bridge()
            if self.forced:
                self.save_identity(task_id)
                return
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
                "--user=nobody" if self.protected else "--user=root",
                "--group=nogroup" if self.protected else "--group=root",
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
            self.save_identity(task_id)
        except BaseException:
            try:
                await self.run("ip", "link", "del", self.bridge)
            except ValueError:
                pass
            raise

    def save_identity(self, task_id):
        self.task_id = task_id
        identity = self.root / "identity.json"
        identity.write_text(json.dumps({"task_id": task_id, "bridge": self.bridge}))
        identity.chmod(0o600)

    async def configure_link(self, public_key):
        async with self.lock:
            if self.link and not self.revoking and self.link_peer == public_key:
                return (await self.run("/usr/bin/wg", "show", self.link, "public-key")).strip()
            if (
                not self.forced
                or self.revoking
                or self.task_id
                or self.link
                or not re.fullmatch(r"[A-Za-z0-9+/]{43}=", public_key)
                or self.next_hop is None
            ):
                raise ValueError("Original protected link cannot reopen")
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            key = self.root / "wireguard.key"
            private = await self.run("/usr/bin/wg", "genkey")
            with os.fdopen(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
                out.write(private)
            name = "bbxwg0"
            await self.run("ip", "link", "add", name, "type", "wireguard")
            await self.run(
                "/usr/bin/wg",
                "set",
                name,
                "listen-port",
                "51820",
                "private-key",
                str(key),
                "peer",
                public_key,
                "endpoint",
                f"{self.next_hop}:51820",
                "allowed-ips",
                ",".join(self.allow),
            )
            await self.run("ip", "link", "set", name, "mtu", "1280", "up")
            self.link = name
            self.link_peer = public_key
            return (await self.run("/usr/bin/wg", "show", name, "public-key")).strip()

    async def protect_bridge(self):
        for table in ("iptables", "ip6tables"):
            version = "6" if table == "ip6tables" else "4"
            mask = "::" if version == "6" else "0.0.0.0"
            chain = "BBX_LEASE"
            await self.run(table, "-N", chain)
            await self.run(
                table,
                "-A",
                chain,
                "-m",
                "recent",
                "--name",
                "bbx_legacy" + version,
                "--mask",
                mask,
                "--rcheck",
                "--seconds",
                "15",
                "-j",
                "RETURN",
            )
            await self.run(table, "-A", chain, "-j", "DROP")
            for hook, direction in (
                ("INPUT", "-i"),
                ("OUTPUT", "-o"),
                ("FORWARD", "-i"),
                ("FORWARD", "-o"),
            ):
                await self.run(table, "-I", hook, "1", direction, self.bridge, "-j", chain)
            if not self.forced:
                await self.run(
                    table, "-I", "OUTPUT", "1", "-m", "owner", "--uid-owner", "65534", "-j", chain
                )
            if not self.forced and table == "iptables":
                await self.run(
                    table,
                    "-A",
                    "INPUT",
                    "-i",
                    self.bridge,
                    "-d",
                    self.gateway + "/32",
                    "-p",
                    "udp",
                    "--dport",
                    "53",
                    "-j",
                    "ACCEPT",
                )
                await self.run(
                    table,
                    "-A",
                    "INPUT",
                    "-i",
                    self.bridge,
                    "-d",
                    self.gateway + "/32",
                    "-p",
                    "tcp",
                    "--dport",
                    "53",
                    "-j",
                    "ACCEPT",
                )
            await self.run(table, "-A", "INPUT", "-i", self.bridge, "-j", "DROP")
            for cidr in self.deny:
                if (":" in cidr) != (table == "ip6tables"):
                    continue
                await self.run(table, "-A", "FORWARD", "-i", self.bridge, "-d", cidr, "-j", "DROP")
                await self.run(table, "-A", "FORWARD", "-o", self.bridge, "-s", cidr, "-j", "DROP")
        self.kernel_gate = True
        if not self.forced:
            return
        assert self.link
        await self.run(
            "ip", "route", "add", "unreachable", "default", "metric", "32767", "table", "119"
        )
        for cidr in self.allow:
            await self.run("ip", "route", "add", cidr, "dev", self.link, "table", "119")
            await self.run(
                "iptables",
                "-A",
                "FORWARD",
                "-i",
                self.bridge,
                "-o",
                self.link,
                "-s",
                "198.18.0.0/24",
                "-d",
                cidr,
                "-p",
                "tcp",
                "-m",
                "conntrack",
                "--ctstate",
                "NEW,ESTABLISHED",
                "-j",
                "ACCEPT",
            )
            await self.run(
                "iptables",
                "-A",
                "FORWARD",
                "-i",
                self.link,
                "-o",
                self.bridge,
                "-s",
                cidr,
                "-d",
                "198.18.0.0/24",
                "-p",
                "tcp",
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED",
                "-j",
                "ACCEPT",
            )
        await self.run("ip", "rule", "add", "priority", "119", "iif", self.bridge, "lookup", "119")
        for table in ("iptables", "ip6tables"):
            await self.run(table, "-A", "FORWARD", "-i", self.bridge, "-j", "DROP")
            await self.run(table, "-A", "FORWARD", "-o", self.bridge, "-j", "DROP")

    async def fence(self):
        async with self.lock:
            if not self.protected:
                raise ValueError("Protected network is unavailable")
            if self.fenced:
                return
            self.revoking = True
            if self.kernel_gate:
                for version, mask in (("4", "0.0.0.0"), ("6", "::")):
                    with suppress(OSError):
                        (Path("/proc/net/xt_recent") / ("bbx_legacy" + version)).write_text(
                            "-" + mask + "\n"
                        )
            for table in ("iptables", "ip6tables"):
                for chain, flag in (("INPUT", "-i"), ("FORWARD", "-i"), ("FORWARD", "-o")):
                    await self.run(table, "-I", chain, "1", flag, self.bridge, "-j", "DROP")
            await self.close()
            self.fenced = True

    def renew(self):
        now = time.monotonic()
        if (
            not self.protected
            or self.revoking
            or (self.deadline is not None and self.deadline <= now)
        ):
            raise ValueError("Original protected execution lease expired")
        self.deadline = now + (15 if self.kernel_gate else 90)
        if self.kernel_gate:
            for version, mask in (("4", "0.0.0.0"), ("6", "::")):
                (Path("/proc/net/xt_recent") / ("bbx_legacy" + version)).write_text(
                    "+" + mask + "\n"
                )

    async def watchdog(self):
        while not self.fenced:
            await asyncio.sleep(0.5)
            if self.deadline is not None and self.deadline <= time.monotonic():
                await self.fence()
                return

    def permitted(self):
        return not self.revoking and self.deadline is not None and self.deadline > time.monotonic()

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
                if self.protected:
                    await self.run("bridge", "link", "set", "dev", host, "isolated", "on")
                await self.run("ip", "link", "set", host, "up")
                await self.run("ip", "link", "set", peer, "netns", namespace)
                prefix = ["ip", "-n", namespace]
                await self.run(*prefix, "link", "set", peer, "name", "eth0")
                await self.run(*prefix, "addr", "add", ipv4 + "/24", "dev", "eth0")
                if not self.forced:
                    await self.run(
                        *prefix, "-6", "addr", "add", ipv6 + "/64", "dev", "eth0", "nodad"
                    )
                else:
                    await self.run(*prefix, "link", "set", "eth0", "mtu", "1280")
                await self.run(*prefix, "link", "set", "lo", "up")
                await self.run(*prefix, "link", "set", "eth0", "up")
                await self.run(*prefix, "route", "add", "default", "via", self.gateway)
                if not self.forced:
                    await self.run(
                        *prefix, "-6", "route", "add", "default", "via", self.gateway_ipv6
                    )
                directory = Path("/etc/netns") / namespace
                directory.mkdir(parents=True, mode=0o755)
                (directory / "resolv.conf").write_text(
                    "" if self.forced else f"nameserver {self.gateway}\n"
                )
                identity = {
                    "agent_id": canonical,
                    "linux_agent_id": linux_agent,
                    "namespace": namespace,
                    "ipv4": ipv4,
                    "ipv6": "" if self.forced else ipv6,
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
        if identity is None or (
            not self.forced and (self.dns is None or self.dns.returncode is not None)
        ):
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
