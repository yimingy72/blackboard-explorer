"""The published solver range never overrides the control deny or probe boundary."""

import runpy
from pathlib import Path

import pytest


def launcher():
    path = Path(__file__).resolve().parents[1] / "launcher/task-vpn.py"
    return runpy.run_path(str(path))


def test_effective_control_deny_prevents_address_installation(monkeypatch):
    code = launcher()
    install = code["install_tun_address"]
    namespace = install.__globals__
    calls = []
    monkeypatch.setitem(namespace, "local_address", lambda _: False)
    monkeypatch.setitem(namespace, "command", lambda *args: calls.append(args))
    config = {"peer": {"ipv4": "172.20.0.4"}, "deny_cidrs": ["10.216.0.0/16"]}
    routes = {"allow_cidrs": ["0.0.0.0/0"], "deny_cidrs": ["192.168.65.254/32"]}
    with pytest.raises(ValueError):
        install(config, "192.168.65.254", [("10.77.0.1", 18888)], routes)
    assert calls == []
    install(config, "10.70.0.2", [("10.77.0.1", 18888)], routes)
    assert calls[0][1:4] == ("address", "add", "10.70.0.2/32")


def test_probe_stays_exact_while_solver_range_respects_bidirectional_denies():
    rules = launcher()["forwarding_rules"]
    grant = {"mode": "probe", "mark": "test", "end": "2099-01-01T00:00:00"}
    routes = {"allow_cidrs": ["10.0.0.0/8"], "deny_cidrs": ["10.216.0.0/16"]}
    probe = rules(grant, [("10.70.0.1", 18888)], routes)
    solver = rules({**grant, "mode": "solver"}, [("10.70.0.1", 18888)], routes)
    assert (
        solver[:2]
        == probe[:2]
        == [
            "-A BBX_VPN -i bbxwg -d 10.216.0.0/16 -j DROP",
            "-A BBX_VPN -i bbxvpn -s 10.216.0.0/16 -j DROP",
        ]
    )
    assert "10.70.0.1/32 -p tcp --dport 18888" in probe[2]
    assert "10.0.0.0/8 -p tcp" in solver[2] and "--dport" not in solver[2]
