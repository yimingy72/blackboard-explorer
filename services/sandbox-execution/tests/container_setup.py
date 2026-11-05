"""Trusted fixture wiring only: no Kubernetes, VPN, live targets, or shared volumes."""

import http.server
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[1:] == ["target-server"]:

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://198.19.1.1:44772/command")
                self.end_headers()
                return
            body = json.dumps({"ok": True, "source_ip": self.client_address[0]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    http.server.ThreadingHTTPServer(("198.19.4.2", 8080), Target).serve_forever()
    sys.exit(0)


def run(*args: str) -> None:
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL, capture_output=True)


if not os.environ.get("BBX_TEST_TOKEN", "").strip():
    raise ValueError("A nonempty execd access token is required")

root = Path("/var/lib/bbx/control")
(root / "proxy").mkdir(mode=0o700)
(root / "proxy" / "grants.json").write_text("{}")
(root / "proxy" / "grants.json").chmod(0o600)
(root / "protected").write_text("bbx-archv2-control-only\n")
(root / "protected").chmod(0o600)
Path("/workspace/.tmp").chmod(0o1777)
with Path("/etc/hosts").open("a") as hosts:
    hosts.write("\n198.19.4.2 bbx-archv2-fixture-target\n")

for index, namespace in enumerate(("worker-a", "worker-b", "helper-a", "target"), start=1):
    run("ip", "netns", "add", namespace)
    run("ip", "link", "add", f"host{index}", "type", "veth", "peer", "name", f"peer{index}")
    run("ip", "link", "set", f"peer{index}", "netns", namespace)
    run("ip", "addr", "add", f"198.19.{index}.1/30", "dev", f"host{index}")
    run("ip", "link", "set", f"host{index}", "up")
    run("ip", "-n", namespace, "addr", "add", f"198.19.{index}.2/30", "dev", f"peer{index}")
    run("ip", "-n", namespace, "link", "set", f"peer{index}", "up")
    run("ip", "-n", namespace, "link", "set", "lo", "up")
    run("ip", "-n", namespace, "route", "add", "default", "via", f"198.19.{index}.1")
    run(
        "ip",
        "-n",
        namespace,
        "-6",
        "addr",
        "add",
        f"fd42:bb:{index}::2/64",
        "dev",
        f"peer{index}",
        "nodad",
    )
    run("ip", "-6", "addr", "add", f"fd42:bb:{index}::1/64", "dev", f"host{index}", "nodad")
    run("ip", "-n", namespace, "-6", "route", "add", "default", "via", f"fd42:bb:{index}::1")
    if index <= 3:
        subprocess.Popen(
            [
                "tcpdump",
                "--immediate-mode",
                "-U",
                "-i",
                f"host{index}",
                "-w",
                str(root / f"capture-{index}.pcap"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

for index in (1, 2, 3):
    interface = f"host{index}"
    # Pod-local INPUT is independent of CNI: solver veths see only this proxy.
    for firewall in ("iptables", "ip6tables"):
        run(firewall, "-I", "INPUT", "1", "-i", interface, "-j", "DROP")
        run(
            firewall,
            "-I",
            "INPUT",
            "1",
            "-i",
            interface,
            "-p",
            "tcp",
            "--dport",
            "18080",
            "-j",
            "ACCEPT",
        )
        run(firewall, "-I", "FORWARD", "1", "-i", interface, "-j", "DROP")
        run(firewall, "-I", "FORWARD", "1", "-i", interface, "-o", "host4", "-j", "ACCEPT")
        run(
            firewall,
            "-I",
            "FORWARD",
            "1",
            "-i",
            "host4",
            "-o",
            interface,
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        )
    for chain in ("INPUT", "FORWARD"):
        run(
            "iptables",
            "-I",
            chain,
            "1",
            "-i",
            interface,
            "!",
            "-s",
            f"198.19.{index}.2/32",
            "-j",
            "DROP",
        )

subprocess.Popen(
    ["ip", "netns", "exec", "target", sys.executable, "-I", __file__, "target-server"],
    stdin=subprocess.DEVNULL,
)
environment = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/root",
    "PYTHONPATH": "/opt/bbx/control/python",
    "BBX_PROXY_CONTROL": str(root / "proxy"),
}
subprocess.Popen(
    [
        "/opt/traffic/venv/bin/mitmdump",
        "--quiet",
        "--listen-host",
        "0.0.0.0",
        "--listen-port",
        "18080",
        "--set",
        "proxyauth=any",
        "--set",
        "connection_strategy=lazy",
        "--set",
        "upstream_cert=false",
        "--scripts",
        "/opt/bbx/control/python/bbx_sandbox_execution/proxy_addon.py",
    ],
    env=environment,
    stdin=subprocess.DEVNULL,
)

for host, port in (("127.0.0.1", 18080), ("198.19.4.2", 8080)):
    for _ in range(100):
        try:
            with socket.create_connection((host, port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError(f"Fixture service {host}:{port} never became ready")

environment = dict(os.environ)
environment.update(
    EXECD_ACCESS_TOKEN=os.environ["BBX_TEST_TOKEN"],
    BBX_CONTROL_MARKER="bbx-archv2-control-must-not-leak",
    EXECD_ENVS=str(root / "execd.env"),
    LD_PRELOAD="/opt/bbx/control/poison.so",
    BASH_ENV="/opt/bbx/test/poison.sh",
    ENV="/opt/bbx/test/poison.sh",
    PYTHONPATH="/var/lib/bbx/control",
    PYTHONSTARTUP="/opt/bbx/test/poison.sh",
)
(root / "execd.env").write_text(
    "BBX_FILE_MARKER=bbx-archv2-envfile-must-not-leak\nBBX_CONTROL_MARKER=bbx-archv2-control-must-not-leak\n"
)
(root / "execd.env").chmod(0o600)
os.execve("/opt/bbx/control/launcher", ["launcher", "--execd"], environment)
