"""Synthetic external OpenVPN peer and a direct control target on the private kind bridge."""

import ipaddress
import json
import runpy
import subprocess
import time

from build_images import VPN, VPN_BASE
from common import NETWORK, SOURCE, STATE, SYSTEM, kubectl, owned_cluster

PEER = "bbx-p4m-openvpn-peer"
PROBE = {"ipv4": "10.70.0.1", "port": 18888}
WORKER = {"ipv4": "10.71.0.5", "port": 18889}
DIRECT_PORT = 18888
VPN_PORT = 1194
DEVICE = "bbx-p4m-tun"
SOURCE_HOST = "vpn-source.p4m.test"

SERVER_CODE = r"""
import socketserver, subprocess, threading

class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(512):
            if data.startswith(b"GET /challenge "):
                body = b'{"container_addr":"10.71.0.5:18889"}'
                self.request.sendall(
                    b"HTTP/1.0 200 OK\r\nContent-Length: " + str(len(body)).encode()
                    + b"\r\nContent-Type: application/json\r\n\r\n" + body
                )
                return
            self.request.sendall(data)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

subprocess.run(["/usr/sbin/ip", "address", "add", "10.71.0.5/32", "dev", "lo"], check=True)
servers = [Server(("0.0.0.0", port), Echo) for port in (18888, 18889)]
for server in servers:
    threading.Thread(target=server.serve_forever, daemon=True).start()
args = [
    "/usr/sbin/openvpn", "--dev", "p4mpeer", "--dev-type", "tun", "--proto", "udp4",
    "--port", "1194", "--topology", "subnet", "--server", "10.70.0.0", "255.255.255.0",
    "--ca", "/creds/ca.crt", "--cert", "/creds/server.crt", "--key", "/creds/server.key",
    "--tls-crypt", "/creds/tls-crypt.key", "--dh", "none", "--disable-dco",
    "--script-security", "0", "--data-ciphers", "AES-256-GCM", "--keepalive", "1", "10",
    "--push", "redirect-gateway def1", "--push", "dhcp-option DNS 10.70.0.53", "--verb", "3"
]
subprocess.run(args, check=True)
"""


def prepare(inventory):
    owned_cluster()
    receipt = STATE / "peer-receipt.json"
    if receipt.exists():
        result = json.loads(receipt.read_text())
        exact_peer(result)
        return result
    plugin = inventory["vpn_source"]["plugin"]
    kubectl(
        "apply",
        "-f",
        "-",
        data={
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": SYSTEM, "labels": {"bbx.dev/owner": "p4m"}},
        },
    )
    kubectl(
        "apply",
        "-f",
        "-",
        data={
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": DEVICE, "namespace": SYSTEM},
            "spec": {
                "selector": {"matchLabels": {"app": DEVICE}},
                "template": {
                    "metadata": {"labels": {"app": DEVICE}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "device-plugin",
                                "image": plugin,
                                "imagePullPolicy": "Never",
                                "args": [
                                    "--device",
                                    json.dumps(
                                        {
                                            "name": "tun",
                                            "groups": [
                                                {"count": 2, "paths": [{"path": "/dev/net/tun"}]}
                                            ],
                                        }
                                    ),
                                ],
                                "securityContext": {
                                    "runAsUser": 0,
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {"name": "devices", "mountPath": "/dev/net", "readOnly": True},
                                    {
                                        "name": "registration",
                                        "mountPath": "/var/lib/kubelet/device-plugins",
                                    },
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "devices",
                                "hostPath": {"path": "/dev/net", "type": "Directory"},
                            },
                            {
                                "name": "registration",
                                "hostPath": {
                                    "path": "/var/lib/kubelet/device-plugins",
                                    "type": "Directory",
                                },
                            },
                        ],
                    },
                },
            },
        },
    )
    kubectl("-n", SYSTEM, "rollout", "status", "daemonset/" + DEVICE, "--timeout=90s")
    p4l = runpy.run_path(str(SOURCE / "deploy/kubernetes/p4l/peer.py"))
    path = p4l["credentials"](STATE / "private-peer")
    for suffix in ("crt", "key"):
        (path / (p4l["SERVER"] + "." + suffix)).replace(path / ("server." + suffix))
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{path}:/creds",
            "--entrypoint",
            "/usr/sbin/openvpn",
            VPN_BASE,
            "--genkey",
            "secret",
            "/creds/tls-crypt.key",
        ],
        check=True,
        capture_output=True,
    )
    cid = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "--name",
            PEER,
            "--label",
            "bbx.fixture=p4m",
            "--network",
            NETWORK,
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_ADMIN",
            "--device",
            "/dev/net/tun",
            "-v",
            f"{path}:/creds:ro",
            "--entrypoint",
            "/usr/bin/python3",
            VPN,
            "-I",
            "-S",
            "-c",
            SERVER_CODE,
        ],
        text=True,
    ).strip()
    container = json.loads(subprocess.check_output(["docker", "inspect", cid]))[0]
    ipv4 = container["NetworkSettings"]["Networks"][NETWORK]["IPAddress"]
    address = str(ipaddress.IPv4Address(ipv4))
    ovpn = (
        f"client\ndev tun\nproto udp\nremote {address} {VPN_PORT}\n"
        f"remote-cert-tls server\nverify-x509-name {p4l['SERVER']} name\n"
        "nobind\npersist-key\npersist-tun\nverb 3\n"
    )
    for name, source in (
        ("ca", "ca.crt"),
        ("cert", p4l["CLIENT"] + ".crt"),
        ("key", p4l["CLIENT"] + ".key"),
        ("tls-crypt", "tls-crypt.key"),
    ):
        ovpn += f"<{name}>\n" + (path / source).read_text() + f"</{name}>\n"
    (path / "client.ovpn").write_text(ovpn)
    (path / "client.ovpn").chmod(0o600)
    deadline = time.monotonic() + 25
    while subprocess.run(
        [
            "docker",
            "exec",
            cid,
            "/usr/sbin/ip",
            "link",
            "show",
            "p4mpeer",
        ],
        capture_output=True,
    ).returncode:
        if time.monotonic() >= deadline:
            raise RuntimeError("The original isolated OpenVPN peer is not ready")
        time.sleep(0.25)
    result = {
        "container_id": cid,
        "peer_ipv4": address,
        "ipv4": address,
        "namespace": SYSTEM + "-peer",
    }
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    return result


def exact_peer(peer):
    value = json.loads(subprocess.check_output(["docker", "inspect", PEER]))[0]
    if (
        value["Id"] != peer["container_id"]
        or value["Config"]["Labels"].get("bbx.fixture") != "p4m"
        or value["NetworkSettings"]["Networks"][NETWORK]["IPAddress"] != peer["peer_ipv4"]
    ):
        raise RuntimeError("The original isolated VPN peer changed")
