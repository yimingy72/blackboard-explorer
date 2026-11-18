# P1：官方 Kubernetes 两 Task 接缝报告

2026-11-06 · GPT-6.1 Sol / max · 已通过独立定点审核，仅局部 G1/G2 证据，未发布生产能力

## 完成范围

新增 [独立部署与复现](../../deploy/kubernetes/p1/README.md)，只修改执行包的固定 Kubernetes 静态启动入口、只读启动脚本和镜像拷贝。未改 Runtime 生产路径、共享锁、P2 文件、默认 kubeconfig 或 docker-desktop CNI；未部署 BBX 业务平台、Runner、模型或靶场。

首轮在已批准的专用 `bbx-archv2-k8s` 中续跑，没有因中断重建重复集群；独立审核后的定点验证在首轮资源清理后按同配置新建一次专用运行。kind 0.30.0 / K8s 1.34.0 / Cilium 1.18.2 / Helm 3.19.0；固定官方 OpenSandbox 1.2.0 Server、Controller、Ingress、execd，具体 digest/工具校验见 [versions.json](../../deploy/kubernetes/p1/versions.json)。官方 BOM 声明最高 K8s 1.34，现有 docker-desktop 1.36.1 未用作该版本兼容证明。

一个专用节点限制 **2 CPU / 6 GiB，swap 不额外扩大**；首轮导入前节点容器存储余 **647 GiB**，宿主余 **295 GiB**；一次样本节点内存 **1.51 GiB / 6 GiB**，不推导多 Task 容量。先等 Cilium Ready，再导已有 CTF 工具层，没有盲拉完整上游组件。首轮镜像 amd64 manifest 为 `sha256:0b64dccc84152df9527ef7aec4fa51d209f8e6333480f2e0dd97bf7e974867aa`；低权限 `.tmp` 修复后为 `sha256:c35c45ce83129ea7ed5122c56b63d9c362b56d60735c07c01acf28c8aa908fe4`。

## 实际关键贯通

最终固定模板使用官方控制链创建两个 Task Namespace/Tenant，各一个非池化 Sandbox 与同名但独立 PVC。没有直接创建或接管 Sandbox Pod；测试目标只是 Pod 内只读 HTTP 夹具。

| 项目 | 已取得的真实证据 |
| --- | --- |
| 生命周期/租户 | 真实 SDK→Server→BatchSandbox→Controller→Pod；错 Key list 为 401；互用另一 Task 的 Sandbox ID get/delete 被拒。A 停止后 A 的 list 不包含 B、B 的 list 包含自身；不是只测本机假服务 |
| 两层端点鉴权 | 同一已知执行记录，有权请求 200；缺网关路由头 400；路由有效但缺 execd Token 401；篡改 `OpenSandbox-Secure-Access` 为 401。没有把两种 Token 当成一层鉴权 |
| 最终启动链 | 固定 shardPatch 的最外层静态入口清 env/FD，保留 Sandbox ID、EXECD_INIT 与受信控制变量，再 exec 官方注入 bootstrap；PID1 为官方 execd/init，注入 execd 与镜像内固定二进制 SHA 相同，官方 bin 挂载只读。未增加 Tini/第二 daemon |
| 凭据/文件 | 每 Namespace SecretRef 提供不同 execd Token，不放进 SDK create env 明文或可展示 Pod 清单；证据只存 env 名和引用。共同 UID10000 文件正常读写；真实低身份 `file_read('/proc/self/environ',offset=1,limit=1)` 不含测试执行 Token 或控制标记，只保存布尔断言，未 fallback root |
| 控制与 Solver 身份 | 最终非 privileged Pod 的可信进程具有装配、降权和中断所需的明确能力；Solver 实测 UID10000、NNP=1、effective/permitted/bounding 仅 NET_RAW；本地原始 ICMP 收到回包，setuid(0)、NET_ADMIN 建链路失败 |
| 网络 | 同 Task 的数值 HTTP 目标可达；跨 Task 目标端口和 execd Pod IP 均不可达。接收端自身可达，避免把目标未启动误记为策略成功。Cilium namespace 规则与 Pod 本地 INPUT/FORWARD 同时存在；未启用官方 Egress |
| 命令/取消 | 前后台使用原官方 execution ID，真实最终 Pod 中 interrupt 成功，父子 PID 随后均消失。原命令入账、输出和取消记录保存在控制侧审计库；不承诺任意 detached 后代覆盖 |
| 停止/保全 | 首轮确认 A 的 Pod/Runtime 收尾、PVC 仍 Bound、只读 marker 可读、B 健康与同名私卷数据不同。独审指出当时缺少停止前 UID 快照，故撤回基于事件推断的 UID 延续结论。本轮首个成功 Ready 时直接保存 A/B 的 PVC UID，分别停止后直接对比相等，见下节；不再用事件代替身份依据 |

## 必要修正与已知失败

1. Docker 29 归档保留多架构 index，但 kind 0.30 默认 `--all-platforms` 导入缺未下载平台 blob。修正为保留 Docker 原 index、仅 containerd import 指定 linux/amd64，官方 BOM 根 digest 不变，未拉其他架构。工具镜像缓存跳过须匹配真实 amd64 manifest，不能只看可变 tag。
2. 首次 A 创建的 SecretRef 条目在上游分片合并/反序列化后残留旧 env 值，Pod 被 API 校验拒绝，未启动执行。固定模板显式 `value:""` 后有效。原请求超时、Server 已回滚 CR；确认只有 FailedCreate/无 SuccessfulCreate、原 Pod 为零后保留旧 attempt，以新的明确 create_op 验证修正，不把 unknown 当未创建；未发删除 PVC 或改归属标签请求。当时没有保存该失败尝试的原 PVC UID，不能据此断言它和后续卷身份相同。
3. 初始可信能力列表漏 KILL，真实官方 interrupt 报 operation not permitted；原取消记录保留 unknown，没有重派原命令。补控制侧 KILL 后由官方 Controller 在原 Sandbox 下替换 B Pod；核验旧 Runtime 停止、新 Pod UID 和环境代次，再用新测试命令确认取消成功。Solver 未保留 KILL。
4. 初轮有限 15 分钟 TTL 正常到期；证据已保存。确认旧 CR/Pod/Runtime 停止后，在同一集群、Namespace、保留卷中以新明确操作做最终短轮，两 Task TTL 为有限 30 分钟。历史结果单独留存，不自动恢复旧调用；同时活跃的逻辑 Task 始终只有两个。
5. 排障时一次读取了上游 FailedCreate 原始事件，它包含整个 Pod 和本地产生的测试路由注解。该失败绑定已废止；后续不再输出原始事件 message，报告和提交不含 Token。生产错误事件/日志采集仍需显式脱敏，不声称上游已自动去密。

## 检查与复现

- 最终固定模板的两 Task 全链探针通过；原始 ICMP、取消、原 PVC 标记读取与鉴权断言均为真实 Pod/官方组件结果。没有调用真实模型、靶场、VPN 或已有平台。
- 新部署 Python：ruff/lint、pyright 0 错；三个 shell 入口及启动脚本语法检查通过。
- 受影响旧执行包 `make check` 通过：**5 普通测试**，格式/lint/pyright 全绿；只重跑旧前台身份/文件和静态 FD/提权两个真实 Docker 场景：**2 passed / 8 deselected**。没有重复根全量或无关网络矩阵。
- 脱敏 `evidence.json`、审计与已知失败分类保留在忽略的 `.data/bbx-archv2-k8s/`；测试凭据、KUBECONFIG 和运行资源交付前清理，官方/预存 CTF 镜像与官方下载缓存保留。

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync --frozen --project services/sandbox-execution --python 3.12
bash deploy/kubernetes/p1/run.sh
bash deploy/kubernetes/p1/cleanup.sh
```

## 独立审核后的三项定点修复

1. **保留卷启动权限边界。** 删除 root 对 `/workspace/.tmp` 的 `install/chown`。root 仅处理可信挂载根；随后先 `env -i` 清控制环境，再由现有 setpriv 降为 UID/GID10000、清组与全部能力、NNP=1，使用隔离 Python 的 `mkdir/open(O_DIRECTORY|O_NOFOLLOW)/fstat/fchmod` 创建或检查 `.tmp`。不接受 symlink，不允许低权限失败后回落 root。
2. **复用节点先验归属。** `run.sh` 在 prepare、docker update、kubelet 写入之前检查已存在节点的 kind ownership label；新建后再次检查。`tests/ownership.py` 的错误 owner stub 只收到两次 inspect，拒绝退出，未发生 update/exec 或 kubelet 写入，**1/1 通过**。
3. **直接捕获 PVC 身份。** SDK 首个成功 Ready 后保存不可覆盖的 `ready-claim-<sandbox_id>.json`，包含 Sandbox/Namespace/Pod/PVC UID；停止后直接对比该快照。删除 WaitForFirstConsumer 推断及无条件 historical failed-claim sameUID。旧 `evidence.json` 未修改，旧 UID 推断字段仅作已撤回历史，不计作通过；新结果写入独立 `affected-evidence.json`。

本轮只验证受影响路径，不重复首轮鉴权/网络矩阵。缓存工具和镜像用于专用 kind 的两 Task 小运行：

- 共同低 UID 在真实保留 PVC 中把 `.tmp` 替为指向控制区的 symlink；新的启动脚本明确拒绝；控制目录前后均 **0:0 / 0700**，owner/mode 不变。以低 UID 恢复原目录，无 root 解引用或修复。
- 安全启动后官方 background interrupt 成功，父子 PID 均消失。
- 两 Task 首 Ready 的直接 PVC UID 快照不同；分别停止后各自 UID 与其停止前快照严格相等。没有以历史事件或停止后的读取反填基线。

上述真实定点探针 **3 项断言组通过**；Python ruff/pyright、shell 语法通过。新建空运行另暴露 containerd import 没有自动创建 CRI 查询所需 canonical `repo@BOM-digest` 名称，已在验证根 digest 后补显式 alias/managed label；仅本私有 runtime 缓存适配，不改官方二进制、不拉其他架构，组件真实 Ready 后才执行定点探针。

```sh
services/sandbox-execution/.venv/bin/python deploy/kubernetes/p1/tests/ownership.py
bash deploy/kubernetes/p1/run.sh deploy-only
services/sandbox-execution/.venv/bin/python deploy/kubernetes/p1/tests/affected.py
bash deploy/kubernetes/p1/cleanup.sh
```

原普通/容器基线检查结果保留，本轮不重复未变化的旧接缝、鉴权矩阵或根全量测试。原失败现场、原始结论与新直接快照分开保存，不伪造缺失的历史 UID 证据。

上述三项修复及定点证据已获独立复核通过。本批精确提交范围未改变根 `make check` 覆盖的源码、锁或配置输入，复用 `401d7cf` 的干净候选门禁，并叠加本报告的专门检查；不以含其他 WIP 的原树替代候选快照，不重复无关全量测试。

## 剩余边界与依据

本批只是单节点两 Task 的局部 G1/G2。未验证成功创建但响应完全丢失的故障注入、跨节点/分区停止、完整租约接管、跨主机 journal/归档、完整 IPv6/DNS/TLS/代理/回连、真实 Runner/MCP/agentgateway 组合、单 VPN/可信 Lead 提交；未改 TSec 线路。informer 无界 Namespace 回收问题未修，**G7 未通过**。默认 runc 共享内核，不宣称 VM 隔离。官方未加补丁 Pi 子插件的能力边界仍以其独立报告为准。

固定官方源码依据：[BOM](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/docs/releases/1.2.0.yaml)、[K8s 注入/主入口](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/provider_common.py)、[bootstrap/init](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/components/execd/bootstrap.sh)、[Controller 分片应用](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/kubernetes/internal/controller/batchsandbox_controller.go)、[文件 worker 环境白名单](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/components/execd/pkg/runtime/filesystem_worker_linux.go)、[TenantProvider](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/tenants/http_provider.py)。

独立提交边界仅本批部署目录、必要执行包增量与本报告，标题为 `test(infra): verify isolated OpenSandbox Kubernetes seams`；不纳入 P2/P3、共享文件、原 WIP、缓存、凭据或二进制，不推送或合并。完整 G1/G2、G7 及生产开放仍按上述剩余边界处理。
