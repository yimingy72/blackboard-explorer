"""Actual iptables renewal gate test, in a disposable network namespace only."""

import importlib.util
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("task_vpn", Path("/opt/bbx/control/task-vpn.py"))
assert spec and spec.loader
vpn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpn)
vpn.install_drop_hooks((vpn.WG_LINK, vpn.TUN), edge=True)
targets = [("10.70.0.1", 18888)]


def lease():
    now = vpn.now_ms()
    return {"mode": "solver", "issued_at_ms": now, "authority_until_ms": now + 15000}


first, _, _ = vpn.edge_grant(lease(), vpn.boot_ms(), targets, None)
second, _, _ = vpn.edge_grant(lease(), vpn.boot_ms(), targets, first)
assert first["mark"] != second["mark"]
assert "src=0.0.0.0 " not in vpn.recent_path(first["mark"]).read_text()
assert "src=0.0.0.0 " in vpn.recent_path(second["mark"]).read_text()

# Freeze the supervisor after future rules are installed but before the kernel trigger.
trigger = vpn.confirm_local_renewal


def late():
    time.sleep(10.1)
    trigger()


vpn.confirm_local_renewal = late
try:
    vpn.edge_grant(lease(), vpn.boot_ms(), targets, second)
except (TimeoutError, PermissionError):
    pass
else:
    raise AssertionError("an expired original gate armed a newer forwarding marker")
assert "src=0.0.0.0 " not in vpn.recent_path(first["mark"]).read_text()
vpn.edge_fence()
assert all("src=0.0.0.0 " not in vpn.recent_path(mark).read_text() for mark in vpn.MARKS)
print("kernel verified first grant, live renewal, delayed-trigger rejection and terminal fence")
