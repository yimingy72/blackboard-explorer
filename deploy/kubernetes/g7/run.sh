#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
cd "$ROOT"
STATE="$ROOT/.data/bbx-archv2-g7"
TOOLS="$ROOT/.data/bbx-archv2-k8s"
NODE=bbx-archv2-g7-control-plane
umask 077
mkdir -p "$STATE"
chmod 700 "$STATE"
# Refuse foreign containers before any node mutation, as in the P1 fixture.
if docker inspect "$NODE" >/dev/null 2>&1; then
    test "$(docker inspect "$NODE" --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}')" = bbx-archv2-g7
fi
test "$(shasum -a 256 "$TOOLS/bin/kind" | cut -d ' ' -f 1)" = 4f0b6e3b88bdc66d922c08469f05ef507d4903dd236e6319199bb9c868eed274
test "$(shasum -a 256 "$TOOLS/downloads/helm.tar.gz" | cut -d ' ' -f 1)" = 09a108c0abda42e45af172be65c49125354bf7cd178dbe10435e94540e49c7b9
tar -xOf "$TOOLS/downloads/helm.tar.gz" darwin-amd64/helm > "$STATE/helm"
chmod 700 "$STATE/helm"
export KUBECONFIG="$STATE/kubeconfig"
export HELM_CACHE_HOME="$STATE/helm-cache" HELM_CONFIG_HOME="$STATE/helm-config" HELM_DATA_HOME="$STATE/helm-data"
python3 deploy/kubernetes/g7/build.py
if ! docker inspect "$NODE" >/dev/null 2>&1; then
    KIND_EXPERIMENTAL_DOCKER_NETWORK=bbx-archv2-g7-network "$TOOLS/bin/kind" create cluster --name bbx-archv2-g7 --config deploy/kubernetes/g7/kind.yaml --kubeconfig "$KUBECONFIG" --wait 0s
fi
test "$(docker inspect "$NODE" --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}')" = bbx-archv2-g7
test "$(kubectl --kubeconfig "$KUBECONFIG" config current-context)" = kind-bbx-archv2-g7
docker update --cpus 2 --memory 6g --memory-swap 6g "$NODE" >/dev/null
kubectl --kubeconfig "$KUBECONFIG" wait --for=condition=Ready node/"$NODE" --timeout=120s
docker exec "$NODE" df -h /var/lib/containerd
python3 deploy/kubernetes/g7/load-images.py
kubectl --kubeconfig "$KUBECONFIG" apply -f "$STATE/upstream/kubernetes/config/crd/bases" >/dev/null
PYTHON="$ROOT/services/sandbox-execution/.venv/bin/python"
"$PYTHON" deploy/kubernetes/g7/probe.py setup
for component in controller server ingress; do
    chart=$component
    if [ "$component" = ingress ]; then chart=ingress-gateway; fi
    set -- "$STATE/helm" --kubeconfig "$KUBECONFIG" upgrade --install "bbx-archv2-g7-$component" "$STATE/upstream/manifests/charts/$chart" -n bbx-archv2-g7-system -f "deploy/kubernetes/g7/values/$component.yaml"
    if [ "$component" = server ]; then
        # Generated server.podAnnotations binds the Pod template to this build/runtime content.
        set -- "$@" -f "$STATE/server-runtime.json"
    fi
    "$@" >/dev/null
done
for name in opensandbox-controller-manager bbx-archv2-g7-server bbx-archv2-g7-ingress bbx-archv2-g7-tenant; do
    kubectl --kubeconfig "$KUBECONFIG" -n bbx-archv2-g7-system rollout status deployment/"$name" --timeout=180s
done
"$PYTHON" deploy/kubernetes/g7/probe.py verify
