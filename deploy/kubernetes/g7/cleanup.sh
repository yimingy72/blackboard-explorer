#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
STATE="$ROOT/.data/bbx-archv2-g7"
NODE=bbx-archv2-g7-control-plane
if docker inspect "$NODE" >/dev/null 2>&1; then
    test "$(docker inspect "$NODE" --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}')" = bbx-archv2-g7
    "$ROOT/.data/bbx-archv2-k8s/bin/kind" delete cluster --name bbx-archv2-g7 --kubeconfig "$STATE/kubeconfig"
fi
if docker network inspect bbx-archv2-g7-network >/dev/null 2>&1; then
    test -z "$(docker network inspect bbx-archv2-g7-network --format '{{range .Containers}}{{.Name}}{{end}}')"
    docker network rm bbx-archv2-g7-network >/dev/null
fi
for image in bbx-archv2-server-g7:bbx-g7-direct-1 bbx-archv2-server-g7:1.2.0-direct.1; do
    if docker image inspect "$image" >/dev/null 2>&1; then docker image rm "$image" >/dev/null; fi
done
python3 - "$STATE" <<'PY'
import sys
from datetime import datetime, timezone
from pathlib import Path

state = Path(sys.argv[1])
history = state / 'closed-runs' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
for name in ('create-a.attempted', 'create-b.attempted'):
    source = state / name
    if source.exists():
        history.mkdir(parents=True, exist_ok=True)
        source.rename(history / name)
for name in ('credentials.json', 'server-runtime.json', 'kubeconfig'):
    (state / name).unlink(missing_ok=True)
PY
