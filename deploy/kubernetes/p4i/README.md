# P4i：逐 Agent 网络归属和席位复用

用审过的干净候选，在仓库根目录运行：

```sh
python3 deploy/kubernetes/p4i/infra.py
python3 deploy/kubernetes/p4i/build_images.py --candidate <clean-candidate>
PATH="$PWD/.data/p4i-native-seats-traffic-20261108/k8s/bin:$PATH" \
  <clean-candidate>/.venv/bin/python deploy/kubernetes/p4i/probe.py --candidate <clean-candidate>
```

探针使用隔离的 kind、实际 Cilium、唯一 Gateway EndpointSlice、固定 Agentgateway/官方 OpenSandbox 和脚本假模型。Lead、原 Worker、前台 helper 和同席新 Worker 通过四个受信独立 veth 到同一隔离 IPv4/TCP 假目标，核对控制入口、未授权端口和跨 Agent 访问均未放通。用户原只读包查询读取四份不同源的归档；原 Task 正证停止并保留两个 PVC。创建时固定 15 分钟截止，首次业务观察上限 660 秒。异常留在 `.data/p4i-native-seats-traffic-20261108/k8s/joint/` 供诊断；用 `--stop-only` 核对同一 Task 的停止，不重放。经过首次启动修正的本轮实测单独保存在 `k8s/joint-fixed/`；此目录由构建和探针的 `--revised-startup` 共同选择，保留前次证据和镜像身份。四份合成 PCAP 和读回的索引在该目录的 `sealed-parts/`、`sealed-indexes/`。

本片冻结 `binding-pcap-v1` 和 required 采集。普通停止给独立归档至多 20 秒，仍受原 Task 硬截止限制；未确认内容保持未知。仅原 veth 的封存 PCAP 有归属：每包最多 512 字节，单源至多 4 MiB，截断显式记录。内核用双向单调过期，配合保守 UTC 绝对上界；启用环境的可信节点与控制面须维持有界时钟偏差。探针不调用真实模型、靶场或默认 Kubernetes context。

可选的聚焦根进程检查：用同一 Ubuntu 工具层构建独立镜像（`--build-arg ASSIGNMENT_NETWORK_V1=1`），在私有 Docker `--network none` 容器以 `deploy/kubernetes/p4i/root_probe.py` 为入口，提供 G1 所需的 `NET_ADMIN`、`NET_RAW`、`SYS_ADMIN`、`SETPCAP`、`DAC_OVERRIDE`、`KILL` 与 net.ipv4.ip_forward=1。它单独验证内核授权、根捕获封存、同摘要确认与原来源不可重开。
