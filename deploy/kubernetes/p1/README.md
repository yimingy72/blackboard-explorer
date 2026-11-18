# P1：官方 Kubernetes 最小接缝

只验证两 Task 的基础设施合同，不部署 BBX 业务平台。专用 kind 集群 `bbx-archv2-k8s`、独立 KUBECONFIG 和所有局部工具/下载/生成值位于仓库 `.data/bbx-archv2-k8s/`。不修改 docker-desktop CNI、默认 kubeconfig、全局工具或其他容器。

固定版本和完整性见 [versions.json](versions.json)：OpenSandbox release-1.2.0 固定提交/官方 BOM 镜像；kind 0.30.0 / Kubernetes 1.34.0；Cilium 1.18.2；Helm 3.19.0。本机 docker-desktop 是 1.36.1，超出该 OpenSandbox BOM 的声明范围，因此没有拿它作发布证明。

## 最小组成

- 一个 2 CPU / 6 GiB、无额外 swap 的 kind 节点；Cilium 保留 kube-proxy、关闭 socket LB/Hubble/L7/Envoy独立组件。组件都明确 requests/limits，不使用官方合计 12 GiB 的默认请求。
- 一个官方 Server、Controller、Ingress 与只有两条本地产生测试 Key 的 HTTP TenantProvider 夹具；两 Task 各一个 Namespace、非池化 Sandbox、私有 PVC。CR/Pod 由官方控制链创建，未写新 Operator。
- 官方固定 installer 向 `/opt/opensandbox` 注入 execd/bootstrap。固定 shardPatch 的最外层静态 `--kubernetes-bootstrap` 先清理环境/FD，再 exec 官方 bootstrap；保留 `OPENSANDBOX_ID`、`EXECD_INIT=1`、测试 Token 与受信生命周期变量，官方 execd 最终成为 PID1。镜像原 ENTRYPOINT/Tini 不参与，不另启动第二个 daemon。
- 各 Namespace 同名 `bbx-archv2-execd` Secret 提供不同测试 Token。模板显式 `value: ""` 清掉反序列化可能保留的原 env 值后设置 SecretRef。创建请求不携带明文执行 Token，证据只保存 env 名称与 Secret 引用。
- 控制能力包括 setns/网络装配、降身份与官方中断所需 KILL；Solver 不获得这些管理能力，`net-raw` 档只留下 NET_RAW/共同 UID10000/no-new-privs。仅本 kind kubelet允许固定 `net.ipv4.ip_forward` sysctl。官方二进制挂载只读。
- 保留卷只有挂载根由 root 处理；`.tmp` 先清控制环境、再以共同低 UID/零能力创建和检查，`O_NOFOLLOW` 拒绝 symlink，不由 root 解引用保留卷的子路径。复用同名节点在任何 update/kubelet 写入前验证 kind ownership label。
- Cilium 按 Namespace 拒绝跨 Task/节点/管理面；Pod 内 INPUT 拒绝嵌套 Agent 到控制端口，固定 18081 仅用于同 Task 只读测试目标。原始 `networkPolicy` 不传，不启用官方 Egress；没有 VPN、真实靶场或模型。

上游 CRD、Cilium、Controller 部分对象名是协议/Chart固定名，原样放在这个独立集群；本批自建 Namespace、Docker网络、Task、Secret和发布均有 `bbx-archv2` 前缀，最终随私有集群清理，不污染现有集群。

## 运行

适用于当前 macOS Intel / Docker Desktop。先在执行接缝独立目录准备 frozen Python 依赖；不依赖 `.env`。

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync --frozen --project services/sandbox-execution --python 3.12
bash deploy/kubernetes/p1/run.sh
bash deploy/kubernetes/p1/cleanup.sh
```

`prepare.sh` 把校验后的 kind/Helm 安装到项目局部路径；下载代理默认 `127.0.0.1:7897`，可用本次进程变量 `BBX_P1_FETCH_PROXY` 调整。先等 Cilium Ready，再检查节点存储是否余 50 GiB，才导入已缓存的大 CTF 镜像。官方镜像逐项与 BOM digest 核对，不拉取快照、池、nodeagent、task-executor 或其他架构组件。

Docker 29 的多架构归档含未下载平台的描述符。`load-image.py` 保留原 BOM index，只有 containerd import 选择 linux/amd64，避免 kind 0.30 的 `--all-platforms` 缺 blob 错误；验证根 digest 后显式保留 CRI 所需 canonical repo@digest 名称与 managed label。工具镜像跳过条件核对实际 amd64 manifest，不能仅按可变 tag 存在就跳过。

`probe.py setup` 只生成两组私有测试凭据；`verify` 先查稳定标签对应的原 CR，已存在则连接；已记录创建而原 CR 缺失时拒绝自动重发。创建准入文件、只含脱敏字段的 `evidence.json` 与命令记录保留在忽略目录。正常执行包括错租户 get/delete、鉴权、低身份文件、网络、实际 raw ICMP、取消与父子收尾、停止后原 PVC/只读 marker，以及同名 PVC 的 Namespace 隔离。

每 Task 首个成功 Ready 后直接保存 PVC UID，不以 WaitForFirstConsumer 事件推断身份。独审修复只需 `run.sh deploy-only` 后运行 `tests/affected.py`，验证保留卷 `.tmp` symlink 的启动拒绝/控制 owner 与 mode 不变、取消、两 Task 的 Ready→停止直接 UID 对比；`tests/ownership.py` 为无节点写入的错误 owner stub。旧 UID 事件推断已撤回，旧证据与新 `affected-evidence.json` 分开保留。

发生已知错误时先分类/保留现场，不读原始 FailedCreate 事件正文——上游可能把整个 Pod（含路由凭据注解）写在其中。`postverify` 仅供已保存两 Task 证据后、Task B 仍在有效 TTL 内的收尾续验；不会创建新 Task。TTL 到期不可当作原调用没发生，更不能删 admission 再盲试。

`cleanup.sh` 检查 kind 所有权标签后只删除本批集群、专用网络、自建工具镜像和测试凭据；保留官方/既有 CTF镜像及官方下载缓存。旧接缝测试另建的两个 `bbx-archv2-sandbox-seam*` 标签由测试执行者收尾清理。不执行 Docker prune。

## 验证边界

实际结果与排障的已知失败见 [本批报告](../../../docs/tasks/P1-Kubernetes接缝-20261106-report.md)。这是单节点、两 Task 的局部 G1/G2 证明；跨节点、网络分区、成功创建但回执丢失、永久归档、完整 IPv6/DNS/TLS/代理与回连、真实 agentgateway/Runner/MCP 组合、单 VPN/可信提交仍未通过。Server Namespace informer 的无界回收问题未修，不据此宣布 G7。
