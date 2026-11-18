# P3c：Agentgateway 费用阈值调整

2026-11-06 · GPT-6.1 Sol / max · 方案修订，产品预算代码保持冻结，未部署。

## 1. 授权与覆盖范围

用户在侧聊明确授权“如果评估下来可行请发给主会话修改方案”。root 与 runner_protocol_review 交叉审核通过本方向后，先同步[整体方案](平台架构重构开发方案-20261105.md)与四份正本，独立文档批；真实 agentgateway probe 的事实补齐并最终独审后才提交，不重复向用户确认。

新 V1 预算目标是累计费用达到阈值后暂停/终止新工作，允许在途/超额。本文 supersede 未提交 P3c 默认严格预留/硬 context 证明/自建 provider-SSE-计价方向；原平台通用自研也优先逐项替代，不只裁剪该草稿。每项固定版本覆盖→最薄接入→必要行为/迁移验证→同步删旧实现/失效配置依赖测试，不全等 P6，不未接通先删或机械包完整旧层。本轮只修文档，产品预算代码保快照冻结。

已验证 SDK/journal、持久控制/身份和 G1 环境的能力/证据不误删，probe 不当产品控制器。MCP 实际事务 fence、可信结果、unknown 不重派与历史 completed 只读保留。旧 CNY/数据/已发生调用保最小读取结算；旧执行栈只服务明确存量活动/待收尾，迁完/转只读后退出，不能因任意未来可续跑无限保留。

## 2. 唯一职责

| 部件 | 本轮职责 | 明确边界 |
| --- | --- | --- |
| agentgateway 1.7 standalone | 唯一上游模型 provider 连接/协议适配/凭据、usage 归一化、USD cost 目录、APIKey DB 的 per-key 预算执行 | 原生费用按目录估算，不冒称供应商已确认账单 |
| 可信 Runtime 薄层 | live Task/binding/epoch/permit fence、已确认 journal、原请求/call/attempt 关联、一次派发、字节透传；保管该 Task 原生 budget key | 不持 provider key、不解 SSE、不二次 token 计价，不建立第二预算执行器 |
| Blackboard/平台控制 | 可信 native 请求费用去重归集、status 对账与阈值补触发、持久 stop/hold、来源/缺口/币种展示 | 不用 Prom 累计作账本、不造预算 webhook；快照与逐请求费用不相加 |
| Runner/Pi | SDK 原生会话循环/确认与受限模型请求 | 仅沿 binding/epoch 短 token，不获得 Task budget key/provider key，不给每 Worker 一份总额 |

模型/usage/cost 优先使用原生组件已有能力，见 [Model costs](https://agentgateway.dev/docs/standalone/latest/documentation/llm/cost-controls/costs/)。保留 catalog 来源、版本/部署修订与适用信息，成本缺失和目录版本不明均可见；不能把一次目录查到价格等同外部真实账单保证。

采用 standalone 模式，可用[官方 standalone Helm](https://agentgateway.dev/docs/standalone/latest/documentation/setup/install/helm/)将同一数据面部署到 K8s。首版单活；它不是 Gateway API 控制面模式，不能混用另一种模式的资源/配置说明或把可配置多副本当成预算一致性已验证。

### 2.1 直接复用、必要薄适配与明确缺口

Pi 调 Sandbox 也统一经 agentgateway，不重复客户端/聚合/目录/执行服务。现使用官方 pi-mcp，business_mcp 是官方 MCP Server 薄 host，不是自研协议整层；高层 extension 会要求更多身份/effect 桥，暂推荐官方低层＋最小平台桥。矩阵保候选/门槛，不强替换安全边界。

| 能力 | 直接复用候选 | 必要薄适配/缺口 |
| --- | --- | --- |
| Pi 客户端与 extension | 暂推荐官方低层 pi-mcp＋窄桥；高层 createMcpExtension 的连接/分页/命名/content 候选 | 忽略 callId/meta、404 重发等未闭合前不裸用；只载受信 gateway，禁默认 codemode/stdio/全局文件与本机执行，保硬 gate |
| 路由、聚合与权限目录 | [MCP modes](https://agentgateway.dev/docs/standalone/latest/documentation/mcp/configuration-modes/)、[Virtual MCP](https://agentgateway.dev/docs/standalone/latest/integrations/mcp/servers/virtual/)、[mcpAuthorization](https://agentgateway.dev/docs/standalone/latest/documentation/configuration/security/mcp-authz/)及每 target backendAuth | target 条件必须可信 JWT；tools/list 隐藏不是不初始化 foreign target 的证明，须核握手/调用隔离 |
| 业务工具转换 | 官方 [OpenAPI→MCP](https://agentgateway.dev/docs/standalone/latest/integrations/mcp/servers/openapi/)严格 Agent 专用子集 | 禁全量内部 OpenAPI；不假定 _meta 透传。可信 invocation/header、稳定 request_id、实际事务 fence 与 host 保存结果/确认不能丢 |
| Sandbox 工具引擎 | 官方 MCP command_run/interrupt/text files＋SDK background status/log | 仅低 UID files、Assignment/nonce/cgroup 等已知缺口窄补，不重写引擎或开命令旁路 |
| 观测与永久审计 | 原生 MCP 日志/OTel | 当前 request_logs 仅 LLM，非永久 MCP 命令审计；netns/流量/eBPF 与实际 execution_id/取消清理证据独立保留 |

v1.7 OpenAPI converter 的已知边界是 4xx 可仍为 isError=false 且不带 HTTP 状态，5xx 才为 RPC -32603。真实 Pi→agentgateway→工具 backend 的必要接缝须包含一个 OpenAPI 工具对比，覆盖 403/409/422、可信身份/target、稳定调用/请求关联及结果保存后丢回包。仅 200 成功不够；若不能保持这些语义，保留最小可信确认 hook/薄 adapter 并明确实证缺口，不为迁就 converter 造新复杂协议。

Pi/网关只读能力核查已给出下列限制，仍待真实接缝验证：原生 execute 不提供稳定 SDK callId/_meta 注入；session 404 可重试一次 tools/call，progress 可续 server timeout，因此须保每 session 身份、absolute deadline 和不重派/receipt gate。tool_call 可 await/block，tool_result 抛错会被吞，不能作唯一持久屏障；后台 connect 非硬 readiness，dispose 不发 session_shutdown，须公开 emit 并 await。大结果/二进制的 Runner 临时路径对 Sandbox-only 文件工具不可用，须分页/artifact 或结果薄层。网关 JWT 验证后默认剥 Authorization，共享 board 需显式 backendAuth passthrough；generic `/tools/{operation}` 不是自动十四工具的 Agent schema。

### 2.2 后续独立小改与原生边界

Runner poll 复用现 SSEDispatcher.subscribe/Repository.notify：先订阅后读命令表，成功提交后唤醒，通知不是命令/ACK/费用 webhook。读取/核验短事务，等待释放连接；listener 未 ready、断连/漏通知按租约有界回退，不阻塞续期，不新增总线。business_manifest/MUTATIONS/schema/REST 参数从共享 DTO/小元数据派生，不造通用 registry。

模型目录/路由单源编辑，其余选择/SDK snapshot 派生；动态原生资源 hybrid＋DB、Helm database 模式与 file-owned 资源分清，不维护三份源。原生 `/v1/models` 缺字段就省略或同一源显式补齐，不另跑 provider 发现。动态 Task key 显式 no window＋Block，创建/失 ACK 读回同 key/hash/name，不另发额度；API 细节留下一批核定。

K8s 1.34 复用官方 OpenSandbox Server/BatchSandbox/Controller、合适 Runner workload/watch、probes/资源/身份/配置/网络；无新 Operator/scheduler/rebuild loop。OwnerRef 不跨 Namespace 且遵 scope 规则，PVC/审计显式保留；Job 字段仅适用 Job，suspend 不保进程，RWO 非单 Pod、RWOP 需 CSI，Lease 不替 DB epoch。K8s/网关不代同 Pod Assignment 清理/流量/命令审计，G1/P1 probe 不是产品控制器。原模块→原生替代→必要残留→调用迁移/删除条件统一见[整体方案第 14 节](平台架构重构开发方案-20261105.md)，不为任意历史复开无限保留旧执行栈。

## 3. Task key、累计与币种

- 每 Task 一个稳定共享模型 budget key，连同原生预算标识/名称持久关联；不设置 window、持续累计。Lead、Worker、helper、主回合、自动/手动压缩、summary 等同 Task 模型用途共用，不按 Worker 复制阈值。Pod/owner/epoch/席位变化不能重置累计或另发同额预算；轮换/追加不得靠换预算标识清零。
- 原生按响应结束后的 usage/cost 记费，跨阈请求可以完成，后续请求按 Block 策略拦截。平台主动拉取 native budget status 补充阈值触发，不能依赖下一次模型请求才发现。原生管理与累计配置以 [per-key 预算/Admin API](https://agentgateway.dev/docs/standalone/latest/documentation/llm/cost-controls/budget-limits/per-key/)为来源，实际调用/字段由 1.7 probe 锁定。
- 原生 USD 原值、来源/catalog 版本保留。平台现有 CNY 不能隐式按 1:1：新 V1 须显式配置并冻结 `rate_cny_per_usd`、来源、时点、精度/舍入规则，一次换算 `threshold_usd = threshold_cny / rate_cny_per_usd`；展示保留 USD 与冻结汇率下的 CNY。缺汇率不静默启动 CNY 预算，不声称 CNY 保障，本任务不猜实时汇率。
- legacy CNY 与原用途额度继续原合同；新 V1 不为预算耗尽后的 Worker/helper/收尾另发总额来绕过阈值。必要控制、清理和费用对账保持可执行；需要新增模型费用或恢复时由 P4 明确追加/恢复语义。

## 4. 归集、停止与未知

费用去重键为 accounting_source_id＋native_log_id；前者是稳定逻辑部署/持久 DB 命名空间，不能用 PodUID、启动 UUID 或 Runtime epoch。原 attempt/Task key 关联与冲突校验不能另开同日志的计费身份；薄层覆盖伪造归属头并同 key 映射核验，重启回读不双计。来源参考[日志数据库](https://agentgateway.dev/docs/standalone/latest/documentation/observability/access-logs/database/)和[数据库配置](https://agentgateway.dev/docs/standalone/latest/documentation/setup/database/)。已测 search/get、opaque completed_at/id 分页，保重叠/未核 attempt，不只取 latest500/started_at；分页非消费完成证明，自定义属性过滤未验证。

Native budget status 是执行快照/阈值证据，请求日志是明细。同一费用不把两者相加；已确认明细、已观测阈值与平台已持久暂停不因原生重启快照较低而回退。缺费用行、usage、cost、catalog 或消费缺口记待核，不能补 0、自动完成或借未确认数额放行；已知零必须有 usage 和计价证据齐全。必要时暂停受影响 Task。

达到阈值沿现 stop/hold 持久阻止新派工与新模型准入，保留停止/清理通道。`budget_exceeded` 与普通 provider/rate-limit 429 分开；若响应 body 可能相同，须以原生预算状态或可信日志证明来源，不能仅靠字符串误停 Task。没有已核 native webhook 就主动查询/消费，不杜撰回调。

在途请求可能继续结束并增加费用，最终 USD/换算 CNY 与超额如实显示。原生内存计数约 5 秒写库带来异常重启窗口，首版单活；异常损失幅度、日志完整性、升级/接管及多副本需实证，不扩成默认绝对硬预算。跨 Task 公平调度、阈值暂停/恢复与额度更新由 P4 闭合，不增加第二 Agent 循环。

## 5. 目前事实与发布门槛

真实 probe 使用 `cr.agentgateway.dev/agentgateway:v1.7.0`，镜像 digest `sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`，git `26f5aebe99a36a037fe54f8080a608aca5547e15`；脱敏本地证据为 `.data/p3c-agentgateway-20261106/report.md`、`summary.json` 和其 evidence 索引。一个隔离 gateway/fake provider/SQLite、随机本机端口，无真实模型或凭据，资源已清理。

| 场景 | 已观察结果 | 可得结论 |
| --- | --- | --- |
| 同 Task 两角色共享 key | 两次各 200，费用各 0.003 USD，合计 0.006 超过阈值 0.005；跨阈请求完成 | 共享累计且允许超额，不能写成每请求硬预留 |
| 超阈后的下一次调用 | budget_exceeded 429，fake provider 零调用 | 原生预算拦截可在上游前发生 |
| 另一 Task | 仍 200 | key/预算按 Task 分离 |
| provider 凭据 | fake provider 收到自己的 fake key，未收到 Task key | 上游 provider 凭据与 Task budget key 分层 |
| 缺 usage/断流/普通 429 | 最终 DB/native API 找到行，cost 可为 0.0、error=NULL，input/output/total 全 NULL；普通上游 429 的 code 为 rate_limit_exceeded 且 provider 被调用 | 必须 unknown_fee，不能从 HTTP200/error=NULL/cost=0 推导已知零或完成，不与本地预算拒绝混同 |
| 无 catalog | usage 100/100/200、cost=NULL，原生 USD 预算不扣 | 金额待核，不能补 0 放行 |
| 迟完成与查询 | search/get 支持稳定 id/attrs；opaque nextCursor 为 completed_at/id 组合，较早开始的悬挂请求晚于快请求完成仍可覆盖 | 保留重叠去重与未核 attempt；started_at 水位会漏，分页不是完整性保证 |
| 异常重启 | SIGKILL 前 API 0.006、DB 0.003；重启 API 0.003。等 5.2 秒持久后再重启保持 0.006；原日志 id 可 get | 原生 counter 有回退窗口，平台已确认明细/暂停不回退，不把两种数额相加 |
| payload/初次观察 | 本配置 metadata 行 has_payload=0，未存 prompt/completion。首轮宿主机 SQLite 轮询曾未见最新行，最终容器内 SQL/API 取到 | 保留初次停点，根因未证；既不写成永久缺行，也不承诺所有请求必有费用行 |

以上仅证明预算/来源/单活重启局部接缝，不表示真实 Runner→平台→MCP、持久平台消费者、K8s 隔离或完整 G5/发布通过。仍须实证平台持久一次派发/费用消费与暂停恢复、日志缺口及重复消费、受限属性追查、单活升级/多副本、冻结汇率/历史 CNY 隔离，以及第 2.1 节真实原生 MCP 对比。Probe 薄门面仅内存去重示意，不能当已实现的产品 durable CAS。

## 6. 实施与必要检查

1. 先独立文档批，产品预算代码仍冻结；原平台通用自研按每个替代闭环同步删除及清配置/依赖/测试，不只删未提交严格草稿，也不 whole-file 回退 WIP。明确存量活动/未决调用迁完前不盲删；保历史读算而非永存旧引擎。原生 MCP 待必要实证，暂保官方低层＋窄桥。
2. 保留 MCP/REST 共用业务规则与 host 结果权威；runtime_context＋teammate 的 `board_challenges` 在原 scope 上进一步限当前 binding.challenge_id，公告 Task 共享；普通/历史读取语义保持。
3. 当前 manifest 仅有 Lead 建题和既有知识/Flag 等操作。Lead 题目信息/范围修改、派工/关闭与 scope_revision 失效工具闭环列 P4，不在薄适配中临时扩 Assignment。
4. 只复用必要 fake provider、原生 agentgateway 与隔离数据库 probe，不跑真实模型/靶场、e2e/eval-run。文档只查差异、链接、围栏和合同一致；产品下一批另经独审和一份组合 candidate 门禁。

本批文档共七份：整体开发方案、概念/实现/开发/PI 迁移四份正本，以及本文与[报告](P3c-Agentgateway费用阈值调整-20261106-report.md)。用户授权覆盖这些必要相关段落，不翻写其它已审核架构，不自动发布或迁移运行中 Task。
