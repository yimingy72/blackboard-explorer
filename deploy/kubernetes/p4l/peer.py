"""Install one locally generated OpenVPN peer and isolated numeric TCP echo target."""

import base64
import hashlib
import json
import subprocess
from pathlib import Path

from build_images import VPN
from common import NODE, STATE, kubectl, owned_cluster

NAMESPACE = "bbx-p4l-vpn-peer"
DEVICE_NAMESPACE = "bbx-p4l-tun-system"
SERVER = "p4l-openvpn-peer"
CLIENT = "p4l-client"
TARGET = "10.70.0.1"
CLIENT_TUN = "10.70.0.2"
PORT = 18888
UDP = 1194

SERVER_CODE = """
import signal,socketserver,subprocess,threading,time
class Echo(socketserver.BaseRequestHandler):
 def handle(self):
  while data := self.request.recv(256): self.request.sendall(data)
subprocess.run(["/usr/sbin/ip","address","add","10.70.0.1/32","dev","lo"],check=True)
servers=[socketserver.ThreadingTCPServer(("0.0.0.0",p),Echo) for p in (18888,18889)]
for server in servers: threading.Thread(target=server.serve_forever,daemon=True).start()
vpn=subprocess.Popen(["/usr/sbin/openvpn","--dev","p4lpeer","--dev-type","tun",
 "--proto","udp4","--port","1194","--topology","subnet","--server","10.70.0.0","255.255.255.0",
 "--client-config-dir","/creds","--ca","/creds/ca.crt","--cert","/creds/server.crt",
 "--key","/creds/server.key","--dh","none","--disable-dco","--script-security","0",
 "--data-ciphers","AES-256-GCM","--keepalive","1","10",
 "--push","redirect-gateway def1","--push","dhcp-option DNS 10.70.0.53","--verb","3"])
signal.signal(signal.SIGTERM,lambda *_: vpn.terminate())
exit_code=vpn.wait()
time.sleep(25)
raise SystemExit(exit_code)
"""


def create(document):
    kubectl("create", "-f", "-", data=document)


def openssl(dir_path, *args):
    subprocess.run(["openssl", *args], cwd=dir_path, capture_output=True, check=True)


def credentials(path: Path):
    path.mkdir(mode=0o700)
    openssl(
        path,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-subj",
        "/CN=p4l-fixture-ca",
        "-keyout",
        "ca.key",
        "-out",
        "ca.crt",
    )
    for name, purpose in [(SERVER, "serverAuth"), (CLIENT, "clientAuth")]:
        openssl(
            path,
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=" + name,
            "-keyout",
            name + ".key",
            "-out",
            name + ".csr",
        )
        ext = path / (name + ".ext")
        ext.write_text(
            "extendedKeyUsage=" + purpose + "\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        )
        openssl(
            path,
            "x509",
            "-req",
            "-in",
            name + ".csr",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            name + ".crt",
            "-days",
            "2",
            "-extfile",
            ext.name,
        )
    return path


def install(inventory):
    owned_cluster()
    receipt = STATE / "peer-receipt.json"
    if receipt.exists():
        raise RuntimeError("The original peer already exists; use its exact readback")
    plugin = inventory["vpn_source"]["plugin"]
    for namespace in (DEVICE_NAMESPACE, NAMESPACE):
        create(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace, "labels": {"bbx.dev/owner": "p4l-fixture"}},
            }
        )
    kubectl("label", "node", NODE, "bbx.dev/vpn-tun=true")
    create(
        {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "bbx-p4l-tun", "namespace": DEVICE_NAMESPACE},
            "spec": {
                "selector": {"matchLabels": {"app": "bbx-p4l-tun"}},
                "template": {
                    "metadata": {"labels": {"app": "bbx-p4l-tun"}},
                    "spec": {
                        "nodeSelector": {"bbx.dev/vpn-tun": "true"},
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
                                    "privileged": False,
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
        }
    )
    kubectl("-n", DEVICE_NAMESPACE, "rollout", "status", "daemonset/bbx-p4l-tun", "--timeout=90s")
    path = credentials(STATE / "vpn-private-ca")
    server_data = {
        "ca.crt": (path / "ca.crt").read_bytes(),
        "server.crt": (path / (SERVER + ".crt")).read_bytes(),
        "server.key": (path / (SERVER + ".key")).read_bytes(),
        CLIENT: b"ifconfig-push 10.70.0.2 255.255.255.0\n",
    }
    client_data = {
        "ca.crt": server_data["ca.crt"],
        "tls.crt": (path / (CLIENT + ".crt")).read_bytes(),
        "tls.key": (path / (CLIENT + ".key")).read_bytes(),
    }
    for name, contents in [(SERVER, server_data), (CLIENT, client_data)]:
        create(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "immutable": True,
                "metadata": {"name": name, "namespace": NAMESPACE},
                "data": {k: base64.b64encode(v).decode() for k, v in contents.items()},
            }
        )
    image = inventory["images"][VPN]["reference"]
    create(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": SERVER,
                "namespace": NAMESPACE,
                "labels": {"bbx.dev/vpn-peer": "allowed", "bbx.dev/owner": "p4l-fixture"},
            },
            "spec": {
                "automountServiceAccountToken": False,
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "peer",
                        "image": image,
                        "imagePullPolicy": "Never",
                        "command": ["/usr/bin/python3", "-I", "-S", "-c", SERVER_CODE],
                        "securityContext": {
                            "runAsUser": 0,
                            "privileged": False,
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"], "add": ["NET_ADMIN"]},
                        },
                        "resources": {
                            "requests": {"devic.es/tun": "1"},
                            "limits": {"devic.es/tun": "1"},
                        },
                        "readinessProbe": {
                            "exec": {
                                "command": [
                                    "/usr/bin/python3",
                                    "-c",
                                    "import socket; "
                                    "assert any(n=='p4lpeer' for _,n in socket.if_nameindex()); "
                                    "socket.create_connection(('10.70.0.1',18888),1).close()",
                                ]
                            }
                        },
                        "volumeMounts": [
                            {"name": "creds", "mountPath": "/creds", "readOnly": True}
                        ],
                    }
                ],
                "volumes": [
                    {"name": "creds", "secret": {"secretName": SERVER, "defaultMode": 256}}
                ],
            },
        }
    )
    kubectl("-n", NAMESPACE, "wait", "--for=condition=Ready", f"pod/{SERVER}", "--timeout=90s")
    pod = json.loads(kubectl("-n", NAMESPACE, "get", "pod", SERVER, "-o", "json"))
    uid = kubectl("-n", NAMESPACE, "get", "secret", CLIENT, "-o", "jsonpath={.metadata.uid}")
    result = {
        "namespace": NAMESPACE,
        "name": SERVER,
        "pod_uid": pod["metadata"]["uid"],
        "ipv4": TARGET,
        "peer_ipv4": pod["status"]["podIP"],
        "credential": {
            "namespace": NAMESPACE,
            "name": CLIENT,
            "uid": uid,
            "cert_sha256": hashlib.sha256(client_data["tls.crt"]).hexdigest(),
        },
    }
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    return result
