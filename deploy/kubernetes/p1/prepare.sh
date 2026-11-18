#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
STATE="$ROOT/.data/bbx-archv2-k8s"
umask 077
mkdir -p "$STATE/bin" "$STATE/downloads"
chmod 700 "$STATE"
fetch() {
    local url=$1 target=$2 checksum=$3
    if [ ! -f "$target" ]; then
        curl --proxy "${BBX_P1_FETCH_PROXY:-http://127.0.0.1:7897}" --connect-timeout 10 --max-time 120 -fsSL "$url" -o "$target"
    fi
    test "$(shasum -a 256 "$target" | cut -d ' ' -f 1)" = "$checksum"
}
fetch https://github.com/kubernetes-sigs/kind/releases/download/v0.30.0/kind-darwin-amd64 "$STATE/bin/kind" 4f0b6e3b88bdc66d922c08469f05ef507d4903dd236e6319199bb9c868eed274
fetch https://get.helm.sh/helm-v3.19.0-darwin-amd64.tar.gz "$STATE/downloads/helm.tar.gz" 09a108c0abda42e45af172be65c49125354bf7cd178dbe10435e94540e49c7b9
fetch https://codeload.github.com/opensandbox-group/OpenSandbox/tar.gz/62a64f0b48cdd6b410f36e53c74dc7559c87719a "$STATE/downloads/opensandbox.tar.gz" c2ca1bd964d5f1933f69f01d210aabfe83d8174761b1487a5b1bd5f790abdf2b
fetch https://helm.cilium.io/cilium-1.18.2.tgz "$STATE/downloads/cilium.tgz" 39b62a72f0892dd16548bd08ee97d5db2e975f7c1f57155578a144186e22992f
tar -xzf "$STATE/downloads/helm.tar.gz" -C "$STATE/downloads"
cp "$STATE/downloads/darwin-amd64/helm" "$STATE/bin/helm"
chmod 755 "$STATE/bin/kind" "$STATE/bin/helm"
if [ ! -d "$STATE/OpenSandbox-62a64f0b48cdd6b410f36e53c74dc7559c87719a" ]; then
    tar -xzf "$STATE/downloads/opensandbox.tar.gz" -C "$STATE"
fi
