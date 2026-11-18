#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
cd "$ROOT"
STATE="$ROOT/.data/bbx-archv2-g1-k8s"
CLUSTER=bbx-archv2-g1-k8s
NODE="$CLUSTER-control-plane"
PYTHON="$ROOT/services/sandbox-execution/.venv/bin/python"
umask 077
# Refuse a foreign reused node before preparation or node mutation.
if docker inspect "$NODE" >/dev/null 2>&1; then
    test "$(docker inspect "$NODE" --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}')" = "$CLUSTER"
fi
python3 deploy/kubernetes/g1/prepare.py
export KUBECONFIG="$STATE/kubeconfig"
export HELM_CACHE_HOME="$STATE/helm-cache" HELM_CONFIG_HOME="$STATE/helm-config" HELM_DATA_HOME="$STATE/helm-data"
if ! docker inspect "$NODE" >/dev/null 2>&1; then
    KIND_EXPERIMENTAL_DOCKER_NETWORK=bbx-archv2-g1-k8s-network "$STATE/bin/kind" create cluster --name "$CLUSTER" --config deploy/kubernetes/g1/kind.yaml --kubeconfig "$KUBECONFIG" --wait 0s
fi
test "$(docker inspect "$NODE" --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}')" = "$CLUSTER"
test "$(kubectl --kubeconfig "$KUBECONFIG" config current-context)" = "kind-$CLUSTER"
# Verify the API before node updates or Helm/kubectl writes.
PYTHONPATH=deploy/kubernetes/g1 "$PYTHON" -c 'from common import owned_cluster; owned_cluster()'
docker update --cpus 2 --memory 6g --memory-swap 6g "$NODE" >/dev/null
docker exec "$NODE" /bin/sh -c 'if ! grep -q "net.ipv4.ip_forward" /var/lib/kubelet/config.yaml; then printf "\nallowedUnsafeSysctls:\n- net.ipv4.ip_forward\n" >> /var/lib/kubelet/config.yaml; systemctl restart kubelet; fi'
python3 deploy/kubernetes/g1/load_images.py control
"$STATE/bin/helm" --kubeconfig "$KUBECONFIG" upgrade --install bbx-archv2-g1-k8s-cilium "$STATE/cilium.tgz" -n kube-system -f deploy/kubernetes/g1/values/cilium.yaml >/dev/null
kubectl --kubeconfig "$KUBECONFIG" -n kube-system rollout status daemonset/cilium --timeout=180s
kubectl --kubeconfig "$KUBECONFIG" wait --for=condition=Ready node/"$NODE" --timeout=90s
kubectl --kubeconfig "$KUBECONFIG" apply -f "$STATE/upstream/kubernetes/config/crd/bases" >/dev/null
"$PYTHON" deploy/kubernetes/g1/probe.py setup
for component in controller server ingress; do
    chart=$component
    if [ "$component" = ingress ]; then chart=ingress-gateway; fi
    set -- "$STATE/bin/helm" --kubeconfig "$KUBECONFIG" upgrade --install "bbx-archv2-g1-k8s-$component" "$STATE/upstream/manifests/charts/$chart" -n bbx-archv2-g1-k8s-system -f "deploy/kubernetes/g1/values/$component.yaml"
    if [ "$component" = server ]; then set -- "$@" -f "$STATE/server-runtime.json"; fi
    "$@" >/dev/null
done
for name in opensandbox-controller-manager bbx-archv2-g1-k8s-server bbx-archv2-g1-k8s-ingress; do
    kubectl --kubeconfig "$KUBECONFIG" -n bbx-archv2-g1-k8s-system rollout status deployment/"$name" --timeout=180s
done
"$PYTHON" deploy/kubernetes/g1/probe.py preflight
if [ -f "$STATE/unsupported.json" ]; then exit 0; fi
# Build only the reviewed execution tree and inspect real node disk before exporting its large layer.
git diff --quiet d5c84059c55864a84a3efdd09c4ab3477268a318 -- services/sandbox-execution
docker exec "$NODE" df -h /var/lib/containerd
docker build --network none -t bbx-archv2-sandbox-g1-k8s:20261106 services/sandbox-execution
python3 deploy/kubernetes/g1/load_images.py sandbox
"$PYTHON" deploy/kubernetes/g1/probe.py verify
