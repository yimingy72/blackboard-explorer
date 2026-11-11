# P9 · 平台全量本地 Kubernetes

把当前唯一站点的应用、共同 Runtime、PostgreSQL 和 MinIO 部署到现有本地 Kind。继续使用同一集群内的 OpenSandbox controller、ingress、Cilium、TUN 以及已发布的执行镜像。保留 `127.0.0.1:15800` 登录入口、账号、任务、配置、预算、账本与对象；两种任务仍由一个 Runtime 监督。迁移不更改全局 Docker 设置、任务工具、执行网络选择、费用语义或用户并发配置。

## P9.1 · 服务、镜像与持久卷

- 使用显式 kubeconfig，确认既有 Kind 的节点、集群 UID 与 Cilium 就绪状态。平台放在 `bbx-platform`。应用和 Runtime 各一个 Deployment；Runtime 使用 `Recreate`、独立身份、有限的优雅退出时间和现有 PostgreSQL advisory lock + epoch。Runtime 只有持锁且本地 API 可用时才接流量。应用在数据库迁移完成并提供登录接口后才接流量。
- PostgreSQL 16/pgvector 与 MinIO 各一个 StatefulSet、ClusterIP Service 和独立的 `Retain` PVC。保留已有 MinIO 版本；备份可独立于 Kind 节点读取。现 local-path 路径在节点 `/var` 卷内，节点和卷回收仍须走数据恢复流程。用独立的测试数据验证两个 Pod 重建后数据未丢失。
- 锁定 Linux/amd64 运行镜像。应用和 Runtime 从同一受审源码提交构建，记录 Git 树、源码归档及 OCI manifest digest。导入当前节点后核对实际 CRI digest 和运行的 Pod imageID。Runtime 镜像包含固定版本并校验 SHA-256 的 Linux `kubectl`。平台容器以非 root 身份运行，按需用空目录存临时数据。应用、对象存储和 TLS 代理无 Kubernetes API 凭据。
- 节点报告 4 核、约 15.6 GiB，外层 Kind 容器实际限额为 2 核、6 GiB，当前常驻控制面约 1.6 GiB。以外层限额规划平台内存、探针与两任务并发；保留 `MAX_RUNNING_TASKS=2`。如果受限于实际资源，记录测量和需要的专属节点资源调整，不以降低并发、重建集群或改变其他服务来掩盖限制。

## P9.2 · 内部控制和任务隔离

- 应用控制与 Runtime 模型代理使用各自的内部 Service DNS 和 TLS，证书 SAN 与主机名匹配，调用端装载相同受信 CA。只通过既有 Secret/API token 验证身份，不把供应商凭据下发给 Runner、沙箱、代理或应用前门。证书、口令及运行令牌通过最小范围的 Secret 引用或挂载传入，不写入源码、普通日志或回执。
- 在 `bbx-local-native` 建一套 P9 专属 OpenSandbox Server Service 和 Deployment，复用已运行的固定 Server 镜像、controller、ingress 和签名 Secret 的引用。其 tenant resolver 指向 P9 应用的内部 HTTPS；原 P8 Server 在切换前继续使用旧回调。把 P9 Server 广告的 execd 地址从宿主机转发改为集群 ingress DNS 和服务端口。当前 SDK 用同一协议访问 Server 与 execd，保留两者受 Cilium 限定的集群 HTTP，并继续核验 tenant token、签名头及 execd token；OpenSandbox 到平台的认证回调和两条 Runner 控制链路保留 TLS 与证书验证。
- 为受管 Runner 加窄 Cilium 规则，只允许经 `bbx-platform` 的两个 TLS 服务后端身份和端口访问控制面；目标侧也只接受所需的 Runner、应用、Runtime 或 OpenSandbox 来源。Runtime 至受管 Runner 的 sandbox-host `8766` 和 Runtime/Runner 至现有 execd ingress 按工作负载标签与端口限流，应用、数据库、MinIO 与 Runtime 仅开放各自所需的链路。普通 Pod 的 CoreDNS 与 Blackboard 执行环境现有公网 DNS/路由是两条路径，后者继续使用已发布的 `223.5.5.5` 及现有 allow/deny。保持 CTF 按需网络、Blackboard 强制网络、逐 Agent 捕获和只读命令边界；不改集群全局 Cilium 模式。
- Runtime 仅使用投影 ServiceAccount 令牌、集群 CA 与内部 API 地址。集群级权限仅用于受管任务 Namespace 引导以及必要的非秘密元数据。创建 Namespace 后，通过固定 RoleBinding 授权该任务命名空间的资源操作。Kubernetes admission 按 Runtime 身份验证任务命名/归属，限定 RoleBinding 唯一主体与唯一允许的 ClusterRole，并约束任务 Pod、PVC；禁止任意 hostPath、主机网络/进程空间、privileged、额外令牌、显式 PV 预绑定或任意数据源。VPN 只保留固定镜像和容器所需的 `NET_ADMIN`、TUN 与已发布启动约束。没有运行级 cluster-admin、跨命名空间 Secret/Pod exec 或自动退休的全局写权限；当前生产未开启自动退休。
- 本机 Kind 原端口映射只有 Kubernetes API，当前不能直接从 macOS 用 NodePort 取代 `15800`。保留平台内 Service；本机以显式 kubeconfig 对该 Service 使用仅回环地址的 `kubectl port-forward` 接浏览器，断线可重连。应用、Runtime、数据库、存储和 OpenSandbox 不使用该转发，断开它不影响平台运行。正式后关闭旧 `15811`–`15819` 的所属转发和存储中继。

## P9.3 · 发布与必要验收

先用 P9 将原地升正式的 Service、DNS、CA、镜像、Runtime 资源配置及授权策略连接独立的合成 PostgreSQL、MinIO 和模型/MCP 目录。待复核的 P9 acceptance 与发布文件绑定同一资源配置 digest；不以旧 P7 物理验收或另一套 URL、CA、命名空间的测试冒充新拓扑验证。保留 P7/P8 回执原字节与镜像身份。预验期间 P8 继续服务，合成 tenant 无权访问 P8 的资源和管理命名空间。

两种模式只用无害合成任务和受控网络服务，核验实际 Pod 启动、调度、调用账本、停止/归档、Agent 流量归属和涉及的网络链路。测正向 HTTPS 与证书或身份拒绝，验证 Runner 可访问必要的控制服务、沙箱不能访问平台或 Kubernetes API，并验证 Runtime 无法在非受管命名空间绑定角色、读取 Secret 或 exec。合成模型地址若只可集群内部解析，仅预验夹具可在隔离配置中使用精确测试 seam；生产入口不得允许绕过既有公网供应商边界。保留一套原始镜像和正式启动方式的就绪、身份、锁、管理接口及配置读回证据。不发真实模型或第三方 MCP 请求。

## P9.4 · 数据与正式切换

由唯一数据/切换负责人记录源数据、账号和模型/MCP 配置的核验方式，检查任务、清理、供应商调用以及待处理操作达到安全切点。冻结生产写入，停止 P8 共同 Runtime 并读回持锁者清零；制作并验证独立、可恢复且带校验的 PostgreSQL 与 MinIO 备份。恢复生产数据到专属 P9 PVC，核对 PostgreSQL schema、角色与对象身份、账本/汇率/未知费用、对象或版本及已保存设置。保留原容器和卷供回退。候选合成数据及其模型、MCP 和令牌不进入生产数据。

在不更换 P9 已验配置的 Service、DNS 或 CA 的情况下连接已恢复的生产数据与既有认证，启用生产入口与唯一正式 Runtime。对照新 Pod 与 OCI 证据，确认锁恰有一个且 epoch 单调、两种创建能力、原账号、配置页面、历史与存储读回。更换候选配置后复核正式 Pod 就绪、证书与真实 API 连接；只读验收不创建任务或访问供应商。退役旧宿主进程、旧站点容器和 P8 OpenSandbox Server，保留停止的实例、PVC 与备份。

只有在生产写尚未开放，且证据确认没有 P9 新生产写入或任务资源时，才允许直接恢复旧服务。如果 P9 已接受生产写，保留 P9 权威数据，重新冻结并核对增量，再按独立恢复方案操作。不能直接切回旧数据库或让两侧 Runtime 各持自己的数据库锁运行。

## P9.5 · 工作分片与验收记录

1. 平台部署：镜像、manifest、Service/CA、Runtime 任务授权、Cilium、P9 Sandbox Server 和浏览器接入。先做配置/授权的定向检查与集群服务端 dry-run，再在当前集群预验。发布编排和应用目录行为维持原合同。
2. 数据与发布：唯一负责人掌握隔离验证数据、生产备份/恢复、切点与退出状态。向平台部署者提供不含秘密的 schema、数据量、卷与证据合同。按实测形成 P9 发布和版本证明。
3. 独立复核：核对网络闭环、权限边界、制品、两个模式与新数据写边界。完成后记录执行的必要检查、实际结果、偏差与待决、接入方式和 Pod 重建/回退说明；P9 任务报告不把没有完成的演练写成通过。
