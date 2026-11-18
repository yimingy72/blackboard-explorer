"""A local ICMP echo and failed administration attempts under the real solver UID."""

import json
import os
import socket
import struct
import subprocess
import time
from pathlib import Path

raw = False
reply = False
try:
    connection = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
except PermissionError:
    connection = None
if connection is not None:
    raw = True
    identifier = os.getpid() & 0xFFFF
    message = struct.pack("!BBHHH", 8, 0, 0, identifier, 1) + b"bbx-archv2-local-echo"
    padded = message + b"\0" * (len(message) % 2)
    checksum = sum(struct.unpack("!" + "H" * (len(padded) // 2), padded))
    checksum = (checksum & 0xFFFF) + (checksum >> 16)
    checksum = (checksum & 0xFFFF) + (checksum >> 16)
    message = message[:2] + struct.pack("!H", (~checksum) & 0xFFFF) + message[4:]
    connection.settimeout(1)
    connection.sendto(message, ("198.19.4.2", 0))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        packet, source = connection.recvfrom(4096)
        header = (packet[0] & 15) * 4
        if source[0] == "198.19.4.2" and packet[header] == 0:
            reply = struct.unpack("!H", packet[header + 4 : header + 6])[0] == identifier
            if reply:
                break
    connection.close()

can_setuid = True
try:
    os.setuid(0)
except PermissionError:
    can_setuid = False

admin = subprocess.run(
    ["/usr/sbin/ip", "link", "add", "bbx-forbidden", "type", "dummy"],
    capture_output=True,
    text=True,
)
print(
    json.dumps(
        {
            "uid": os.getuid(),
            "raw_socket": raw,
            "icmp_reply": reply,
            "can_setuid": can_setuid,
            "net_admin_exit": admin.returncode,
            "net_admin_error": admin.stderr,
            "status": Path("/proc/self/status").read_text(),
        }
    )
)
