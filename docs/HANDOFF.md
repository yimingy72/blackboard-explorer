# 黑板式探索系统 · 开发交接文档

> 交接时间：2026-09-24。交接方：Claude（负责调度与审查）。接手方：用户，后续直接使用 Codex 开发。
> 本文保留 2026-09-24 起的开发交接与里程碑记录。当前进度以第 0 节为准；第 3–6 节中的旧派发命令、沙箱限制和代理方案是当时的历史记录。当前启动、探索机制和网络配置见 [README](../README.md) 与 [使用与部署](使用与部署.md)。

---

## 0. 一页速览

- **项目定位**：**通用问题求解引擎**。黑板式多Agent是实现机制：给定目标、领域资料与文字验收条件，Agent通过共享Fact/Intent协作，Close裁定是否达成。代码诊断和mini-shop只是测试场景；通用Profile不预置任何单一评测的专属检查清单。
- **仓库**：`/Users/yym/blackboard-explorer`（`main` 分支，已打标签 `m0`、`m1`、`m2`、`m3`、`m4`、`m5`）。
- **设计正本**：`docs/design/` 下三份文档（桌面上的同名文件是指向它们的符号链接）。实现以它们为准。
- **进度**：

  | 里程碑 | 状态 |
  |---|---|
  | M0 工程基座 | ✅ 已完成、审查、合并 |
  | EVAL 评估目标（玩具任务 + mini-shop） | ✅ 已完成、审查、合并（mini-shop 规模偏小，见 EVAL-2） |
  | M1a 黑板存储与领域规则 | ✅ 已审查、修复、验证并合并（2026-09-24） |
  | M2-env 执行环境与出网代理 | ✅ 镜像构建、容器测试和安全修复完成，已合并（2026-09-24） |
  | M1b 黑板接口层 | ✅ API、鉴权、SSE、对象存储、镜像与模拟器已合并；按用户新设计取消向量预检 |
  | M1-W 画布最小版 | ✅ 实时图谱、页面、类型与浏览器验收完成，已合并；`m1` 标签已打 |
  | M2a 技术验证与 runtime 骨架 | ✅ 173 普通、12 集成、4 DeepSeek live 检查通过，已合并 |
  | M2b 单 Agent 跑通 | ✅ 206 普通、13 集成检查与真实种子交接通过，已合并；`m2` 已打 |
  | M3a 调度器、清扫与恢复 | ✅ 293 普通、18 集成检查通过，决策覆盖率100%，已合并 |
  | M3b 裁定、收尾与完整闭环 | ✅ 304 普通、31 集成（含13场景）、真实e2e通过；已合并，m3已打 |
  | M4 工作台 | ✅ 309 普通、29 前端、32 集成、6 Playwright 与真实运行 UI 复盘通过；已合并，m4 已打 |
  | EVAL-2 | ✅ 用户改由 Codex 完成；1434 行基线、6 个分支提交、54 项目标测试与既有验证通过，已合并 |
  | M5 | ✅ 运行器/评分/基线实现已合并，m5已打；真实5+5评估与复核已补做，当前数据未证明多 Agent 优于 single |
  | M5-tool-feedback | ✅ 工具参数错误反馈独立合入 main；359 普通、35 集成检查通过（2026-09-26） |
  | Agent-budget-context | ✅ Agent不再接收剩余金额/时间；保留系统限制与记账，369普通/35集成检查通过（2026-09-26） |
  | M5-E2 通用框架收尾 | ✅ 通用提示词、领域无关e2e入口、产品定位已合并；373普通/35集成/前端构建、数据核对与代码诊断两个真实闭环通过 |
  | Direct-egress 默认直连 | ✅ 按用户最新决定更正旧网络限制；375普通、36集成、29前端通过，当前工作台已部署，运行时新建Ubuntu容器直连用户靶机返回HTTP200 |
  | Workbench-UX | ✅ 任务内 Agent 常驻列表、上下文/注入/模型输出对话记录、紧凑工作台与耗时、创建表单重做；380普通、38集成、33前端、9浏览器测试通过，已部署 |
  | Detail-polish | ✅ 缩略图缩至128×84，详情正文Markdown排版，移除筛选与自动折叠；380普通、38集成、31前端、10浏览器测试通过，已部署（2026-09-27） |
  | Header-polish | ✅ 顶部验收入口及工具栏移除，状态行下方加分割线，结束原因与运行统计同排；380普通、38集成、31前端、10浏览器测试通过（2026-09-27） |
  | Contribution-view | ✅ 作者颜色与Fact/Intent关联、真实产出数量、长正文阅读排版/原文、底部抽屉改顶部按需复盘；380普通、38集成、35前端、11浏览器通过，已热更新（2026-09-27） |
  | Interactive-agents | ✅ 及时发布/并行derive、运行中定向消息、持久Session/只读续聊、确认删除与重试清理；412普通、45集成、36前端、14浏览器及真实隔离闭环通过，已部署v2配置（2026-09-27） |
  | Platform-config / CNY | ✅ 平台多模型与加密凭据、Explore外部MCP/工具白名单、worker完整提示词/参数表单、移除版本对比、人民币账本展示；435普通、47集成、37前端、18浏览器通过，schema0005/default与single v3已部署（2026-09-28） |
  | Worker-settings | ✅ 三Worker直接编辑/提示词下次模型调用生效、去历史/浏览器默认、平台默认模型/任务选模型、凭据统一加密保存/自动迁移、MAF Python 17连接方式；481普通、48集成、32前端、17浏览器通过，schema0006已部署（2026-09-28） |
  | Completion-review | ✅ 明确完成可免复核，其余正常收尾必经derive与再次Close；事务门控/过期失效/无效回执保护；509普通、55集成、32前端、17浏览器通过，schema0007已部署（2026-09-28） |
  | Runtime-continuation | ✅ 同任务续跑/归档恢复、Derive同ID会话复用与轮次隔离、MAF看图、证据分页、峰谷计费及审计校正；545普通/59集成/32前端/18浏览器通过，schema0010已部署，真实DeepSeek看图通过（2026-09-28） |
  | Runtime-failure-hardening | ✅ NUL安全会话、SDK瞬时错误4次重试、脱敏model_error trace、Derive分轮初始上下文；570普通/62集成/33前端/18浏览器通过并部署（2026-09-28） |
  | Provider-failure-coalescing | ✅ 同一120秒窗口内并行模型瞬时故障只计一次，不消耗Intent/种子尝试；终态冻结与Close重试同步修复；580普通/65集成通过，schema0011已部署（2026-09-29） |
  | Agent-settings | ✅ 主会话重做Worker/模型/MCP配置页，模型context_window与80%交接阈值；586普通/65集成/33前端/20浏览器通过，本地已部署（2026-09-29） |
  | Runtime-streaming | ✅ 六类 OpenAI 系连接器的 Worker/只读对话启用后端流式；逐调用记账、完整终态校验、百智云空占位兼容；671普通/65集成与真实两轮工具烟测通过，已合并本地部署（2026-09-29） |
  | Responses-tool-order | ✅ 修复混合助手正文与工具调用的 Responses 本地回放顺序；最新任务 12 次内部400001/外层502通过中性A/B定位；677普通/65集成及真实混合输出两轮通过，已合并本地部署（2026-09-29） |
  | Control-connection-resilience | ✅ 调度/清扫内部传输异常恢复、Session取消提交确认、归档413保留工作区并暂停重复压缩；695普通/65集成通过，已合并本地部署；4891d8c7第2轮工作区约12.8GB/10.6万条目，完整归档及旧任务续跑仍受容量阻塞（2026-09-29） |
  | 历史E2效果样本 | 已归档3次default及1次single，4次均由主代理复核；1次中断、1次未开始，不能据此推断统计收益 |

- **当前交付**：M0–M5软件及通用框架收尾已完成；当前执行环境默认直接出网，代理白名单仅在显式代理隔离模式启用。详见`docs/tasks/M5-E2-report.md`与`docs/tasks/Direct-egress-report.md`。本机`http://127.0.0.1:58000`已按新配置启动，原用户任务已finished且workspace归档仍在；需新建任务以使用新网络。最终通用Profile在非代码CSV核对（3行统计、9条问题全部匹配参考）与代码诊断（100/100、200/200复现且有互斥对照）均完成真实闭环；两次估算费用合计约USD0.1447。预算数值不注入Agent；自主判重、Explore声明、Close裁定的机制保持。旧E1/E2采用代理限制且对比样本不完整，不能与最新网络配置混算或宣称统计优势。已有开发、设计同步、构建和Git授权继续有效；用户已允许按需使用GPT-6 Sol high协作开发。
- **最新运行能力**：Runtime-continuation已合并部署；任务页可显式续跑（保留黑板/会话/旧产物、追加预算与活动时间，软硬链接/系统包/进程不恢复）。derive同ID多轮复用，旧轮token/请求受事务隔离；超阈值仅切模型输入段，完整会话不删除。新增view_image、证据分页与官方DeepSeek峰谷估算；最新任务4a695a5b费用校正v5532，由约¥10.89改为¥5.45，未自动重新执行。默认模型v2使用时段估算；545普通/59容器/32前端/18浏览器与独立DeepSeek红图测试通过，实测视觉估算¥0.00242224。详见[实施及性能报告](tasks/Runtime-continuation-report.md)。
- **最新故障修复**：任务425ceb85因3次模型请求异常连续触发失败；另有13次命令二进制NUL导致Session JSONB写入失败。现已在PostgreSQL边界转写U+0000、为DeepSeek/OpenAI系启用SDK 4次瞬时错误重试、持久化脱敏model_error类别/状态/请求ID，并按derive轮次保存初始上下文。570普通、62容器集成、33前端、18浏览器通过；部署HTTP NUL烟测成功，失败任务未自动续跑。见[运行失败收敛报告](tasks/Runtime-failure-hardening-report.md)。
- **并发故障合并**：任务ae3919e0的4个Agent在13秒内各自耗尽5次连接尝试，旧策略在第三个结束时误判为三次独立失败。现将120秒内的connection/timeout/429/409/5xx合并为一个任务级故障，不消耗Intent attempts或种子空产；持续跨三个窗口仍终止，正常完成清空窗口，终态后交接不改写失败计数。580普通/65容器集成及生产4并发事件烟测通过，schema0011已部署；原任务未自动续跑。见[模型瞬时故障合并报告](tasks/Provider-failure-coalescing-report.md)。
- **最新调度与验证**：普通met不再直接收尾；只有Close逐项记录明确完成依据及支撑事实、且覆盖最新黑板，才能免derive。其余静止任务必须“完成复核”derive，再由Close裁定；新黑板变化使旧复核失效，失败/拒绝/假空回执不可通过。关闭主动推导仍保留必要复核，single新旧评估口径不能混算。509普通、55集成、32前端、17浏览器通过；schema0007/worker revision5已部署，两个容器无重启，原3任务保留。见[Completion-review报告](tasks/Completion-review-report.md)。
- **Worker-settings交付记录**：Worker页面仅explore/derive/close直接编辑，隐藏历史版本、配置名、浏览器默认、内部标识。平台默认模型跨浏览器保存，任务创建选择模型；三角色共用任务模型。系统提示词保存后下一模型调用生效，模型/工具/预算仍固定。支持17种MAF Python连接方式；凭据全部平台加密保存，旧DeepSeek环境key已自动迁移为stored。schema0006，两个容器无重启、唯一运行锁；原3个任务USD账本保留。481普通、48集成、32前端、17浏览器通过，本轮无真实收费调用。见[Worker-settings报告](tasks/Worker-settings-report.md)。
- **Platform-config交付记录**：Agent配置页有Worker配置、平台模型、MCP工具三个入口，移除版本对比。支持DeepSeek/OpenAI Chat Completions/Responses/OpenAI兼容接口；Explore可挂载Streamable HTTP MCP并选工具。平台密钥加密且只写，已有DeepSeek复用环境密钥。default/single v3以人民币计价，旧3个任务保持USD；schema0005，服务运行正常。435普通、47集成、37前端、18浏览器通过，本轮未调用真实模型。见[Platform-config报告](tasks/Platform-config-report.md)。
- **上一轮真实闭环及验证**：Interactive-agents 的412普通、45集成、36前端和14浏览器检查通过；真实CSV闭环2/2验收满足，首Fact约31秒、种子58秒，峰值2个Explore/3工作槽位，1次并行derive。运行中消息送达、结束后两轮复盘与重启Session保持、删除测试任务清理均通过，估算总费用USD0.165891。
- **Interactive-agents 工作台记录**：Agent右侧支持输入消息与持久续聊；运行中下一轮模型调用送达，结束后只读复盘，删除任务才清会话。旧任务使用明确标记的legacy上下文。已部署schema0004，default/single最新v2，原3个任务与3份归档保留；新任务应选最新Profile。见 [Interactive-agents报告](tasks/Interactive-agents-report.md)。
- **最新界面修订**：Detail-polish（`a12275c`）将事实/意图/目标正文改为 Markdown，图谱筛选和自动折叠已移除，全部对象可见；保留验收摘要与运行统计，缩略图128×84。本地前端与最终构建一致。验证为380普通、38集成、31前端、10浏览器通过；见 [Detail-polish 报告](tasks/Detail-polish-report.md)。

---

## 1. 仓库与文档

```
blackboard-explorer/
├─ AGENTS.md                 # Codex 的项目规则（每次启动自动读取）——务必先读
├─ docs/
│  ├─ design/                # 设计正本（概念设计 / 实现架构 / 开发方案）
│  ├─ tasks/                 # 每个任务的说明 <T>.md 与 Codex 报告 <T>-report.md
│  └─ HANDOFF.md             # 本文
├─ packages/contracts/       # 共享数据模型（pydantic v2）与导出的 JSON Schema
├─ services/{blackboard,agent-runtime,envd,egress-proxy}/
├─ profiles/default/         # 默认 Agent 配置（deepseek-flash、参数、提示词模板占位）
├─ eval/                     # 玩具任务与评估任务（targets / tasks / answers / runner）
├─ docker-compose.yml        # postgres(pgvector) + minio(pgsty) + 建桶
├─ docker-compose.dev.yml    # eval-targets（叠加使用）
└─ Makefile
```

**常用命令**

| 命令 | 作用 |
|---|---|
| `uv sync` | 安装/同步 Python 依赖 |
| `make check` | ruff + pyright + 普通测试（不需要 Docker、不访问网络、不调用真实模型） |
| `make test-integration` | 需要 Docker 的测试（testcontainers，标记 `integration`） |
| `make up` / `make down` | 启动 / 停止本地基础设施（需要 `.env`） |
| `make schemas` | 导出 contracts 的 JSON Schema |
| `make eval-targets` / `make eval-verify` | 生成评估目标 tar 包 / 验证植入问题确实存在 |

**三份设计文档各管什么**

| 文档 | 内容 | 写任务说明时常用的章节 |
|---|---|---|
| 黑板式探索架构设计.md | 概念与规则：Fact / Intent、写入规则、调度、裁定、提示词、参数 | 3（黑板内容）、5（调度）、6（同步）、8（提示词）、9（参数） |
| 黑板系统实现架构.md | 容器、数据库表、API、MAF 用法、中间件、前端 | 0（框架判断）、2（执行环境）、3（数据模型）、4（blackboard API）、5（调度实现）、6–7（Agent 与中间件）、8（前端） |
| 黑板系统开发方案.md | 仓库结构、里程碑与检查点、测试策略、技术验证清单、评估任务 | 4（里程碑）、6（技术验证）、7（玩具/评估任务） |

---

## 2. 当前状态明细

### 2.1 git

- `main`：已包含 M0–M5、M5-E2、Direct-egress 与 Workbench-UX；最近功能提交为 `7cc9ced`（对话记录后端）和 `7b53890`（工作台）。现有里程碑标签为 `m0`、`m1`、`m2`、`m3`、`m4`、`m5`。
- M1a：设计提交 `f47808d`，实现提交 `46bc578`，合并提交 `430c5ad`。
- M2-env：token 设计 `ff8565f`、信号能力设计 `1d5f4c0`、实现 `bca1fd9`，合并提交 `108d0c5`。合并时保留两边依赖并重建 `uv.lock`。
- M1b：接口设计 `6371507`、profile 正文快照 `561103c`、Agent 自主判重设计 `7de008a`、实现 `87f774f`，已快进合并；原 m1b 工作树与分支已删除。
- 合并时 main 的旧 embedding 段落有一处用户未提交编辑，已保存为 stash `Preserve pending edit to superseded embedding design`；该段现由用户新设计整体替代，未将旧段重新应用。
- M1b 模拟器生命周期修复 `eed65d4` 已合并；M1-W 实现 `a17a905` 已合并，`m1` 指向该提交。原 m1w 工作树与 m1-w 分支已删除。
- 原 `~/bbx-wt/m1a`、`~/bbx-wt/m2env` 与对应分支均已删除。原工作树的 AGENTS.md 副本已确认与 main 完全相同后清理，最新规则保留在 main。
- 仓库本地 git 身份仍为 `yym <yym@localhost>`；未修改全局配置。用户已要求创建并上传 GitHub，私有远端为 `https://github.com/yimingy72/blackboard-explorer`。

### 2.2 早期基线验证（2026-09-24）

| 项 | 结果 |
|---|---|
| `make check`（main） | 50 项通过 |
| `make up` | postgres、minio 健康；bucket `blackboard` 已建；pgvector 0.8.6 |
| mini-shop | 5 个提交；P1–P6 全部验证存在（P2、P3 各 20/20 复现）；tar 包无答案线索 |
| order-service 玩具任务 | 偶发失败率 36%（18/50）；稳定复现方法 100/100 |
| DeepSeek `deepseek-flash` | 工具调用可用；不回传 `reasoning_content` 不报错；`reasoning_effort` low/high/max 可用；用量含缓存命中/未命中 token |

### 2.3 各阶段验证记录（2026-09-24 起）

- **M1a**：修复 attempts、derive 失败计数与已结束 Agent 写入后，`make check` 110 通过、`make test-integration` 6 通过、领域层覆盖率 91%；Alembic 升降级通过。
- **M2-env**：`make check` 61 通过、`make test-integration` 2 通过，两个镜像已构建。新增必要的 `KILL` capability，修复 token 传递、路径竞态及测试内部网络访问。
- **M1b**：141 个普通测试、11 个集成测试通过；blackboard 镜像构建和真实容器启动/自动迁移验证通过，OpenAPI 已导出并由测试守护。
- **M1-W**：前端 ESLint/TypeScript/Vitest 10 项通过，构建与镜像静态托管验证通过；真实浏览器完成中文创建、启动停止、SSE、争议与详情、390/1440 视口检查，预览资源已清理。
- **M2a**：设计 `1d5d853` / `1256820` / `701e214`、实现 `c025955` 已合并；普通检查 173、集成 12、真实 DeepSeek 4 项通过。详细逐项结论见 `docs/tasks/M2a-report.md`；M2b 必须遵循增量持久追加与缓存字段适配。
- **M2b**：设计 `8e13d90` / `f42bcf0` / `0e46c91`、contracts `d947e1e`、实现 `9f8c853` 已合并，m2 指向实现提交。取消清理、内部 MCP 避免外部代理、单请求/整个 Agent.run 超时均有回归验证；真实检查详情见 M2b 报告。原 m2a/m2b 工作树和分支已删除，M2b 诊断产物保留在主工作树忽略目录 `.data/checkpoints/m2b/`。
- **M3a**：归档/恢复计数 `8b2c5f3`、调度与部署 `74f67cc` 已合并；293 普通、18 集成、37 决策/100%覆盖、11 前端测试通过。runtime/blackboard 镜像已构建；3并发、重启零attempts、PG锁排他/丢失、镜像SIGTERM检查通过。M3b需接着验证13完整场景和真实e2e，m3尚未打。原m3a工作树/分支已删除。
- **M3b**：核心/13场景 `55703c3`、真实e2e入口 `44686c4` 已合并，m3指向后者；304普通、31集成检查通过。真实任务 `25ca10e4-8e1e-46cf-8901-82e165f0ea6c` 完成2/2验收、5事实/2意图/4Agent，报告6233字节、归档5032793字节，账本估算USD0.070260516。M4已用 `bbx-e2e-f7903553` 完成真实浏览器复盘；2026-09-25 已清理该项目全部容器、网络和卷，原55013端口不再服务。产物/Compose配置保存在主工作树 `.data/checkpoints/m3b/e2e/bbx-e2e-f7903553/`。原m3b工作树和分支已删除。
- **M4**：设计 `af29bb3`、只读归档 API `2b6f677`、工作台 `a4c6403` 已合并，`m4` 指向后者；309普通、29前端、32集成、6 Playwright通过，blackboard镜像已重建。真实任务可在UI追溯结束原因、事实证据、Agent回执与费用；回放不会泄漏后续费用/回执。新增 workspace/tree、workspace/file 受限预览；Profile默认固定版本仅存本浏览器。完整报告见 `docs/tasks/M4-report.md`。main已同步Python/前端依赖；Playwright缓存保存在 `.data/playwright`，日志在 `.data/checkpoints/m4`。M4工作树/分支已删除，临时浏览器页已关闭。
- **EVAL-2**：用户改由Codex完成；基线 `63b4c6e`、分支正常查询与报告 `f7db7a0` 已合并。基线1434行/42测试，feature1942行/54测试、6个提交/512新增4删除，P1–P6与100/100玩具复现不变。309普通/32集成通过；原评估函数与答案未改。日志保存在 `.data/checkpoints/eval2`，工作树/分支已清理。
- **M5**：设计 `e34cb1c`、基线 `2a96394`、评估工具 `d1f13f5` 已合并，m5指向工具提交。338普通/34集成/29前端通过；blackboard/runtime/eval镜像已构建，模拟记录跑通score/review-template/comparison。2026-09-25 补做真实 DeepSeek 5+5：10/10 success、0 pending/provisional；default 平均召回/精确率/复现率/耗时/金额为 0.9333/0.7514/0.45/287.9s/0.187192，single 为 1.0000/0.7631/0.85/195.1s/0.088848。补充后 `make check` 356 通过、`make test-integration` 35 通过。当前数据未证明多 Agent 优于 single。原始产物在 `eval/results/real-20260925-01/`，精简 checkpoint 在 `.data/checkpoints/m5/real-20260925-01/`。
- **当前架构**：不再部署本地 embedding，Agent 根据快照/增量同步自行判断重复；黑板保留确定性写入规则与关键词查找。M1 阶段未调用真实 DeepSeek；M2a 按用户提供的配置指引，由测试子进程加载 DeepSeek 配置完成 4 项 live 验证，未显示或修改密钥。
- **当前执行环境网络**：Direct-egress按用户纠正将Ubuntu执行容器改为默认直连公网。旧M2-env/M3a/M4记录的internal网络和全局白名单是当时方案的历史状态；需要时现以显式proxy模式保留该行为，默认模式没有代理变量或域名过滤。375普通、36集成、29前端通过；运行时新建临时envd并从正式Agent工具直连`101.200.203.31`返回200，临时容器已清理，原用户任务归档保持。

---

## 3. 收尾记录与后续入口

> 3.1、3.2 为已完成的收尾背景；M4 与 M5 实现已完成，M5 真实评估及复核也已补做。软件源参数、README 与报告已落地；最新构建代理命令见 `services/envd/README.md`。

### 3.1 收尾 M2-env（工作树 `~/bbx-wt/m2env`）

**问题一：镜像构建失败。** 两个原因叠加：

1. Codex 沙箱不允许 Docker buildx 写 `~/.docker/buildx/activity`，所以 Codex 无法构建镜像——**镜像需要你在沙箱外构建**。
2. 在沙箱外构建时，`apt-get` 直连 `archive.ubuntu.com` 极慢并最终失败（退出码 100）。需要改用国内软件源。

本轮已应用并验证的修改已写成任务说明 `docs/tasks/M2-env-fix.md`，可直接派给 Codex（`--cwd ~/bbx-wt/m2env`，提示语中用绝对路径 `~/blackboard-explorer/docs/tasks/M2-env-fix.md` 引用，因为该工作树切出时还没有这个文件）。修改内容如下，也可以手动改：

```dockerfile
# services/envd/Dockerfile —— 在 ENV DEBIAN_FRONTEND 之后
ARG APT_MIRROR=
ARG PIP_INDEX_URL=
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://archive.ubuntu.com/ubuntu/|$APT_MIRROR/|g; s|http://security.ubuntu.com/ubuntu/|$APT_MIRROR/|g" \
            /etc/apt/sources.list.d/ubuntu.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
    ...（原有包列表不变）
# pip 安装一行改为：
    && /opt/envd/venv/bin/pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} /opt/envd/source \
```

```dockerfile
# services/egress-proxy/Dockerfile
ARG APK_MIRROR=
RUN if [ -n "$APK_MIRROR" ]; then sed -i "s|dl-cdn.alpinelinux.org|$APK_MIRROR|g" /etc/apk/repositories; fi \
    && apk add --no-cache tinyproxy
```

```makefile
# Makefile —— 默认用国内源；设为空即回到官方源
APT_MIRROR ?= http://mirrors.aliyun.com/ubuntu
PIP_INDEX_URL ?= https://mirrors.aliyun.com/pypi/simple
APK_MIRROR ?= mirrors.aliyun.com

image-exec-env:
	docker build -f services/envd/Dockerfile --build-arg APT_MIRROR=$(APT_MIRROR) \
		--build-arg PIP_INDEX_URL=$(PIP_INDEX_URL) -t bbx-exec-env:latest .

image-egress-proxy:
	docker build -f services/egress-proxy/Dockerfile --build-arg APK_MIRROR=$(APK_MIRROR) \
		-t bbx-egress-proxy:latest .
```

然后在沙箱外执行：

```sh
cd ~/bbx-wt/m2env
make image-exec-env image-egress-proxy
make test-integration        # 注意：Makefile 中 test-integration 依赖这两个镜像目标
```

**问题二：变量命名。** 设计文档（开发方案第 9 节）只有 `ENVD_TOKEN_SECRET`；M2-env 让 envd 容器读取 `ENVD_TOKEN`。两者其实不矛盾，建议在设计文档中写明：agent-runtime 持有 `ENVD_TOKEN_SECRET`，为每个任务派生一个 token，创建执行环境容器时以 `ENVD_TOKEN` 注入；envd 只认 `ENVD_TOKEN`。

**审查要点**：集成测试（`services/envd/tests/test_integration.py`）用一个大测试覆盖了鉴权、非法 agent id、目录隔离、shared 不可删他人文件、超时杀进程组、截断与全文读取、`/files` 限制与路径穿越、privileged 白名单、未知 agent、归档排除；另有出网代理 403 测试。全部通过后按报告中建议的提交划分提交，合并到 `main`。

### 3.2 审查并合并 M1a（工作树 `~/bbx-wt/m1a`）

审查清单：

1. 读 `docs/tasks/M1a-report.md`，重点看 `BoardService` 公开方法清单（M1b 的 HTTP 层要用）与"偏差与待决"。
2. 独立运行 `uv sync && make check && make test-integration`，核对 97 / 6 / 覆盖率 91%。
3. 抽查 `services/blackboard/src/bbx_blackboard/domain/` 是否覆盖设计文档 3.5 的每条写入规则；争议状态是否完整递归；finish_agent 的计数规则是否符合实现架构 5.3 段落。
4. 确认每个写操作事务开头 `SELECT … FOR UPDATE` 锁任务行；实时写入与重放走同一个投影器。
5. 已知偏差：**任务创建是"事件是唯一写入入口"的引导例外**（先插任务行，再追加 `task.created` 并投影，因为 events 有外键）。建议在实现架构第 3.1 节补一句说明。

合并顺序：M2-env 与 M1a 互相独立，先合并哪个都可以；两者都改了 `pyproject.toml` / `uv.lock` / `Makefile` / `.env.example`，第二个合并时若 `uv.lock` 冲突，取任一版本后运行 `uv lock` 重新生成，再跑 `make check`。

合并后删除工作树：`git worktree remove ~/bbx-wt/<name> && git branch -d <branch>`。

### 3.3 第二波：M1b 与 M1-W（可并行）

任务说明已写好：`docs/tasks/M1b.md`、`docs/tasks/M1-W.md`。M1b 依赖 M1a 合并；M1-W 可以与 M1b 同时开始（先用 contracts 的 JSON Schema 与 MSW 模拟接口，M1b 完成后切到 OpenAPI 生成的类型）。

---

## 4. 用 Codex 开发的操作手册

### 4.1 派发一个任务

先把任务说明写到 `docs/tasks/<T>.md`（模板见 4.3），提交到要开发的分支上，然后：

```sh
export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897 \
       all_proxy=socks5://127.0.0.1:7897 no_proxy=localhost,127.0.0.1,::1,host.docker.internal \
       UV_CACHE_DIR=/private/tmp/bbx-uv-cache

node ~/.claude/plugins/marketplaces/openai-codex-plugin-cc/plugins/codex/scripts/codex-companion.mjs task \
  --cwd <工作树或仓库路径> --model gpt-6-sol --effort high --write --fresh \
  "请先阅读 AGENTS.md，然后按 docs/tasks/<T>.md 完成任务 <T>。严格遵守任务范围与完成标准，结束时写报告 docs/tasks/<T>-report.md 并输出摘要。" \
  > /tmp/bbx-codex-<T>.log 2>&1 &
```

| 参数 | 说明 |
|---|---|
| `--cwd` | Codex 的工作区；沙箱只允许写这个目录（及 /tmp） |
| `--model gpt-6-sol --effort high` | 本项目使用的模型与思考强度（可用模型见 `~/.codex/models_cache.json`） |
| `--write` | 允许修改文件 |
| `--fresh` / `--resume-last` | 新会话 / 接着该工作区上一个会话（用于"审查后修正"） |
| 代理变量 | 必须带上，否则 Codex 运行时的登录检查与依赖下载会失败 |
| `UV_CACHE_DIR` | Codex 沙箱写不了 uv 默认缓存，用这个共享目录 |

- 注意：`task --help` 会被当成提示词执行，不要用它看帮助。
- 查看进度：`/codex:status`，或 `tail -f /tmp/bbx-codex-<T>.log`。
- 继续某个 Codex 会话：`codex resume <session id>`（见第 8 节）。
- 也可以直接在 Codex CLI / 桌面应用里打开仓库，给出同样的提示语——`AGENTS.md` 会自动生效。

### 4.2 并行开发（多个 Codex 同时做不同任务）

```sh
cd ~/blackboard-explorer
git worktree add -b <branch> ~/bbx-wt/<name>     # 每个任务一个工作树
# 用 4.1 的命令分别派发，--cwd 指向各自工作树
```

规则（已写入 `AGENTS.md` 第 8 节）：各任务只改自己目录；共享文件（根 `pyproject.toml`、`uv.lock`、`Makefile`、`docker-compose*.yml`、`packages/contracts`）只做追加式修改并在报告中列出；需要 Docker 的测试用 testcontainers 随机端口，不用 `make up`；自建镜像/容器加任务前缀。本机 4 核 / 9.7GB，同时 3 路比较合适。

### 4.3 任务说明模板

```markdown
# 任务 <T> · <名称>

依据：<设计文档章节>

先阅读 `AGENTS.md`。

## 范围
<只做什么；明确不做什么（属于哪个后续任务）>

## 已定的实现决定
<技术选型、分层、并发策略、接口约定——不留给 Codex 自由发挥的部分>

## 任务
| # | 内容 |

## 测试
<普通测试 / 集成测试分别要覆盖什么>

## 完成标准
<可执行、可核对的条目；最后一条：报告写到 docs/tasks/<T>-report.md，含接口清单、共享文件修改、建议提交划分、偏差与待决>
```

经验：把"必须满足的数字/行为"写进完成标准（例如"50 个并发认领恰好 1 个成功"），Codex 会照着验证；把"不做什么"写清楚，能有效防止越界。

### 4.4 审查与合并

1. 读报告，尤其"偏差与待决"。
2. 独立运行 `make check`、`make test-integration`，以及任务特有的验证。
3. 对照设计抽查关键代码；发现问题写 `docs/tasks/<T>-fix.md`，用 `--resume-last` 派回同一个会话。
4. 按报告建议的提交划分提交（交互使用时 Codex 申请、你批准后由它提交，见 5.1），提交信息注明 `Implemented by Codex (gpt-6-sol) for task <T>.`
5. 合并到 `main`（`git merge --ff-only` 或普通合并），删除工作树——这两步 Codex 必须先得到你的明确确认（`AGENTS.md` 第 7 节）。
6. 设计需要改的，**先改 `docs/design/`，再改代码**。

---

## 5. 环境与已知坑（历史交接记录）

### 5.1 当时的 Codex 沙箱限制

以下表格只反映 2026-09-24 的派发环境；当前沙箱权限以实际运行环境为准，不应据此推断 Docker 或 Git 必然不可用。

| 限制 | 影响 | 现在的做法 |
|---|---|---|
| 不能写 `.git` | 沙箱内切分支、提交会失败 | 交互使用（桌面应用 / CLI）时 Codex 申请在沙箱外执行，你批准（`AGENTS.md` 第 7 节）；插件派发是非交互的，无法申请，Codex 会在报告里列出命令由你执行 |
| 不能 `docker build`（buildx 要写 `~/.docker`） | 镜像需在沙箱外构建 | 同上：申请批准，或由你手动执行 |
| 可以 `docker run` / `docker compose` / testcontainers | 集成测试可以在沙箱内跑（前提是镜像已存在） | M0、M1a 的容器测试均在沙箱内跑通 |
| 写不了 uv 默认缓存 | `uv sync` 失败 | 使用 `UV_CACHE_DIR=/private/tmp/bbx-uv-cache` |

- 不要把 `.git` 加进沙箱可写目录：`.git/hooks` 中的脚本会在沙箱外执行，等于给沙箱开了出口。也不建议用"完全访问"模式。
- 桌面应用不会继承终端里 `export` 的代理与 `UV_CACHE_DIR`。`AGENTS.md` 第 6 节要求 Codex 在缺失时于命令前显式设置；也可以在 `~/.codex/config.toml` 中统一设置：

  ```toml
  [shell_environment_policy]
  set = { https_proxy = "http://127.0.0.1:7897", http_proxy = "http://127.0.0.1:7897", all_proxy = "socks5://127.0.0.1:7897", no_proxy = "localhost,127.0.0.1,::1,host.docker.internal", UV_CACHE_DIR = "/private/tmp/bbx-uv-cache" }
  ```

### 5.2 网络与镜像

- 当时宿主机外网经代理 `127.0.0.1:7897`；本机地址必须放进 `no_proxy`。这不代表当前 Ubuntu 执行容器经代理；当前默认直连，见[网络模式](使用与部署.md#网络模式与排障)。
- M3b 期间镜像源曾出现TLS证书错误，未关闭校验；已用锁文件一致的已验证依赖缓存离线更新镜像，runtime/blackboard Dockerfile现在使用 BuildKit `bbx-uv` 缓存，随后正式构建通过。
- Docker 配置了镜像加速源（xuanyuan）。它**不提供 MinIO 官方镜像**（`minio/minio`、`minio/mc` 返回 422），对象存储统一用 `pgsty/minio:RELEASE.2026-04-17T00-00-00Z`（自带 `mc`）。已确认可拉取：`pgvector/pgvector:pg16`、`ubuntu:24.04`、`python:3.12-slim`、`alpine:3.20`、`node:22-alpine`、`testcontainers/ryuk:0.11.0`。新增镜像前先 `docker pull` 确认。
- Docker 构建中的 `apt` / `pip` / `apk` 直连官方源很慢，用国内源（3.1 的修改）。

### 5.3 模型服务的内容审核

EVAL 任务在最后阶段被 Codex 的模型服务以"可能的网络安全风险"中断——mini-shop 是故意植入越权、注入等问题的评估目标，并附有证明问题存在的验证脚本。后续涉及这类"植入安全问题的评估素材"的工作（例如 EVAL-2），可能再次被拦截。**不要改写措辞去绕过审核**；这部分工作建议人工完成，或只让 Codex 做不涉及植入问题的正常业务代码。

### 5.4 密钥与本地配置

- `.env` 只在主工作树 `~/blackboard-explorer/.env`（权限 600，已被 git 忽略）：数据库/MinIO 口令与各类 token 为随机值。
- 用户已确认 `.env` 配置好 `DEEPSEEK_API_KEY`，M2a 真实认证及 4 项 live 检查通过。不要输出 `.env` 或把密钥写进提交、文档、提示语；普通检查不加载 `.env`。
- `profiles/default/models.yaml` 已按 2026-09-24 官方 USD 高峰价填写（命中/未命中/输出每百万 0.006/0.30/1.20），用于保守预算。off_peak 是固定价格快照标记，不动态切换；真实 M2b 使用当时错峰价，记账 USD 0.013263432，未核对控制台实付。价格缺失仍记 token、金额 0 并告警。

### 5.5 其他

- Python：3.12 已由 uv 安装在默认位置；`.venv` 在每个工作树中各自 `uv sync` 生成。
- `ruff format` 只针对四个 Python 包目录（否则会格式化设计文档里的代码块）。
- `eval/targets/dist/` 是构建产物，已忽略，用 `make eval-targets` 生成。

---

## 6. 历史任务派发顺序（均已完成）

下表是 2026-09-24 的任务派发计划，所列里程碑均已完成；不要按此表重复派发。任务说明和报告保留在 `docs/tasks/`，实际当前进度见第 0 节。

| 顺序 | 任务 | 任务说明 | 依赖 | 可与谁并行 | 需要你在沙箱外做的事 |
|---|---|---|---|---|---|
| 0 | M2-env 收尾 | `M2-env-fix.md` | — | M1a 审查 | 构建两个镜像、`make test-integration` |
| 0 | M1a 审查合并 | 本文 3.2 | — | M2-env 收尾 | 审查、提交、合并 |
| 1 | 黑板接口层 | `M1b.md` | M1a | M1-W | 构建 blackboard 镜像；`make up` 后跑模拟器看 SSE |
| 1 | 画布最小版 | `M1-W.md` | —（M1b 完成后切真实接口） | M1b | 浏览器检查画布 |
| 2 | 技术验证与 runtime 骨架 | `M2a.md` | M1b、M2-env | EVAL-2 | `make test-live`（真实 DeepSeek） |
| 3 | 单个 Agent 跑通 | `M2b.md` | M2a | EVAL-2 | 填 `DEEPSEEK_API_KEY` 与价格表；玩具任务上跑真实种子 |
| 4 | 调度器、清扫与恢复 | `M3a.md` | M2b | — | 构建 agent-runtime 镜像 |
| 5 | 裁定、收尾与完整闭环 | `M3b.md` | M3a | — | `make e2e`（玩具任务完整闭环） |
| 6 | 工作台 | `M4.md` | M1-W、M3b | M5 | `make web-e2e`；用工作台复盘一次运行 |
| 6 | 评估与调参 | `M5.md` | M3b、EVAL-2 | M4 | 已执行 `make eval-run` / `make eval-score`；后续调参需重新评估 |
| 任意 | mini-shop 扩容 | `EVAL-2.md` | — | 任何 | **建议人工完成**（见 5.3） |

**每个任务的标准流程**：建工作树 → 派发 → 读报告 → 独立运行 `make check` / `make test-integration` → 执行任务说明中"需要用户执行"的检查点 → 有问题写 `<T>-fix.md` 用 `--resume-last` 派回 → 按报告建议提交 → 合并 → 删除工作树。

## 6.1 设计文档待同步

Control-connection-resilience 已修复控制链断连引发全任务取消及Session提交确认竞态，保持非传输异常失败语义与CAS保护。归档413改为本进程暂停自动重试、保留容器、不标cleanup_ready；完整大工作区归档/恢复方案及对应设计仍待处理，不能将当前容量保护视作归档成功。见 [报告](tasks/Control-connection-resilience-report.md)。

Runtime-streaming 已合并实现并本地部署；Responses 保持 store:false、本地工具历史回放，六类 OpenAI 系连接器使用 ResponseStream 并在每次完整模型响应后记账。实现架构第 6–7 节的旧非流式示例尚待补充流式处理说明，本轮未改设计文档。详细范围、错误边界、真实网关验证和前端仍按完整响应展示的限制见 [Runtime-streaming 报告](tasks/Runtime-streaming-report.md)。

Agent-settings已先同步设计：模型上下文容量、80%任务阈值/旧任务固定、配置页角色侧栏和分组表单，已合并部署。无待同步项；见tasks/Agent-settings-report.md。

Runtime-failure-hardening已同步设计与任务合同：对象存储保留完整工具记录，PostgreSQL/Session边界转写U+0000；OpenAI系使用SDK有界瞬时重试；最终错误只保存脱敏类别和请求标识；derive initial_context按轮次唯一。无待同步项。

Provider-failure-coalescing已同步设计与任务合同：transient模型错误按任务120秒锚定窗口合并，Intent/种子不受基础设施错误惩罚，持续三窗口仍失败；事件固化增量以保证replay，续跑/正常完成清窗口，终态冻结触发时计数。无待同步项。

Runtime-continuation已按用户确认同步设计与任务合同：同任务续跑、活动时长/历史产物、derive同ID复用与轮次隔离、完整Session的有界模型输入视图、MAF图片引用持久化、证据分页、峰谷估算及审计校正。无待同步项；取代旧“终态不能续跑、derive每次新建、价格只按固定快照”描述。

Completion-review按用户“原则必经、明确达成例外”同步概念/实现设计与任务合同（3ab3fcf）：explicit完成依据，fresh derive_review及再次裁定门控，预算/人工停止保持。无待同步项；取代此前单凭全部met直接收尾及single完全禁derive描述。

Worker-settings已按用户最新确认同步设计（dc72f84）：三角色直接设置、默认模型/任务选择、凭据统一平台保存、MAF Python连接器范围、提示词下一模型调用热更新。模型/工具快照保留，取代旧“所有配置固定”及历史版本UI描述；无待同步项。

Platform-config/CNY已按用户最新要求同步实现架构及任务合同（c82599a、97e044c、ea2f841）：人民币估算、完整worker表单、平台模型/MCP固定版本与加密凭据、受限工具配置；无待同步项。

Interactive-agents已同步概念5.4与实现9.1及API合同（246c5b3、44893b8）：早发布、有界并行derive与原子phase复核、用户干预、原生Session持久化、只读续聊与两阶段删除；当前没有未同步的已采纳设计事项。

Contribution-view 已同步 8.2/8.3/8.5：作者颜色、产出统计、顶部复盘对话框、阅读排版与原文。实测新意图约1秒认领；任务b8b7727f的首个非种子Explore在349秒开始，最终279轮/38分15秒。耗时估算方法与完整结论见 `docs/tasks/Contribution-view-report.md`，本轮未改提示词/推理强度或宣称推理提速。没有待同步的已采纳设计项。

Header-polish 已按最新要求同步实现架构 8.5（`e4af5af`）：顶部不再保留验收入口，状态与结束原因之间设分割线，统计与结束原因同排；验收内容仍在目标详情和裁定历史中。该决定取代前述保留验收摘要的界面描述。

Detail-polish 已先同步实现架构 8.3/8.5（`2022d61`）：取消图谱筛选与自动折叠、128×84缩略图、Markdown详情正文。当前无待同步项。

Workbench-UX 已先提交实现架构 8.5（`1b7400a`）：任务内编号显示、紧凑工作台与时长、Agent 对话记录、trace 事件与对象存储、真实返回内容和历史兼容。没有未同步的已采纳设计事项。

Direct-egress已依据用户2026-09-26的最新决定修改实现架构1.1/2.4/10/11及开发方案0/4/7/9：默认exec网络有网关，Agent命令不注入代理；显式proxy模式才要求internal网络并启用部署级白名单。旧TaskSpec的egress_allowlist继续只记录需求，不能作为默认直连时的访问控制。设计提交`9a3facc`，实现与部署见`docs/tasks/Direct-egress-report.md`；没有未同步的已采纳设计事项。

Agent-budget-context已按用户偏好同步概念设计3.1/5.2/8.1及实现架构7.1（设计提交`a6ae0ee`）：上下文不计算或注入剩余金额/时间；旧Profile的budget_left仅返回非数值说明，调度硬限制与记账不变。实现和回归已合并。

2026-09-26补充：M5-tool-feedback符合实现架构6.2既有错误反馈规则。M5-E2已同步并合并Fact可信度、任务无关证据规则与“通用问题求解引擎”定位（42efeac、6806a04）；最终提示词删除了按单一评测题型编排的条款，领域标准仅从任务输入获取。判重及裁定职责保持不变；目前没有已采纳但未同步的设计事项。

token 归属、tool_call.recorded、任务创建引导例外已同步并合并。M1b 按用户 2026-09-24 的新决定取消本地 embedding 与向量预检，由 Agent 根据快照和增量同步自行判断重复；search 仅作关键词定位，原线性余弦 top-3 决定已废弃。此外，容器实测证明超时跨 UID 发信号需要 `KILL` capability，已先修改实现架构 2.4 并单独提交，再同步实现与测试。M2a 已同步宿主固定目标 relay、JWT 密钥仅属于 blackboard、DeepSeek 缓存用量适配和公开 Content 持久追加退路。M2b 已补 close 回执、宽限剩余 0 的成功语义、模型请求和整体运行超时。M3a 已同步并实现归档事件/API、PG 单实例锁与探测、排队 provisioning 恢复、全局出网白名单边界、终态守护、final 重试上限、种子首次认领后使用普通上限、runtime_restart 计数保持。M3b 已同步并验证硬时限留出交接余量、固定报告防重复覆盖、closing主动release不计attempts与完整证据引用。M4已明确全局出网白名单边界、浏览器固定默认 Profile、保留旧版的回滚语义与受限只读归档预览（设计提交 af29bb3）。EVAL-2已按用户新指令完成并合并。M5已同步derive_enabled默认true及关闭时先裁定再收尾的单Agent语义（e34cb1c），并完成实现和真实5+5复核。当前没有未同步的已采纳设计事项；后续若要改善多Agent收益，应先提交新的设计变更。

| 事项 | 来源 | 建议写法 | 位置 |
|---|---|---|---|
| `derive_enabled` 参数 | M5 任务 | ✅ 已同步并实现：默认 true；单 Agent 基线为 false | 设计文档第 9 节、5.4 |

## 7. 已确定的关键设计决策（避免重复讨论）

- 只有三种 Agent 任务：explore（长驻有界；黑板为空时的第一个为"种子"）、derive（探索停下来且已裁定后推导新意图）、close（裁定 / 终结）。没有 verify / test / arbitrate 角色；质疑用"争议事实"表达，作者可以撤回自己的事实；争议点名深度 2。
- 事实状态只有 proposed / disputed，由代码按争议图递归计算；意图 open / claimed / closed，带 `attempts` 上限与 `retry_of`；`post_intent(claim=true)` 原子认领。
- Goal、domain_context、验收条件都是文字；**验收由 close 裁定**，调度器在"有事实带 `satisfies`"或"探索停下来且黑板有变化"时触发裁定；裁定与探索并行；met 项的支撑事实被争议时自动回到 unmet。不设 setup 脚本。
- 预算按金额（`max_cost`、`close_reserve_ratio`），并发上限只在任务 `budget` 中；`context_threshold` 128K。
- 调度器自建 asyncio 循环，**不用 MAF Workflow**（超步屏障与连续调度冲突）；MAF 只用于 Agent 本体。
- MAF 核对结论：`max_iterations` 默认 40 需调大；函数中间件"替换结果"而不是 terminate；M2a 实测原 enqueue 注入不能跨轮保留，改用公开 Content.text/result 追加增量；MCP 工具经过函数中间件，参数为 `static_headers`。
- 默认模型为 DeepSeek `deepseek-flash`，平台配置支持多模型及MAF Chat Completions/Responses连接器；重复判断由 Agent 根据同步的黑板完成，不再部署本地 embedding 或生成向量；v1 不部署模型网关。
- 部署：本机 Docker；同一时间一个任务；每任务一个 ubuntu 执行环境（每个 Agent 一个 Linux 用户），证据"引用时持久化 + 结束时归档"到 MinIO。

---

## 8. 参考

| 项 | 值 |
|---|---|
| Codex 会话：M0 / M0-fix | `01a0d260-2e17-7af1-9db0-ea6caf29d7b7` |
| Codex 会话：M1a | `01a0d284-c534-7072-8e46-7b128f50e65f` |
| Codex 会话：M2-env | `01a0d284-d0f9-73c0-a5e9-cc064bf72fed` |
| Codex 会话：EVAL（被内容审核中断） | `01a0d284-dac0-74a0-9622-d18680cbd912` |
| 派发日志 | `/tmp/bbx-codex-{M0,M0-fix,M1a,M2env,EVAL}.log`（临时目录，重启后可能丢失） |
| companion 脚本 | `~/.claude/plugins/marketplaces/openai-codex-plugin-cc/plugins/codex/scripts/codex-companion.mjs` |
| DeepSeek 接口 | `https://api.deepseek.com`（OpenAI 兼容 Chat Completions）；模型 `deepseek-flash` |
| 本地端口 | postgres `127.0.0.1:55432`；minio API `127.0.0.1:59000`、控制台 `127.0.0.1:59001` |
