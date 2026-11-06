#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
STATE="$ROOT/.data/bbx-archv2-k8s"
export KUBECONFIG="$STATE/kubeconfig"
if docker inspect bbx-archv2-k8s-control-plane >/dev/null 2>&1; then
    test "$(docker inspect bbx-archv2-k8s-control-plane --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}')" = bbx-archv2-k8s
    "$STATE/bin/kind" delete cluster --name bbx-archv2-k8s
fi
if docker network inspect bbx-archv2-kind-network >/dev/null 2>&1; then
    docker network rm bbx-archv2-kind-network
fi
if docker image inspect bbx-archv2-sandbox-k8s:20261106 >/dev/null 2>&1; then
    docker image rm bbx-archv2-sandbox-k8s:20261106
fi
python3 - "$STATE" <<'PY'
import sys
from datetime import datetime, timezone
from pathlib import Path
root = Path(sys.argv[1])
history = root / 'closed-runs' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
for name in ('create-a.attempted', 'create-b.attempted', 'affected-create-a.attempted', 'affected-create-b.attempted'):
    source = root / name
    if source.exists():
        history.mkdir(parents=True, exist_ok=True)
        source.rename(history / name)
for name in ('credentials.json', 'server-runtime.json', 'kubeconfig'):
    (root / name).unlink(missing_ok=True)
PY
