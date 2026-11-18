#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
cd "$ROOT"
STATE="$ROOT/.data/bbx-archv2-k8s"
SOURCE="$STATE/OpenSandbox-62a64f0b48cdd6b410f36e53c74dc7559c87719a"
PYTHON="$ROOT/services/sandbox-execution/.venv/bin/python"
P1_ACTION=${1:-verify}
case "$P1_ACTION" in verify|deploy-only) ;; *) exit 2 ;; esac
# Refuse a reused foreign node before tool preparation or any node mutation.
if docker inspect bbx-archv2-k8s-control-plane >/dev/null 2>&1; then
    test "$(docker inspect bbx-archv2-k8s-control-plane --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}')" = bbx-archv2-k8s
fi
bash deploy/kubernetes/p1/prepare.sh
export KUBECONFIG="$STATE/kubeconfig"
export HELM_CACHE_HOME="$STATE/helm/cache" HELM_CONFIG_HOME="$STATE/helm/config" HELM_DATA_HOME="$STATE/helm/data"
if ! docker inspect bbx-archv2-k8s-control-plane >/dev/null 2>&1; then
    KIND_EXPERIMENTAL_DOCKER_NETWORK=bbx-archv2-kind-network "$STATE/bin/kind" create cluster --name bbx-archv2-k8s --config deploy/kubernetes/p1/kind.yaml --kubeconfig "$KUBECONFIG" --wait 0s
fi
test "$(docker inspect bbx-archv2-k8s-control-plane --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}')" = bbx-archv2-k8s
test "$(kubectl --kubeconfig "$KUBECONFIG" config current-context)" = kind-bbx-archv2-k8s
docker update --cpus 2 --memory 6g --memory-swap 6g bbx-archv2-k8s-control-plane
docker exec bbx-archv2-k8s-control-plane /bin/sh -c 'if ! grep -q "net.ipv4.ip_forward" /var/lib/kubelet/config.yaml; then printf "\nallowedUnsafeSysctls:\n- net.ipv4.ip_forward\n" >> /var/lib/kubelet/config.yaml; systemctl restart kubelet; fi'
"$STATE/bin/helm" --kubeconfig "$KUBECONFIG" upgrade --install bbx-archv2-cilium "$STATE/downloads/cilium.tgz" -n kube-system -f deploy/kubernetes/p1/values/cilium.yaml
kubectl --kubeconfig "$KUBECONFIG" -n kube-system rollout status daemonset/cilium --timeout=180s
kubectl --kubeconfig "$KUBECONFIG" wait --for=condition=Ready node/bbx-archv2-k8s-control-plane --timeout=30s
# Fail before touching the large local CTF image when node storage is insufficient.
docker exec bbx-archv2-k8s-control-plane df -h /var/lib/containerd
for component in server execd controller ingress; do
    reference=$("$PYTHON" -c "import json;d=json.load(open('deploy/kubernetes/p1/versions.json'));print(d['official_images']['opensandbox/$component:release-1.2.0'])")
    docker image inspect "opensandbox/$component@$reference" >/dev/null 2>&1 || docker pull "opensandbox/$component@$reference"
    if docker image inspect "opensandbox/$component:release-1.2.0" >/dev/null 2>&1; then
        docker image inspect "opensandbox/$component:release-1.2.0" --format '{{json .RepoDigests}}' | grep -Fq "@$reference"
    else
        docker tag "opensandbox/$component@$reference" "opensandbox/$component:release-1.2.0"
    fi
done
docker build --network none -t bbx-archv2-sandbox-k8s:20261106 services/sandbox-execution
"$PYTHON" deploy/kubernetes/p1/load-image.py
kubectl --kubeconfig "$KUBECONFIG" apply -f "$SOURCE/kubernetes/config/crd/bases"
if [ ! -f "$STATE/credentials.json" ]; then
    "$PYTHON" deploy/kubernetes/p1/probe.py setup
fi
for component in controller server ingress; do
    chart=$component
    if [ "$component" = ingress ]; then chart=ingress-gateway; fi
    set -- "$STATE/bin/helm" --kubeconfig "$KUBECONFIG" upgrade --install "bbx-archv2-$component" "$SOURCE/manifests/charts/$chart" -n bbx-archv2-system -f "deploy/kubernetes/p1/values/$component.yaml"
    if [ "$component" = server ]; then set -- "$@" -f "$STATE/server-runtime.json"; fi
    "$@" >/dev/null
done
for deployment in opensandbox-controller-manager bbx-archv2-server bbx-archv2-ingress bbx-archv2-tenant; do
    kubectl --kubeconfig "$KUBECONFIG" -n bbx-archv2-system rollout status "deployment/$deployment" --timeout=180s
done
if [ "$P1_ACTION" = verify ]; then
    "$PYTHON" deploy/kubernetes/p1/probe.py verify
    printf 'Evidence saved under .data/bbx-archv2-k8s; run the scoped cleanup script.\n'
else
    printf 'Dedicated fixture deployed; verification has not run.\n'
fi
