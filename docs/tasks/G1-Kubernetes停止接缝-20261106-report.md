# G1 Kubernetes Assignment 停止接缝报告（2026-11-06）

状态：18 路径 SHA、ownership 前置、拒写 stub 与报告均已获 traffic_image_review 最终定点独审通过，现获准按冻结范围独立提交。actual developer：GPT-6.1 Sol / max。仅新增 `deploy/kubernetes/g1/` 与本任务两份文档，执行包相对 `d5c84059c55864a84a3efdd09c4ab3477268a318` 无字节变化，旧 P1/G7 未改。

## 1. 完成范围与固定合同

采用已审 G1 静态启动器与 SDK 薄接入，未改官方 execd/Server/Controller/Ingress。固定 OpenSandbox 1.2.0 BOM、kind v0.30.0、Kubernetes v1.34.0、Cilium 1.18.2。官方 Server 仍使用原发布镜像；本次只有一个 namespace，不复建 G7 patch，也不声称测了 Server 历史 namespace/租户规模。

新的固定 Task template 使用 `--kubernetes-bootstrap-cgroup-v2`，`BBX_POD_UID` 是 Downward API `metadata.uid`，`BBX_ENVIRONMENT_REVISION` 是模板常量 `bbx-g1-k8s-environment-1`；模型命令未传这些字段。启动器在真实 Pod 生成 nonce，可信 `read_execution_context` 登记后冻结入 Binding。valueFrom 项保留显式空 value，避免固定旧 Go JSON patch slice 元素残值。

执行 Token 同 namespace SecretRef，Server/Ingress 签名 Secret 与租户文件 Secret 都是本地新生成测试值。一个官方静态 file tenant 避免新增 HTTP 租户服务；Task 文件仍由官方 UID/GID10000 文件 API 访问。保留官方 bootstrap、Sandbox ID、EXECD_INIT=1 与 secureAccess 正向控制链。没有启用默认 Egress，没有模型、靶场或 VPN。

控制容器保持原 P1 固定 ceiling 与 AppArmor/seccomp 例外；最终 Pod 的 privileged、hostPID、hostNetwork、hostPath 均为 false/不存在。运行时检查只访问本批精确 PodUID/CID：先核 kind ownership、专用 context 和映射 API endpoint，再核 CRI PodUID/容器名，最后只保留 namespace/布尔信息。原始 CRI（含 env）没有落盘或输出。

## 2. 实际验证

本批独立集群 `bbx-archv2-g1-k8s`，2 CPU/6GiB；实际节点为 amd64、Debian 12、LinuxKit 7.0.12、containerd 2.1.3、kubelet v1.34.0。Cilium 1.18.2 Ready 后先检查本批控制 Pod 的 private cgroup namespace，再核真正 Sandbox；没有更改 runtime cgroupns、额外授权或回退到 host 树。

导入 CTF 大层前，实际节点磁盘约 633GiB 可用，超过 50GiB 门槛；复用既有 17.3GB CTF 层，仅构建任务 tag `bbx-archv2-sandbox-g1-k8s:20261106`。Cilium 核心镜像未缓存时只拉取固定 digest 的 amd64；其余官方组件/源包/工具复用并校验。导入不要求无用架构 blob，显式 canonical digest alias 与实际 BOM 一致。

从仓库根目录复现：

```bash
python3 deploy/kubernetes/g1/prepare.py
services/sandbox-execution/.venv/bin/ruff format --check deploy/kubernetes/g1
services/sandbox-execution/.venv/bin/ruff check deploy/kubernetes/g1
services/sandbox-execution/.venv/bin/pyright --project deploy/kubernetes/g1/pyrightconfig.json
PYTHONPATH=deploy/kubernetes/g1 services/sandbox-execution/.venv/bin/python \
  -m unittest discover -s deploy/kubernetes/g1/tests -p test_fixture.py
bash -n deploy/kubernetes/g1/run.sh deploy/kubernetes/g1/cleanup.sh
bash deploy/kubernetes/g1/run.sh
bash deploy/kubernetes/g1/cleanup.sh
```

Ruff 格式/lint、独立 Pyright（0 errors）、两个 shell 语法通过。2 项专门静态测试通过：固定 JSON template/官方 Helm chart 渲染合同；同名 foreign node 在准备或更新前即拒绝。真正 Kubernetes 场景首次完整贯通成功，输出以下 5 项 true：

| 实际场景 | 证据 |
| --- | --- |
| 真实低身份/文件/环境/caps/FD/netns | Solver UID/GID10000、空 supplementary groups、basic CapEff/CapBnd=0、NNP=1、FD≤4、控制变量不在 solver 环境；CG 路径匹配冻结 scope，worker-a 地址 198.19.1.2；低身份共享文件读写及 file worker environ 不含测试 Token |
| 原 Pod 的 setsid/double fork 后代停止 | 背景启动回执和原 ID 保留；整 Assignment 控制完整终态、nonce 匹配，seal=true/populated=0，scope namespace 与最终 Pod CRI 相同，心跳停止 |
| 一次 Controller 恢复 | 仅删除本批原 Pod，原 CR/Sandbox 不重建；新 PodUID/newnonce 显式登记，旧 binding 新命令返回126、原 scope 得到 UnknownAssignmentCleanup，新 binding 的短命令及 stop 成功；未把404当完成 |
| 替换留卷 | 首次 Ready 保存 PodUID 与 PVC UID；恢复后直接比较同 PVC UID，旧共享文件由低 UID API 读回 |
| Sandbox stop 留卷 | 删除 Sandbox/CR 后直接查询原 PVC，仍为相同 UID 的 Bound claim；之后再清理整个测试集群 |

审计共 3 个命令准入/原 execution ID、3 个控制执行绑定、2 个停止准入、2 个 confirmed 与1 个 unknown；与初始背景命令、旧绑定拒绝、新绑定短命令及原 scope unknown 对应。没有 SDK/launcher 源码修正，没有再跑 NET_RAW/全工具/多租户矩阵。

独审后修复一个顺序缺陷：原脚本只核 context 名称便执行 Cilium Helm/CRD 写入，直到 `probe.setup` 才核 API endpoint。现把既有 `owned_cluster` 校验放到节点建立/复用后、节点更新以及任何 Helm/kubectl 写入之前。新增“同 context 名但错误 endpoint”隔离 stub 1 项通过（约 1.972 s），确认确实走到 endpoint 拒绝原因，且节点/Helm/kubectl 写入计数为零；Ruff/Pyright/bash-n 通过。仅修预检顺序，原真实 Pod/替换/PVC 证据未重跑或改写，不把 stub 当第二次真实验证。

可提交的脱敏证据在 `deploy/kubernetes/g1/evidence/20261106.json`，只含本地测试身份、Secret 引用名称、布尔与 pin；不含 nonce 值、Token、签名或原始 CRI env。本机完整受保护观察在 `.data/bbx-archv2-g1-k8s`。创建/命令 attempt 在 dispatch 前落盘，恢复不盲重发。

## 3. 清理与未验证

- 已执行本批 scoped cleanup：删除本批 kind/空网络、自建任务 image tag、生成测试凭据与专用 kubeconfig；官方/CTF/Cilium/工具缓存保留。默认 context 仍是 docker-desktop，其他 cluster/CNI/业务平台未改。
- **这是已测组合下 G1 Assignment 命令 scope 在最终 Kubernetes Pod 的局部通过；不宣称完整 G1/G2 通过。** Cilium 是已固定部署背景，未重跑完整网络隔离、DNS/TLS、UDP/回连、原始报文、VPN/可信出口、跨任务或审计 UI。
- Server 单 namespace 静态租户，不测试 informer/cache 规模；生产 Operator/Controller 生命周期、长时负载与 G7 全局结论不在本批。
- 不包含在飞文件 worker、外部 Agent loop、平台席位回收或整个靶场生命周期；unknown 仍不能据此释放席位。只核一次 PodUID 改变的 Controller 恢复，同 PodUID 的容器重建沿用前批 Docker nonce 证据，本批未另加重启矩阵。
- private cgroupns/mount/STATX 只对这里的真实运行时成立。脚本遇不支持会 unsupported 停止，不修改 runtime/权限；不得外推到所有 Kubernetes/CRI 环境。

## 4. 下一步与提交建议

新目录与两份任务文档共18路径，最终独审确认 SHA、ownership 前置与新拒写 stub，无剩余阻塞；原真实证据不变。获准独立提交，标题 `test(infra): verify assignment cleanup in isolated Kubernetes sandbox`，正文按 AGENTS 并注明 `Actual developer: GPT-6.1 Sol / max`。不 push/merge，不夹带 P3c 或其他 WIP。

root 已核本批代码/部署均不在根 Makefile/pyright/pytest 覆盖范围，未改共享依赖/锁，因此复用 `522a09c3ef3b098cee474959ab21029e37610d46` 干净候选已通过的根门禁（2615项测试、Pi 5项），配合本批原2项模板/归属检查、增量拒写 stub、静态检查及真实单 Pod/一次恢复证据。不重复根或 Kubernetes 矩阵，没有把原树旧 WIP 声称全绿。
