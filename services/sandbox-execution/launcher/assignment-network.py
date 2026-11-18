"""Trusted root control for the assignment-network-v1 Sandbox image."""

import base64
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import signal
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

ROOT = Path("/var/lib/bbx/control/networks")
MARKER = Path("/opt/bbx/control/assignment-network-v1")
THIS = "/opt/bbx/control/assignment-network.py"
IP, IP4, IP6 = "/usr/sbin/ip", "/usr/sbin/iptables", "/usr/sbin/ip6tables"
RESTORE = "/usr/sbin/iptables-restore"
DUMPCAP = "/usr/bin/dumpcap"
LIMIT, SNAPLEN, MAX_PACKETS = 4 * 1024 * 1024, 512, 7943
TTL_MS, GUARD_MS, UTC_SKEW_MS = 7000, 750, 1000
POOL = ipaddress.IPv4Network("198.19.0.0/16")
PREFIX = "bv"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "HOME": "/root"}


def require(check):
    if not check:
        raise ValueError("Original Assignment network evidence is unavailable")


def command(*argv, input=None, allow_failure=False):
    result = subprocess.run(
        argv, input=input, capture_output=True, text=True, env=ENV, timeout=4, check=False
    )
    if not allow_failure:
        require(result.returncode == 0)
    return result


def atomic(path, data):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    atomic(path, json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def load(path):
    return json.loads(path.read_bytes())


def boot_ms():
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000


def start_ticks(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rindex(")") + 2 :].split()
        return int(fields[19]) if fields[0] not in {"Z", "X"} else None
    except (OSError, ValueError, IndexError):
        return None


def identity(directory, record):
    namespace = Path("/run/netns") / record["namespace"]
    info = namespace.stat()
    index = int((Path("/sys/class/net") / record["veth"] / "ifindex").read_text())
    require(
        (info.st_dev, info.st_ino, index) == tuple(record[k] for k in ("dev", "inode", "ifindex"))
    )
    capture = load(directory / "capture.json")
    require(start_ticks(capture["pid"]) == capture["start_ticks"])
    log = (directory / "capture.log").read_bytes()
    require(
        len(log) <= 256 * 1024 and f"File: {directory / 'active.pcap'}".encode() in log.splitlines()
    )
    return capture


def init():
    require(MARKER.is_file() and os.getuid() == 0 and os.geteuid() == 0 and Path(DUMPCAP).is_file())
    ROOT.mkdir(mode=0o700)
    (ROOT / "lock").touch(mode=0o600)
    for tool in (IP4, IP6):
        command(tool, "-w", "2", "-I", "INPUT", "1", "-i", PREFIX + "+", "-j", "DROP")
        command(tool, "-w", "2", "-I", "FORWARD", "1", "-i", PREFIX + "+", "-j", "DROP")
        command(tool, "-w", "2", "-I", "FORWARD", "1", "-o", PREFIX + "+", "-j", "DROP")
    # Fail startup if kernel-expiring matches cannot be installed; probe never admits packets.
    command(IP4, "-w", "2", "-N", "BBX_CAP")
    command(
        IP4,
        "-w",
        "2",
        "-A",
        "BBX_CAP",
        "-m",
        "recent",
        "--name",
        "bbx_cap",
        "--rcheck",
        "--seconds",
        "7",
        "-m",
        "time",
        "--datestop",
        "2001-01-01T00:00:00",
        "-j",
        "DROP",
    )
    require(Path("/proc/net/xt_recent/bbx_cap").is_file())
    command(IP4, "-w", "2", "-F", "BBX_CAP")
    command(IP4, "-w", "2", "-X", "BBX_CAP")


VPN_MARKER = Path("/opt/bbx/control/task-vpn-v1")
CHOICE_MARKER = Path("/opt/bbx/control/task-network-choice-v1")
VPN_STATE = Path("/var/lib/bbx/control/vpn")
VPN_INTERFACE = "bbxwg"


def check_vpn(proof, record):
    require(
        set(proof)
        in (
            {"vpn_operation_id", "link_ifindex"},
            {"vpn_operation_id", "link_ifindex", "network_digest"},
        )
    )
    require(str(UUID(proof["vpn_operation_id"])) == proof["vpn_operation_id"])
    require(type(proof["link_ifindex"]) is int and proof["link_ifindex"] > 0)
    require(not (VPN_STATE / "fenced").exists())
    operation = load(VPN_STATE / "operation.json")
    link = load(VPN_STATE / "link.json")
    peer = load(VPN_STATE / "peer.json")
    actual_index = int((Path("/sys/class/net") / VPN_INTERFACE / "ifindex").read_text())
    require(
        operation["vpn_operation_id"] == proof["vpn_operation_id"]
        and operation.get("network_digest") == proof.get("network_digest")
        and link["ifindex"] == proof["link_ifindex"] == actual_index
        and (
            peer.get("route_policy") == record["route_policy"]
            if "route_policy" in record
            else peer.get("targets") == [[item["ipv4"], item["port"]] for item in record["targets"]]
        )
    )


def route_policy(value):
    require(set(value) == {"allow_cidrs", "deny_cidrs"})
    for entries in value.values():
        require(
            isinstance(entries, list)
            and 1 <= len(entries) <= 128
            and len(set(entries)) == len(entries)
            and all(str(ipaddress.IPv4Network(item)) == item for item in entries)
        )
    return value


def destinations(record):
    if "route_policy" in record:
        paths: list[tuple[str, str, int | None]] = [
            ("tcp", cidr, None) for cidr in record["route_policy"]["allow_cidrs"]
        ]
        if "dns_ipv4" in record and not record.get("vpn"):
            paths.append(("udp", record["dns_ipv4"] + "/32", 53))
        return paths
    return [("tcp", item["ipv4"] + "/32", item["port"]) for item in record["targets"]]


def plan(value):
    fields = {
        "source_id",
        "ordinal",
        "namespace",
        "network_identity",
        "ipv4",
        "route_policy" if "route_policy" in value else "targets",
        "scope_id",
    }
    if VPN_MARKER.is_file() and not (
        CHOICE_MARKER.is_file() and "route_policy" in value and "vpn" not in value
    ):
        fields.add("vpn")
    if "dns_ipv4" in value:
        fields.add("dns_ipv4")
        require("route_policy" in value)
    if "disable_dns" in value:
        fields.add("disable_dns")
        require(value["disable_dns"] is True and "vpn" in value and "dns_ipv4" not in value)
    require(set(value) == fields)
    ordinal = value["ordinal"]
    require(type(ordinal) is int and 1 <= ordinal <= 256 and ordinal != 64)
    require(
        isinstance(value["source_id"], str) and re.fullmatch(r"[a-z0-9-]{1,80}", value["source_id"])
    )
    require(
        re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value["network_identity"])
        and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value["namespace"])
        and re.fullmatch(r"[a-f0-9]{64}", value["scope_id"])
    )
    host = POOL.network_address + ordinal * 4 + 1
    require(value["ipv4"] == str(host + 1) and value["namespace"] == f"bbx{ordinal:03x}")
    if "route_policy" in value:
        policy = route_policy(value["route_policy"])
        if "dns_ipv4" in value:
            dns = ipaddress.IPv4Address(value["dns_ipv4"])
            require(
                str(dns) == value["dns_ipv4"]
                and dns not in POOL
                and any(dns in ipaddress.IPv4Network(cidr) for cidr in policy["allow_cidrs"])
                and not any(dns in ipaddress.IPv4Network(cidr) for cidr in policy["deny_cidrs"])
            )
    else:
        require(isinstance(value["targets"], list) and 1 <= len(value["targets"]) <= 8)
    for item in value.get("targets", ()):
        require(set(item) == {"ipv4", "port"})
        target = ipaddress.IPv4Address(item["ipv4"])
        require(
            str(target) == item["ipv4"]
            and target not in POOL
            and not (
                target.is_loopback
                or target.is_multicast
                or target.is_unspecified
                or target.is_link_local
                or target.is_reserved
            )
        )
        require(type(item["port"]) is int and 1 <= item["port"] <= 65535)
    if "vpn" in value:
        check_vpn(value["vpn"], value)
    return value | {
        "veth": f"{PREFIX}{ordinal:03x}",
        "gateway": str(host),
        "state": "claimed",
        "grant_revision": 0,
    }


def capture(directory):
    # Only the existing per-source dumpcap process is supervised; no daemon grants egress.
    with (directory / "capture.log").open("xb") as out:
        proc = subprocess.Popen(
            [
                DUMPCAP,
                "-p",
                "-i",
                load(directory / "state.json")["veth"],
                "-P",
                "-s",
                str(SNAPLEN),
                "-c",
                str(MAX_PACKETS),
                "-w",
                str(directory / "active.pcap"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            env=ENV,
            start_new_session=True,
        )
        ticks = start_ticks(proc.pid)
        require(ticks is not None)
        save(directory / "capture.json", {"pid": proc.pid, "start_ticks": ticks})
        status = proc.wait()
        save(directory / "exit.json", {"exit_code": status, "at_ms": boot_ms()})


def prepare(directory, requested):
    record = plan(requested)
    require(not directory.exists())
    entries = [load(p) for p in ROOT.glob("*/state.json")]
    require(
        sum(r["state"] != "acked" for r in entries) < 24
        and all(
            r["ordinal"] != record["ordinal"] and r["namespace"] != record["namespace"]
            for r in entries
        )
    )
    directory.mkdir(mode=0o700)
    save(directory / "state.json", record)  # Commit once before the first kernel side effect.
    ns, veth, address = (record[k] for k in ("namespace", "veth", "ipv4"))
    if "dns_ipv4" in record or record.get("disable_dns"):
        resolver = ROOT / f"{ns}.resolv.conf"
        options = " use-vc" if record.get("vpn") else ""
        text = ""
        if not record.get("disable_dns"):
            text = f"nameserver {record['dns_ipv4']}\noptions timeout:2 attempts:2{options}\n"
        atomic(resolver, text.encode())
        resolver.chmod(0o644)  # Solver can read it only through the launcher's private bind mount.
    command(IP, "netns", "add", ns)
    command(IP, "link", "add", veth, "type", "veth", "peer", "name", veth + "p")
    command(IP, "link", "set", veth + "p", "netns", ns)
    command(IP, "addr", "add", record["gateway"] + "/30", "dev", veth)
    command(IP, "-n", ns, "addr", "add", address + "/30", "dev", veth + "p")
    command(IP, "-n", ns, "link", "set", "lo", "up")
    if record.get("vpn"):
        command(IP, "link", "set", veth, "mtu", "1280")
        command(IP, "-n", ns, "link", "set", veth + "p", "mtu", "1280")
    command(IP, "-n", ns, "link", "set", veth + "p", "up")
    command(IP, "-n", ns, "route", "add", "default", "via", record["gateway"])
    command(IP, "link", "set", veth, "up")  # Prefix defaults already deny both directions.
    info = (Path("/run/netns") / ns).stat()
    record.update(
        dev=info.st_dev,
        inode=info.st_ino,
        ifindex=int((Path("/sys/class/net") / veth / "ifindex").read_text()),
    )
    chain = chain_name(record)
    command(IP4, "-w", "2", "-N", chain)
    command(
        IP4,
        "-w",
        "2",
        "-A",
        chain,
        "-m",
        "recent",
        "--name",
        chain,
        "--rcheck",
        "--seconds",
        "7",
        "-j",
        "DROP",
    )
    command(IP4, "-w", "2", "-I", "FORWARD", "1", "-i", veth, "-j", chain)
    command(IP4, "-w", "2", "-I", "FORWARD", "1", "-o", veth, "-j", chain)
    if not record.get("vpn"):
        for protocol, destination, port in destinations(record):
            command(
                IP4,
                "-w",
                "2",
                "-t",
                "nat",
                "-A",
                "POSTROUTING",
                "-s",
                address + "/32",
                "-d",
                destination,
                "-p",
                protocol,
                *(["--dport", str(port)] if port is not None else []),
                *(["-o", "eth0"] if "route_policy" in record else []),
                "-j",
                "MASQUERADE",
            )
    save(directory / "state.json", record)
    subprocess.Popen(
        [sys.executable, "-I", THIS, "capture", directory.name],
        env=ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"state": "claimed", "source_id": record["source_id"]}


def chain_name(record):
    return "BBX_" + record["veth"].upper()


def renew(directory, record, value):
    require(
        set(value)
        == {"source_id", "grant_revision", "issued_at_ms", "authority_until_ms", "boot_ms"}
    )
    require(
        all(
            type(value[k]) is int
            for k in ("grant_revision", "issued_at_ms", "authority_until_ms", "boot_ms")
        )
    )
    require(
        value["source_id"] == record["source_id"]
        and record["state"] in {"claimed", "ready"}
        and not (ROOT / (record["namespace"] + ".revoked")).exists()
    )
    identity(directory, record)
    ttl = 5000 if record.get("vpn") else TTL_MS
    if record.get("vpn"):
        check_vpn(record["vpn"], record)
    if value["grant_revision"] == record["grant_revision"]:
        return read(directory, record)
    require(value["grant_revision"] > record.get("attempted_revision", record["grant_revision"]))
    now = boot_ms()
    budget = value["authority_until_ms"] - value["issued_at_ms"]
    require(
        0 <= now - value["boot_ms"] and now - value["boot_ms"] + ttl + GUARD_MS <= budget <= 12000
    )
    wall = time.time_ns() // 1_000_000
    ceiling = (value["authority_until_ms"] - UTC_SKEW_MS) // 1000 - 1
    require((ceiling + 1) * 1000 - wall >= ttl)
    from datetime import UTC, datetime

    end = datetime.fromtimestamp(ceiling, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    veth, address = (record[k] for k in ("veth", "ipv4"))
    chain = chain_name(record)
    save(directory / "state.json", record | {"attempted_revision": value["grant_revision"]})
    common = f"-m recent --name {chain} --rcheck --seconds {ttl // 1000}"
    timing = f"-m time --datestop {end}"
    # Replace both directions together. The temporary empty chain falls through to the
    # prefix DROP; no established connection has a path around these two checks.
    rules = ["*filter", f"-F {chain}"]
    outbound = f"-o {VPN_INTERFACE}" if record.get("vpn") else f"! -o {PREFIX}+"
    inbound = f"-i {VPN_INTERFACE}" if record.get("vpn") else f"! -i {PREFIX}+"
    if "route_policy" in record:
        if not record.get("vpn"):
            outbound, inbound = "-o eth0", "-i eth0"
        for cidr in record["route_policy"]["deny_cidrs"]:
            rules += [
                f"-A {chain} -i {veth} -d {cidr} -j DROP",
                f"-A {chain} -o {veth} -s {cidr} -j DROP",
            ]
    for protocol, cidr, port in destinations(record):
        out_port = f" --dport {port}" if port is not None else ""
        in_port = f" --sport {port}" if port is not None else ""
        rules += [
            f"-A {chain} -i {veth} {outbound} -s {address}/32 -d {cidr} -p {protocol}{out_port} "
            f"{common} --rsource {timing} "
            "-m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT",
            f"-A {chain} {inbound} -o {veth} -s {cidr} -d {address}/32 -p {protocol}{in_port} "
            f"{common} --rdest {timing} "
            "-m conntrack --ctstate ESTABLISHED -j ACCEPT",
        ]
    command(
        RESTORE,
        "--wait",
        "2",
        "--noflush",
        input="\n".join(rules + [f"-A {chain} -j DROP", "COMMIT", ""]),
    )
    collector = identity(directory, record)
    refreshed = boot_ms()
    wall_remaining = (ceiling + 1) * 1000 - time.time_ns() // 1_000_000
    require(refreshed - value["boot_ms"] + ttl + GUARD_MS <= budget and wall_remaining >= ttl)
    (Path("/proc/net/xt_recent") / chain).write_text(f"+{address}\n")
    atomic(
        ROOT / (record["namespace"] + ".gate"),
        (
            f"{record['scope_id']} {record['network_identity']} {address} {record['dev']} "
            f"{record['inode']} {refreshed + ttl} {record['veth']} {record['ifindex']} "
            f"{collector['pid']} {collector['start_ticks']}\n"
        ).encode(),
    )
    record.update(
        grant_revision=value["grant_revision"],
        attempted_revision=value["grant_revision"],
        state="ready",
        gate_until_ms=refreshed + ttl,
    )
    save(directory / "state.json", record)
    return read(directory, record)


def read(directory, record):
    data = {"state": record["state"], "source_id": record["source_id"], "boot_ms": boot_ms()}
    if "sealed" in record:
        return data | record["sealed"]
    try:
        capture = identity(directory, record)
    except (OSError, ValueError, KeyError):
        return data | {"capture_ready": False}
    return data | {
        "capture_ready": True,
        "netns_inode": record["inode"],
        "veth_ifindex": record["ifindex"],
        "capture_pid": capture["pid"],
        "capture_start_ticks": capture["start_ticks"],
        "grant_revision": record["grant_revision"],
        "gate_live": record.get("gate_until_ms", 0) > boot_ms(),
    }


def fence(directory, record):
    if record["state"] in {"sealed", "acked", "fenced"}:
        return read(directory, record)
    atomic(ROOT / (record["namespace"] + ".revoked"), b"revoked\n")
    record.update(state="fencing")
    save(directory / "state.json", record)
    (ROOT / (record["namespace"] + ".gate")).unlink(missing_ok=True)
    chain = chain_name(record)
    with contextlib.suppress(FileNotFoundError):
        (Path("/proc/net/xt_recent") / chain).write_text(f"-{record['ipv4']}\n")
    command(
        RESTORE,
        "--wait",
        "2",
        "--noflush",
        input=f"*filter\n-F {chain}\n-A {chain} -j DROP\nCOMMIT\n",
    )
    record.update(state="fenced", fenced_ms=boot_ms())
    save(directory / "state.json", record)
    return read(directory, record)


def pcap_prefix(data):
    mode, frac = {
        b"\xd4\xc3\xb2\xa1": ("<", 10**6),
        b"\xa1\xb2\xc3\xd4": (">", 10**6),
        b"\x4d\x3c\xb2\xa1": ("<", 10**9),
        b"\xa1\xb2\x3c\x4d": (">", 10**9),
    }[data[:4]]
    require(24 <= len(data) <= LIMIT)
    major, minor, _, _, snap, link = struct.unpack_from(mode + "HHIIII", data, 4)
    require((major, minor, snap, link) == (2, 4, SNAPLEN, 1))
    offset, count, truncated = 24, 0, 0
    while len(data) - offset >= 16:
        _, fraction, length, original = struct.unpack_from(mode + "IIII", data, offset)
        require(fraction < frac and 0 < length <= min(snap, original))
        end = offset + 16 + length
        if end > len(data):
            break
        offset = end
        count += 1
        truncated += length < original
    require(count <= MAX_PACKETS)
    return offset, count, truncated


def sealed(directory, record):
    if record["state"] in {"sealed", "acked"}:
        return read(directory, record)
    require(record["state"] == "fenced")
    capture = load(directory / "capture.json")
    graceful = False
    if not (directory / "exit.json").exists():
        with contextlib.suppress(ProcessLookupError):
            fd = os.pidfd_open(capture["pid"])
            try:
                require(start_ticks(capture["pid"]) == capture["start_ticks"])
                signal.pidfd_send_signal(fd, signal.SIGINT)
                graceful = True
            finally:
                os.close(fd)
    for _ in range(30):
        if (directory / "exit.json").exists():
            break
        time.sleep(0.05)
    exit = load(directory / "exit.json")
    require(start_ticks(capture["pid"]) != capture["start_ticks"])
    active = directory / "active.pcap"
    final = directory / "sealed.pcap"
    fd = os.open(active if active.exists() else final, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == 0)
        os.fsync(handle.fileno())
        data = handle.read(LIMIT + 1)
        require(len(data) == info.st_size)
    offset, count, truncated = pcap_prefix(data)
    tail = len(data) - offset
    log = (directory / "capture.log").read_bytes()
    stats = re.findall(
        rb"Packets received/dropped on interface '"
        + record["veth"].encode()
        + rb"': ([0-9]+)/([0-9]+) \(pcap:([0-9]+)/dumpcap:([0-9]+)"
        + rb"/flushed:([0-9]+)/ps_ifdrop:([0-9]+)\)",
        log,
    )
    seen = re.findall(rb"Packets captured: ([0-9]+)", log)
    stats_error = b"Can't get packet-drop statistics" in log
    counters = tuple(map(int, stats[0])) if len(stats) == 1 and not stats_error else None
    complete = bool(
        graceful
        and exit["exit_code"] == 0
        and not tail
        and not truncated
        and count < MAX_PACKETS
        and counters == (count, 0, 0, 0, 0, 0)
        and len(seen) == 1
        and int(seen[0]) == count
    )
    part = data[:offset]
    result = {
        "sha256": hashlib.sha256(part).hexdigest(),
        "size_bytes": len(part),
        "packet_count": count,
        "complete": complete,
        "discarded_tail_bytes": tail,
        "truncated_packets": truncated,
        "dropped_packets": counters[1] + counters[5] if counters else None,
        **({"error": "capture_coverage_partial"} if not complete else {}),
        "netns_inode": record["inode"],
        "veth_ifindex": record["ifindex"],
        "capture_pid": capture["pid"],
        "capture_start_ticks": capture["start_ticks"],
    }
    intent = directory / "seal-intent.json"
    if intent.exists():
        original = load(intent)
        require(
            original["sha256"] == result["sha256"]
            and original["size_bytes"] == result["size_bytes"]
        )
        result = original
    else:
        save(intent, result)
    if tail:
        with active.open("r+b") as writer:
            writer.truncate(offset)
            os.fsync(writer.fileno())
    if active.exists():
        os.replace(active, final)
    save(directory / "state.json", record | {"state": "sealed", "sealed": result})
    return result | {"state": "sealed", "source_id": record["source_id"]}


def main():
    require(os.getuid() == os.geteuid() == 0 and MARKER.is_file())
    mode = sys.argv[1]
    if mode == "init":
        init()
        return
    source = sys.argv[2]
    require(re.fullmatch(r"[a-z0-9-]{1,80}", source))
    directory = ROOT / source
    if mode == "capture":
        capture(directory)
        return
    value = json.loads(base64.urlsafe_b64decode(sys.argv[3])) if len(sys.argv) == 4 else {}
    with (ROOT / "lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if mode == "prepare":
            require(value["source_id"] == source)
            result = prepare(directory, value)
        elif not directory.exists():
            result = {"state": "absent", "source_id": source}
        else:
            record = load(directory / "state.json")
            if mode == "renew":
                result = renew(directory, record, value)
            elif mode == "fence":
                result = fence(directory, record)
            elif mode == "seal":
                result = sealed(directory, record)
            elif mode == "ack":
                require(
                    record["state"] in {"sealed", "acked"}
                    and value == {"sha256": record["sealed"]["sha256"]}
                )
                (directory / "active.pcap").unlink(missing_ok=True)
                (directory / "sealed.pcap").unlink(missing_ok=True)
                record.update(state="acked")
                save(directory / "state.json", record)
                result = read(directory, record)
            else:
                require(mode == "read")
                result = read(directory, record)
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    os.umask(0o077)
    main()
