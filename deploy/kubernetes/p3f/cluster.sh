#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
cd "$ROOT"
STATE="$ROOT/.data/p3f-k8s-20261106"
CLUSTER=bbx-p3f-k8s
NODE="$CLUSTER-control-plane"
umask 077
if docker inspect "$NODE" >/dev/null 2>&1; then
    test "$(docker inspect "$NODE" --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}')" = "$CLUSTER"
fi
export KUBECONFIG="$STATE/kubeconfig"
export HELM_CACHE_HOME="$STATE/helm-cache" HELM_CONFIG_HOME="$STATE/helm-config" HELM_DATA_HOME="$STATE/helm-data"
if ! docker inspect "$NODE" >/dev/null 2>&1; then
    KIND_EXPERIMENTAL_DOCKER_NETWORK=bbx-p3f-k8s-network "$STATE/bin/kind" create cluster --name "$CLUSTER" --config deploy/kubernetes/p3f/kind.yaml --kubeconfig "$KUBECONFIG" --wait 0s
fi
PYTHONPATH=deploy/kubernetes/p3f python3 -c 'from common import owned_cluster; owned_cluster()'
docker update --cpus 2 --memory 6g --memory-swap 6g "$NODE" >/dev/null
docker exec "$NODE" /bin/sh -c 'if ! grep -q "net.ipv4.ip_forward" /var/lib/kubelet/config.yaml; then printf "\nallowedUnsafeSysctls:\n- net.ipv4.ip_forward\n" >> /var/lib/kubelet/config.yaml; systemctl restart kubelet; fi'
python3 deploy/kubernetes/p3f/load_images.py control
"$STATE/bin/helm" --kubeconfig "$KUBECONFIG" upgrade --install bbx-p3f-cilium "$STATE/cilium.tgz" -n kube-system -f deploy/kubernetes/p3f/values/cilium.yaml >/dev/null
"$STATE/bin/kubectl" --kubeconfig "$KUBECONFIG" -n kube-system rollout status daemonset/cilium --timeout=180s
"$STATE/bin/kubectl" --kubeconfig "$KUBECONFIG" wait --for=condition=Ready node/"$NODE" --timeout=90s
"$STATE/bin/kubectl" --kubeconfig "$KUBECONFIG" apply -f "$STATE/upstream/kubernetes/config/crd/bases" >/dev/null
python3 - <<'PY'
import json,sys
sys.path.insert(0,'deploy/kubernetes/p3f')
from common import NODE,STATE,owned_cluster,kubectl
identity=owned_cluster()
node=json.loads(kubectl('get','node',NODE,'-o','json'))
(STATE/'cluster-identity.json').write_text(json.dumps({'container_id':identity,'node_uid':node['metadata']['uid'],'node_name':NODE,'version':node['status']['nodeInfo']['kubeletVersion']},indent=2))
print('Owned Kubernetes 1.34 node, Cilium and official CRDs are ready.')
PY
