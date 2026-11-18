#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
STATE="$ROOT/.data/bbx-archv2-g1-k8s"
NODE=bbx-archv2-g1-k8s-control-plane
if docker inspect "$NODE" >/dev/null 2>&1; then
    test "$(docker inspect "$NODE" --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}')" = bbx-archv2-g1-k8s
    "$STATE/bin/kind" delete cluster --name bbx-archv2-g1-k8s --kubeconfig "$STATE/kubeconfig"
fi
if docker network inspect bbx-archv2-g1-k8s-network >/dev/null 2>&1; then
    test -z "$(docker network inspect bbx-archv2-g1-k8s-network --format '{{range .Containers}}{{.Name}}{{end}}')"
    docker network rm bbx-archv2-g1-k8s-network >/dev/null
fi
if docker image inspect bbx-archv2-sandbox-g1-k8s:20261106 >/dev/null 2>&1; then
    docker image rm bbx-archv2-sandbox-g1-k8s:20261106 >/dev/null
fi
python3 - "$STATE" <<'PY'
import sys
from datetime import datetime, timezone
from pathlib import Path

state = Path(sys.argv[1])
history = state / 'closed-runs' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
for name in ('create.attempted', 'verify.attempted', 'first-ready-pvc.json', 'unsupported.json'):
    source = state / name
    if source.exists():
        history.mkdir(parents=True, exist_ok=True)
        source.rename(history / name)
for name in ('credentials.json', 'server-runtime.json', 'kubeconfig'):
    (state / name).unlink(missing_ok=True)
PY
