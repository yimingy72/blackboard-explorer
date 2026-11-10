# P6 · 开发前调用、费用与退出审计

审计基线 `30ce04a343fafe4e4df7f4c967e9b93d17db9c64`，仅核对源码、固定依赖、历史回执和公开插件资料。本轮不安装插件、不写产品、不改 `docs/design/`、不运行模型、不删服务/数据。方案见 [P6 迁移计划](P6-Pi原生模型与MCP迁移-20261110.md)。

## 1. 已确认的调用证据

- 普通 Runtime：[`models.py`](../../services/agent-runtime/src/bbx_runtime/models.py) 把冻结模型投影成 AGW alias 与 `openai_compatible`，创建 `GatewayDispatch`；[`pi/client.py`](../../services/agent-runtime/src/bbx_runtime/pi/client.py) 第 258 行的 Pi 模型费率全零。[`pi/package.json`](../../services/agent-runtime/pi/package.json) 锁 Pi AI/core/telemetry 1.4.0；[`pi/worker.mjs`](../../services/agent-runtime/pi/worker.mjs) 已用 `streamSimple`、`onPayload`、`onResponse`、`onProviderStreamEvent` 和既有 Python 回调，AGW 是传输分支。`bbxUsageObserved` 来自事件中的输入、输出字段，不等同于最终 `Usage` 对象存在。现有 provider 特例不能当作所有新路径都兼容，新增范围需按协议明确验证。
- Native Runner：[`session.mjs`](../../services/agent-runtime/task-runner/src/session.mjs) 第 126–133 行把批准模型强制注册到 AGW proxy，API 是 `openai-completions`，四类费率全零。[`model-boundary.mjs`](../../services/agent-runtime/task-runner/src/model-boundary.mjs) 保留工具/发送 freeze、journal、精确请求摘要、一次发送、禁重试以及 SDK 终态；现有 settle 是 SDK 行为报告，并不结算费用。[`native_fees.py`](../../services/blackboard/src/bbx_blackboard/native_fees.py) 和 [`gateway_execution.py`](../../services/blackboard/src/bbx_blackboard/gateway_execution.py) 同时有需要保留的域授权、原调用身份以及应退出的 AGW 账务实现。
- 固定包实查：1.4/1.5 的 `pi-ai/dist/models.js` 均有 `calculateCost`，按输入、输出、缓存读写分别计算，选择可达的最高阈值 tier 作为整次请求费率，并为 Anthropic `cacheWrite1h` 使用 2× 基础输入价。模型价格的单位是 USD/百万 tokens。不能把旧 `usage_details` 的较少字段、未知 tier 或一个较长输入用例作为所有计价的证明。1.5 SDK 的 `getSessionStats()` 会统计 journal 的 usage、assistant、compaction/branch summary 及工具用量，包括压缩掉的历史；适合界面/审计核对，不是跨站跨 Agent 的幂等费用账本。
- Runner 信任：当前前台和后台 hosted helper 由 [`helper-host.mjs`](../../services/agent-runtime/task-runner/src/helper-host.mjs) 及固定 hosted 补丁进入 Runner 会话进程，后台是父方异步持有结果，不能作为密钥隔离。Runner 与 Sandbox sidecar 同 Pod 不同容器，真正执行 Sandbox/execd 另 Pod。长期模型凭据必须留在现有受信 Runtime，Runner 只持作用域能力。这里选择可信 Runtime 发 Pi 请求，避免两个各自不同的直连发送权。
- 摘要实际接缝：1.4 普通 `runAgentLoop` 接受现有 `streamFn`，Python 现有确认前缀压缩是宿主本地处理；成果整理/复盘的付费模型调用须继续走可信 worker。固定 1.5 的 `agent-session.js:2227` 和 `:3358` 将 `this.agent.streamFunction` 传给压缩/分支总结；`compaction.js:485–487` 无该参数会落入 `completeSimple`，`cache-warmer.js:239` 会直接用 `ModelRuntime.streamSimple`。维持当前两处 cache warming 关闭、禁隐式 provider 重试、无 Runner 长期凭据或直接上游网络、无任意扩展。Hosted helper 的 `prepareSession/installSession` 也须复用同一发送边界。验收从合成 provider 真实收包反证未登记的付费路径，不能只测 session UI 或计数器。

## 2. 候选复用结论

| 候选 | 公开证据 | 判定 |
|---|---|---|
| `pi-cost-tracker@0.1.0` | [固定包](https://registry.npmjs.org/pi-cost-tracker/0.1.0)、[维护者说明](https://github.com/leesama/pi-cost-tracker#how-it-works)：被动订阅会话/工具/压缩，累计本地用量、做预警；公布的实际 E2E 用 Pi 0.85.1 | 不安装。没有证明 Pi 1.5 API 兼容，也不能在跨 worker 的真实调用前原子分配额度。 |
| `pi-usage-cli@1.1.3`，命令 `pi-usage` | [固定包](https://registry.npmjs.org/pi-usage-cli/1.1.3)、[维护者计数规则](https://github.com/sumit1612/pi-usage#how-it-counts)：读 JSONL、按 lineage 去重；`--budget` 按已记账费用退出 3 | 不安装。其去重思想用于审计对照；退出码不是 provider 发送许可，不能承担额度或账本。 |
| `pi-cost@0.2.0` | [固定包](https://registry.npmjs.org/pi-cost/0.2.0)、[维护者仓库](https://github.com/NikiforovAll/pi-cost)：本地 JSONL 费用看板、实际与估算展示 | 不安装。平台已有用户账单；引入第二个看板/目录扫描会使同一用量有两套来源。 |
| Pi 1.5 内置 MCP、第三方 `pi-mcp-adapter` | 当前本地 `pi-mcp@1.5.0` 官方低层 client/transport 已用；上游 [adapter host-managed 接口](https://github.com/nicobailon/pi-mcp-adapter/blob/main/docs/extension-api.md#host-managed-embedding) | 沿用原生 transport 和平台工具权限边界；高阶自动注册没有当前 `permit/callId/journal` 约束，第三方 adapter 会增加工具治理面，暂不装。 |

协议核对资料：[OpenAI 流式用量及中断](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[Anthropic 缓存时长/费率](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)、[DeepSeek 缓存字段](https://api-docs.deepseek.com/guides/kv_cache/)。允许列表依据固定 SDK 的事件和实际合成协议验收，而非仅凭文档。非 USD、未知费用或共享中转用户未确认的价格不变成免费；原生已知金额和本地估算分别展示。若上游请求未完成，正常响应流末尾没有用量的机制不能作为实际零费证据。

## 3. 删除前反查清单

1. 从 [`native_runner_bootstrap.py`](../../services/blackboard/src/bbx_blackboard/native_runner_bootstrap.py)、`runner_gateway.py`、`runner_gateway_api.py` 检查动态路由创建、credentials prepare/reconcile 和 `/mcp-route`；从 `native_recovery.py`、`native_resource_retirement.py`、`conversations.py` 检查停止、退役、归档仍依赖旧 route 的门槛，先迁移或保存证据再删。`api.py` lifespan 和 `sandbox_tools.py` 共同依赖也要解除。`runner_resources.py` 删除只通 AGW:3000 的网络授权，替换为确证的 Blackboard 私网/Pod 内连接，保留 Sandbox 入口及业务隔离。保留命令、抓包、重组、终端及 VPN 的原只读历史和原执行路径。
2. 核对 `native_task_mcp_routes`、`native_task_budgets`、`native_request_costs`、`gateway_execution_*`、`gateway_task_models` 和 `runner_calls` 的旧读者。不可删除发布过的迁移，也不能将 SQLite 原生日志删除后再补账；必须先导出原金额/币种、冻结价目、未决/已确认和原身份，验证与两站 PG、对象存储和 journal 的同一切点。未知记录保留未知，不能用今日目录回填旧价。
3. 退出 `deploy/agentgateway/`、`deploy/kubernetes/` 的 AGW 专用探针、Relay/AGW 端口映射、镜像及部署样例、`docker-compose.yml` / `.env.example` 的旧变量和 `settings.py` 运行配置时，按调用图保留与 OpenSandbox、Runner 或历史数据共用的项。检查 Web 旧 `GatewayCatalog.tsx` 入口及生成 OpenAPI/TS。只迁移必要守卫，不让历史包测试把已退出的 AGW 重新装进新产品验收。
4. 两站均确认旧流量、未决费用、Task 路由与原身份全部可处置前，不停止 AgentGateway。独立加密备份它的配置、SQLite/PVC、原费用数据和两站 PG/S3/journals；迁移配置不读原 `.env`、不打印或尝试恢复哈希虚拟密钥。切换后停止并移除本任务拥有的 AGW 工作负载、服务、入口、专用部署 Secret 属于本次授权；原 PV/SQLite/账务备份先只读保留，物理销毁另行确认。上线出现新写时，不能直接回滚旧库，使用受保护的回退读证据或前向修复。
5. 当前现场零任务是一次快照。以实际切点盘点暂停/保留任务；可恢复者须核对冻结模型、原币种累计费用、journal 和未决证据后换代，并拒绝旧 token 和重放。不能证明迁移时保全原快照并显示阻塞。用一份旧暂停夹具证明恢复或明确阻塞，不能把旧账只读当作丢弃旧恢复路径。

## 4. 实施前硬门槛

- root 定案保留旧累计停止阈值及多席位并发，逐笔在所属站 PG 事务内核发新许可；已在途可能越线。未知不可当作零，未决阻断不安全准入，严格预留仅作为后续可选能力。主站 `platform_configs` 唯一目录/凭据权威；两站读取冻结版本，各自管本地 Task 费用，用户复盘跨站以 `(site_id, callId)` 统一去重汇总。
- 在合成端到端验证 1.5 journal/compaction/hosted helper 到可信发送 worker 的消息与结算合同；两个 Pi 版本不同不能只凭类型名称认为可以直连。任何自动 provider 重试必须关闭或由持久新授权覆盖，且检查绕过 `fetch` 的 SDK 协议。
- 外部 MCP 注册、密钥与探测必须按管理员授权并加 egress 守卫；已发布工具和可信 schema 版本决定角色调用能力。侧车环回不等于认证；direct 业务和 Sandbox 必须先启用分开的新 issuer/audience 并拒绝未过期旧 `aud=mcp`，保持 initialize/list/call 作用域、任务停/恢复时的 epoch 与短期凭证，以及未决写不重发。

root 已确认由主站现有存储作为唯一权威、以已核对的稳定 UID 或单用户操作者授权、保留并发累计阈值并按所属站计费，以及 P6a/P6b/P6c 的验收顺序。运行审计核实了双目标接线、hosted helper 信任和原数据保全；独立审计复核了费用证据、缓存预热绕路、旧 MCP audience、停用/删除边界和操作者授权。这是方案级通过；实施前按本次授权同步正式设计，环境验证及产品交付仍须满足各阶段门槛。本轮没有运行模型、安装候选插件或运行 `make check`。
