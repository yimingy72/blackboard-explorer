# 普通黑板前端、稳定性与性能优化报告

日期：2026-10-15。前端在 `/Users/yym/bbx-wt/ui-rebuild`，后端/运行时在 `/Users/yym/bbx-wt/ctf-cooperation`。两处原有在办改动均保留，本次未执行 Git 写入。

## 1. 前端交付

采用用户已经认可的 CTF 明亮风格：浅色画布、蓝色操作、白色详情、紧凑顶栏、短时动作。使用已有 Ant Design、Ant Design X、React Flow、resizable panels；没有新增产品依赖或另建前端框架。

- 重做 Goal、Fact、Intent、Agent 节点、图标和状态标签。状态有文字、图形及浅底；作者颜色继续关联实际产出。已关闭且未定的意图不会被标为成功，Fact“已提出”不冒充验证通过。
- 默认画布，按点击展开详情。事实分概览/关联/证据，意图分概览/执行记录/关联；正文、代码与原文保留，低频元信息收紧。修正 Fact `resolves` 的展示含义为关闭的意图，未制造“解决其他事实”的关系。
- 普通画布提供按需/全关系、节点定位、缩放、全图和整理；历史 Agent 收起但仍可从常驻列表、产出或定位进入会话。
- 抽取公共坐标校验与存储，普通/CTF 模式、任务分别存储；原 CTF key、格式与边界保持。拖动节点可保存，刷新恢复；固定标记可解除，存储失败明确提示。回放布局不覆盖实时布局。
- 与 CTF 一致的短时光束及状态反馈，仅跟随初始基线之后的真实事件；回放、减少动态不播放。新节点布局按拓扑调度，持续进度不能反复取消期限，坐标重置在成功应用前不会丢失。
- 对话保持草稿、初始/同步上下文、真实模型推理、工具与复盘。思考置于回复前，移除每轮“没有推理”等重复说明，工具结果采用显式失败标记及格式化 JSON，不把任意 shell JSON 的 `error` 当平台异常。
- 补齐 `agent.reactivated`、`task.resumed`、`cost.reconciled` 的 SSE 订阅与 Derive 复用投影。账户用量补登记仅加使用量，不修改当前步数、上下文或运行状态。

没有改变通用黑板的 Fact/Intent/Explore/Derive/Close 职责，不把 CTF 的 Lead 角色套入普通模式。

## 2. 后端实际缺陷与修复

### 2.1 快照内容与水位不一致

原 `state()` 在 READ COMMITTED 的多笔 SELECT 间可能发生正常写入：读到旧 `task.status=running`，却取得新 `version=5`，随后 `events(since=5)` 为空。随机 PostgreSQL 并发复现后，普通读取使用同一 REPEATABLE READ 快照，不加任务写锁。现在旧内容只配旧水位 4，版本 5 的 failed 事件仍可追读。

当前普通前端原本用全量 events 初始化，不依赖这个 state 快照；不能把上述 API 缺陷说成当前 UI 已必然丢事件。runtime 的状态和同步水位语义则确实需要一致快照。

### 2.2 不确定结果诱导重复操作

旧 ToolLog 将所有普通异常变成“检查参数或换方法重试”，工具函数也吞 RemoteError。原生 MAF 复现首次 `post_intent` 已提交但应答丢失，随后模型再提交，最终两条意图。

现在只将既有、可纠正 RuleViolation 返回固定提示；未知、权限、代次、控制传输、日志及保存故障通过公共 `MiddlewareFailure` 桥接退出循环。已完成工具结果留在原恢复缓存，不用再次执行换取日志。

内建 exec 原 MAF MCP 还会在连接关闭后重新发同一请求。现在通过公共 `MCPStreamableHTTPTool.call_tool` 覆写、`ClientSession.call_tool` 单次派发及 `FunctionTool.parse_result` 保留工具发现、连接生命周期与原 JSON 契约；等待使用命令 timeout 加 30 秒传输余量。原生 MAF + FastMCP 内存传输验证：丢应答仅一次效果，没有 SDK 重连重发。

### 2.3 启动恢复的一次短断会退出整个运行器

旧 supervisor 初始 recover 位于正常 tick 错误处理之外，一次 state ReadError 即结束监督任务并使 runtime 退出。现在仅对控制传输退避，跳过本实例已经启动的任务；部分恢复后重试不会把新 Agent 当旧进程结束。真实配置错误不被永久隐藏。

### 2.4 成功模型用量可能丢失或重复计费

旧路径取得实际 usage 后先发非幂等 heartbeat；提交前断开时，原生 Session 没有该费用凭据。原生 300 输出 token 复现已确认。

现在完整响应、原生 history 与 identified receipt 同次保存，再发幂等 heartbeat。服务端验证持久 Session、actor、原 task run/derive round、请求摘要及固定 Profile，按请求时刻重新计价，不信客户端 cost。

- 同 ID 同 payload 返回原确认，步数、费用不重计；同 ID 不同 payload 拒绝。
- 旧 round、旧 run、终态只补费用，不更新心跳、上下文、游标或唤醒 Agent。
- finish、resume 与最终报告提交前补结算，随机 PG 验证费用更新先于报告/完成事件。
- Session 回执能力标记确认后才发新 receipt heartbeat，防止旧 backend 忽略新字段后按旧 additive 接口重复收费。旧客户端无 ID 的接口保持兼容，但不宣称幂等。
- 完整响应缺少 usage 时显式 `usage_known=False`，保留已报告 token 和成功步骤；确认后停止继续工具/模型，并保留原 receipt 阻止重启盲跑。费用仍可能不完整，不能把它说成已知的零元。
- 严格退出前登记一次已保存 `model_output`，费用异常不会让可见正文消失；trace 错误也不能覆盖原费用登记问题。

## 3. 性能证据

| 路径与合成规模 | 优化前 | 优化后 |
| --- | --- | --- |
| 普通 state，1000 条工具日志 | 7 SELECT，读取 **10,361,000 B** 后丢弃，PG 5 次中位 37.78 ms | 6 SELECT，不取工具日志，3.91 ms；写规则默认仍完整校验 |
| Agent 消息列表，1000 结果的 Session | 为元数据读取 **6,593,149 B**，48.99 ms | 仅 origin **22 B**，3.10 ms；查询数 4、API 正文 89 B 保持 |
| 2000 Fact 的 relied_by | 遍历 **400 万**次，纯 CPU 422.45 ms | 逆引用累计 **2000**次，2.289 ms；重复引用去重、不同作者、争议状态和深度等价 |
| 前端 1 万历史事件 + 1000 Fact，一个 progress | 重放 10001 事件，Node 9 次中位 2.578 ms | 推进 1 事件，0.0026 ms；输出相等、旧快照不变 |
| 正常一次模型响应的可靠记账 | 原 opening/response/final 保存 | 仍 **3** 次，合成快照 `[196,1631,1365]` B、receipt 509 B，无额外清空保存 |

前端拓扑相等比较改为集合线性比较；只有关系变化而无新增节点不再重新调用 ELK，普通时序消息关联也改为索引匹配。乱序新旧事件仍正确重建日志及投影，未以减少状态正确性换速度。

这些是组件微测及操作/字节数，不代表实际模型求解时间、缓存率或通关率；本轮未调用真实模型或靶机。

## 4. 验证与本地发布

实际最终结果：

- Python `make check`：Ruff format/lint、Pyright **0 errors/0 warnings**，**1971 passed**。33 个警告为现有及新增 MCP 兼容测试的 SDK sampling 弃用提示。
- 随机隔离集成：**340 passed**，426.88 秒，包含普通用量/快照/投影与原 CTF 关闭、恢复、结算、执行回归。
- 前端 ESLint/TypeScript：全绿，**117 passed**；生产构建成功。Ant CLI 对修改的组件目录及工作台页检查 **0 issues**。
- Mock Chromium 浏览器：**36 passed**，桌面/手机、普通/CTF 兼容回归通过；新的拖动刷新、持续进度坐标重置、Derive 复用专项均已运行。
- 已查看生成截图，确认桌面及 390px 会话/详情排版、代码块、状态与画布。截图只含合成任务，位于前端 `.data/blackboard-refresh/conversation-1440.png`、`conversation-390.png` 及 `.data/qa/contribution-desktop.png`。

本地应用已更新，[测试地址](http://127.0.0.1:58000/login)。部署前及关闭黑板后均确认 **0 active tasks/0 running turns**，仅替换停止的应用容器内源码/静态构建后启动；数据库、MinIO、卷和用户配置未重建，未发布新提示词或 Profile。运行容器镜像身份保持原值，其源码/前端字节已与新候选核对；当前镜像标签已更新，后续重建可保持同版。

候选及运行文件核对：runtime **75**、blackboard/前端 **162** 文件无差异，Linux runtime 新模块及普通用量协议导入成功。镜像为 `bbx-blackboard-refresh-runtime:20261015`（`sha256:4e04a5b0c39d4e43e14353e30678ee2b33933e11dfda9a8d80d63b6fd3269452`）、`bbx-blackboard-refresh-blackboard:20261015`（`sha256:d9b7ca1b6c79fe2a0bd4a5843111c2182bce55162347bd83e3c8b79592b26923`）。当前 `bbx-agent-runtime:latest`、`bbx-ctf-blackboard:latest` 指向它们。

发布前后 **13 张业务表数量及内容摘要完全一致**：13 任务、17 运行轮次、80 Session、82 CTF成员、349 回合、651 条团队消息、198 题目、1050 记录、26 Profile、3 平台配置及既有事件。schema仍 **0016**，本次无需迁移。

证据位于后端 `.data/blackboard-refresh/{check.log,integration.log,release/}` 与前端 `.data/blackboard-refresh/{web-check.log,web-build.log,e2e-final.log,frontend-performance.json}`。源码备份在 `release/before-source/`，发布验证与前后数据库摘要为该目录 JSON 文件，不含 `.env`。

可复现命令：

```sh
cd /Users/yym/bbx-wt/ctf-cooperation
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env -o image-agent-runtime test-integration
cd /Users/yym/bbx-wt/ui-rebuild/web
pnpm check
pnpm build
BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  pnpm e2e smoke.spec.ts detail-polish.spec.ts goal-disclosure.spec.ts contribution.spec.ts \
  blackboard-refresh.spec.ts conversation-continuity.spec.ts ctf-orbit.spec.ts ctf-challenge-delivery.spec.ts --workers=2
```

浏览器使用合成 API，未认证真实任务、创建任务或发送实际 Agent 消息。覆盖桌面、手机、拖动/刷新、持续事件中的坐标重置、Goal/附件/草稿、推理、证据、回放、Derive 复用与原 CTF 环绕布局/多 Agent 同题。

Ant Design 按项目 6.6.5 查阅公开 API，CLI lint 无问题。临时更新 CLI 仅在忽略的任务目录，未更改全局工具或产品依赖。技能扫描未识别已存在的 `web/PRODUCT.md`，实际直接读取现有产品和设计规范，未重新初始化或覆盖它们。

## 5. 偏差与待决

- 内建 exec 单派发不等于远端恰好一次，也不证明超时即已停止；仍需原有取消/归档机制。外部 MCP 仍采用原 MAF SDK 连接重试，任意富媒体转换没有足够公共默认接口，不宣称所有外部写工具均已消除 SDK 重放。
- 未增加缺失 usage 的补齐接口、unknown 账本或预留系统；模型服务须报告完整用量。未知 receipt 停止继续 dispatch，需要显式处理，不能强行抹除未知标记。
- 普通控制层仍有全量 facts/intents 状态读取；已消除无用工具日志和二次引用扫描，本轮未新增第二套控制缓存/窄控制协议，也未改变轮询周期、Agent 数量或角色语义。
- CTF 接口、提示词、模型配置、用户价格及 `docs/design/` 不变；共享 Session 新路径只有普通 outbox 时启用，CTF/review 回归保留。
- 前端已有大 bundle 警告仍在，ELK 延后按需加载；不将 bundle 阈值调整冒充性能优化。手动坐标仅当前浏览器保存，不宣称跨设备同步。

## 6. 建议提交划分

原工作树有此前 CTF/前端改动，提交须分 hunk，不混记成本次新实现：

1. `feat(ui): align ordinary blackboard workbench with ctf visual system`：节点、按需连线、详情/会话、公共坐标存储及浏览器回归。
2. `perf(ui): advance board events incrementally and retain live state`：COW 投影、事件订阅/复用、索引及相关测试。
3. `perf(blackboard): make state reads consistent and trim unused payloads`：读取快照、工具日志/Session 投影、依赖累计及随机 PG 回归。
4. `fix(runtime): stop uncertain tool replay and retry startup recovery`：公共异常、单派发内建执行、恢复及原生 MAF 回归。
5. `fix(billing): persist ordinary usage receipts before idempotent settlement`：必要合同/生成物、runtime history/receipt、后端补结算及兼容回归。
6. `docs: record ordinary blackboard refactor and performance evidence`：任务和本报告。

开发交付时未提交、合并或推送。后续按用户明确的 Git 发布要求执行，记录如下；提交均遵循仓库要求的任务 footer。

## 7. Git 发布补充 · 2026-10-15

用户明确要求将本次普通黑板前端与架构优化提交、合并 main 并推送 GitHub。已按已验证源码分组提交：

- `162c7da`：普通 state/Session 读取及事实依赖性能。
- `a2aadf0`：普通工具不确定结果、内建执行单派发与启动恢复。
- `b430bb0`：普通模型用量持久凭据、幂等补结算及合同生成物。
- `7f21272`：前端增量事件、消息索引及复用/费用订阅。
- `da9e57f`：普通画布、状态、详情、会话及布局。

同工作树内已完成并部署的前序交付分开记录，未将其冒记为普通黑板新功能：CTF 代码 `37605e2`、角色说明 `b4c5a73`、报告 `550b714`；前序界面细节 `6d99d1a`、CTF 环绕及题目 A 方案 `ed109fd`。公共坐标存储随共享 CTF 组件提交，再由普通画布复用。原后端分支还有已提交的上下文修复，合并保留这些历史及 main 的 Kali/事件契约修复。

生成前端类型取自最终后端 OpenAPI；后端工作树里较旧的前端副本不覆盖最终 UI。未纳入旧的预览方案及其专用浏览器用例，也未提交 `.data/`、构建输出、日志、数据库备份或 `.env`。改动前的完整差异及未跟踪文件备份保留在两工作树忽略的 `.data/blackboard-refresh/git-publication/`。

最终合并、检查和远端同步状态记录在 `docs/HANDOFF.md` 第 0 节。此次 Git 发布不重启运行服务，不调用真实模型或靶机，不改现有任务与配置。

后端及前端合并分别为 `a397c02`、`d7a9937`。main 的148个后端/94个前端改动路径与已验证源文件一致，候选部署清单的非静态后端文件无差异，最终OpenAPI重新生成的TypeScript也完全一致。main追加执行 `make check`：**1971 passed**、Pyright **0 errors/0 warnings**；`pnpm check`：**117 passed**。首次前端检查仅因主工作树未安装已声明组件依赖失败，`pnpm install --frozen-lockfile`恢复后通过；锁文件和依赖版本不变。已验证同版的340项隔离集成与36项Mock浏览器未为Git发布重复运行。
