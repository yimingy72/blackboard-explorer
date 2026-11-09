"""Own a fixed authenticated Task link and one VPN or SOCKS5 egress."""

import fcntl
import ipaddress
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/run/bbx-gateway")
CONFIG = ROOT / "config.json"
KEY = ROOT / "wireguard.key"
MANAGEMENT = ROOT / "openvpn.sock"
IP, IPT, WG = "/usr/sbin/ip", "/usr/sbin/iptables", "/usr/bin/wg"
INNER, TUN = "bbxwg0", "bbxovpn"
SOURCE = "198.18.0.0/24"
INNER_IP = "198.19.0.2"
WG_PORT = "51820"
SOCKS_PORT = "12345"
CONFIG_LIMIT = 300 * 1024
STOPPING = False


def require(check):
    if not check:
        raise ValueError("Original gateway configuration or process is unconfirmed")


def run(*args):
    return subprocess.run(
        args,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=5,
        env={},
    ).stdout


def read_config():
    fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o077 == 0)
        payload = file.read(CONFIG_LIMIT + 1)
    require(len(payload) <= CONFIG_LIMIT)
    CONFIG.unlink()
    return json.loads(payload)


def ip4(value):
    parsed = ipaddress.IPv4Address(value)
    require(
        str(parsed) == value
        and not (
            parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_multicast
            or parsed.is_reserved
            or parsed.is_unspecified
        )
    )
    return parsed


def route(address):
    (value,) = json.loads(run(IP, "-j", "-4", "route", "get", str(address)))
    return value


def port(value):
    require(type(value) is int and 1 <= value <= 65535)
    return str(value)


def validate(config):
    require(
        set(config)
        == {
            "mode",
            "envd_ipv4",
            "gateway_ipv4",
            "envd_public_key",
            "allow",
            "deny",
            "upstream",
            "credentials",
        }
        and config["mode"] in {"vpn", "socks5"}
        and re.fullmatch(r"[A-Za-z0-9+/]{43}=", config["envd_public_key"])
    )
    envd, gateway = (ip4(config[key]) for key in ("envd_ipv4", "gateway_ipv4"))
    outer = config["upstream"]
    require(set(outer) == {"ipv4", "port", "server_name"})
    upstream = ip4(outer["ipv4"])
    service = port(outer["port"])
    require(len({envd, gateway, upstream}) == 3)
    inside, outside = route(envd), route(upstream)
    require(inside["prefsrc"] == str(gateway) and inside["dev"] != outside["dev"])
    require(all(1 <= len(config[key]) <= 128 for key in ("allow", "deny")))
    for key in ("allow", "deny"):
        require(all(str(ipaddress.IPv4Network(cidr)) == cidr for cidr in config[key]))
    require(any(upstream in ipaddress.IPv4Network(cidr) for cidr in config["deny"]))
    return envd, gateway, upstream, service, inside["dev"], outside["dev"]


def start_link(config, envd, gateway, inside):
    peer = [str(envd) + "/32", str(gateway) + "/32"]
    for chain, direction, src, dst in (
        ("INPUT", "-i", peer[0], peer[1]),
        ("OUTPUT", "-o", peer[1], peer[0]),
    ):
        run(
            IPT,
            "-A",
            chain,
            direction,
            inside,
            "-s",
            src,
            "-d",
            dst,
            "-p",
            "udp",
            "--sport",
            WG_PORT,
            "--dport",
            WG_PORT,
            "-j",
            "ACCEPT",
        )
    run(IP, "link", "add", INNER, "type", "wireguard")
    run(
        WG,
        "set",
        INNER,
        "listen-port",
        WG_PORT,
        "private-key",
        str(KEY),
        "peer",
        config["envd_public_key"],
        "endpoint",
        f"{envd}:{WG_PORT}",
        "allowed-ips",
        SOURCE,
    )
    run(IP, "address", "add", INNER_IP + "/32", "dev", INNER, "noprefixroute")
    run(IP, "link", "set", "dev", INNER, "mtu", "1280", "up")
    run(IP, "route", "add", SOURCE, "dev", INNER)
    run(IPT, "-N", "BBX_LEASE")
    run(
        IPT,
        "-A",
        "BBX_LEASE",
        "-m",
        "recent",
        "--name",
        "bbx_gateway",
        "--mask",
        "0.0.0.0",
        "--rcheck",
        "--seconds",
        "15",
        "-j",
        "RETURN",
    )
    run(IPT, "-A", "BBX_LEASE", "-j", "DROP")
    for hook, direction in (
        ("INPUT", "-i"),
        ("OUTPUT", "-o"),
        ("FORWARD", "-i"),
        ("FORWARD", "-o"),
    ):
        run(IPT, "-I", hook, "1", direction, INNER, "-j", "BBX_LEASE")


def upstream_lease(seconds):
    return [
        "-m",
        "recent",
        "--name",
        "bbx_gateway",
        "--mask",
        "0.0.0.0",
        "--rcheck",
        "--seconds",
        str(seconds),
        "-j",
        "ACCEPT",
    ]


def permit_upstream(upstream, service, outside, protocol):
    run(IPT, "-N", "BBX_UPSTREAM")
    run(IPT, "-A", "BBX_UPSTREAM", *upstream_lease(30))
    run(IPT, "-A", "BBX_UPSTREAM", "-j", "DROP")
    Path("/proc/net/xt_recent/bbx_gateway").write_text("+0.0.0.0\n")
    cidr = str(upstream) + "/32"
    run(
        IPT,
        "-A",
        "OUTPUT",
        "-o",
        outside,
        "-d",
        cidr,
        "-p",
        protocol,
        "--dport",
        service,
        "-m",
        "conntrack",
        "--ctstate",
        "NEW,ESTABLISHED",
        "-j",
        "BBX_UPSTREAM",
    )
    run(
        IPT,
        "-A",
        "INPUT",
        "-i",
        outside,
        "-s",
        cidr,
        "-p",
        protocol,
        "--sport",
        service,
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED",
        "-j",
        "BBX_UPSTREAM",
    )


def child_process(args):
    return subprocess.Popen(
        args,
        env={},
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def private_file(name, value):
    require(isinstance(value, str) and 0 < len(value) <= 64 * 1024)
    with (ROOT / name).open("x", encoding="ascii") as file:
        file.write(value)
    return str(ROOT / name)


def redsocks(config, upstream, service, outside):
    require(config["upstream"]["server_name"] is None)
    permit_upstream(upstream, service, outside, "tcp")
    nat = (IPT, "-t", "nat", "-A", "PREROUTING", "-i", INNER, "-s", SOURCE)
    run(*nat, "-m", "addrtype", "--dst-type", "LOCAL", "-j", "RETURN")
    for cidr in config["deny"]:
        run(*nat, "-d", cidr, "-j", "RETURN")
    for cidr in config["allow"]:
        run(*nat, "-p", "tcp", "-d", cidr, "-j", "REDIRECT", "--to-ports", SOCKS_PORT)
    run(
        IPT,
        "-A",
        "INPUT",
        "-i",
        INNER,
        "-s",
        SOURCE,
        "-d",
        INNER_IP + "/32",
        "-p",
        "tcp",
        "--dport",
        SOCKS_PORT,
        "-m",
        "conntrack",
        "--ctstate",
        "DNAT",
        "-j",
        "ACCEPT",
    )
    run(
        IPT,
        "-A",
        "OUTPUT",
        "-o",
        INNER,
        "-d",
        SOURCE,
        "-p",
        "tcp",
        "--sport",
        SOCKS_PORT,
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED",
        "-j",
        "ACCEPT",
    )
    credentials = config["credentials"]
    auth = ""
    if credentials:
        require(set(credentials) == {"login", "password"})
        for field, value in credentials.items():
            require(re.fullmatch(r"[A-Za-z0-9!$%&()*+,./:;<=>?@^_~\[\]{}|-]{1,255}", value))
            auth += f'{field} = "{value}"; '
    path = private_file(
        "redsocks.conf",
        'base { log_debug = off; log_info = off; log = "stderr"; '
        "daemon = off; redirector = iptables; }\n"
        f"redsocks {{ local_ip = {INNER_IP}; local_port = {SOCKS_PORT}; "
        f"ip = {upstream}; port = {service}; type = socks5; {auth}}}\n",
    )
    run("/usr/sbin/redsocks", "-t", "-c", path)
    return child_process(["/usr/sbin/redsocks", "-c", path])


def openvpn(config, upstream, service, outside):
    permit_upstream(upstream, service, outside, "udp")
    credentials = config["credentials"]
    require(set(credentials) in ({"ca", "cert", "key"}, {"ca", "cert", "key", "tls_crypt"}))
    name = config["upstream"]["server_name"]
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}", name))
    argv = [
        "/usr/sbin/openvpn",
        "--client",
        "--proto",
        "udp4",
        "--remote",
        str(upstream),
        service,
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
    ]
    for option in ("ifconfig ", "topology ", "peer-id ", "cipher ", "protocol-flags "):
        argv += ["--pull-filter", "accept", option]
    argv += [
        "--pull-filter",
        "ignore",
        "",
        "--remap-usr1",
        "SIGTERM",
        "--script-security",
        "0",
        "--remote-cert-tls",
        "server",
        "--verify-x509-name",
        name,
        "name",
        "--tmp-dir",
        str(ROOT),
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
        "--management",
        str(MANAGEMENT),
        "unix",
        "--management-client-user",
        "root",
    ]
    for option, field in (
        ("ca", "ca"),
        ("cert", "cert"),
        ("key", "key"),
        ("tls-crypt", "tls_crypt"),
    ):
        if field in credentials:
            argv += ["--" + option, private_file(field, credentials[field])]
    return child_process(argv)


def negotiated_address(child, upstream, service):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(0.5)
        conn.connect(str(MANAGEMENT))
        pid, uid, _ = struct.unpack(
            "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        require(pid == child.pid and uid == 0)
        conn.sendall(b"state\n")
        with conn.makefile("rb") as output:
            address = None
            for _ in range(12):
                line = output.readline(512)
                require(line.endswith(b"\n"))
                fields = line.rstrip().decode("ascii").split(",")
                if fields == ["END"]:
                    return address
                if fields[0].isdigit() and fields[1:2] == ["CONNECTED"]:
                    require(fields[2] == "SUCCESS" and fields[4:6] == [str(upstream), service])
                    address = ip4(fields[3])
    raise ValueError("Original OpenVPN address is unconfirmed")


def vpn_routes(config, child, upstream, service):
    deadline = time.monotonic() + 25
    address = None
    while not STOPPING and address is None:
        require(child.poll() is None and time.monotonic() < deadline)
        try:
            address = negotiated_address(child, upstream, service)
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        if address is None:
            time.sleep(0.2)
    require(address is not None)
    require(not any(address in ipaddress.IPv4Network(cidr) for cidr in config["deny"]))
    require(
        all(
            str(address) != addr["local"]
            for interface in json.loads(run(IP, "-j", "-4", "addr"))
            for addr in interface.get("addr_info", [])
        )
    )
    run(IP, "address", "add", str(address) + "/32", "dev", TUN, "noprefixroute")
    run(IP, "link", "set", "dev", TUN, "mtu", "1280", "up")
    run(IP, "route", "add", "unreachable", "default", "metric", "32767", "table", "119")
    run(IP, "rule", "add", "priority", "119", "iif", INNER, "lookup", "119")
    for cidr in config["deny"]:
        run(IPT, "-A", "FORWARD", "-i", INNER, "-d", cidr, "-j", "DROP")
        run(IPT, "-A", "FORWARD", "-i", TUN, "-s", cidr, "-j", "DROP")
    for cidr in config["allow"]:
        run(IP, "route", "add", cidr, "dev", TUN, "table", "119")
        run(
            IPT,
            "-A",
            "FORWARD",
            "-i",
            INNER,
            "-o",
            TUN,
            "-s",
            SOURCE,
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
        run(
            IPT,
            "-A",
            "FORWARD",
            "-i",
            TUN,
            "-o",
            INNER,
            "-s",
            cidr,
            "-d",
            SOURCE,
            "-p",
            "tcp",
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED",
            "-j",
            "ACCEPT",
        )
    run(
        IPT,
        "-t",
        "nat",
        "-A",
        "POSTROUTING",
        "-o",
        TUN,
        "-s",
        SOURCE,
        "-p",
        "tcp",
        "-j",
        "SNAT",
        "--to-source",
        str(address),
    )
    return address


def stopping(*_):
    global STOPPING
    STOPPING = True


def configure():
    os.umask(0o077)
    require(os.geteuid() == 0 and (ROOT / "public-key").is_file())
    payload = sys.stdin.buffer.read(CONFIG_LIMIT + 1)
    require(0 < len(payload) <= CONFIG_LIMIT)
    with (ROOT / "configure.once").open("x"):
        pass
    temporary = ROOT / "config.pending"
    with temporary.open("xb") as file:
        file.write(payload)
    (ROOT / "lease").write_text(str(time.monotonic_ns() + 90_000_000_000))
    temporary.rename(CONFIG)


def lease(*, renew=False):
    with (ROOT / "lease.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        deadline = ROOT / "lease"
        now = time.monotonic_ns()
        require(int(deadline.read_text()) > now and not (ROOT / "fenced").exists())
        if renew:
            Path("/proc/net/xt_recent/bbx_gateway").write_text("+0.0.0.0\n")
            pending = ROOT / "lease.pending"
            pending.write_text(str(now + 15_000_000_000))
            pending.replace(deadline)


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    require(os.geteuid() == 0)
    for binary in (IPT, "/usr/sbin/ip6tables"):
        for chain in ("INPUT", "OUTPUT", "FORWARD"):
            run(binary, "-P", chain, "DROP")
    KEY.write_text(run(WG, "genkey"), encoding="ascii")
    public = subprocess.run(
        [WG, "pubkey"],
        input=KEY.read_bytes(),
        capture_output=True,
        check=True,
        timeout=5,
        env={},
    ).stdout
    (ROOT / "public-key").write_bytes(public)
    # No private config or forwarding exists while the scheduler restores inputs.
    deadline = time.monotonic() + 900
    while not STOPPING and not CONFIG.exists():
        require(time.monotonic() < deadline)
        time.sleep(0.1)
    if STOPPING:
        return
    config = read_config()
    envd, gateway, upstream, service, inside, outside = validate(config)
    start_link(config, envd, gateway, inside)
    child = None
    try:
        vpn = config["mode"] == "vpn"
        child = (
            openvpn(config, upstream, service, outside)
            if vpn
            else redsocks(config, upstream, service, outside)
        )
        address = vpn_routes(config, child, upstream, service) if vpn else None
        run(IPT, "-R", "BBX_UPSTREAM", "1", *upstream_lease(15))
        lease(renew=True)
        (ROOT / "ready").touch()
        while not STOPPING and child.poll() is None:
            lease()
            if vpn:
                require(negotiated_address(child, upstream, service) == address)
            time.sleep(0.3)
    finally:
        # A failed teardown still exits the gateway network namespace.
        (ROOT / "fenced").touch()
        Path("/proc/net/xt_recent/bbx_gateway").write_text("-0.0.0.0\n")
        (ROOT / "ready").unlink(missing_ok=True)
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(2)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["configure"]:
            configure()
        elif sys.argv[1:] == ["renew"]:
            lease(renew=True)
        else:
            require(len(sys.argv) == 1)
            main()
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise SystemExit("Original protected gateway unavailable") from None
