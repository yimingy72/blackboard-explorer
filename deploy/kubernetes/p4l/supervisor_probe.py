"""Exercise the shipped supervisor's explicit hold and terminal unexpected lease loss."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

source = Path("/opt/bbx/control/task-vpn.py")
spec = importlib.util.spec_from_file_location("task_vpn", source)
assert spec and spec.loader
vpn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpn)
OP = str(UUID(int=1))
PAUSE = str(UUID(int=2))
KEY = "mock-peer"
SCOPE = {"runner_pod_ipv4": "10.1.1.2", "sandbox_pod_ipv4": "10.1.1.3"}
states = [
    {"state": "requested"},
    {"state": "held", "pause_id": PAUSE},
    {"state": "held", "pause_id": PAUSE},
    {"state": "ready", "resume_pause_id": PAUSE},
    {"state": "ready"},
    {"state": "disconnecting"},
]
events = []
state_count = lease_count = clock = 0
LINK = {"ifindex": 4, "public_key": "mock-local"}
TUN = 5


class FakeControl:
    def __init__(self, _config):
        pass

    def call(self, action, payload=None):
        global state_count, lease_count
        if action == "desired":
            selected = states[state_count]
            state_count += 1
            return {**selected, "scope": SCOPE, "vpn_operation_id": OP, "remote_public_key": KEY}
        if action == "claim":
            events.append(("claim", payload))
            return {"launch_allowed": True}
        if action == "lease":
            lease_count += 1
            return {"grant_revision": lease_count}
        assert action == "observe"
        events.append((payload["phase"], payload))
        return {}


class FakeProcess:
    pid = 512
    exited = False

    def poll(self):
        return 0 if self.exited else None

    def terminate(self):
        self.exited = True
        events.append(("process-exit", self.pid))

    def wait(self, *, timeout):
        assert timeout > 0 and self.exited


child = FakeProcess()


def openvpn(_config):
    events.append(("process-start", child.pid))
    return child


def grant(lease, sampled, targets, previous):
    assert previous is None
    events.append(("kernel-open", lease["grant_revision"]))
    return {"lease": lease}, sampled + 10_000, vpn.now_ms() + 10_000


def sleep(_seconds):
    global clock
    clock += 11_000 if state_count == 4 else 800


vpn.Control = FakeControl
vpn.local_address = lambda address, device=None: (
    address == SCOPE["runner_pod_ipv4"] or device == vpn.TUN
)
vpn.install_drop_hooks = lambda *args, **kwargs: None
vpn.persist = lambda *args: None
vpn.install_wg = lambda root: LINK
vpn.peer = lambda *args: None
vpn.command = lambda *args, **kwargs: None
vpn.openvpn = openvpn
vpn.live_link = lambda *_: True
vpn.ifindex = lambda _name: TUN
vpn.process_ticks = lambda _pid: 67
vpn.route_unchanged = lambda *_: True
vpn.edge_grant = grant
vpn.edge_fence = lambda: events.append(("kernel-fence", None))
vpn.boot_ms = lambda: clock
vpn.time.sleep = sleep
vpn.edge_run(
    {
        "scope": SCOPE,
        "targets": [{"ipv4": "10.70.0.1", "port": 18888}],
        "tun_ipv4": "10.70.0.2",
        "peer": {"ipv4": "10.1.1.4"},
        "hard_deadline": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
    },
    lambda: state_count == len(states),
)
names = [name for name, _ in events]
assert names.count("claim") == names.count("process-start") == names.count("process-exit") == 1
assert names.count("held") == names.count("kernel-open") == 2
assert names.index("kernel-open") < names.index("held") < names.index("process-exit")
assert names.index("process-exit") < names.index("failed") < names.index("closed")
applied = [value for name, value in events if name == "tunnel" and value.get("lease_revision")]
assert [value["lease_revision"] for value in applied] == [1, 2]
assert all(value["tunnel"] == applied[0]["tunnel"] for value in applied)
assert all(value["tunnel"] == applied[0]["tunnel"] for name, value in events if name == "held")
print("deliberate hold rearmed the same process; unexpected elapsed grant fenced without reopening")
