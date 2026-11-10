# P6c · 独立模型演练与 AgentGateway 退役

基线 `1174b2b11c92bb15d015ee7cafbb79d11fe13fbc`；使用 P6a/P6b 的隔离源码和本地分支。原工作区 104 项、默认索引和 HEAD 保持原样，不读取原 `.env`。承接 [P6 合同](P6-Pi原生模型与MCP迁移-20261110.md)、[P6a 账本](P6a-Pi模型与平台账本-20261110-report.md) 和 [P6b 双目标 MCP](P6b-独立MCP管理与双目标接入-20261110-report.md)。本轮最终切流并退出受管网关部署；原 SQLite/PVC、两站账务和任务数据只读保全。

## P6c.1 平台模型演练

- 在平台模型页为已保存且有凭据的启用模型提供“测试模型”。由原平台账号登录；沿主站模型管理者权限启用实际发送。无需虚拟 API key，不使用任务额度、不借用户复盘用途。单轮纯文本测试，限制 UTF-8 输入字节和输出；固定空工具、无自动重试；显示冻结版本、答复、已观察用量、USD 估算或“费用未知”。缺价模型允许演练，但继续不能用于有预算的任务；不自动引入官方或同名模型价目。
- 公共接口 `POST /api/platform/pi-model-tests` 只收 `{request_id,model_id,model_revision,prompt}`。同一站点、用户和 request ID 保存同一逻辑调用，绑定精确模型版本与输入摘要。首次写入后只向当前站受信 Runtime 私有固定 `/playground` 发一次请求（原有 Pi sender 专用身份）；`GET .../{request_id}` 和重复 POST 只返回原状态/结果，修改输入或模型返回冲突，别的用户不能读。原 ID 先读持久结果，再评估目录最新状态；模型后来编辑或停用不能让原 ID 二次发送。浏览器没有可信 Runtime 令牌、提供方密钥或任意转发地址。私有入口不接受未登记的用户发送，不作为外部通用模型代理。
- 扩展现有 `pi_model_calls`：`purpose=playground` 独立，`task_id IS NULL` 且用户必填，其他用途必须仍有 Task；另建部分唯一索引 `(site_id,request_user,logical_call_id) WHERE purpose='playground'`，避免 PostgreSQL NULL 跳过去重。执行原状态机，Taskless 的 claim/取消/结算使用精确调用行锁；并行重复 POST 不会再调用 Runtime。公共入口先持久认领并冻结：原 request/用户/站点/attempt、模型/价目和请求摘要。Pi Runtime 使用现有 `PiSender` 的序列化、准备、实际 provider POST 前的唯一 claim 和最终 settle；没有新 SDK/协议或计费公式。只有原 attempt、正文摘要和真实证据可以结算；未知不自动重发或计零。若入口失联，只有在调用行锁内仍未获发送许可时才可关为未发送；已跨许可的失联保留原调用为待确认，不产生新尝试。演练回复有界并和终态一同落盘；其他用途的结算不能附带回复正文。GET/POST 不缓存，页面失败回读原 UUID，不自动换 UUID。不单独保存用户输入，也不将它写入日志；模型在答复中复述的内容仍属于该用户的原答复。无回复但结算已保存时仍可用原 ID 读回。按各自 purpose 汇总，演练不会出现在 Task 或跨站复盘费用里。
- 两站保留现有 P6a 的任务累计阈值（已在途可越线）、按站点/调用去重、用户只读复盘不限额和跨站部分覆盖提示。模型选择、Pi 1.4/1.5、摘要/Helper、可信域名与连接固定限制不变。

## P6c.2 源码与历史的删除边界

| 切面 | 退役内容 | 保留内容 |
|---|---|---|
| Blackboard 模型、费用 | `gateway_models.py`、`gateway_catalog_api.py` 活目录/管理、`gateway_budgets.py`、`gateway_review_key.py`、`gateway_maintenance.py`、`gateway_execution.py` 活协议/周期对账、`native_admin.py`、`native_fees.py` 活日志/key/模型请求；`native_budget_increase.py` 旧费率/key 更新部分 | 普通/成果/复盘的纯 PG 当前身份守卫迁到 Pi 账本模块；Native 阈值/暂停/接管/工具回执仍用原域逻辑；按账本原字段读取旧任务、旧 USD/CNY、费用来源、历史未知。旧汇率/价格/额度/结算不可写，不从网关补数据。 |
| Blackboard MCP / Native | 删除 `native_mcp.py`、`native_mcp_routes.py`、旧 route/模型 dispatch API、轮换 RSA/JWKS/后端 secret、旧 MCP mount 与流量前置、旧 key 与路由发布启动任务；`native_runner_bootstrap.py`、`native_pause.py`、`native_recovery.py`、`native_resource_retirement.py` 移除现役旧分支 | `runner_gateway.py` / API 的现有可信 Runner 和工具边界、Business-v2 和 Sandbox v2、已有命令只读和网络归属。新 Task 仅直连；原无明确冻结映射的任务标迁移阻塞，只读保留原 journal/历史，旧 token 不可恢复发送。 |
| Runtime、Runner 与网页 | 删 `gateway_model.py`、旧执行客户端工厂/SDK gateway 分支、`runner_proxy.py` 旧 chat completions/opaque SSE/旧观测路径、SessionSpec/RunnerBootstrap 的现役旧 transport；删 Web `GatewayCatalog.tsx`、旧目录/管理链接与新建/复盘 legacy fallback | 可信 Runtime Pi 事件/许可、Pi 1.5 私有 messages、普通及 Native Runner、全部既有只读视图和来源标识。旧历史 JSON 需要解码时用读侧兼容，不能给运行时旧协议入口。 |
| 部署与测试 | 删除 `deploy/agentgateway/`（含 relay/patch/probe）和当前 Compose、`.env.example`、README 中的网关数据面/控制面配置；去掉当前验收与自动测试对旧 SDK、服务镜像/端口/路由的要求。纯 AGW 合同测试随现役实现删除；混合 fixtures 改为测试现活 Pi/MCP 业务行为 | 保留已经发布的迁移与旧表（含 0024–0030 的 DDL 依赖）、保留历史报告、离线旧账样本和非 AGW K8s/Sandbox 验收。混合里程碑的构建探针若依赖退役网关，停用旧可执行入口并标历史，不删其证据、网络/Sandbox 共用构建和生产文件。 |

历史读取使用本地已持久 Task 快照、`gateway_task_models`、`gateway_execution_attempts`、`native_request_costs`、旧预算和复盘范围行；冻结 CNY/USD 数值和来源，未知项不伪装为零。旧复盘按原 Task 读权限及其存储的站点/时间读取，不要求配置旧站点环境变量，也不把缺少原用户证据的旧金额混到新跨站个人汇总。保留 schema/历史 DTO 不是活网关依赖。明确兼容读取路径与无网关启动；删除目录、API 和部署配置的扫描只允许 migration/存量只读合同/历史文档提及。

## P6c.3 两站切点、迁移与回退

1. 现场只读盘点两个服务面、当前 provider/model/真实费率来源、现存任务与暂停、unknown/在途调用、费用/路由、部署对象的所有权。禁止输出或导出原凭据明文。将可确认的模型和真实凭据从可信源在受限进程中直封主站现有 vault；核对目标模型、版本、别名和两个站可读；中转缺价明确待设置；原源缺凭据或没有可证明的上下文、输出、工具能力时，目录保留可编辑草稿，不从同名模型补造默认值。缺连接字段的草稿不能发送；不制造新测试真实扣费。只存在哈希的旧虚拟密钥不尝试恢复。
2. 锁住两个旧服务写入口与调度，在同一时间点核验有效租约、付费/工具 in-flight、未对账及已完成旧请求、旧路由、任务/费率快照。为两站 PG / MinIO、必要 journal / 工作卷和 AGW SQLite（含 WAL）建立可读可验证的备份，记录对象 ID、摘要及不含密钥的数量。两站必须能区分正常终态和不能安全迁移的旧暂停/未知项；从未获确认的调用不写入新发送入口。
3. 新部署时新写只走 Pi / 直连 MCP，禁旧发送权、不双写。旧 Task 若有确证可迁移，才用新 epoch/短令牌、已核准冻结模型与原始历史花费/币种并保留原 journal 和未决信息；否则可见标注“旧执行无法安全恢复”，保留阅读/导出和安全收尾，不归零或换模型继续跑。第一条新写后不能用旧数据覆盖回退。
4. 用合成 provider/工具验证没有 AGW 的候选和两站新入口；不触发真实模型账单。Native 发行用新版验收回执 v3，把同一次新 Pi/双目标验证与 Runtime 配置、Runner 和 sidecar 摘要绑定，保留原环境/VPN/采集证明。通过后停止并移除本任务拥有的 AgentGateway 工作负载、Service/入口/转发、专用部署 Secret 和 relay。只读保全 SQLite/PVC/备份、两站 Task/费用与 OpenSandbox 相关资产，不物理销毁。回滚手册分“新写前可恢复原入口”和“已新写仅新路径修复或受控重放原已确认结果”，不得还原旧库盖住新业务。

## 必要验证和分工

- Core：Blackboard、共享合同、迁移、历史读侧、产品/部署验收报告；Runtime：Pi sender 私有接线、Runner/Sandbox/Web、现场盘点/备份/切流和退役；Review：独立检查费用/身份、删除闭环和实际资产。保持隔离候选、独立 index、精确文件提交，不推送。
- 一条真实 PostgreSQL + Pi + 合成 provider 测试从已登录账号发演练：冻结版本/明确计量/缺价、重复请求同 ID 不二发、未知无法自动重试、他人或无身份不可执行/读结果、Purpose 分离。保留一条旧币种/未知离线历史读、旧暂停明确阻塞、现 Pi 暂停/恢复无重发。实际 Native/普通启动、一次合成模型与 MCP 按角色调用均在不具备 AgentGateway 环境、网络、镜像、端口的隔离实例验证。真实页面验演练成功和费用未知、无旧管理入口。
- 最终 `make check`、必要隔离/浏览器证据、两站备份/部署/退出回执、旧资产保全和原工作区精确对照后提交。在拿到现场切流门槛前先保全、不要假报已退役。
