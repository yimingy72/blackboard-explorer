"""Fixed OpenVPN egress and the original Task's private WireGuard link."""

import base64
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

IP, IPT, WG = "/usr/sbin/ip", "/usr/sbin/iptables", "/usr/bin/wg"
RESTORE = "/usr/sbin/iptables-restore"
OPENVPN = "/usr/sbin/openvpn"
WG_LINK, TUN, PORT = "bbxwg", "bbxvpn", 51820
SOURCE = "198.19.0.0/16"
PROBE_IP, PROBE_NET, PROBE_VETH = "198.19.255.2", "bbxvpnprobe", "bp-vpn"
EDGE_CHAIN = "BBX_VPN"
RENEW, SET_MARK, ANCHOR = "BBX_VPN_RENEW", "BBX_VPN_SET", "BBX_VPN_ANCHOR"
MARKS = ("bbx_vpn_a", "bbx_vpn_b")
RENEW_PORT = 47655
SANDBOX_ROOT = Path("/var/lib/bbx/control/vpn")
EGRESS_ROOT = Path("/var/run/bbx-vpn/state")
CONFIG = Path("/var/run/bbx-vpn/config")
DYNAMIC_PROFILE = "openvpn-task-ipv4-tcp-v1"
MANAGEMENT = EGRESS_ROOT / "openvpn.sock"
MATERIAL_LIMIT = 256 * 1024
MATERIAL_FILES = {"ca": "ca.crt", "cert": "tls.crt", "key": "tls.key", "tls_crypt": "tls-crypt.key"}
PUB = re.compile(r"[A-Za-z0-9+/]{43}=\Z")
DEADLINE_MS, SKEW_MS = 10000, 1000
LOG_LIMIT = 16 * 1024


def require(check):
    if not check:
        raise ValueError("Original Task VPN evidence is unconfirmed")


def command(*argv, input=None, check=True):
    result = subprocess.run(
        argv,
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
        env={},
    )
    if check:
        require(result.returncode == 0)
    return result


def now_ms():
    return time.time_ns() // 1_000_000


def boot_ms():
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000


def ipv4(value):
    parsed = ipaddress.IPv4Address(value)
    require(
        str(parsed) == value
        and not (
            parsed.is_loopback
            or parsed.is_unspecified
            or parsed.is_link_local
            or parsed.is_multicast
            or parsed.is_reserved
        )
    )
    return value


def target_pairs(items):
    require(isinstance(items, list) and 1 <= len(items) <= 8)
    targets = []
    for item in items:
        require(set(item) == {"ipv4", "port"})
        addr, port = ipv4(item["ipv4"]), item["port"]
        require(type(port) is int and 1 <= port <= 65535)
        require(ipaddress.IPv4Address(addr) not in ipaddress.IPv4Network(SOURCE))
        targets.append((addr, port))
    require(len(set(targets)) == len(targets))
    return targets


def ifindex(name, *, up=False):
    result = command(IP, "-details", "-j", "link", "show", "dev", name, check=False)
    if result.returncode:
        return None
    links = json.loads(result.stdout)
    require(len(links) == 1)
    link = links[0]
    if up and "UP" not in link.get("flags", []):
        return None
    if name == WG_LINK:
        require(link.get("linkinfo", {}).get("info_kind") == "wireguard")
    elif name == TUN:
        require(link.get("linkinfo", {}).get("info_kind") == "tun")
    return link["ifindex"]


def local_address(ip, device=None):
    args = ("dev", device) if device else ()
    interfaces = json.loads(command(IP, "-j", "-4", "address", "show", *args).stdout)
    return any(
        "UP" in link.get("flags", []) and item.get("local") == ip
        for link in interfaces
        for item in link.get("addr_info", [])
    )


def process_ticks(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19]) if fields[0] not in {"Z", "X"} else None
    except (OSError, IndexError, ValueError):
        return None


def persist(path, value):
    path = Path(path)
    temporary = path.with_suffix(".pending")
    with temporary.open("w") as output:
        json.dump(value, output, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def operation(config, scope, vpn_operation_id, network=None):
    require(scope == config["scope"])
    require(str(UUID(vpn_operation_id)) == vpn_operation_id)
    result = {"scope": scope, "vpn_operation_id": vpn_operation_id}
    if config.get("profile") == DYNAMIC_PROFILE:
        require(re.fullmatch(r"[a-f0-9]{64}", network["network_digest"]))
        result["network_digest"] = network["network_digest"]
    return result


def link_key(root):
    private = root / "private-key"
    with private.open("x") as output:
        output.write(command(WG, "genkey").stdout)
        output.flush()
        os.fsync(output.fileno())
    public = command(WG, "pubkey", input=private.read_text()).stdout.strip()
    require(PUB.fullmatch(public))
    return public


def install_wg(root):
    require(ifindex(WG_LINK) is None)
    public = link_key(root)
    command(IP, "link", "add", WG_LINK, "type", "wireguard")
    command(WG, "set", WG_LINK, "private-key", str(root / "private-key"), "listen-port", str(PORT))
    command(IP, "link", "set", "dev", WG_LINK, "mtu", "1280", "up")
    index = ifindex(WG_LINK)
    require(index is not None)
    return {"public_key": public, "ifindex": index}


def peer(key, underlay, allowed):
    require(PUB.fullmatch(key))
    command(
        WG,
        "set",
        WG_LINK,
        "peer",
        key,
        "endpoint",
        f"{ipv4(underlay)}:{PORT}",
        "allowed-ips",
        ",".join(allowed),
    )


def live_link(link, key, underlay):
    return (
        ifindex(WG_LINK, up=True) == link["ifindex"]
        and command(WG, "show", WG_LINK, "public-key").stdout.strip() == link["public_key"]
        and command(WG, "show", WG_LINK, "peers").stdout.strip() == key
        and command(WG, "show", WG_LINK, "endpoints").stdout.strip() == f"{key}\t{underlay}:{PORT}"
    )


def install_drop_hooks(links, *, edge=False):
    # Order matters even when the namespace starts with IP forwarding enabled.
    for tool in (IPT, "/usr/sbin/ip6tables"):
        for link in links:
            command(tool, "-w", "2", "-I", "INPUT", "1", "-i", link, "-j", "DROP")
            command(tool, "-w", "2", "-I", "OUTPUT", "1", "-o", link, "-j", "DROP")
            if tool != IPT or not edge:
                command(tool, "-w", "2", "-I", "FORWARD", "1", "-i", link, "-j", "DROP")
                command(tool, "-w", "2", "-I", "FORWARD", "1", "-o", link, "-j", "DROP")
    if edge:
        command(IPT, "-w", "2", "-N", EDGE_CHAIN)
        command(IPT, "-w", "2", "-A", EDGE_CHAIN, "-j", "DROP")
        for link in links:
            command(IPT, "-w", "2", "-I", "FORWARD", "1", "-i", link, "-j", EDGE_CHAIN)
            command(IPT, "-w", "2", "-I", "FORWARD", "1", "-o", link, "-j", EDGE_CHAIN)
        for chain in (RENEW, SET_MARK, ANCHOR):
            command(IPT, "-w", "2", "-N", chain)
        command(IPT, "-w", "2", "-A", RENEW, "-j", "DROP")
        command(IPT, "-w", "2", "-A", SET_MARK, "-j", "DROP")
        for mark in MARKS:
            command(
                IPT,
                "-w",
                "2",
                "-A",
                ANCHOR,
                "-m",
                "recent",
                "--name",
                mark,
                "--rcheck",
                "--seconds",
                "10",
                "--mask",
                "0.0.0.0",
                "-j",
                "RETURN",
            )
        command(
            IPT,
            "-w",
            "2",
            "-I",
            "OUTPUT",
            "1",
            "-o",
            "lo",
            "-s",
            "127.0.0.1",
            "-d",
            "127.0.0.1",
            "-p",
            "udp",
            "--sport",
            str(RENEW_PORT),
            "--dport",
            str(RENEW_PORT),
            "-m",
            "owner",
            "--uid-owner",
            "0",
            "-j",
            RENEW,
        )


def network_routes(value):
    require(set(value) == {"allow_cidrs", "deny_cidrs"})
    for entries in value.values():
        require(
            isinstance(entries, list)
            and 1 <= len(entries) <= 128
            and len(set(entries)) == len(entries)
            and all(str(ipaddress.IPv4Network(item)) == item for item in entries)
        )
    return value


def route_policy(table, targets, link, *, routes=None):
    fallback = ["metric", "32767"] if routes else []
    command(IP, "route", "add", "unreachable", "default", *fallback, "table", str(table))
    cidrs = routes["allow_cidrs"] if routes else [address + "/32" for address, _ in targets]
    for cidr in cidrs:
        command(IP, "route", "replace", cidr, "dev", link, "table", str(table))
    command(IP, "rule", "add", "priority", str(table), "from", SOURCE, "lookup", str(table))


def route_unchanged(table, targets, link, *, routes=None):
    installed = json.loads(command(IP, "-j", "route", "show", "table", str(table)).stdout)
    rules = json.loads(command(IP, "-j", "-4", "rule", "show").stdout)
    matched = [rule for rule in rules if rule.get("priority") == table]
    if matched != [{"priority": table, "src": "198.19.0.0", "srclen": 16, "table": str(table)}]:
        return False
    if routes:
        expected = set(routes["allow_cidrs"])
        normalized = {
            "0.0.0.0/0" if route["dst"] == "default" else str(ipaddress.IPv4Network(route["dst"]))
            for route in installed
            if route.get("dev") == link
        }
        return (
            len(installed) == len(expected) + 1
            and normalized == expected
            and any(
                route.get("type") == "unreachable"
                and route.get("dst") == "default"
                and route.get("metric") == 32767
                for route in installed
            )
        )
    return any(
        route.get("type") == "unreachable" and route.get("dst") == "default" for route in installed
    ) and all(
        any(
            route.get("dst") in {addr, addr + "/32"} and route.get("dev") == link
            for route in installed
        )
        for addr, _ in targets
    )


def recent_path(mark):
    require(mark in MARKS)
    return Path("/proc/net/xt_recent") / mark


def edge_fence():
    for mark in MARKS:
        recent_path(mark).write_text("-0.0.0.0\n")
    command(
        RESTORE,
        "--wait",
        "2",
        "--noflush",
        input="\n".join(
            [
                "*filter",
                *[
                    line
                    for chain in (EDGE_CHAIN, RENEW, SET_MARK)
                    for line in (f"-F {chain}", f"-A {chain} -j DROP")
                ],
                "COMMIT",
                "",
            ]
        ),
    )


def forwarding_rules(grant, targets, routes=None):
    mask = f"-m recent --name {grant['mark']} --rcheck --seconds 10 --mask 0.0.0.0"
    timing = f"-m time --datestop {grant['end']}"
    source = PROBE_IP + "/32" if grant["mode"] == "probe" else SOURCE
    selected = [
        (addr + "/32", port)
        for addr, port in (targets[:1] if grant["mode"] == "probe" else targets)
    ]
    if routes and grant["mode"] == "solver":
        selected = [(cidr, None) for cidr in routes["allow_cidrs"]]
    rules = []
    for cidr in routes["deny_cidrs"] if routes else ():
        rules += [
            f"-A {EDGE_CHAIN} -i {WG_LINK} -d {cidr} -j DROP",
            f"-A {EDGE_CHAIN} -i {TUN} -s {cidr} -j DROP",
        ]
    for cidr, port in selected:
        out_port = f" --dport {port}" if port is not None else ""
        in_port = f" --sport {port}" if port is not None else ""
        rules += [
            f"-A {EDGE_CHAIN} -i {WG_LINK} -o {TUN} -s {source} -d {cidr} -p tcp{out_port} "
            f"{mask} --rsource {timing} "
            "-m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT",
            f"-A {EDGE_CHAIN} -i {TUN} -o {WG_LINK} -s {cidr} -d {source} -p tcp{in_port} "
            f"{mask} --rdest {timing} -m conntrack --ctstate ESTABLISHED -j ACCEPT",
        ]
    return rules


def confirm_local_renewal():
    # Only the fixed root-owned OUTPUT hook may set a live gate. sendto is not evidence.
    nonce = os.urandom(24)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as trigger:
        endpoint = ("127.0.0.1", RENEW_PORT)
        trigger.bind(endpoint)
        trigger.settimeout(0.5)
        trigger.sendto(nonce, endpoint)
        observed, source = trigger.recvfrom(64)
    require(source == endpoint and observed == nonce)


def edge_grant(lease, sampled, targets, prior, *, routes=None):
    require(lease["mode"] in {"probe", "solver"})
    issued, until = lease["issued_at_ms"], lease["authority_until_ms"]
    require(type(issued) is int and type(until) is int)
    ceiling = (until - SKEW_MS) // 1000 - 1
    require(
        0 <= boot_ms() - sampled
        and boot_ms() - sampled + DEADLINE_MS + 750 <= until - issued <= 15000
    )
    require((ceiling + 1) * 1000 - now_ms() >= DEADLINE_MS)
    end = datetime.fromtimestamp(ceiling, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    mark = MARKS[1] if prior and prior["mark"] == MARKS[0] else MARKS[0]
    recent_path(mark).write_text("-0.0.0.0\n")
    grant = {"mark": mark, "end": end, "mode": lease["mode"]}
    rules = ["*filter", f"-F {EDGE_CHAIN}", f"-F {RENEW}", f"-F {SET_MARK}"]
    if prior:
        rules += forwarding_rules(prior, targets, routes)
        # A delayed root process cannot arm the inactive marker once the old one expired.
        rules.append(
            f"-A {RENEW} -m time --datestop {prior['end']} "
            f"-m recent --name {prior['mark']} --rcheck --seconds 9 "
            f"--mask 0.0.0.0 --rsource -j {SET_MARK}"
        )
    else:
        rules.append(f"-A {RENEW} -j {SET_MARK}")
    rules += [
        *forwarding_rules(grant, targets, routes),
        f"-A {EDGE_CHAIN} -j DROP",
        f"-A {RENEW} -j DROP",
        f"-A {SET_MARK} -m time --datestop {end} -m recent --name {mark} "
        "--set --mask 0.0.0.0 --rsource -j ACCEPT",
        f"-A {SET_MARK} -j DROP",
        "COMMIT",
        "",
    ]
    command(RESTORE, "--wait", "2", "--noflush", input="\n".join(rules))
    require(boot_ms() - sampled + DEADLINE_MS + 750 <= until - issued)
    require((ceiling + 1) * 1000 - now_ms() >= DEADLINE_MS)
    applied_boot = boot_ms()
    confirm_local_renewal()
    require("src=0.0.0.0 " in recent_path(mark).read_text())
    if prior:
        recent_path(prior["mark"]).write_text("-0.0.0.0\n")
    return grant, applied_boot + DEADLINE_MS, min(issued + DEADLINE_MS, (ceiling + 1) * 1000)


def openvpn(config):
    peer_config = config["peer"]
    remote, port, server = (
        ipv4(peer_config["ipv4"]),
        peer_config["port"],
        peer_config.get("server_name"),
    )
    require(type(port) is int and 1 <= port <= 65535)
    dynamic = config.get("profile") == DYNAMIC_PROFILE
    require(
        (server is None and dynamic) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}", server)
    )
    material = EGRESS_ROOT if dynamic else CONFIG
    args = [
        OPENVPN,
        "--client",
        "--proto",
        "udp4",
        "--remote",
        remote,
        str(port),
        "--nobind",
        "--dev",
        TUN,
        "--dev-type",
        "tun",
        "--disable-dco",
        "--topology",
        "subnet",
        "--ifconfig-noexec",
        "--route-noexec",
        "--route-nopull",
        "--pull-filter",
        "accept",
        "ifconfig ",
        "--pull-filter",
        "accept",
        "topology ",
        "--pull-filter",
        "accept",
        "peer-id ",
        "--pull-filter",
        "accept",
        "cipher ",
        "--pull-filter",
        "accept",
        "protocol-flags ",
        "--pull-filter",
        "ignore",
        "",
        "--remap-usr1",
        "SIGTERM",
        "--remote-cert-tls",
        "server",
        "--script-security",
        "0",
        "--tmp-dir",
        str(EGRESS_ROOT),
        "--auth-nocache",
        "--auth-retry",
        "none",
        "--connect-retry-max",
        "1",
        "--ping",
        "3",
        "--ping-exit",
        "10",
        "--verb",
        "1",
        "--data-ciphers",
        "AES-256-GCM:AES-128-GCM",
        "--ca",
        str(material / "ca.crt"),
        "--cert",
        str(material / "tls.crt"),
        "--key",
        str(material / "tls.key"),
    ]
    if server is not None:
        args += ["--verify-x509-name", server, "name"]
    if dynamic:
        args += ["--management", str(MANAGEMENT), "unix", "--management-client-user", "root"]
        if (material / "tls-crypt.key").is_file():
            args += ["--tls-crypt", str(material / "tls-crypt.key")]
    return subprocess.Popen(
        args,
        env={},
        cwd=EGRESS_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Control:
    def __init__(self, config):
        ca = CONFIG / "ca.pem"
        ssl_ctx = ssl.create_default_context(cafile=str(ca) if ca.stat().st_size else None)
        self.client = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl_ctx),
        )
        origin = config["blackboard_url"].rstrip("/")
        require(origin.startswith("https://"))
        self.path = origin + f"/api/tasks/{UUID(config['scope']['task_id'])}/runner-vpn/"
        self.headers = {
            "x-bbx-vpn-egress": config["report_key"],
            "content-type": "application/json",
        }

    def call(self, action, data=None):
        raw = None if data is None else json.dumps(data, separators=(",", ":")).encode()
        request = urllib.request.Request(self.path + action, data=raw, headers=self.headers)
        limit = MATERIAL_LIMIT if action == "claim" else LOG_LIMIT
        with self.client.open(request, timeout=3) as response:
            payload = response.read(limit + 1)
        require(len(payload) <= limit)
        return json.loads(payload)


def store_material(material):
    require(
        isinstance(material, dict) and set(material) in ({"ca", "cert", "key"}, set(MATERIAL_FILES))
    )
    for field, value in material.items():
        require(isinstance(value, str) and 0 < len(value) <= 64 * 1024)
        fd = os.open(
            EGRESS_ROOT / MATERIAL_FILES[field], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(fd, "w", encoding="ascii") as output:
            output.write(value)


def assigned_address(child, peer_config):
    # OpenVPN 2.7.8 reports the negotiated address even with --ifconfig-noexec.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(0.5)
        conn.connect(str(MANAGEMENT))
        pid, uid, _ = struct.unpack(
            "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        require(pid == child.pid and uid == 0)
        conn.sendall(b"state\n")
        with conn.makefile("rb") as response:
            result = None
            for _ in range(12):
                line = response.readline(512)
                require(line.endswith(b"\n"))
                line = line.rstrip(b"\r\n")
                if line == b"END":
                    return result
                fields = line.split(b",")
                if fields[0].isdigit() and fields[1:2] == [b"CONNECTED"]:
                    require(
                        len(fields) >= 6
                        and fields[2] == b"SUCCESS"
                        and fields[4].decode("ascii") == peer_config["ipv4"]
                        and fields[5].decode("ascii") == str(peer_config["port"])
                    )
                    result = ipv4(fields[3].decode("ascii"))
            raise ValueError("Original OpenVPN state is unconfirmed")


def check_tun(config, address, targets, routes=None):
    require(
        address not in {a for a, _ in targets}
        and address != config["peer"]["ipv4"]
        and not local_address(address)
        and not any(
            ipaddress.IPv4Address(address) in ipaddress.ip_network(network)
            for network in (
                SOURCE,
                "198.18.0.0/16",
                *config.get("deny_cidrs", ()),
                *(routes["deny_cidrs"] if routes else ()),
            )
        )
    )


def install_tun_address(config, address, targets, routes):
    if routes:
        check_tun(config, address, targets, routes)
    command(IP, "address", "add", address + "/32", "dev", TUN, "noprefixroute")
    command(IP, "link", "set", "dev", TUN, "mtu", "1280", "up")


def terminal(child):
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=2)


def edge_close(child):
    try:
        edge_fence()
    finally:
        terminal(child)
        for name in MATERIAL_FILES.values():
            (EGRESS_ROOT / name).unlink(missing_ok=True)


def edge_run(config, stop):
    control = Control(config)
    scope = config["scope"]
    require(local_address(ipv4(scope["runner_pod_ipv4"])))
    dynamic = config.get("profile") == DYNAMIC_PROFILE
    targets, approved = [], None
    if not dynamic:
        targets = target_pairs(config["targets"])
        approved = ipv4(config["tun_ipv4"])
        check_tun(config, approved, targets)
    require(Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() == "1")
    install_drop_hooks((WG_LINK, TUN), edge=True)
    child = op = link = key = tunnel = None
    last_revision, applied_boot = 0, None
    active_grant = None
    held = closed = pending = None
    network = routes = None
    last_work = time.monotonic()
    hard_deadline = int(datetime.fromisoformat(config["hard_deadline"]).timestamp() * 1000)
    try:
        while not stop() and now_ms() < hard_deadline:
            try:
                if applied_boot is not None and boot_ms() >= applied_boot:
                    raise ValueError("The original VPN data-plane lease expired")
                view = control.call("desired")
                state = view["state"]
                remote = view.get("remote_public_key")
                if state in {"disconnecting", "disconnected", "stopped", "closed"}:
                    if op and closed != "closed":
                        edge_close(child)
                        applied_boot = active_grant = None
                        closed = "closed"
                        pending = {**op, "phase": "closed", "forwarding_closed": True}
                if state in {"failed", "unknown"} and op and not closed:
                    edge_close(child)
                    applied_boot = active_grant = None
                    closed = "failed"
                    pending = {**op, "phase": "failed", "forwarding_closed": True}
                if pending is not None:
                    control.call("observe", pending)
                    pending = None
                if closed or state in {"failed", "unknown", "stopped", "closed", "disconnected"}:
                    time.sleep(0.8)
                    continue
                if view.get("vpn_operation_id") and op is None and state == "requested":
                    candidate = operation(
                        config, view["scope"], view["vpn_operation_id"], view.get("network")
                    )
                    if dynamic:
                        network = view["network"]
                        targets = target_pairs([network["preflight"]])
                        routes = network_routes(network["route_policy"])
                        probe = ipaddress.IPv4Address(targets[0][0])
                        require(
                            any(
                                probe in ipaddress.IPv4Network(cidr)
                                for cidr in routes["allow_cidrs"]
                            )
                            and not any(
                                probe in ipaddress.IPv4Network(cidr)
                                for cidr in routes["deny_cidrs"]
                            )
                        )
                        config = {**config, "peer": network["peer"]}
                        # A lost response may have consumed the only claim. Never repeat it.
                        op = candidate
                        persist(EGRESS_ROOT / "claimed.json", op)
                    try:
                        claim = control.call("claim", candidate)
                    except (OSError, ValueError):
                        if not dynamic:
                            raise
                        closed = "unknown"
                        pending = {**candidate, "phase": "unknown", "forwarding_closed": True}
                        continue
                    if claim.get("launch_allowed") is True:
                        if dynamic:
                            store_material(claim["material"])
                        else:
                            op = candidate
                            persist(EGRESS_ROOT / "claimed.json", op)
                        link = install_wg(EGRESS_ROOT)
                    elif dynamic:
                        closed = "unknown"
                        pending = {**candidate, "phase": "unknown", "forwarding_closed": True}
                        continue
                if op:
                    require(
                        view.get("vpn_operation_id") == op["vpn_operation_id"]
                        and (not dynamic or view.get("network") == network)
                    )
                    if link and key is None:
                        control.call("observe", {**op, "phase": "link", "link": link})
                    if link and remote and key is None and state != "held":
                        peer(remote, scope["sandbox_pod_ipv4"], [SOURCE])
                        command(IP, "route", "add", SOURCE, "dev", WG_LINK)
                        key = remote
                        child = openvpn(config)
                        last_work = time.monotonic()
                    if child and (
                        child.poll() is not None
                        or remote != key
                        or not live_link(link, key, scope["sandbox_pod_ipv4"])
                    ):
                        raise ValueError("Original VPN process or link stopped")
                    idx = ifindex(TUN) if child else None
                    if child and (
                        (idx is None and tunnel)
                        or (not tunnel and time.monotonic() - last_work > 25)
                    ):
                        raise ValueError("Original VPN TUN route is unavailable")
                    if child and idx and tunnel is None:
                        if dynamic:
                            try:
                                approved = assigned_address(child, config["peer"])
                            except (FileNotFoundError, ConnectionRefusedError):
                                approved = None
                            if approved is None:
                                time.sleep(0.8)
                                continue
                        require(approved is not None)
                        install_tun_address(config, approved, targets, routes)
                        route_policy(119, targets, TUN, routes=routes)
                        destinations = (
                            [(cidr, None) for cidr in routes["allow_cidrs"]]
                            if routes
                            else [(address + "/32", port) for address, port in targets]
                        )
                        for cidr, port in destinations:
                            command(
                                IPT,
                                "-w",
                                "2",
                                "-t",
                                "nat",
                                "-A",
                                "POSTROUTING",
                                "-o",
                                TUN,
                                "-s",
                                SOURCE,
                                "-d",
                                cidr,
                                "-p",
                                "tcp",
                                *(["--dport", str(port)] if port is not None else []),
                                "-j",
                                "SNAT",
                                "--to-source",
                                approved,
                            )
                        ticks = process_ticks(child.pid)
                        require(ticks is not None)
                        tunnel = {
                            "pid": child.pid,
                            "start_ticks": ticks,
                            "tun_ifindex": idx,
                            "tun_ipv4": approved,
                            "link": link,
                        }
                    if tunnel:
                        require(
                            idx == tunnel["tun_ifindex"]
                            and local_address(approved, TUN)
                            and process_ticks(tunnel["pid"]) == tunnel["start_ticks"]
                            and (not dynamic or assigned_address(child, config["peer"]) == approved)
                            and route_unchanged(119, targets, TUN, routes=routes)
                        )
                        if state == "held":
                            pause_id = str(UUID(view["pause_id"]))
                            require(held in {None, pause_id})
                            if held is None:
                                edge_fence()
                                held, applied_boot = pause_id, None
                                active_grant = None
                            control.call(
                                "observe",
                                {
                                    **op,
                                    "phase": "held",
                                    "pause_id": held,
                                    "tunnel": tunnel,
                                    "forwarding_closed": True,
                                },
                            )
                            time.sleep(0.8)
                            continue
                        if held is not None and view.get("resume_pause_id") != held:
                            time.sleep(0.8)
                            continue
                        control.call("observe", {**op, "phase": "tunnel", "tunnel": tunnel})
                        sampled = boot_ms()
                        lease = control.call("lease", op)
                        revision = lease["grant_revision"]
                        require(type(revision) is int and revision > last_revision)
                        last_revision = revision
                        active_grant, applied_boot, applied_until = edge_grant(
                            lease,
                            sampled,
                            targets,
                            active_grant,
                            **({"routes": routes} if routes else {}),
                        )
                        held = None
                        control.call(
                            "observe",
                            {
                                **op,
                                "phase": "tunnel",
                                "tunnel": tunnel,
                                "lease_revision": revision,
                                "applied_until_ms": applied_until,
                            },
                        )
            except urllib.error.URLError:
                # The last real kernel grant, not an HTTP response, bounds traffic.
                pass
            except (OSError, ValueError, KeyError, TypeError):
                if op and not closed:
                    edge_close(child)
                    applied_boot = active_grant = None
                    closed = "failed"
                    pending = {**op, "phase": "failed", "forwarding_closed": True}
            time.sleep(0.8)
    finally:
        edge_close(child)


def sandbox_context(scope):
    result = command("/opt/bbx/control/launcher", "--assignment-context")
    value = json.loads(result.stdout)
    require(value["pod_uid"] == scope["sandbox_pod_uid"])
    if scope.get("sandbox_execution_context_id"):
        require(value["execution_context_id"] == scope["sandbox_execution_context_id"])
    require(local_address(ipv4(scope["sandbox_pod_ipv4"])))


def sandbox_key(request):
    SANDBOX_ROOT.mkdir(mode=0o700)
    persist(SANDBOX_ROOT / "operation.json", request)
    install_drop_hooks((WG_LINK,))
    link = install_wg(SANDBOX_ROOT)
    persist(SANDBOX_ROOT / "link.json", link)
    return link


def sandbox_state(request):
    require(not (SANDBOX_ROOT / "fenced").exists())
    require(json.loads((SANDBOX_ROOT / "operation.json").read_text()) == request)
    link = json.loads((SANDBOX_ROOT / "link.json").read_text())
    require(ifindex(WG_LINK, up=True) == link["ifindex"])
    return link


def sandbox_peer(request, key, targets, routes=None):
    link = sandbox_state(request)
    marker = SANDBOX_ROOT / "peer.json"
    expected = {
        "key": key,
        **({"route_policy": routes} if routes else {"targets": [list(x) for x in targets]}),
    }
    if marker.exists():
        require(json.loads(marker.read_text()) == expected)
        require(
            live_link(link, key, request["scope"]["runner_pod_ipv4"])
            and route_unchanged(118, targets, WG_LINK, routes=routes)
        )
        return link
    with (SANDBOX_ROOT / "peer.claim").open("x") as output:
        output.write("claimed\n")
    cidrs = routes["allow_cidrs"] if routes else [addr + "/32" for addr, _ in targets]
    peer(key, request["scope"]["runner_pod_ipv4"], cidrs)
    route_policy(118, targets, WG_LINK, routes=routes)
    persist(marker, expected)
    return link


def sandbox_probe(request, key, targets, routes=None):
    link = sandbox_peer(request, key, targets, routes)
    # A distinct system-only low-UID veth exercises the actual nested Sandbox FORWARD path.
    marker = SANDBOX_ROOT / "probe.claim"
    with marker.open("x") as output:
        output.write("claimed\n")
    addr, port = targets[0]
    command(IP, "netns", "add", PROBE_NET)
    try:
        command(IP, "link", "add", PROBE_VETH, "type", "veth", "peer", "name", "bp-peer")
        command(IP, "link", "set", "bp-peer", "netns", PROBE_NET)
        command(IP, "addr", "add", "198.19.255.1/30", "dev", PROBE_VETH)
        command(IP, "-n", PROBE_NET, "addr", "add", PROBE_IP + "/30", "dev", "bp-peer")
        command(IP, "-n", PROBE_NET, "link", "set", "bp-peer", "mtu", "1280", "up")
        command(IP, "-n", PROBE_NET, "route", "add", "default", "via", "198.19.255.1")
        command(IPT, "-w", "2", "-I", "INPUT", "1", "-i", PROBE_VETH, "-j", "DROP")
        forward = [
            "-i",
            PROBE_VETH,
            "-o",
            WG_LINK,
            "-s",
            PROBE_IP,
            "-d",
            addr,
            "-p",
            "tcp",
            "--dport",
            str(port),
            "-m",
            "conntrack",
            "--ctstate",
            "NEW,ESTABLISHED",
            "-j",
            "ACCEPT",
        ]
        reverse = [
            "-i",
            WG_LINK,
            "-o",
            PROBE_VETH,
            "-s",
            addr,
            "-d",
            PROBE_IP,
            "-p",
            "tcp",
            "--sport",
            str(port),
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED",
            "-j",
            "ACCEPT",
        ]
        command(IPT, "-w", "2", "-I", "FORWARD", "1", *forward)
        command(IPT, "-w", "2", "-I", "FORWARD", "1", *reverse)
        command(IP, "link", "set", PROBE_VETH, "up")
        code = (
            "import socket,sys;"
            "s=socket.create_connection((sys.argv[1],int(sys.argv[2])),2);s.close()"
        )
        result = command(
            IP,
            "netns",
            "exec",
            PROBE_NET,
            "/usr/bin/setpriv",
            "--reuid=10000",
            "--regid=10000",
            "--clear-groups",
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--no-new-privs",
            "/usr/bin/python3",
            "-I",
            "-S",
            "-c",
            code,
            addr,
            str(port),
            check=False,
        )
        evidence = {"link": link, "passed": result.returncode == 0}
        persist(SANDBOX_ROOT / "probe.json", evidence)
        return evidence
    finally:
        command(IP, "link", "del", PROBE_VETH, check=False)
        command(IP, "netns", "del", PROBE_NET, check=False)


def sandbox(args):
    mode, encoded = args
    body = json.loads(base64.urlsafe_b64decode(encoded))
    request = {"scope": body["scope"], "vpn_operation_id": body["vpn_operation_id"]}
    if "network_digest" in body:
        require(re.fullmatch(r"[a-f0-9]{64}", body["network_digest"]))
        request["network_digest"] = body["network_digest"]
    sandbox_context(request["scope"])
    if mode == "key":
        return sandbox_key(request)
    if mode == "status":
        return sandbox_state(request) if SANDBOX_ROOT.exists() else {"state": "absent"}
    if mode == "probe-status":
        sandbox_state(request)
        if not (SANDBOX_ROOT / "probe.claim").exists():
            return {"state": "absent"}
        return json.loads((SANDBOX_ROOT / "probe.json").read_text())
    if mode == "fence":
        if not SANDBOX_ROOT.exists():
            return {"state": "absent"}
        require(json.loads((SANDBOX_ROOT / "operation.json").read_text()) == request)
        if not (SANDBOX_ROOT / "fenced").exists():
            persist(SANDBOX_ROOT / "fenced", request)
        if ifindex(WG_LINK) is not None:
            command(IP, "link", "del", "dev", WG_LINK)
        return {"state": "fenced"}
    routes = network_routes(body["route_policy"]) if "network_digest" in request else None
    targets = target_pairs([body["preflight"]] if routes else body["targets"])
    if mode == "peer":
        return sandbox_peer(request, body["remote_public_key"], targets, routes)
    require(mode == "probe")
    return sandbox_probe(request, body["remote_public_key"], targets, routes)


def main():
    os.umask(0o077)
    require(os.getuid() == os.geteuid() == 0)
    if sys.argv[1] == "sandbox":
        print(json.dumps(sandbox(sys.argv[2:]), separators=(",", ":")))
        return
    require(sys.argv[1:] == ["egress"])
    EGRESS_ROOT.chmod(0o700)
    closing = False

    def terminate(*_):
        nonlocal closing
        closing = True

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    deadline = time.monotonic() + 180
    path = CONFIG / "config.json"
    while not path.is_file() and not closing:
        require(time.monotonic() < deadline)
        time.sleep(0.25)
    if not closing:
        with path.open("rb") as source:
            raw = source.read(LOG_LIMIT + 1)
        require(len(raw) <= LOG_LIMIT)
        edge_run(json.loads(raw), lambda: closing)


if __name__ == "__main__":
    main()
