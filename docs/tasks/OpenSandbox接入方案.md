# OpenSandbox 接入方案

> 2026-11-06 当前方案见[完整开发方案](平台架构重构开发方案-20261105.md)第 7–10 节，三路第二轮审核已通过进入 P0/P1，用户已授权开发和正本同步。本稿以下内容为前轮接入记录；原版 MCP 直通、不保留执行身份、默认 Egress 等选择已被当前合同覆盖，使用受控窄扩展、低权限文件和命令审计。固定源码依据保留，G1–G7/端到端组合均须实际验证，未部署。

2026-11-05 · 官方 Kubernetes / BatchSandbox 为新任务后端 · 文字设计待审核，未开发或部署。

统一架构见 [平台底层架构改造方案](平台底层架构改造方案.md)。本稿只定义环境、网络和官方接口接入；源码依据及三位 Astra max 评审结论见 [实现核对报告](OpenSandbox官方Kubernetes实现核对-report.md)。旧拓扑本轮不更新，不作为本版部署合同。

## 1. 最终选择

- SDK→Server→非池化 BatchSandbox CR→官方 Controller→一个 Sandbox Pod。平台不直接创建/patch该执行Pod，不写第二个Sandbox Operator。
- 一个 Task 一个 Namespace/Tenant。HTTP TenantProvider 的实际 tenant.name 等于 namespace；Task、命名空间和租户映射由 BBX 保存。
- 两个 Task Pod：可信 Runner（Pi、官方MCP、task-network）；求解 Sandbox（工具镜像/execd、官方Egress、task-link）。N个Worker/subagent席位不对应N个Pod。
- 同 Task 共享 Sandbox 文件、进程和网络，只做提示词/黑板协作；不同 Task 隔离凭据、API资源、卷和网络。一期runc共享宿主内核，不宣称抗容器逃逸；Kata另作加固部署档。
- Agent命令/文件用官方MCP；平台生命周期、已有执行观测、收尾用官方SDK/API。默认关闭Pi本机bash/read/write/edit，不复刻远端四工具。
- 固定shardPatches仅由管理员发布。VPN终止在可信Runner，Solver通过内部WireGuard隧道使用出口；不把Lead的HTTP提交流量放进Solver共享netns。

## 2. 版本基线

采用本轮 GitHub Releases API 返回的稳定版 **OpenSandbox 1.2.0**，发布日期为 **2026-10-27**。发布清单为 `1.2.0.yaml`，不能混用搜索缓存里的 1.1.x、主线最新代码和不同版本镜像。

| 项目 | 基线 |
| --- | --- |
| 官方 MCP 包 | `opensandbox-mcp==1.2.0`，另锁 MCP 依赖与构建产物 |
| Python SDK | `opensandbox==1.2.0` |
| Server 包 | `opensandbox-server==1.2.0` |
| 官方 Server 镜像 | `docker.io/opensandbox/server:release-1.2.0`，按发布清单锁 digest |
| 官方 execd 镜像 | `docker.io/opensandbox/execd:release-1.2.0`，按发布清单锁 digest |
| 官方 Egress 镜像 | `docker.io/opensandbox/egress:release-1.2.0`，按发布清单锁 digest |
| 发布标签 | `release-1.2.0`，本轮解析到提交 `62a64f0b48cdd6b410f36e53c74dc7559c87719a` |
| 发布清单中的 gitCommit | `25873227f60424bdc59bfa83b963bbc00e871297`；与标签相差 2 个提交，已查差异仅涉及 `docs/releases/1.2.0.yaml`，未见执行源码变化 |

已读取的核心镜像 digest：

```text
server  sha256:25bebdcf1cf7724eb923faecbdbbf82d4038c6ab35c736151574726d14006f30
execd   sha256:c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c
egress  sha256:a3d30246e557525f3fa44d16226258268cea175532e652822bd1eb4e83e0f54d
```

这些值来自公开发布附件；本轮未拉取镜像、验签或验证本机架构清单。部署时还要锁定自己的 CTF 工作负载镜像。`execd_image` 是执行组件镜像，`Sandbox.create(image=...)` 才是包含 CTF 工具的解题镜像，两者分别管理。

依据：[1.2.0 发布](https://github.com/opensandbox-group/OpenSandbox/releases/tag/release-1.2.0)、[发布清单](https://github.com/opensandbox-group/OpenSandbox/releases/download/release-1.2.0/1.2.0.yaml)。后文源码链接优先指向该标签。

## 3. 官方组件与平台保留的接入

| 部分 | 直接复用 | BBX保留 |
| --- | --- | --- |
| 官方Helm/CRD/Controller/Ingress | BatchSandbox Pod、端点、TTL、资源状态和部分启动恢复 | 固定版本部署参数与实际状态观测，不复制控制器 |
| Server多租户 | 请求Key→namespace；资源所有权校验 | 创建Namespace；已有后端提供HTTP TenantProvider；task_ref/tenant_ref绑定 |
| 官方MCP | 命令/文件及固定出站SDK配置、连接缓存 | 每Task部署一个实例；Task target、角色/调用准入与结果关联 |
| 官方SDK | 生命周期、执行观测、中断、字节文件操作、归档基础能力 | create未知对账、deadline、归档保全、业务停止与恢复 |
| PVC能力 | createIfNotExists、挂载、托管卷GC保护 | 预记claim名、Task保留策略、最后回收与归档失败处理 |
| Egress | dns+nft、策略、DNS上游与可选代理观测 | 任务网络附件、可信内层过滤、VPN/提交/采集验证 |

新增接入集中在现有Runtime内两个薄模块：Task资源模板（Namespace/配额/网络/Runner/身份/卷）与OpenSandbox SDK adapter。模型预算、业务MCP、网关发布仍使用既有模块。网络附件不提供通用命令/文件API。

固定版本有一处规模门槛：Server K8sClient按资源/Namespace保留informer，删除Task不会单项回收。长期运行须使用已修复官方版本或向上游补有界回收（停止、移除、订阅竞态）；不新建watcher服务，也不把定期重启当成永久方案。

## 4. 创建请求如何编译

平台环境Profile不是任意Pod YAML。以下是官方CreateSandboxRequest可承载的字段：

| 字段 | 平台来源与规则 |
| --- | --- |
| image / platform | 已发布工具镜像digest、实际系统/架构；execd镜像单独锁定 |
| resourceLimits / resourceRequests | 执行资源档，明确区分预留与硬上限；省略requests会沿用limits，不默认如此 |
| timeout | 有限生命周期，涵盖任务可执行时间与有界收尾；任务deadline另由平台硬停 |
| metadata | 可信Task、create_op_id、规格版本标签，用于结果未知对账；不放密钥 |
| volumes | 确定claim名与挂载；普通任务不提交任意hostPath |
| networkPolicy | 显式传入以创建官方Egress；内部隧道端点和任务网络规则一致 |
| secureAccess | true，配合官方ingress.mode=gateway及实际令牌验证 |
| env / entrypoint | 镜像启动所需非敏感、受支持值；不混入Lead/平台管理凭据 |
| lifecycle | 首版只使用确认过的必要钩子；不靠自动pause/renew延长任务或充当Agent恢复 |

namespace来自租户身份，不是模型选择的创建参数。templateId属于fast-sandbox，不能拿它选择普通K8s模板。extensions.poolRef第一版不用；它与每请求volumes/networkPolicy等不兼容。官方TaskScheduler处理进程任务，不替代题目/Agent调度。

创建状态建议并入现有环境绑定：requested → creating → bound → api_ready，异常为unknown/failed；网络和采集有独立readiness。先写create_op_id、claim名和目标规格，再调用SDK。每次官方create产生新ID；回执丢失不立即重试。按本租户metadata查证零/一/多结果，并等待原请求/回滚终结，不能把一次查询为空当作未创建。

SDK拿到ID后初始化失败会尝试kill；cleaner仍核对真实结果。没有确切ID时依赖已记Task/操作/claim绑定查证，不新建来掩盖未知。

## 5. 任务资源与MCP生命周期

1. 平台取得Task租约/epoch，创建唯一Namespace和默认网络策略、配额、身份、已知卷引用及待机配置。
2. 建Runner Service/Pod与当前任务MCP target，固定官方MCP包和依赖。官方MCP使用streamable-http，显式监听容器可达地址；其API Key是出站凭证，不保护HTTP入站。
3. 网关/可信管理入口是MCP入站允许来源。普通Agent不拿Key，Solver不得连接MCP或Runner管理接口。
4. SDK以该Task的租户Key创建Sandbox。官方Ingress共享，不按Task部署。发布端点前核对SandboxID/PodUID和API健康。
5. 可管理预连接sandbox_connect做预热；普通工具允许当前Sandbox的connect_if_missing=true，官方只连接既有实例、不创建环境。MCP重启丢缓存时用这条路径恢复，不重复副作用。
6. Task结束先撤销新准入并收尾，再销毁Sandbox、清理Runner/卷、回收Tenant Key/Namespace。没有“kill MCP = kill Sandbox”的假设。

MCP/Server出站Key不可作为Agent可选参数；目标Sandbox与环境代次由可信绑定核对。同Task保存的旧Sandbox也不能被新Assignment误用。每Task仅一个MCP副本，避免状态在副本间漂移。缓存状态不做独立业务权威或新数据库服务。

## 6. 文件、命令与权限

| 接口 | Lead / Worker / subagent |
| --- | --- |
| command_run | 在当前Sandbox执行各自职责；Lead准备/验收不解题，Worker及helper求解 |
| command_interrupt | 关联当前Task/环境代次和命令归属；取消不等于任意后台进程全部退出 |
| file_read / file_search | 正常开放当前任务文件；题目目录只是协作约定 |
| file_write / file_create_directories / file_move / file_replace_contents | 按角色工具集开放，沿用官方schema与容器权限，不做逐Agent路径/owner/group ACL |
| file_delete / file_delete_directories | 按Profile发布；并发协作规则由提示词/黑板说明 |
| sandbox_create / kill / renew / list /显式connect | 不作为Agent工具；平台SDK或管理连接使用 |
| endpoint及网络/凭据管理 | 默认只给可信控制侧；Lead通过BBX environment/platform发意图 |

执行文件工具不访问Runner本机。官方read/write是文本工具，replace_contents返回替换计数，不等于Pi唯一edit语义。提示词要求先读、确认范围、检查计数并复读。图片、二进制和大产物用已授权材料/SDK归档通道，不伪称文本工具能返回图片。

command_run未开放SDK所有timeout/uid/gid等参数，Execution ID/exit_code可能为空；官方MCP没有完整命令状态/增量日志工具。平台SDK只观测/清理已绑定执行并投影到既有记录，不另开通用执行旁路。未知请求不重放，远端命令ID缺失如实保留unknown。

## 7. 网络部署合同

### 7.1 双Pod与单外部VPN

可信task-network位于Runner Pod，包含成熟VPN客户端、WireGuard服务端、DNS转发和经身份认证的TLS forward proxy。Solver侧task-link只连接本Task的内部隧道；没有外部VPN凭据和提交Token。

解题流量：Solver → Egress → task-link → WireGuard UDP → task-network → VPN或获准公网。
提交流量：Lead业务工具 → 现有可信平台客户端 → 本Task认证proxy → 相同VPN → Benchmark。
控制流量：Runtime/MCP/模型网关/Server/Ingress走原集群路由，不进入任务VPN。

Runner主路由不变；按隧道入接口及可信代理/DNS进程标记进入Task路由表。VPN配置不得覆盖控制默认路由。Solver的控制回包和隧道外层有显式路由，断链不回落为直接出网。外部VPN会话数不随Worker数增长。

这是项目需要适配的固定网络装配，不是OpenSandbox原生VPN产品。IP层TCP/UDP/ICMP可以由隧道承载；二层广播、特殊设备、专有客户端和交互MFA不自动获得支持。VPN客户端的协议支持通过发布镜像/能力清单扩展。

### 7.2 使用真实官方扩展点

Server只读全局batchsandbox_template_file。runtime覆盖普通containers列表，但保留spec.shardPatches；官方Controller最后对Pod战略合并。模板固定加入task-link，并按name合并egress的DNS上游env。

补丁是管理员可信配置，可以覆盖整个Pod，必须校验最终生成清单，而不是让Agent传入。Task差异只在本Namespace Secret/受支持创建参数；同名Secret不会跨Namespace共享。网络/隧道密钥只挂给对应网络组件，不能挂进Solver工具容器。

DNS变量OPENSANDBOX_EGRESS_DNS_UPSTREAM支持IP[:port]，可指向每对隔离隧道的固定对端DNS地址。使用shardPatches注入；不存在[egress.env]这个公共TOML入口。任务链路端点由平台先解析为内部地址，避免引导依赖任务DNS。

官方egress.upstream_proxy是全局、创建时固定的HTTP(S) CONNECT链路，不用于每Task网络配置；不支持完整UDP/QUIC VPN。本方案首版不叠加透明MITM，正文观测单独验证。

### 7.3 就绪与控制接口

两端容器先healthy/standby，再建立隧道/外部VPN。Pod readiness只表示管理循环可用，不以VPN连通为条件，避免SDK创建等待Lead、断VPN同时断execd。环境分别报告api_ready、tunnel_ready、vpn_ready、capture_ready及配置revision。

内部控制仅需apply/status/freeze-cleanup-only/release，带Task、epoch、expected_revision、operation_id、配置hash与凭据引用。由现有environment/cleaner调用；不新增Agent工具体系。apply收到Secret内容不等于已应用，只有实际revision与检查证据匹配才ready。

运行配置通过只读投射卷交给组件，禁止任意配置脚本/plugin。网络apply前先freeze并确认Solver新旧流量阻断，核对受影响命令，再发布配置；仅允许可信预检，新revision通过后重发运行许可。紧急freeze通过直接受控接口与短期许可失效兜底，不能等待Secret投射周期。control失联后组件停止Solver转发，不无限沿用active许可。

### 7.4 过滤与权限

- Cilium约束Pod外层：Solver出站仅至本Task隧道端口与明确引导/回包例外；执行入站仅官方Ingress和必要探针。限制host、remote-node、kube-apiserver，不把普通Namespace规则当全部边界。
- task-network解封装后再次约束INPUT/FORWARD：Solver不能通过隧道访问Runner服务、其他Task或控制面；只开放必要任务DNS/目标转发。可信proxy另做身份与目标检查。
- 不复用官方Egress mark=0x1，不清空它的规则。task-link/network只管理自己的接口、路由与策略。NET_ADMIN仅给可信网络组件。
- CTF保留NET_RAW时，Egress的同netns规则不作为不可绕过的最终边界；外部CNI与可信转发过滤不能省。是否禁用原始socket必须与工具能力说明一致。
- 官方默认IPv6禁用init可能privileged；按锁定镜像/模板配置必要准入，验证选定内核/CNI/RuntimeClass。没有未经测量的“全部restricted”承诺。
- 避免目标、隧道、Pod/Service网段冲突。结合实际出口过滤，允许VPN的合法私网目标；不简单禁止整个RFC1918。

回连监听也是环境能力：任务需要时分配受控TCP/UDP入口/端口映射，经可信出口转发到当前Solver；公开入口取决于集群发布能力，不能用只支持HTTP的Ingress虚构原始TCP回连。配置前先核验实际可达，未提供该能力时明确blocked。

## 8. 凭据与TSec预检

Benchmark Token保留在可信平台客户端，只发给当前任务绑定目标；不进入Solver命令、文件、黑板材料、抓包文件或全Sandbox的Vault自动注入。共享Solver netns无法保护明文HTTP，因此真实提交只在可信Runner网络侧发起。

Lead具备environment/platform权限，Worker/helper没有。Lead不逐命令审批；缺少全局环境能力通过求助处理。任务材料不能扩权或修改集群模板。

若用户授权先配置VPN，Lead先请求配置；随后必须从实际Solver路径GET http://10.0.100.58，HTTP 200且status=ok才继续。首次失败立即输出指定文案并停止，不能换HTTPS/平台URL、跟随跳转冒充通过或自行修复后续跑。可信提交出口可达性另查证；Solver可达不证明Runtime可达。

## 9. 停止、恢复与数据保全

业务停止先写STOPPING/关闭新准入，再将network切cleanup-only：阻断Solver新旧业务连接，只保留规则允许的可信外部资源收尾。若目标平台禁止结束后再调用API，则不继续发close，记录未知资源状态。

并行取消Pi/child与已知execution，保存回执/journal/产物。官方interrupt不证明任意脱离进程已停；影响范围未知就冻结共享Task。整Task通过SDK删除Sandbox并核对实际Pod/CR，不能以API受理、强删对象或finalizer消失冒充进程终止。

旧Runner的Pi死亡但MCP/network仍运行时，共享cleaner接管；不热收养存活detached进程。网络收尾、Solver清理后旧Runner退出释放会话卷，再创建恢复Runner。ReadWriteOncePod卷不能在旧Pod仍占用时交给新Pod。新PodUID触发环境代次变化，重验隧道/目标/采集，不自动续跑旧命令。

工作PVCdeleteOnSandboxTermination=false，创建失败也由Task绑定追踪；正常先归档，最后删PVC/Namespace。异常保留卷，可用固定只读归档Job提取；无法访问则cleanup_pending，不丢证据。Pv的实际回收按StorageClass/reclaimPolicy记录，不能只删PVC对象就假称底层数据擦除。

SDK/MCP连接重建不重放副作用。模型、工具、child启动、create与Flag submit分别以稳定操作ID对账。详细崩溃窗口与统一恢复顺序以主稿第10.4节为准。

## 10. 观测、部署与验收

采集Solver隧道内侧，避免Runner外部VPN接口无差别抓取可信提交。Task网络与模型/MCP日志分别保存；逐Agent网络归属需要execution/PID/socket证据，缺失标Task级/unknown。HTTPS正文、证书固定、非HTTP协议各自声明支持范围。旧traffic/artifact格式与UI继续复用。

部署用官方固定Helm版本，Server单活/Recreate。MCP是任务专属单实例；Ingress共享跨Namespace。默认Helm资源不可照搬本机：Server 4Gi加两个Ingress各4Gi，已预留12Gi。首版显式一个Ingress副本与小规模values；requests/limits按测量调整，不承诺20席位等于本机能承载20个重型工具进程。

平台网关配置仍由已有唯一发布者驱动；可写运行快照不能挂在只读ConfigMap路径。新Task target发布后核对活动资源再启动工具。Node会话、模型队列、环境与网络均有独立状态，UI不要求用户理解K8s资源。

第一阶段只集中验证三组：两Task的路由/租户/卷/网络隔离；创建未知、MCP/Runner重启、停止与留卷；单VPN的Solver/提交/采集及cleanup-only。模型使用fake provider。U-S1–U-S4、M1–M5、K1–K6未通过项保持待验证。

本轮只核对公开源码、审阅相关上游测试，并运行一次官方纯模板合并检查；未部署、未运行集群/网络/性能或模型测试，不读取.env，不改应用代码。
