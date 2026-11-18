import base64
import hashlib
import json
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path("/var/lib/bbx/control/networks")
source = "probe-source"
helper = ["/usr/bin/python3", "-I", "/opt/bbx/control/assignment-network.py"]


def root(mode, data=None):
    args = helper + [mode, source]
    if data is not None:
        args += [base64.urlsafe_b64encode(json.dumps(data).encode()).decode()]
    return json.loads(subprocess.check_output(args))


def ip(*args):
    subprocess.run(["ip", *args], check=True)


subprocess.run(helper + ["init"], check=True)
ip("netns", "add", "target")
ip("link", "add", "target0", "type", "veth", "peer", "name", "target1")
ip("link", "set", "target1", "netns", "target")
ip("addr", "add", "198.18.0.1/30", "dev", "target0")
ip("link", "set", "target0", "up")
ip("-n", "target", "addr", "add", "198.18.0.2/30", "dev", "target1")
ip("-n", "target", "link", "set", "target1", "up")
server_code = """
import socket
with socket.socket() as s:
    s.bind(('198.18.0.2', 18888))
    s.listen()
    while True:
        c,a = s.accept()
        with c:
            c.sendall(c.recv(8))
"""
server = subprocess.Popen(["ip", "netns", "exec", "target", "python3", "-c", server_code])
try:
    assert (
        root(
            "prepare",
            {
                "source_id": source,
                "ordinal": 1,
                "namespace": "bbx001",
                "network_identity": "n1",
                "ipv4": "198.19.0.6",
                "scope_id": "f" * 64,
                "targets": [{"ipv4": "198.18.0.2", "port": 18888}],
            },
        )["state"]
        == "claimed"
    )
    for _ in range(30):
        initial = root("read")
        if initial.get("capture_ready"):
            break
        time.sleep(0.05)
    assert initial["capture_ready"]
    peer = """
import socket
with socket.create_connection(('198.18.0.2', 18888), timeout=.4) as s:
    s.sendall(b'original')
    assert s.recv(8) == b'original'
"""
    send = ["ip", "netns", "exec", "bbx001", "python3", "-c", peer]
    assert subprocess.run(send, stderr=subprocess.DEVNULL).returncode != 0
    now = time.time_ns() // 1_000_000
    grant = {
        "source_id": source,
        "grant_revision": 1,
        "issued_at_ms": now,
        "authority_until_ms": now + 12000,
        "boot_ms": root("read")["boot_ms"],
    }
    active = root("renew", grant)
    assert active["gate_live"] and active["grant_revision"] == 1
    subprocess.run(send, check=True)
    gate_path = ROOT / "bbx001.gate"
    gate = gate_path.read_bytes()
    assert root("renew", grant)["grant_revision"] == 1 and gate_path.read_bytes() == gate
    assert root("fence")["state"] == "fenced" and not gate_path.exists()
    assert subprocess.run(send, stderr=subprocess.DEVNULL).returncode != 0
    final = root("seal")
    print(json.dumps(final, sort_keys=True), flush=True)
    assert final["state"] == "sealed" and final["packet_count"] >= 5
    assert final["complete"] and final["dropped_packets"] == 0 and final["truncated_packets"] == 0
    capture = ROOT / source / "sealed.pcap"
    data = capture.read_bytes()
    assert hashlib.sha256(data).hexdigest() == final["sha256"]
    assert socket.inet_aton("198.19.0.6") in data
    assert root("ack", {"sha256": final["sha256"]})["state"] == "acked" and not capture.exists()
    assert (
        subprocess.run(
            helper
            + [
                "renew",
                source,
                base64.urlsafe_b64encode(
                    json.dumps(grant | {"grant_revision": 2}).encode()
                ).decode(),
            ],
            stderr=subprocess.DEVNULL,
        ).returncode
        != 0
    )
    print(
        "root verified capture, permitted traffic, exact duplicate, fence, seal, "
        "archive acknowledgement, and no old reopening"
    )
finally:
    server.terminate()
    server.wait()
