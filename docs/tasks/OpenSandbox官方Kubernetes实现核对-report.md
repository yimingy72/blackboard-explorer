# OpenSandbox 官方 Kubernetes 实现核对与平台重构报告

> 2026-11-06 当前开发入口为[平台架构重构开发方案](平台架构重构开发方案-20261105.md)。三路第二轮审核通过进入 P0/P1；以下为前轮固定源码核对记录，不表示 G1–G7 已验证，冲突选择按当前入口和正本覆盖。

2026-11-05。本轮根据官方实现修订文字架构，并由三位 gpt-6-astra / max 子代理分别复核官方接入、网络权限、Runtime与产品合同；不开发、不部署，按用户要求暂不更新拓扑。

统一设计入口：[平台底层架构改造方案](平台底层架构改造方案.md)。环境合同：[OpenSandbox 接入方案](OpenSandbox接入方案.md)。本报告记录依据与修正，不另建一份架构正本。

## 1. 核对范围与基线

OpenSandbox `release-1.2.0`，源码提交 `62a64f0b48cdd6b410f36e53c74dc7559c87719a`。逐层核对 Server API/schema、Kubernetes service/provider、模板合并、租户、PVC、官方 MCP、Controller/CRD、Helm 和相关测试源码。没有把 README 的能力描述直接当成端到端验证。

平台侧核对现有 `execenv/manager.py` 的 Docker 创建、网络 relay、envd 凭据、逐成员用户与流量采集耦合，以及当前 SDK/网关/黑板设计。已有开发 WIP 不变。

## 2. 源码确认与架构含义

| 项目 | 已确认实现 | 平台因此如何设计 |
| --- | --- | --- |
| Kubernetes 创建链 | SDK 调 Lifecycle API；Server 生成 BatchSandbox CR；Controller 创建 Pod。Provider 固定该创建路径的 replicas=1 | Sandbox Pod 只由官方控制链创建；不把 Worker 数写成 replicas，不自研第二个 Sandbox Operator |
| 官方 TaskScheduler | 处理 taskTemplate 中的进程任务与其启动/清理 | 不替代黑板认领、LLM 会话、Flag 验收；BBX 只保留业务调度 |
| 租户范围 | HTTP provider实际tenant.name=namespace；请求解析namespace且get/list/delete校验所有权；旧无标签资源兼容可见 | 每Task唯一Namespace，不用同namespace不同HTTP Key冒充不同租户；不混入旧无标签资源 |
| MCP 范围 | 同一个 MCP 实例共享 ConnectionConfig 与 Sandbox 字典 | 每 Task 一个官方 MCP 实例，使用本 Task 租户 Key；网关按 Task 路由，不能只分 MCP session |
| 创建参数 | image、requests/limits、env、metadata、volumes、networkPolicy、secureAccess、lifecycle 等；templateId 指 fast-sandbox 模板 | BBX 环境 Profile 编译为支持字段；不伪造每请求任意 PodTemplate API，也不把 templateId 当 Kubernetes YAML 模板选择器 |
| 创建重试 | create_sandbox 每次生成新 ID；SDK 在取得 ID 后初始化失败会尝试 kill，但响应完全丢失时仍可能不知 ID | create_op_id 放可信 metadata，超时按本租户查询对账；不直接重试 create；查证歧义时暂停 |
| 普通模板合并 | Server 启动加载一份 batchsandbox_template_file；字典深合并，运行值优先，列表替换；只专门补回部分卷/挂载/主容器 securityContext | 不能把额外容器直接塞进 template.spec.containers 后宣称会保留；不允许 Lead 改 Server 全局模板 |
| 官方 Pod 扩展点 | CRD 有 spec.shardPatches；Controller 在创建 Pod 前使用 Kubernetes StrategicMergePatch 应用对应分片补丁 | 单副本可用 shardPatches[0] 添加固定、受控的任务网络容器；无需改官方 Operator；跨组件装配仍待验证 |
| Egress 权限 | 请求 networkPolicy 才添加 sidecar；普通 Linux 主容器丢弃 NET_ADMIN；Egress 获得 NET_ADMIN；默认禁 IPv6 的 execd init 使用 privileged | 不把所有 Pod 标记 restricted 后假定可运行；按固定镜像和角色配置准入例外，验证 CNI/RuntimeClass；不将全部求解命令提权 |
| 官方上游代理 | egress.upstream_proxy 是 Server 级、管理员配置，作用于被 mitmproxy 处理的流量；不是每任务/每请求代理，且不是完整 UDP/QUIC/L3 VPN | 不用修改全局代理满足某一任务；任务连接条件交给受控任务网络附件；不能把 HTTP CONNECT 当完整 VPN |
| PVC | 官方支持 createIfNotExists；只有显式 opt-in 的托管 PVC 才随 Sandbox 删除；有所有权检查与 GC/补偿逻辑 | 工作 PVC 复用官方创建，deleteOnSandboxTermination=false，保证计算退出不先删证据；任务归档完成后平台再回收 |
| 停止与回收 | 过期 CR 进入删除；有 taskTemplate 时使用 cleanup finalizer；普通 command_run 不自动变成该 TaskScheduler 的任务 | 复用官方资源销毁，但平台仍负责模型/命令 drain、结果归档与业务状态；不能把 finalizer 当 Agent 全链路停止证明 |
| 恢复 | 未曾 Ready 的启动故障有界替换；缺失副本可按模板补建；默认 restartPolicy=Never | K8s 补回 Pod 不等于保留内存/进程；记录 Pod UID 与环境代次，变化后暂停新执行并核对，不继续旧命令或盲重放 |
| Pool | poolRef 与每次请求的 volumes/networkPolicy 等互斥；PoolService 使用固定 namespace | 第一版非池化，不用跨 Task 共享 Pool 换启动速度；先做镜像预拉和并行准备 |
| Server/配置 | Helm Server使用Recreate；配置启动时解析；一般多活未承诺 | 一个活动Server；升级状态可见，不默认HPA |
| Informer生命周期 | Server按资源/namespace建常驻watcher，仅有全部停止，没有已删除namespace回收 | 保留Task namespace边界，增加窄上游修补/升级门槛；不为规避问题增加新的权限或模板系统 |
| Helm默认预算 | Server requests=4Gi；Ingress默认2副本，每个requests=4Gi | 默认合计12Gi尚未含BBX/Task；本机不能照搬，显式小规模values，资源数字待测 |

主要代码依据（固定提交，行号为本轮读取版本）：

- [Kubernetes provider：生成 Pod/CR](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/batchsandbox_provider.py#L131)、[列表合并与模板附加字段](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/template_manager.py#L75)。
- [官方 shardPatches 类型](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/kubernetes/apis/sandbox/v1alpha1/batchsandbox_types.go#L102)、[Controller 应用补丁](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/kubernetes/internal/controller/batchsandbox_controller.go#L681)、[上游相关测试](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/kubernetes/internal/controller/batchsandbox_controller_test.go#L275)。测试源码已读，未运行其 envtest。
- [租户 namespace 解析](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/kubernetes_service.py#L316)、[所有权校验](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/workload_access.py#L39)、[HTTP 租户缓存](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/tenants/http_provider.py#L104)。
- [创建与 UUID](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/kubernetes_service.py#L1036)、[PVC](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/kubernetes_service.py#L725)、[请求字段](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/api/schema.py#L936)。
- [Egress 组装与权限](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/egress_helper.py#L46)、[上游代理范围与限制](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/docs/guides/egress-upstream-proxy.md#L12)。
- [TTL/删除优先](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/kubernetes/internal/controller/batchsandbox_controller.go#L129)、[删除收敛](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/kubernetes/internal/controller/batchsandbox_deletion.go#L32)、[启动故障恢复范围](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/kubernetes/internal/controller/pod_recovery.go#L284)。
- [Pool 固定 namespace](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/pool_service.py#L43)、[互斥参数](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/kubernetes_service.py#L611)。
- [Server Helm 模板](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/manifests/charts/server/templates/deployment.yaml#L22)、[官方部署限制](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/docs/deployment/index.md#L128)、[官方 MCP 状态](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/sdks/mcp/sandbox/python/src/opensandbox_mcp/server.py#L49)。

## 3. 相对上一轮候选方案的修正

保留一任务一 Namespace/Tenant、任务专属 MCP、可信 Runner 与 Sandbox 分离；它们是本平台选择，不能说成 OpenSandbox 自动提供 BBX 集成。

修正“任意 sidecar 通过模板直接附加”为“经官方 shardPatches 添加管理员固定的 task-link”。最终补丁可覆盖整个Pod，Server的前置检查看不到其全部效果，因此验收最终Pod清单，不能将补丁当安全受限的用户API。

三方讨论后选择两个Pod、一个外部VPN：可信Runner包含Pi、官方MCP、task-network；Solver包含工具/execd、Egress、task-link。VPN和可信提交终止于Runner网络，Solver只通过WireGuard三层隧道使用出口。这样可支持Benchmark明文HTTP和单VPN会话约束，不把Token流量暴露在Solver共享netns。新增的是固定网络装配与配置监督，协议复用成熟软件，不写新Operator。

待机配置必须使组件健康；隧道/VPN就绪另存。将VPN连通放入Pod readiness会形成创建等待Lead的环，也可能在VPN断线时让官方Ingress拒绝execd修复入口。实际控制/转发/DNS/MTU、密钥挂载、能力与采集仍需集群验证。

DNS通过固定shardPatches按name合并egress的env设置OPENSANDBOX_EGRESS_DNS_UPSTREAM，指向内部隧道对端；该变量接受IP。不存在[egress.env]公共配置字段，评审中这一错误建议已经核对并撤回。官方upstream_proxy也不是每Task全协议出口。

NET_RAW对CTF工具有实际用途，不因一个同netns策略接缝就自动取消能力；也不能因此将Egress宣称为不可绕过边界。最终跨任务/控制面隔离由Pod外Cilium和可信出口内层过滤承担。默认runc不宣称VM级抗逃逸，Kata列为加固部署选择。

MCP允许当前Task/环境代次的connect_if_missing懒连接。预登记只做启动预热；不再把该参数当安全硬禁或实现复杂全局登记引擎。保留当前Sandbox绑定、Task target、角色、实时停止/调用准入与网络入站约束。

将 PVC 保全、创建结果未知、Pod UID 变化、停止与 Namespace 回收顺序补入正式合同。将共享 MCP 的复杂文件路径/owner/group 参数 ACL 删除；保留 Task→MCP target、角色工具和实时停止准入。目录不作任务内安全边界。

## 4. 平台应保留的实现量

保留任务/黑板/模型预算/验收与配置发布；Pi SDK、pi-subagents、agentgateway 的分工不另起一套。

基础设施适配分为现有 Runtime 内的两部分，不拆新服务：

1. Task 运行资源：Namespace、配额、网络策略、Runner Pod/Service、身份和会话卷，按固定模板创建、观测和回收。
2. OpenSandbox adapter：官方 SDK 的创建/查询/连接/续期/中断/归档/销毁，以及 Task 绑定与结果未知对账。工作卷创建复用官方 PVC 支持。

官方管理 Sandbox CR/Pod、execd、Egress、TTL、基础启动恢复；平台不直接改这些 Pod，也不复制官方池调度。task-network/task-link是本项目仍需实现的窄网络接口；另有官方informer生命周期的小型上游修补/升级门槛。不能伪称官方已包含完整VPN管理或修复已落地。

## 5. 完成、验证与未验证

- 本轮更新主架构稿、OpenSandbox 接入稿和 CTF 对接边界；旧图集仅标注待同步，不绘图、不重导出。
- 运行一次离线结构检查：提取已审阅的官方纯 `_deep_merge/_deep_copy` 方法，确认 runtime containers 替换模板列表、shardPatches 保留、输入不被修改。结果 PASS。
- 该检查不等于 Kubernetes StrategicMergePatch、准入、Controller、网络或 VPN 联调通过。实际集群检查、SDK/plugin/MCP 全链路和性能均未运行。
- 交付检查已完成：8份文档、78个本地引用、代码围栏均无错误；跨稿的K8s后端、MCP懒连接和五个工作状态抽查通过。既有tracked代码diff的SHA-256仍为fd25d1963eaec356c435ca5cad1b26e143d63f10af8bef315212d56b23468450，应用代码未改。未读取.env、部署、调用平台真实模型做测试、提交或推送。

下一步先审核文字中的组件归属、网络附件与凭据通道。开发时只做三个关键贯通验证：两 Task 的路由/租户/Pod IP 隔离；MCP 重启与创建/停止/留卷恢复；TSec 的 VPN、提交与采集。其余沿用既有 fake provider/fixture，不新增重复测试矩阵。

## 6. Astra max 联合评审的采用结论

| 议题 | 采用结论 | 主要取舍 |
| --- | --- | --- |
| 每Task命名空间 | 保留；HTTP tenant_ref=namespace；补官方informer有界回收 | 避免共享Namespace下租户/同名Secret混用，不另写权限系统 |
| 网络终止位置 | VPN与可信proxy在Runner；Solver仅内部隧道 | 增加一跳与两个固定网络容器，换取单VPN且HTTP Token不经过Solver |
| MCP恢复 | 允许当前绑定的官方懒连接，预登记可预热 | 不恢复逐文件ACL或全局MCP登记编排 |
| 恢复源 | 当前官方journal保存在专属持久卷，增量确认；对象存储分段备份 | 不在每个模型/工具边界复制全历史，卷丢失不能静默回退旧记录 |
| 控制服务重启 | 封旧epoch、核旧Runner/child/命令，再恢复 | 现有全局锁加“旧Python协程已死”假设不能沿用 |
| 模型断流 | 区分本地连接槽与上游未决容量，费用独立保留 | 不用断流/本地超时证明厂商已停止 |
| 题目状态 | 保留既有五个work_status；验收等待写Assignment.wait_reason | 不增加claimed/solving/awaiting_verification并行状态权威 |
| CTF能力与内核隔离 | runc基本档明确范围，NET_RAW能力如实声明；Kata可选 | 不因“强隔离”字眼自动增加VM架构，也不许夸大Egress效果 |

新增依据：

- [Informer创建](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/client.py#L113)、[仅全停](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/client.py#L315)、[HTTP租户名来源](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/tenants/http_provider.py#L222)。
- [Ingress跨Namespace共享观察](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/components/ingress/pkg/sandbox/batchsandbox_provider.go#L51)、[Ready门槛](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/components/ingress/pkg/sandbox/batchsandbox_provider.go#L144)、[Ingress副本/资源默认值](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/manifests/charts/ingress-gateway/values.yaml#L45)。
- [Egress DNS上游](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/components/egress/pkg/dnsproxy/proxy.go#L616)、[实际EgressConfig字段](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/config.py#L1109)、[nft mark规则](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/components/egress/pkg/nftables/manager.go#L289)。SO_MARK/NET_RAW为需要实际验证的策略条件，不据此宣称已经复现上游漏洞。
- [WireGuard官方策略路由](https://www.wireguard.com/netns/)、[Cilium实体策略](https://docs.cilium.io/en/stable/security/policy/layer3/)、[拒绝策略](https://docs.cilium.io/en/stable/security/policy/deny/)。
- 现平台 [RuntimeLock](../../services/agent-runtime/src/bbx_runtime/scheduler/lock.py) 是全局advisory lock；[supervisor](../../services/agent-runtime/src/bbx_runtime/scheduler/supervisor.py) 的本地协程恢复假设须随独立Runner重构。CTF现有work_status五态见 [contracts](../../packages/contracts/src/bbx_contracts/ctf.py)。

其余U-S1子会话屏障、官方MCP观测缺口、最终网络清单和性能不因评审通过就变成已实现。主稿第10.4节给出统一恢复步骤与崩溃窗口，第14节统一列出所有待验证闸门。

最后交叉复核已收口两点：网络策略仅允许Solver到本Task Runner的隧道端口，仍禁止管理/工具接口；网络apply前必须freeze新旧Solver流量并核对受影响命令，通过可信预检后再发运行许可。没有新增拓扑或应用实现。
