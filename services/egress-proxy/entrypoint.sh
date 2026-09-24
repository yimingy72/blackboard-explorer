#!/bin/sh
set -eu

cat > /etc/tinyproxy/tinyproxy.conf <<'EOF'
User tinyproxy
Group tinyproxy
Port 8888
Listen 0.0.0.0
Timeout 30
LogFile "/dev/stdout"
LogLevel Info
Filter "/etc/tinyproxy/allowlist"
FilterDefaultDeny Yes
FilterExtended On
ConnectPort 443
EOF

: > /etc/tinyproxy/allowlist
old_ifs=$IFS
IFS=,
for domain in ${EGRESS_ALLOWLIST:-}; do
    IFS=$old_ifs
    domain=$(printf '%s' "$domain" | tr '[:upper:]' '[:lower:]' | xargs)
    if ! printf '%s' "$domain" | grep -Eq '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' || printf '%s' "$domain" | grep -Eq '\.\.|^-|-$'; then
        echo "Invalid EGRESS_ALLOWLIST domain" >&2
        exit 1
    fi
    escaped=$(printf '%s' "$domain" | sed 's/\./\\./g')
    printf '(^|\\.)%s$\n' "$escaped" >> /etc/tinyproxy/allowlist
    IFS=,
done
IFS=$old_ifs

exec tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf
