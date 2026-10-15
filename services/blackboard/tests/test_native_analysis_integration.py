"""Real official decoder, private TLS material and PCAPNG sections; no external targets."""

import asyncio
import hashlib
import os
import socket
import ssl
import struct
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from bbx_blackboard.native import NativeAnalysisCache, NativeFilterError, NativeWorker
from bbx_contracts.traffic import TrafficNativeSourceIndex, TrafficNativeSourceRef
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_native_analysis import Objects
from testcontainers.core.container import DockerContainer

pytestmark = pytest.mark.integration


def block(kind, body):
    assert len(body) % 4 == 0
    length = len(body) + 12
    return struct.pack("<II", kind, length) + body + struct.pack("<I", length)


def section(frames):
    data = block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    data += block(1, struct.pack("<HHI", 1, 0, 262144))
    for number, frame in frames:
        stamp = 1700000000000000 + number * 1000
        body = struct.pack("<IIIII", 0, stamp >> 32, stamp & 0xFFFFFFFF, len(frame), len(frame))
        data += block(6, body + frame + b"\0" * (-len(frame) % 4))
    return data


def tcp(payload, seq, ack, *, server=False, flags=0x18):
    source, destination = ("192.0.2.2", "192.0.2.1") if server else ("192.0.2.1", "192.0.2.2")
    source_port, destination_port = (443, 44444) if server else (44444, 443)
    header = struct.pack(
        "!HHIIBBHHH", source_port, destination_port, seq, ack, 0x50, flags, 65535, 0, 0
    )
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        69,
        0,
        20 + len(header) + len(payload),
        1,
        0,
        64,
        6,
        0,
        socket.inet_aton(source),
        socket.inet_aton(destination),
    )
    return b"\0" * 12 + b"\x08\x00" + ip + header + payload


def synthetic_tls(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path, log_path = (
        directory / "cert.pem",
        directory / "key.pem",
        directory / "keys.log",
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    client_context.keylog_filename = str(log_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    inbound = [ssl.MemoryBIO(), ssl.MemoryBIO()]
    outbound = [ssl.MemoryBIO(), ssl.MemoryBIO()]
    peers = [
        client_context.wrap_bio(inbound[0], outbound[0], server_hostname="synthetic.test"),
        server_context.wrap_bio(inbound[1], outbound[1], server_side=True),
    ]
    sequences = [1001, 2001]
    frames = [
        tcp(b"", 1000, 0, flags=2),
        tcp(b"", 2000, 1001, server=True, flags=0x12),
        tcp(b"", 1001, 2001, flags=0x10),
    ]

    def drain(side):
        while payload := outbound[side].read():
            frames.append(tcp(payload, sequences[side], sequences[1 - side], server=bool(side)))
            sequences[side] += len(payload)
            inbound[1 - side].write(payload)

    done = [False, False]
    for _ in range(30):
        for side in (0, 1):
            try:
                peers[side].do_handshake()
                done[side] = True
            except ssl.SSLWantReadError:
                pass
            drain(side)
        if all(done):
            break
    assert all(done)
    peers[0].write(b"GET /native HTTP/1.1\r\nHost: synthetic.test\r\n\r\n")
    drain(0)
    assert b"GET /native" in peers[1].read()
    body = b"local-native-synthetic-body"
    peers[1].write(
        b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    drain(1)
    assert body in peers[0].read()
    return frames, log_path.read_bytes()


def native_input(objects, task_id, frames, keys):
    source_id = "synthetic-source"
    split = max(1, len(frames) // 2)
    parts, first = [], 1
    prefix = f"traffic/{task_id}/run-1/native/"
    for sequence, items in enumerate(
        (list(enumerate(frames[:split], 1)), list(enumerate(frames[split:], split + 1))), 1
    ):
        content = section(items)
        name = f"part-{sequence}"
        parts.append(
            dict(
                id=name,
                segment_id=f"segment-{sequence}",
                sequence=sequence,
                offset=0,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                first_packet=first,
                last_packet=first + len(items) - 1,
            )
        )
        first += len(items)
        objects.data[prefix + f"{source_id}/parts/{name}.bin"] = content
    keylog = dict(
        id="keylog-1",
        sequence=1,
        offset=0,
        size_bytes=len(keys),
        sha256=hashlib.sha256(keys).hexdigest(),
    )
    objects.data[prefix + "keylog/keylog-1.log"] = keys
    index = TrafficNativeSourceIndex.model_validate(
        dict(
            task_id=task_id,
            run=1,
            source_id=source_id,
            agent_id="lead",
            complete=True,
            packet_count=len(frames),
            parts=parts,
            keylog_parts=[keylog],
        )
    )
    data = index.model_dump_json().encode()
    revision = hashlib.sha256(data).hexdigest()
    objects.data[prefix + f"{source_id}/indexes/{revision}.json"] = data
    return TrafficNativeSourceRef(
        source_id=source_id,
        agent_id="lead",
        revision=revision,
        available_bytes=sum(part["size_bytes"] for part in parts),
        packet_count=len(frames),
        complete=True,
    )


async def test_official_native_filters_tls_reassembly_data_sources_and_archive_readback(tmp_path):
    os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"
    helper = Path(__file__).resolve().parents[1] / "src/bbx_blackboard/native_worker.py"
    objects, task_id = Objects(), uuid4()
    frames, keys = synthetic_tls(tmp_path)
    ref = native_input(objects, task_id, frames, keys)
    with ExitStack() as resources:

        async def factory(home):
            uid, gid = home.stat().st_uid, home.stat().st_gid
            name = "bbx-traffic-native-" + uuid4().hex[:8]
            container = (
                DockerContainer(os.environ.get("BBX_NATIVE_TEST_IMAGE", "bbx-traffic-envd:latest"))
                .with_name(name)
                .with_volume_mapping(str(home), str(home), mode="rw")
                .with_volume_mapping(str(helper), "/native_worker.py", mode="ro")
                .with_env("HOME", str(home))
                .with_env("LC_ALL", "C")
                .with_command(["python3", "/native_worker.py", "/usr/bin/sharkd"])
                .with_kwargs(
                    network_mode="none",
                    stdin_open=True,
                    user=f"{uid}:{gid}",
                    cap_drop=["ALL"],
                    read_only=True,
                    mem_limit="1g",
                    pids_limit=32,
                    security_opt=["no-new-privileges"],
                )
            )
            await asyncio.to_thread(resources.enter_context, container)
            process = await asyncio.create_subprocess_exec(
                "docker",
                "attach",
                "--sig-proxy=false",
                name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=4 * 1024**2,
                start_new_session=True,
            )
            return await NativeWorker.attach(process)

        cache = NativeAnalysisCache(factory)
        try:
            with pytest.raises(NativeFilterError):
                await cache.check("ip.addr ==")
            for number in range(40):
                await cache.check(f"frame.number == {number + 1}")
            await cache.check('http.host == "synthetic.test" && tcp.port in {443, 44444}')
            query = cache.create(
                task_id,
                1,
                'http.response.code == 200 && http.file_data contains "local-native"',
                None,
                objects,
            )
            await cache.refresh(query, [ref])
            assert query.future is not None
            await asyncio.wait_for(query.future, 90)
            assert query.status == "ready", query.error
            assert query.records, query.view()
            row = query.records[0]
            assert row.agent_id == "lead" and row.columns[4] == "HTTP"
            assert Decimal(row.columns[1]) > 1700000000
            assert query.revision is not None
            detail = await cache.frame(query, row.source_id, row.source_number, query.revision)
            assert any(
                "Decrypted" in source.name or "Reassembled" in source.name
                for source in detail.data_sources
            )
            assert any(b"local-native".hex() in source.raw_hex for source in detail.data_sources)
            assert keys.decode().splitlines()[1] not in detail.model_dump_json()
            # This frame is decoded exclusively from archived private inputs, without a live envd.
            await cache.invalidate_task(task_id)
            assert not cache.parts and not cache.sessions
        finally:
            await cache.close()
