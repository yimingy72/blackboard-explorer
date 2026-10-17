"""Local synthetic traffic only; build bbx-traffic-envd:latest before running."""

import asyncio
import hashlib
import json
import os
import shlex
import uuid
from contextlib import ExitStack

import httpx
import pytest
from bbx_contracts.traffic import TrafficNativeSourceIndex, TrafficRecordDetail
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

pytestmark = pytest.mark.integration
TOKEN = "synthetic-traffic-token"

SERVER = r"""
import http.server, ssl, threading, time
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(b"first-synthetic-chunk\n") +
                                               (100000 if self.path.startswith("/stream") else 0)))
        self.send_header("Set-Cookie", "synthetic-cookie=private")
        self.end_headers()
        self.wfile.write(b"first-synthetic-chunk\n")
        self.wfile.flush()
        if self.path.startswith("/stream"):
            time.sleep(11)
            self.wfile.write(b"x" * 100000)
    def log_message(self, *args): pass
for port in (24441, 24442, 24443):
    server=http.server.ThreadingHTTPServer(("0.0.0.0",port),Handler)
    if port in (24442,24443):
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cert=("/usr/local/share/ca-certificates/synthetic-target.crt"
              if port==24442 else "/tmp/untrusted.crt")
        context.load_cert_chain(cert,"/tmp/synthetic-key.pem")
        server.socket=context.wrap_socket(server.socket,server_side=True)
    threading.Thread(target=server.serve_forever,daemon=True).start()
while True: time.sleep(60)
"""

RAW = r"""
import fcntl, os, socket, struct, subprocess
for family,host in ((socket.AF_INET,"127.0.0.1"),(socket.AF_INET6,"::1")):
    listener=socket.socket(family,socket.SOCK_STREAM)
    listener.bind((host,0)); listener.listen()
    client=socket.socket(family,socket.SOCK_STREAM)
    client.connect(listener.getsockname()); peer,_=listener.accept()
    client.sendall(b"synthetic-tcp"); assert peer.recv(64)==b"synthetic-tcp"
    client.close(); peer.close(); listener.close()
    receiver=socket.socket(family,socket.SOCK_DGRAM)
    receiver.bind((host,0)); sender=socket.socket(family,socket.SOCK_DGRAM)
    sender.sendto(b"synthetic-udp",receiver.getsockname())
    assert receiver.recv(64)==b"synthetic-udp"
    sender.close(); receiver.close()
packet=bytearray(struct.pack("!BBHHH",8,0,0,4242,1)+b"synthetic-icmp")
if len(packet)%2:packet.append(0)
total=sum(struct.unpack("!"+"H"*(len(packet)//2),packet))
total=(total>>16)+(total&65535);total+=(total>>16)
struct.pack_into("!H",packet,2,(~total)&65535)
raw=socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP)
raw.sendto(packet,("127.0.0.1",0));raw.close()
raw=socket.socket(socket.AF_INET6,socket.SOCK_RAW,socket.IPPROTO_ICMPV6)
raw.sendto(struct.pack("!BBHHH",128,0,0,4242,1)+b"synthetic-icmp6",("::1",0));raw.close()
tun=os.open("/dev/net/tun",os.O_RDWR|os.O_NONBLOCK)
fcntl.ioctl(tun,0x400454ca,struct.pack("16sH",b"bbx-test",0x1001))
subprocess.run(["ip","addr","add","198.19.0.1/30","dev","bbx-test"],check=True)
subprocess.run(["ip","link","set","bbx-test","up"],check=True)
sender=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
sender.sendto(b"synthetic-tun",("198.19.0.2",43210))
assert os.read(tun,4096);sender.close();os.close(tun)
"""


async def command(url: str, agent: str, text: str, privileged: bool = False):
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {TOKEN}", "X-Agent-Id": agent},
        timeout=30,
        trust_env=False,
    ) as client:
        async with streamable_http_client(f"{url}/mcp", http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool(
                    "execute_command", {"command": text, "privileged": privileged}
                )
                assert not result.isError, result
                assert result.structuredContent is not None
                return result.structuredContent


async def test_local_https_stream_sudo_agents_and_all_raw_protocols():
    os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"
    task_id = str(uuid.uuid4())
    container = (
        DockerContainer(os.environ.get("BBX_TRAFFIC_TEST_IMAGE", "bbx-traffic-envd:latest"))
        .with_name("bbx-traffic-test-" + uuid.uuid4().hex[:8])
        .with_env("ENVD_TOKEN", TOKEN)
        .with_env("ENVD_TASK_ID", task_id)
        .with_env("ENVD_TRAFFIC_ENABLED", "true")
        .with_env("HTTP_PROXY", "")
        .with_env("HTTPS_PROXY", "")
        .with_env("http_proxy", "")
        .with_env("https_proxy", "")
        .with_env("NO_PROXY", "")
        .with_env("no_proxy", "")
        .with_exposed_ports(8080)
        .with_kwargs(
            cap_add=["NET_ADMIN", "SYS_ADMIN"],
            sysctls={"net.ipv4.ip_forward": "1", "net.ipv6.conf.all.forwarding": "1"},
            devices=["/dev/net/tun:/dev/net/tun:rwm"],
            mem_limit="2g",
            pids_limit=256,
        )
    )
    with ExitStack() as resources:
        network = Network()
        network.name = "bbx-traffic-network-" + uuid.uuid4().hex[:8]
        resources.enter_context(network)
        resources.enter_context(
            DockerContainer("bbx-traffic-envd:latest")
            .with_name("bbx-traffic-target-" + uuid.uuid4().hex[:8])
            .with_network(network)
            .with_network_aliases("synthetic-target")
            .with_command(["python3", "-m", "http.server", "8080", "--bind", "0.0.0.0"])
        )
        resources.enter_context(container.with_network(network))
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        async with httpx.AsyncClient(
            base_url=url, headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False, timeout=30
        ) as client:
            for _ in range(100):
                try:
                    if (await client.get("/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail("envd startup failed")
            code, output = container.exec(
                [
                    "bash",
                    "-c",
                    "openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost "
                    "-addext subjectAltName=IP:127.0.0.1,IP:198.18.0.1,DNS:localhost "
                    "-keyout /tmp/synthetic-key.pem "
                    "-out /usr/local/share/ca-certificates/synthetic-target.crt "
                    ">/dev/null 2>&1 && update-ca-certificates >/dev/null 2>&1",
                ]
            )
            assert code == 0, output
            assert (
                container.exec(
                    [
                        "bash",
                        "-c",
                        "openssl req -x509 -new -key /tmp/synthetic-key.pem -days 1 "
                        "-subj /CN=untrusted -addext subjectAltName=IP:127.0.0.1 "
                        "-out /tmp/untrusted.crt >/dev/null 2>&1",
                    ]
                )[0]
                == 0
            )
            launcher = (
                "import subprocess; subprocess.Popen(['python3','-c',"
                + repr(SERVER)
                + "] ,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)"
            )
            assert container.exec(["python3", "-c", launcher])[0] == 0
            await asyncio.sleep(0.3)
            for agent in ("agent-1", "agent-2"):
                assert (await client.post("/users", json={"agent_id": agent})).status_code == 200
            started = (
                await client.post("/traffic/start", json={"task_id": task_id, "run_number": 1})
            ).json()
            assert started["https"] == "enabled", started
            results = await asyncio.gather(
                command(
                    url,
                    "agent-1",
                    "curl --noproxy '' -fsS http://198.18.0.1:24441/agent1?token=synthetic-secret",
                ),
                command(
                    url, "agent-2", "sudo -n curl --noproxy '' -fsS https://198.18.0.1:24442/agent2"
                ),
            )
            assert all(item["exit_code"] == 0 for item in results), results
            direct = await command(
                url, "agent-1", "curl --noproxy '*' -fsS http://synthetic-target:8080/ >/dev/null"
            )
            assert direct["exit_code"] == 0, direct
            stream = await command(
                url,
                "agent-1",
                "curl --noproxy '' --no-buffer -fsS https://198.18.0.1:24442/stream "
                "> stream.out & client_pid=$!; sleep 0.8; test -s stream.out; "
                "marker=$?; wait $client_pid; exit $marker",
            )
            assert stream["exit_code"] == 0, stream
            raw_results = await asyncio.gather(
                *[
                    command(url, agent, "python3 -c " + shlex.quote(RAW), privileged=True)
                    for agent in ("agent-1", "agent-2")
                ]
            )
            assert all(item["exit_code"] == 0 for item in raw_results), raw_results
            assert (
                raw_results[0]["execution"]["network"]["namespace"]
                != raw_results[1]["execution"]["network"]["namespace"]
            )
            bounds = await command(
                url,
                "agent-1",
                "sudo -n python3 -c "
                + shlex.quote(
                    "from pathlib import Path; "
                    "lines=Path('/proc/self/status').read_text().splitlines(); "
                    "value=int(next(x.split()[1] for x in lines "
                    "if x.startswith('CapBnd:')),16); "
                    "assert not value&(1<<21); assert value&(1<<12); "
                    "assert value&(1<<13); print('ok')"
                ),
            )
            assert bounds["exit_code"] == 0, bounds
            shared = await command(url, "agent-1", "echo shared-proof > /workspace/shared/proof")
            assert shared["exit_code"] == 0
            shared = await command(url, "agent-2", "cat /workspace/shared/proof")
            assert "shared-proof" in shared["stdout"]
            secret = await command(url, "agent-1", "printenv HTTP_PROXY")
            assert "[redacted]" in secret["stdout"]
            await asyncio.sleep(2.5)
            stopped = (await client.post("/traffic/stop", json={})).json()
            assert stopped["status"] == "complete", stopped
            assert stopped["dropped_packets"] == 0
            stopped_files = (await client.get("/traffic/artifacts?limit=1024")).json()["items"]
            native = (await client.get("/traffic/native")).json()
            assert native["error"] is None, native
            sources = [TrafficNativeSourceIndex.model_validate(item) for item in native["sources"]]
            assert {item.agent_id for item in sources} == {None, "agent-1", "agent-2"}
            assert all(item.complete and item.packet_count for item in sources)
            keylog_ids = {item.id for source in sources for item in source.keylog_parts}
            assert keylog_ids
            native_bytes = {}
            for item in native["pending"]:
                response = await client.get(f"/traffic/native/parts/{item['id']}")
                assert response.status_code == 200
                assert hashlib.sha256(response.content).hexdigest() == item["descriptor"]["sha256"]
                native_bytes[item["id"]] = response.content
            # Every byte, including all section/interface options and trailing blocks,
            # is identical to the existing official full-slice download.
            for source in sources:
                expected = b"".join(native_bytes[part.id] for part in source.parts)
                originals = [
                        item for item in stopped_files if item["source_id"] == source.source_id
                ]
                actual = b"".join(
                    [
                        (await client.get(f"/traffic/artifacts/{item['id']}")).content
                        for item in originals
                    ]
                )
                assert actual == expected
            assert any(len({part.segment_id for part in source.parts}) >= 2 for source in sources)
            # Official native analysis consumes concatenated, unchanged source bytes
            # plus private TLS secrets across the 10-second capture rotation.
            check_native = r"""
import json, pathlib, subprocess
root=pathlib.Path('/var/lib/bbx/traffic')
index=json.loads((root/'current.json').read_text())
directory=root/index['state']['source_id']
native=json.loads((directory/'native-index.json').read_text())
keys=b''.join((directory/(p['id']+'.bin')).read_bytes() for p in native['keylog_parts'])
keypath=pathlib.Path('/tmp/native-test.keys');keypath.write_bytes(keys);keypath.chmod(0o600)
seen=False
for source in native['sources'].values():
    if source['agent_id']!='agent-1':continue
    path=pathlib.Path('/tmp/native-test.pcapng')
    path.write_bytes(b''.join((directory/(p['id']+'.bin')).read_bytes() for p in source['parts']))
    result=subprocess.run(['tshark','-n','-r',str(path),'-o','tls.keylog_file:'+str(keypath),
                           '-Y','http.response.code == 200','-T','fields',
                           '-e','http.response.code'],
                          capture_output=True,check=True)
    assert b'200' in result.stdout
    result=subprocess.run(['tshark','-n','-r',str(path),'-o','tls.keylog_file:'+str(keypath),
                           '-Y','http.file_data contains "first-synthetic-chunk"',
                           '-T','fields','-e','frame.number'],capture_output=True,check=True)
    assert result.stdout.strip(),result.stderr
    seen=True
assert seen
assert (directory/'tls.keys').stat().st_mode&0o777==0o600
"""
            code, output = await asyncio.to_thread(container.exec, ["python3", "-c", check_native])
            assert code == 0, output
            assert not keylog_ids.intersection(item["id"] for item in stopped_files)
            assert (
                await client.post("/traffic/native/ack", json={"parts": list(native_bytes)})
            ).status_code == 200
            acknowledged = (await client.get("/traffic/native")).json()
            assert not acknowledged["pending"]
            assert acknowledged["sources"] == native["sources"]
            chunks = (await client.get("/traffic/chunks")).json()["items"]
            details = []
            for chunk in chunks:
                response = await client.get(f"/traffic/chunks/{chunk['id']}")
                produced = [json.loads(line) for line in response.text.splitlines()]
                full = [
                    TrafficRecordDetail.model_validate(item).model_dump(mode="json")
                    for item in produced
                ]
                assert (
                    len(json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode())
                    <= 1024 * 1024
                )
                details.extend(produced)
            decoded = [
                item
                for item in details
                if item.get("record_kind") == "http" and item["plaintext"] == "decoded"
            ]
            assert any(
                item.get("record_kind") == "packet" and item["plaintext"] == "decoded"
                for item in details
            )
            assert {item["agent_id"] for item in decoded} == {"agent-1", "agent-2"}
            assert any(item["protocol"] == "https" for item in decoded)
            assert any(item["response"]["body_truncated"] for item in decoded)
            assert "synthetic-secret" not in json.dumps(decoded)
            assert "synthetic-cookie=private" not in json.dumps(decoded)
            protocols = {item["protocol"] for item in details}
            assert {"tcp", "udp", "icmp"} <= protocols, protocols
            assert any(item["source_ip"] == "::1" for item in details)
            assert any(item["destination_ip"] == "198.19.0.2" for item in details)
            for agent in ("agent-1", "agent-2"):
                owned = [item for item in details if item["agent_id"] == agent]
                assert {"tcp", "udp", "icmp"} <= {item["protocol"] for item in owned}
                assert all(item["attribution"] == "exact" for item in owned)
                assert any(item["source_ip"] == "::1" for item in owned)
                assert any(item["destination_ip"] == "198.19.0.2" for item in owned)
            assert all(
                item["attribution"] == "shared" for item in details if item["agent_id"] is None
            )
            assert any(
                item["attribution"] == "shared" and item["destination_port"] == 8080
                for item in details
            )
            packets = [item for item in details if item.get("record_kind") == "packet"]
            assert packets and all(item["packet_detail"]["raw_hex"] for item in packets)
            for source in {item["packet"]["source_id"] for item in packets}:
                numbers = [
                    item["packet"]["number"]
                    for item in packets
                    if item["packet"]["source_id"] == source
                ]
                assert numbers == sorted(set(numbers))
            files = (await client.get("/traffic/artifacts")).json()["items"]
            assert files and all(item["sealed"] for item in files)
            assert {item["agent_id"] for item in files} == {None, "agent-1", "agent-2"}
            for item in files:
                response = await client.get(f"/traffic/artifacts/{item['id']}")
                assert len(response.content) == item["size_bytes"]
                assert response.content[:4] == b"\x0a\x0d\x0d\x0a"
                assert TOKEN.encode() not in response.content
            assert (
                await client.post(
                    "/traffic/ack",
                    json={
                        "chunks": [item["id"] for item in chunks],
                        "artifacts": [item["id"] for item in files],
                    },
                )
            ).status_code == 200
            assert (await client.get("/traffic/artifacts")).json()["items"] == []
            assert (await client.get("/traffic/chunks")).json()["items"] == []
            assert (await client.post("/traffic/stop", json={})).json()["source_id"] == started[
                "source_id"
            ]
            second = (
                await client.post("/traffic/start", json={"task_id": task_id, "run_number": 2})
            ).json()
            assert second["source_id"] != started["source_id"]
            failed = await command(
                url, "agent-1", "curl --noproxy '' -fsS https://198.18.0.1:24443/untrusted"
            )
            assert failed["exit_code"] != 0
            final = (await client.post("/traffic/stop", json={})).json()
            assert final["status"] == "complete", final
            assert "TLS handshake failed" not in (final.get("error") or "")
            assert any(
                item["plaintext"] == "encrypted"
                for item in (await client.get("/traffic/records")).json()["items"]
            )
            remaining_chunks = (await client.get("/traffic/chunks")).json()["items"]
            remaining_files = (await client.get("/traffic/artifacts")).json()["items"]
            await client.post(
                "/traffic/ack",
                json={
                    "chunks": [item["id"] for item in remaining_chunks],
                    "artifacts": [item["id"] for item in remaining_files],
                },
            )
            remaining_native = (await client.get("/traffic/native")).json()
            await client.post(
                "/traffic/native/ack",
                json={"parts": [item["id"] for item in remaining_native["pending"]]},
            )
            third = (
                await client.post("/traffic/start", json={"task_id": task_id, "run_number": 3})
            ).json()
            assert third["https"] == "enabled"
            before_limit = await command(
                url, "agent-1", "curl --noproxy '' -fsS https://198.18.0.1:24442/before-limit"
            )
            assert before_limit["exit_code"] == 0, before_limit
            limited = (await client.post("/traffic/limit", json={})).json()
            limited_native = (await client.get("/traffic/native")).json()
            assert limited["status"] == "partial" and not limited["capturing"]
            assert limited["proxy_serving"] and "capacity" in limited["error"]
            await client.post("/users", json={"agent_id": "agent-3"})
            for agent, scheme, port in (("agent-2", "http", 24441), ("agent-3", "https", 24442)):
                forwarded = await command(
                    url, agent, f"curl --noproxy '' -fsS {scheme}://198.18.0.1:{port}/after-limit"
                )
                assert forwarded["exit_code"] == 0, forwarded
            sealed = (await client.post("/traffic/seal", json={})).json()
            sealed_items = (await client.get("/traffic/status?include_items=true")).json()
            limited_items = (await client.get("/traffic/status?include_items=true")).json()
            assert sealed["record_count"] == limited["record_count"]
            assert sealed_items["files"] == limited_items["files"]
            assert sealed_items["chunks"] == limited_items["chunks"]
            assert (await client.get("/traffic/native")).json() == limited_native
            final_stop = (await client.post("/traffic/stop", json={})).json()
            assert not final_stop["proxy_serving"]
