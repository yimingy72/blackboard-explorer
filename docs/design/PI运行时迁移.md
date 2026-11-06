# PI 运行时迁移

2026-11-06 当前合同，承接 2026-10-30 用户以 [Pi](https://github.com/earendil-works/pi) 替换 MAF 的决定及后续 SDK 选型。用户已授权审核通过后开发和 [开发方案第 16 节](../tasks/平台架构重构开发方案-20261105.md)的正本同步；三路第二轮审核通过进入 P0/P1，不代表 G1–G7 已通过。

本合同替换旧 core＋逐模型/工具 Python 驱动＋数据库复制完整会话。领域规则保持，明确存量活动/待收尾任务按冻结版本短期服务，不能运行中换核；迁完/转只读后退出旧栈，终态未来复开显式走已登记产物的新运行，不无限保旧可执行引擎。

2026-11-06 用户侧聊“如果评估下来可行请发给主会话修改方案”授权的 [P3c 费用阈值调整](../tasks/P3c-Agentgateway费用阈值调整-20261106.md)经交叉审核通过：新 V1 模型与费用执行收敛 agentgateway standalone，覆盖旧默认严格预留。后续替代也覆盖原平台通用自研，按验证闭环逐项删除，不仅未提交草稿；SDK/journal/控制/环境与 MCP 安全语义保全，legacy CNY 历史不重算，不等于旧栈永久运行。

## 组件选择

| 能力 | 实现 |
|---|---|
| 会话与 Agent 循环 | `@earendil-works/pi-coding-agent` SDK 1.5.0 的 AgentSession/SessionManager；core/ai 是内部依赖，不并列维护直接 core 执行器 |
| 多提供方模型、多模态、思考、流式和用量 | SDK/pi-ai 保留客户端会话能力；上游 provider 连接/协议/凭据、usage 与 USD cost/per-key 执行由 agentgateway standalone 唯一承担，Runtime 不解 SSE/二次计价 |
| 子 Agent | 社区 `pi-subagents` 0.85.0 / b6647222bd2e849021054292927ef39fea0793ed；复用公开子会话、消息、结果与取消，平台补身份/席位/确认/收尾 |
| 业务工具 | 暂推荐官方低层 pi-mcp＋最小平台桥→agentgateway→受限 OpenAPI 候选或官方 Server 薄 host→原领域；高层 extension 未裸采用，不重复客户端/聚合 |
| 命令与文件 | 本 Task Sandbox MCP 窄扩展→官方 OpenSandbox SDK/execd；稳定命令身份、低权限文件、审计和流量归属从首命令接入 |
| 任务与运行治理 | PostgreSQL 保存 Task/Assignment、租约/epoch、调用关联、native 费用去重/待核与阈值停止、输入/业务回执及会话确认引用；不建立第二会话历史或并行模型预算执行器 |
| 本机工具与资源 | 默认关闭 Pi bash/read/write/edit、cache warmer 及未登记后台模型路径；显式平台 ResourceLoader 只加载已发布配置、Skills 和可信扩展 |

不采用 pi-durable、Chord 应用框架、管理 LLM 或第二套工作流循环。所选 SDK/插件的持久确认与前后台宿主接口须先通过 G3，包名或源码存在不能证明可用。评估版本与部署锁分开记录：当前旧锁仍为 core/ai/telemetry 1.4.0；P0 不以改文档宣称依赖已升级。网关/OpenSandbox 固定基线和镜像锁计划见开发入口第 0.2、15–17 节。

按[原生复用矩阵](../tasks/平台架构重构开发方案-20261105.md)比较 Pi 1.5 公开 createMcpExtension/loadConfig/createTransport、credentials/extensionFactories/bindExtensions；只从平台冻结配置注入，禁止默认项目/全局 mcp.json、stdio/codemode 和本机执行。Sandbox 同经 agentgateway，优先原生 Virtual MCP/工具角色/target 授权及 backendAuth；可信 JWT 条件须证明不会初始化 foreign target。现薄 host 不作必选，官方 OpenAPI 转换只用严选 Agent 子集，不能暴露内部全量 OpenAPI或假定 _meta 透传；v1.7 的 4xx isError=false/省状态及 5xx RPC -32603 限制须以 403/409/422 真实对比闭合。MCP 日志/OTel 可复用，但 request_logs 当前仅 LLM；官方 Sandbox 引擎与 low-UID/background 窄补、Assignment/nonce/cgroup/netns/流量/eBPF 的独立证据不删。

高层 extension 忽略稳定 toolCallId/meta、404 effect 重发等会要求更多桥，故暂推荐官方低层客户端＋窄桥，保可等待确认/absolute permit/结果/关闭 gate；不是保留自研客户端或自研 MCP 整层。模型目录/路由单源可编辑，SDK 快照派生；DTO/小元数据去重不造 registry。Runner poll 后续只复用现 PG 通知（先订阅后读命令表、提交生效、listener/租约回退），不增总线。K8s 原生 workload/watch/Lease/卷约束不替 Task DB epoch、实际进程清理和 journal 单写证据，probe 不等产品控制器。

## 运行边界

1. Python Runtime 保留 Task 租约、普通 Explore/Derive/Close 调度、CTF Assignment/席位、成果整理租约、准入与独立 cleaner；Node/Pi 负责原生会话循环。Task Runner 是独立 Pod，可能跨控制服务重启存活，不能以 Python 协程死亡直接 finish、重派或释放席位。
2. 受控 Node.js >=22.21 进程只读取平台明确提供的资源；不读取用户 `~/.pi`、`.env`、全局提示词/扩展/凭据。Runner 不挂求解工作卷，不执行题目脚本；默认工具/Skills/子插件均不能恢复本机执行。命令、文件、业务和生命周期按实现正本四条路径分开。
3. 模型前完成权限/预算、提示词热更、输入与黑板/公告增量及 journal/业务回执确认。已发布提示词仍在下一次模型调用前替换单份系统指令，并记录版本；不改模型/工具快照、不扩权。完整模型响应确认后才允许其工具，不完整流/取消不派发半截工具。
4. 普通 subscribe 回调不可等待，不能承担硬屏障。主会话通过公开 session.agent.streamFunction 包装先确认再调用原函数；确认失败返回 SDK 可消费的 error/aborted 终态流、上游 0 请求，不直接 throw/reject。包装后不继承 ModelRuntime 私有 auth 标记，不 deep import/复制私有标记；主回合、自动/手动压缩均用受限网关身份。最终 HTTP 薄层重核 live fence/确认与原请求、call/attempt 一次派发，字节透传，不持 provider key、解 SSE 或第二次 token 计价；子会话独立证明同等语义，未登记后台调用保持关闭。
5. 工具参数使用 Pi schema，REST/MCP 共用领域处理器，实际写事务同连接重核 Task/Agent/role/Assignment/generation/epoch/runtime fence。Worker MCP 列题在既有 scope 上再限当前 binding.challenge_id，公告 Task 共享；metadata 不充当权限。可纠正参数错误可作工具结果，旧代次/未确认/未知副作用冻结相关准入。
6. 每个工具调用使用稳定 invocation/command_id，可信 host 先保存准入/审计，再实际执行；原结果由 host 持久后才交 Pi，Runner 不能上传任意结果造完成。ACK 丢失只按原 ID/SDK/参数读已保存 completed；当前合法 owner 恢复同 binding/SDK 可读原结果，历史读取不授新执行。已发 unknown 不重派，后台执行保原 ID/status/log cursor 和独立 drain。
7. 新 V1 每 Task 一个稳定共享模型 budget key，不设置 window；Lead/Worker/helper/主回合/压缩/summary 等同 Task 用途累计 USD。原生响应结束计费、跨阈请求可完成，之后拦截；平台拉 status 补触发并持久停止新派工/模型准入，不等下一次 429。Task key 仅可信 Runtime/网关持有，Runner 沿 binding/epoch 短 token，不能给每 Worker 一份总额或因 epoch/Pod 重启重置。允许在途/超额，不承诺绝对零超支；P4 闭合暂停/恢复，控制/清理不被模型预算阻断，legacy 角色额度不改。
8. helper 从父有效快照继承模型/强度、获准 Skills/工具/环境，权限取交集、独立身份和 journal，主/子共用原子席位。父普通回合结束不取消子运行；换题、分配取消、整题完成或 Task 停止级联收尾。迟到产物归原 ChildRun，不投下一题。

## 会话与兼容

- Pi SessionManager 是新会话内容权威；journal 放 Runner 专属持久卷，一会话单有效写入者。平台保存 active leaf、confirmed_seq/hash、归属、输入和业务回执，保存确认备份/归档，不逐 token 复制完整历史。同步 append 不自动等于 fsync，实际存储与持久屏障须验证。
- 人工输入先持久入箱，确认进入 journal 后才显示已应用；普通 steer 在安全边界注入，停止并改计划先收尾旧执行。终态复盘另开只读用途会话，不恢复求解工具权限或自动重跑任务。
- 已有任务、预算、模型配置、黑板对象、MinIO 档案及原始 MAF/core 历史不重写、不删除。旧 Fact/Intent 保持空题目范围，普通 holder/resolves 与完成复核不被 CTF Assignment 替换。
- 历史读取适配只转换消息结构；保留工具调用 ID、工具结果、图片引用、来源 ID、思考/签名及 provider 元信息。旧工具配对修复依然不重执行原工具。
- 服务端统一 ConversationEntry/TaskEvent 投影供浏览器读取；浏览器不解析原始旧 Session 或 SDK JSONL。旧格式转换可单独测试，无须安装 MAF 才能读取历史；实际未保存的历史明确缺失，不补造。
- 停止先持久撤权/冻结，再取消 SDK、helper 宿主与远端命令，独立核对费用、执行和归档。新 epoch 接管先 frozen，旧 Runner/副作用、卷单写和环境确认后才恢复；Kubernetes 404 不证明分区旧进程已结束。
- 模型默认值、上下文与最大输出、角色覆盖、价格/币种和 Responses 兼容模式不能因框架切换被静默重置。不支持的旧连接明确报兼容性错误，不擅自换模型。
- 新 V1 原生 USD/source/catalog 与冻结 FX 保留，缺汇率不静默启动 CNY 保障；不猜值或 1:1。费用按稳定逻辑部署/持久 DB 命名空间 accounting_source_id＋native_log_id 去重，attempt 关联；不因 Pod/进程/epoch 变化重计。status 不加明细、不用 Prom/假 webhook；缺证据待核、确认费用/暂停不回退。native 局部重启窗口已见，单活部署/多副本仍各自验证，不混 Gateway API。

## 验收与发布

先用脚本假模型和本机 HTTP/MCP 服务验证 SDK/子插件持久接缝，再覆盖既有角色用途、图片/流式和用量。G3 继续证明前后台确认、失败终态流且上游 0 请求、结果持久后不重放、继承及主/自动/手动压缩身份；G5 以同 Task 共享 key/跨阈与在途、预算来源和一般 429 区别、日志去重/status 补触发、缺行/usage/catalog、冻结汇率及单活重启证据验证。一次 streamFunction 不等于一次发送，保留 attempt/native 日志关联；前台或一个阈值例子不代表全闸门通过。只补必要接缝，不调用真实模型/e2e/eval-run 或真实靶场。

最终标准是每个替代闭环同时移除对应旧通用代码及失效配置/依赖/测试，不全部拖 P6；新 Task 实际进锁定 SDK/网关/OpenSandbox，对应旧创建停止，明确旧活动/收尾绑定迁完或转只读。历史数据/已发生调用结算保最小读取，普通/成果整理/chatworker 等真实调用有迁移出口后删旧循环，不能用未来可续跑永久保引擎；也不能未接通就删或包完整旧层冒替代。

P0/P1 源码开发不自动切换本地/公网服务。发布另核 G1–G7、活跃资源、未知调用/费用和归档，保留数据库/对象存储/配置与可回退镜像。回退先停新准入，仅未启动新 Task 可选旧路径；已发生新副作用的任务保留现场核对，不倒回旧执行器重跑。
