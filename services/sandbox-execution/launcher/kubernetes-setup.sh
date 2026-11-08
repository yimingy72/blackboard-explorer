#!/bin/sh
set -eu
# This fixed control script runs only after the static bootstrap environment reset.
install -d -o root -g root -m 0700 /var/lib/bbx/control /var/lib/bbx/control/launches
if [ ! -e /var/lib/bbx/control/execd.env ]; then
    (umask 077; : > /var/lib/bbx/control/execd.env)
fi
chown 10000:10000 /workspace
chmod 0755 /workspace
# The retained volume is solver-controlled: remove control env before lowering identity.
/usr/bin/env -i /usr/bin/setpriv --reuid=10000 --regid=10000 --clear-groups \
    --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs \
    /usr/bin/python3 -I - <<'PY'
import os

assert os.getuid() == os.getgid() == 10000
assert "EXECD_ACCESS_TOKEN" not in os.environ
try:
    try:
        os.mkdir("/workspace/.tmp", 0o700)
    except FileExistsError:
        pass
    fd = os.open("/workspace/.tmp", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != 10000 or info.st_gid != 10000:
            raise PermissionError("Unexpected workspace temporary directory identity")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
except OSError:
    raise SystemExit("Unsafe workspace .tmp; low-identity preparation refused") from None
PY
test "$(cat /proc/sys/net/ipv4/ip_forward)" = 1
if [ -f /opt/bbx/control/assignment-network-v1 ]; then
    /usr/bin/python3 -I /opt/bbx/control/assignment-network.py init
else
    ip netns add worker-a
    ip link add bbx-host type veth peer name bbx-peer
    ip link set bbx-peer netns worker-a
    ip addr add 198.19.1.1/30 dev bbx-host
    ip link set bbx-host up
    ip -n worker-a addr add 198.19.1.2/30 dev bbx-peer
    ip -n worker-a link set bbx-peer up
    ip -n worker-a link set lo up
    ip -n worker-a route add default via 198.19.1.1
    iptables -I INPUT 1 -i bbx-host -j DROP
    iptables -I INPUT 1 -i bbx-host -p tcp --dport 18081 -j ACCEPT
    iptables -I INPUT 1 -i bbx-host ! -s 198.19.1.2/32 -j DROP
    iptables -I FORWARD 1 -i bbx-host -j ACCEPT
    iptables -I FORWARD 1 -i bbx-host ! -s 198.19.1.2/32 -j DROP
    iptables -I FORWARD 1 -o bbx-host -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -t nat -A POSTROUTING -s 198.19.1.2/32 ! -d 198.19.1.0/30 -j MASQUERADE
fi
