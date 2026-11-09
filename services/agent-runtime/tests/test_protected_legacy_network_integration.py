"""Ordinary user creation, legacy scheduler, real Docker commands and attributed raw archive."""

import asyncio
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import cast
from uuid import uuid4

import docker
import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.settings import Settings as BoardSettings
from bbx_objects import ObjectStore
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.execenv import ExecEnvManager
from bbx_runtime.runner import AgentRunner, RunResult
from bbx_runtime.scheduler.supervisor import TaskSupervisor
from bbx_runtime.settings import Settings
from docker.models.networks import NetworkCollection
from docker.types import IPAMConfig, IPAMPool
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
TARGET = "192.0.2.50"
PORT = 18888
SERVER = r"""
import os, select, socket, socketserver, struct, subprocess, sys, threading
mode=sys.argv[1]
class Echo(socketserver.BaseRequestHandler):
 def handle(self):
  while data:=self.request.recv(4096):
   if data.startswith(b'GET /health HTTP/1.1\r\n'):
    self.request.sendall(b'HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n')
    return
   self.request.sendall(data)
subprocess.run(['ip','addr','add','192.0.2.50/32','dev','lo'],check=True)
echo=socketserver.ThreadingTCPServer(('192.0.2.50',18888),Echo)
threading.Thread(target=echo.serve_forever,daemon=True).start()
if mode=='vpn':
 while not os.path.exists('/tmp/ca.crt'): threading.Event().wait(.1)
 subprocess.run(['/usr/sbin/openvpn','--dev','bbxpeer','--dev-type','tun','--proto','udp4',
 '--port','1194','--topology','subnet','--server','10.70.0.0','255.255.255.0',
 '--ca','/tmp/ca.crt','--cert','/tmp/server.crt','--key','/tmp/server.key','--dh','none',
 '--disable-dco','--keepalive','2','8','--data-ciphers','AES-256-GCM',
 '--push','redirect-gateway def1','--push','dhcp-option DNS 10.70.0.53'],check=True)
else:
 def exact(conn,n):
  data=b''
  while len(data)<n:
   block=conn.recv(n-len(data)); assert block; data+=block
  return data
 class Socks(socketserver.BaseRequestHandler):
  def handle(self):
   try:
    c=self.request;c.settimeout(3)
    assert exact(c,1)==b'\x05'; assert 2 in exact(c,exact(c,1)[0]); c.sendall(b'\x05\x02')
    assert exact(c,1)==b'\x01'; assert exact(c,exact(c,1)[0])==b'fixture-user'
    assert exact(c,exact(c,1)[0])==b'fixture-password'; c.sendall(b'\x01\x00')
    assert exact(c,4)==b'\x05\x01\x00\x01'; addr=socket.inet_ntoa(exact(c,4))
    port=struct.unpack('!H',exact(c,2))[0]; assert (addr,port)==('192.0.2.50',18888)
    with socket.create_connection((addr,port),1) as upstream:
     print('SOCKS5 CONNECT 192.0.2.50:18888 auth-ok',flush=True)
     c.sendall(b'\x05\x00\x00\x01'+bytes(6))
     while True:
      ready,_,_=select.select((c,upstream),(),(),3)
      if not ready: return
      for source in ready:
       data=source.recv(4096)
       if not data: return
       (upstream if source is c else c).sendall(data)
   except (AssertionError,OSError): pass
 socketserver.ThreadingTCPServer(('0.0.0.0',1080),Socks).serve_forever()
"""


def pinned(client, tag):
    image = client.images.get(tag)
    return tag.rsplit(":", 1)[0] + "@" + image.id


def vpn_credentials(path, peer):
    def openssl(*arguments):
        subprocess.run(["openssl", *arguments], cwd=path, capture_output=True, check=True)

    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=synthetic-p4m2",
        "-keyout",
        "ca.key",
        "-out",
        "ca.crt",
    )
    for name, purpose in (("server", "serverAuth"), ("client", "clientAuth")):
        openssl(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            f"/CN={name}",
            "-keyout",
            name + ".key",
            "-out",
            name + ".csr",
        )
        ext = path / (name + ".ext")
        ext.write_text(
            f"extendedKeyUsage={purpose}\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        )
        openssl(
            "x509",
            "-req",
            "-in",
            name + ".csr",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            ext.name,
            "-out",
            name + ".crt",
        )
    stream = io.BytesIO()
    with tarfile.open(mode="w", fileobj=stream) as out:
        for name in ("server.key", "server.crt", "ca.crt"):
            source = (path / name).read_bytes()
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(source), 0o600
            out.addfile(entry, io.BytesIO(source))
    assert peer.put_archive("/tmp", stream.getvalue())
    return {
        field: (path / name).read_text()
        for field, name in (
            ("ca", "ca.crt"),
            ("cert", "client.crt"),
            ("key", "client.key"),
        )
    }


async def wait_for(read, seconds=90):
    async with asyncio.timeout(seconds):
        while not (value := await read()):
            await asyncio.sleep(0.2)
        return value


class FixtureAgent:
    """Script the Agent in place of a model; envd HTTP and every command remain real."""

    def __init__(self, board, address, port, marker, blocked):
        self.board = board
        self.address = address
        self.port = port
        self.marker = marker
        self.blocked = blocked
        self.results: asyncio.Queue = asyncio.Queue()
        self.release = asyncio.Event()

    async def reconcile_final_receipt(self, *_):
        return False

    def marker_for(self, agent_id):
        return self.marker + b"/" + agent_id.encode()

    async def run_agent(self, tid, aid, kind, *_args, handle=None, **_kwargs):
        if kind != "explore":
            receipt = {"accepted": False, "reason": "synthetic task has no final solution"}
            await self.board.finish_agent(tid, aid, receipt, "normal")
            return RunResult(receipt, "normal")
        assert handle is not None
        async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
            headers = {"Authorization": "Bearer " + handle.token, "X-Agent-Id": aid}

            async def command(text, **kwargs):
                return await http.post(
                    handle.base_url + "/execute",
                    headers=headers,
                    json={
                        "command": text,
                        "timeout_sec": 6,
                        **kwargs,
                    },
                )

            mark = self.marker_for(aid)
            code = (
                "import os,socket\n"
                "assert os.getuid()!=0 and 'ENVD_TOKEN' not in os.environ\n"
                "assert not ({'HTTP_PROXY','HTTPS_PROXY',"
                "'ALL_PROXY','NO_PROXY'} & set(os.environ))\n"
                "info=dict(x.split(':',1) for x in open('/proc/self/status') if ':' in x)\n"
                "assert all(int(info[k],16)==0 for k in ('CapEff','CapBnd','CapAmb'))\n"
                "assert info['NoNewPrivs'].strip()=='1'\n"
                f"s=socket.create_connection(({self.address!r},{self.port}),2)\n"
                f"s.sendall({mark!r})\n"
                f"assert s.makefile('rb').read({len(mark)})=={mark!r}\n"
                "s.close()\n"
            )
            for address, port in self.blocked:
                code += (
                    "try:\n"
                    f"  socket.create_connection(({address!r},{port}),.3).close()\n"
                    "except OSError: pass\n"
                    "else: raise RuntimeError('direct control/underlay bypass')\n"
                )
            code += "print('protected-low-uid-roundtrip')"
            denied = await command("id -u", privileged=True)
            result = await command("python3 -c " + shlex.quote(code))
            await self.results.put((aid, handle, denied, result))
            await self.release.wait()
        receipt = {"accepted": False, "reason": "fixture complete"}
        await self.board.finish_agent(tid, aid, receipt, "normal")
        return RunResult(receipt, "normal")


@pytest.mark.parametrize("mode", ("default", "vpn", "socks5"))
async def test_protected_ordinary_docker_paths(mode, runtime_infrastructure, tmp_path, monkeypatch):
    images = {
        key: os.environ.get("BBX_P4M2_" + key.upper() + "_IMAGE") for key in ("envd", "gateway")
    }
    address = os.environ.get("BBX_P4M2_DIRECT_IPV4")
    if not all(images.values()) or not address:
        pytest.skip("Requires the explicitly built P4m2 images and an owned host test IPv4")
    client = docker.from_env()
    create_bridge = NetworkCollection.create
    occupied_prefixes = set()

    def deterministic_fixture_bridge(collection, name, **kwargs):
        # Do not let Docker pick a test bridge overlapping the real local test listener.
        if kwargs.get("labels", {}).get("bbx.managed") == "agent-runtime-legacy-network":
            for index in range(8, 240, 8):
                subnet = f"10.222.231.{index}/29"
                if subnet not in occupied_prefixes:
                    occupied_prefixes.add(subnet)
                    break
            else:
                raise RuntimeError("No private fixture test bridge available")
            kwargs["ipam"] = IPAMConfig(pool_configs=[IPAMPool(subnet=subnet)])
        return create_bridge(collection, name, **kwargs)

    monkeypatch.setattr(NetworkCollection, "create", deterministic_fixture_bridge)
    envd_image, gateway_image = (pinned(client, images[key]) for key in ("envd", "gateway"))
    own = "bbx-p4m2-e2e-" + uuid4().hex[:8]
    route_policy = None
    peer = uplink = None
    direct_server = None
    direct_clients = set()
    established = asyncio.Event()
    try:
        if mode == "default":

            async def echo(reader, writer):
                direct_clients.add(writer)
                try:
                    while data := await reader.read(4096):
                        if b"bbx-existing-flow" in data:
                            established.set()
                        writer.write(data)
                        await writer.drain()
                finally:
                    writer.close()
                    direct_clients.discard(writer)

            direct_server = await asyncio.start_server(echo, address, 0)
            target, port = address, direct_server.sockets[0].getsockname()[1]
            blocked = [("198.18.0.1", 8080)]
        else:
            uplink = client.networks.create(own, driver="bridge", labels={"bbx.fixture": own})
            peer = client.containers.run(
                gateway_image,
                ["-I", "-S", "-u", "-c", SERVER, mode],
                entrypoint="/usr/bin/python3",
                detach=True,
                network=uplink.name,
                name=own,
                labels={"bbx.fixture": own},
                cap_add=["NET_ADMIN"],
                devices=["/dev/net/tun:/dev/net/tun:rwm"] if mode == "vpn" else [],
            )
            peer.reload()
            remote = peer.attrs["NetworkSettings"]["Networks"][uplink.name]["IPAddress"]
            credentials = (
                vpn_credentials(tmp_path, peer)
                if mode == "vpn"
                else {
                    "login": "fixture-user",
                    "password": "fixture-password",
                }
            )
            private = tmp_path / "private.json"
            private.write_text(json.dumps(credentials))
            private.chmod(0o600)
            route_policy = tmp_path / "policy.json"
            route_policy.write_text(
                json.dumps(
                    {
                        "mode": mode,
                        "gateway": {
                            "image": gateway_image,
                            "uplink_network": uplink.name,
                            "upstream": {
                                "ipv4": remote,
                                "port": 1194 if mode == "vpn" else 1080,
                                "server_name": "server" if mode == "vpn" else None,
                            },
                            "allow_cidrs": ["192.0.2.0/24"],
                            "deny_cidrs": ["169.254.0.0/16"],
                            "probe": {
                                "ipv4": TARGET,
                                "port": PORT,
                                "path": "/health",
                                "status": 204,
                            },
                            "credential_file": str(private),
                            "credential_sha256": hashlib.sha256(private.read_bytes()).hexdigest(),
                        },
                    }
                )
            )
            target, port = TARGET, PORT
            blocked = [(remote, 1080), ("198.18.0.1", 8080)]
        shutil.copytree(ROOT / "profiles", tmp_path / "profiles")
        profiles = tmp_path / "profiles/default"
        profile = profiles / "profile.yaml"
        profile.write_text(profile.read_text().replace("bbx-exec-env:latest", envd_image))
        database, endpoint = runtime_infrastructure
        db = make_url(database)
        bucket = "bbx-p4m2-physical"
        board_settings = BoardSettings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            postgres_host=db.host,
            postgres_port=db.port,
            postgres_user=db.username,
            postgres_password=SecretStr(db.password or ""),
            postgres_db=db.database,
            minio_root_user="bbxm2buser",
            minio_root_password=SecretStr("bbxm2b-test-password"),
            minio_endpoint="http://" + endpoint,
            minio_bucket=bucket,
            service_token=SecretStr("fixture-service"),
            agent_token_secret=SecretStr("fixture-user-key"),
            admin_users=SecretStr("alice:test"),
            profiles_dir=profiles,
            legacy_network_policy_file=route_policy,
        )
        objects = ObjectStore(endpoint, "bbxm2buser", "bbxm2b-test-password", bucket)
        await objects.ensure_bucket()
        engine = create_async_engine(database)
        app = create_app(board_settings, engine=engine, objects=objects)
        marker = ("p4m2-" + mode + "-" + own).encode()
        async with AsyncExitStack() as stack:
            stack.push_async_callback(engine.dispose)
            await stack.enter_async_context(app.router.lifespan_context(app))
            board_http = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://board",
                    trust_env=False,
                )
            )
            service = BlackboardClient("http://board", "fixture-service", board_http)
            user = {"Authorization": "Bearer " + issue_user_token(board_settings, "alice")}
            # Use the normal user create/start routes. The Runtime alone sees its exact receipt.
            create = await board_http.post(
                "/api/tasks",
                headers=user,
                json={
                    "goal": "Reach the isolated synthetic marker",
                    "agent_profile": "default",
                    "acceptance": [{"id": "A1", "desc": "Marker"}],
                    "budget": {"max_cost": "1", "max_minutes": 5},
                },
            )
            assert create.status_code in (200, 201), create.text
            tid = create.json()["id"]
            start = await board_http.post(f"/api/tasks/{tid}/start", headers=user)
            start.raise_for_status()
            runtime_settings = Settings(
                _env_file=None,  # pyright: ignore[reportCallIssue]
                service_token=SecretStr("fixture-service"),
                envd_token_secret=SecretStr("fixture-envd"),
                minio_root_password=SecretStr("unused"),
                exec_access_mode="relay",
                legacy_network_policy_file=route_policy,
                legacy_envd_image=envd_image,
                traffic_enabled=False,
            )
            manager = ExecEnvManager(runtime_settings, docker_client=client, objects=objects)
            agent = FixtureAgent(service, target, port, marker, blocked)
            errors: asyncio.Queue = asyncio.Queue()
            provision = manager.protected.provision

            async def diagnose(task, *args):
                try:
                    return await provision(task, *args)
                except Exception as error:
                    known = await manager.protected.roles(task)
                    logs = {
                        role: item.logs(tail=12).decode(errors="replace")
                        for role, item in known.items()
                    }
                    await errors.put((type(error).__name__, str(error), logs))
                    raise

            monkeypatch.setattr(manager.protected, "provision", diagnose)
            supervisor = TaskSupervisor(
                runtime_settings, service, manager, cast(AgentRunner, agent)
            )
            work = asyncio.create_task(supervisor.run())
            second = None
            try:
                completed = asyncio.create_task(agent.results.get())
                failed = asyncio.create_task(errors.get())
                try:
                    async with asyncio.timeout(90):
                        done, _ = await asyncio.wait(
                            (completed, failed), return_when=asyncio.FIRST_COMPLETED
                        )
                    if failed in done:

                        async def no_original_launch():
                            state = (await service.state(tid))["task"]
                            receipt = state.get("legacy_network_receipt")
                            return (
                                receipt
                                if receipt and receipt["phase"] in {"absent", "exited"}
                                else None
                            )

                        closed_boot = await wait_for(no_original_launch, 25)
                        print(json.dumps({"failed_task": tid, "original_closure": closed_boot}))
                        pytest.fail(repr(failed.result()))
                    aid, handle, denied, result = completed.result()
                finally:
                    completed.cancel()
                    failed.cancel()
                    await asyncio.gather(completed, failed, return_exceptions=True)
                assert denied.status_code == 400 and "privileged" in denied.text
                assert result.status_code == 200, result.text
                assert (
                    result.json()["exit_code"] == 0 and "protected-low-uid-roundtrip" in result.text
                ), result.text
                row = (await service.state(tid))["task"]
                before = row["legacy_network_receipt"]
                assert before["phase"] == "ready" and before["capture_ready"]
                assert before["probe_ready"] == (mode != "default")
                agents = [aid]
                if mode == "default":
                    registration = await service.register_agent(tid, "explore", is_seed=True)
                    other = registration["agent_id"]
                    assert other != aid
                    await manager.create_user(handle, other)
                    second = asyncio.create_task(
                        agent.run_agent(tid, other, "explore", handle=handle)
                    )
                    second_id, _, _, second_result = await asyncio.wait_for(agent.results.get(), 10)
                    assert second_id == other and second_result.json()["exit_code"] == 0
                    agents.append(other)
                    async with httpx.AsyncClient(trust_env=False, timeout=32) as control:
                        script = (
                            "import socket,time\n"
                            f"s=socket.create_connection(({target!r},{port}),2);s.settimeout(1.5)\n"
                            "deadline=time.monotonic()+21\n"
                            "while time.monotonic()<deadline:\n"
                            " try:\n"
                            "  s.sendall(b'bbx-existing-flow')\n"
                            "  assert s.recv(17)==b'bbx-existing-flow'\n"
                            " except OSError:\n"
                            "  print('original established flow fenced');break\n"
                            " time.sleep(.3)\n"
                            "else: raise RuntimeError('established flow survived owner loss')"
                        )
                        held = asyncio.create_task(
                            control.post(
                                handle.base_url + "/execute",
                                headers={
                                    "Authorization": "Bearer " + handle.token,
                                    "X-Agent-Id": aid,
                                },
                                json={
                                    "command": "python3 -c " + shlex.quote(script),
                                    "timeout_sec": 25,
                                },
                            )
                        )
                        try:
                            await asyncio.wait_for(established.wait(), 5)
                            supervisor.network_tasks[tid].cancel()
                            await asyncio.gather(
                                supervisor.network_tasks[tid], return_exceptions=True
                            )
                            result = await held
                            assert result.status_code == 200 and result.json()["exit_code"] == 0
                            assert "original established flow fenced" in result.text
                        finally:
                            held.cancel()
                            await asyncio.gather(held, return_exceptions=True)
                if mode == "socks5":
                    assert peer is not None
                    assert "SOCKS5 CONNECT 192.0.2.50:18888 auth-ok" in peer.logs().decode()
                if peer:
                    await asyncio.to_thread(peer.stop, timeout=1)
                    async with httpx.AsyncClient(trust_env=False, timeout=10) as control:
                        negative = await control.post(
                            handle.base_url + "/execute",
                            headers={"Authorization": "Bearer " + handle.token, "X-Agent-Id": aid},
                            json={
                                "command": "python3 -c "
                                + shlex.quote(
                                    "import socket;"
                                    f"s=socket.create_connection(({target!r},{port}),1);"
                                    "s.settimeout(1);s.sendall(b'after-loss');"
                                    "assert s.recv(20)==b'after-loss'"
                                ),
                                "timeout_sec": 3,
                            },
                        )
                    assert negative.status_code != 200 or negative.json()["exit_code"] != 0
                response = await board_http.post(f"/api/tasks/{tid}/stop", headers=user)
                response.raise_for_status()
                agent.release.set()

                async def closed():
                    value = (await service.state(tid))["task"]
                    receipt = value.get("legacy_network_receipt")
                    return (
                        receipt
                        if receipt and receipt["phase"] == "exited" and receipt["capture_closed"]
                        else None
                    )

                after = await wait_for(closed)
                assert all(
                    after[key] == before[key]
                    for key in (
                        "envd_id",
                        "envd_started_at",
                        "network_id",
                        "relay_id",
                        "relay_network_id",
                        "gateway_id",
                        "gateway_network_id",
                    )
                )
                capture = await board_http.get(f"/api/tasks/{tid}/traffic", headers=user)
                capture.raise_for_status()
                assert capture.json()["raw_only"] and capture.json()["native_sources"]
                prefix = f"traffic/{tid}/run-1/"
                manifest = json.loads(await objects.get(prefix + "manifest.json"))
                sources = []
                for part in manifest["files"]:
                    part_agent = part.get("agent_id")
                    if part_agent not in agents:
                        continue
                    packet = await objects.get(prefix + "pcap/" + part["id"])
                    assert all(
                        agent.marker_for(other) not in packet
                        for other in agents
                        if other != part_agent
                    )
                    if agent.marker_for(part_agent) in packet:
                        sources.append(
                            {
                                "agent_id": part_agent,
                                "source_id": part["source_id"],
                                "sha256": hashlib.sha256(packet).hexdigest(),
                            }
                        )
                assert {row["agent_id"] for row in sources} == set(agents)
                assert len({row["source_id"] for row in sources}) == len(agents)
                print(
                    json.dumps(
                        {"mode": mode, "task_id": tid, "original": after, "sources": sources}
                    )
                )
            finally:
                agent.release.set()
                if second:
                    await asyncio.gather(second, return_exceptions=True)
                await supervisor.stop()
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
    finally:
        if direct_server:
            direct_server.close()
            for writer in list(direct_clients):
                writer.close()
            await direct_server.wait_closed()
        if peer:
            peer.reload()
            assert peer.labels.get("bbx.fixture") == own
            peer.remove(force=True, v=True)
        if uplink:
            uplink.reload()
            assert uplink.attrs["Labels"]["bbx.fixture"] == own
            uplink.remove()
