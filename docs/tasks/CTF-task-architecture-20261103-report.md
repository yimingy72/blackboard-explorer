# CTF 实际任务架构图与受控子 Agent 规则

日期：2026-11-03，北京时间。基于前轮[交叉复核与更正](CTF-proposal-review-20261103-report.md)，将方案画成实际 CTF 工作流，并纳入用户提出的“题目 owner 调用 subagent 协助”。这是目标架构与待实施规则，不是当前平台已经具备的全部能力。

## 1. 图文件

- [可编辑 draw.io，两页](ctf-task-architecture-20261103/ctf-task-architecture.drawio)
- [第一页：任务架构与题内委派](ctf-task-architecture-20261103/01-architecture.drawio.png)
- [第二页：调度规则与权限](ctf-task-architecture-20261103/02-rules-permissions.drawio.png)

PNG 内嵌 draw.io 数据；原生文件保留两页可编辑节点、连线和文本。图中逐个标记既有、扩展或待接入能力，示例配额不冒充历史任务数据。

## 2. 推荐的协作结构

全局调度器分配独立题目；每题一个 owner 对交付负责。owner 可以申请限定在本题的子 Agent，给出不同的分析方向或可验收子目标。子结果回到 owner 合并，Lead 处理跨题优先级、资源争用、升级和总结果。

子 Agent 是独立、可追踪的受控执行身份，不是 owner 在 shell 中私自启动的模型进程。复用平台已有执行池、Session、turn、费用和命令体系；可以将空闲成员临时挂接到某个 owner 的题内工作，避免同时维护两个互不知情的 Agent 池。全局帮手与题内 subagent 统一为同一个受控委派合同，不能同时由 Lead 和 owner 对同一帮手下达互相冲突的执行指令。

首版建议只允许 owner→child 一层，同时最多两个 child；这是可调的产品起始策略，不是测得的硬件容量。子 Agent 不再递归创建后代，不决定题目完成，不把自己报告的候选当作平台得分。

例如逆向题 owner 正在恢复算法，可以让子 Agent 检查数据格式或独立验证某个假设；不应让所有子 Agent 都重复“完整解这道题”。明确输入快照、负责范围和产物，比固定专家名称更重要。

## 3. 实际调度规则

| 触发 | 平台动作 | owner / Lead 的工作 |
| --- | --- | --- |
| 新题发现 | 稳定来源 ID 幂等建题，一题一行；已发现可执行步骤即可排队 | Lead 核对总范围、确定优先级；不等全部附件下载。 |
| 材料可用或外部资源恢复 | 更新可执行条件，唤醒调度；保留失败与 unknown 原因 | owner 依据材料判断路线；unknown 副作用先查状态。 |
| 存在可做题与空闲容量 | 短事务分配 owner 并发 instruction；空闲成员优先复用 | 首先铺开独立题；相同优先级按等待时间避免长期搁置。 |
| owner 申请 child | 验证本题身份、委派范围、权限交集和名额，幂等登记后启动 | 提供子目标、输入、输出要求与为什么能独立推进。 |
| 名额或预算暂不足 | 返回确定的未启动/等待原因，不伪装已创建 | 没获准 child 时 owner 继续单 Agent 路线；不全员阻塞等待子任务。 |
| child 已获准并执行 | 模型、命令、资源仍走统一准入；独立记录来源与用量 | owner 可推进不依赖该结果的工作；等待事件不循环问状态。 |
| child 交付 | 持久化产物/已知用量，沿既有清理与结算释放其工作槽 | owner 采用、否定或要求一次明确补充，不把确认采用当成占槽条件。 |
| owner 完成或要求交接 | 关闭旧范围的新委派，通知或取消相关 child，确认旧执行收尾再转交 | 保存已知结论、未解项、脚本和未知操作；不能用 CAS 代替旧权限撤销。 |
| 用户停止 / 结束任务 | 停新准入，撤销该范围父子执行，drain 后结算并归档 | 父任务最终回答不能脱离仍运行的 child 提前宣称收尾。 |
| 单个工作失败 | 已确认局部错误只中断相关工作；共享状态不明进入既有恢复 | 其他独立题继续；Lead 接收需要全局判断的摘要。 |

父 owner 的临时模型回合结束、上下文压缩或续接，不等于题目委派生命周期结束。child 归属应绑定稳定的父派工范围，而非父的某一次模型调用；父题交接、范围撤销或任务停止才触发对应的失效检查。每个 child 自身仍使用独立 turn/generation，迟到结果仅入允许的历史，不覆盖新分工。

沿用已核实的 `runner.settle → stop_member/drain → finish_turn` 链路，不再新建第二条清理服务。停止与正常结束的窄竞态仍需先修复，不能因为图中画了流程就当作已解决。

## 4. 容量与费用

新自动模式中，owner 与 child 共用总工作名额，Lead 另计；例如总额 20，不能理解成 20 个 owner 每人还能再开两个 child。现有 `max_teammates` 的名册语义仍需保留，新增活动工作上限和子任务上限应在有效配置中明确展示，旧快照不自动改义。

独立可执行题默认优先于同题的额外路线；紧急重点题可由 Lead 明确调整，不能因固定优先级使帮助永远排不到。首版容量满时拒绝/推迟额外子任务并让 owner 继续，不以“父等待时偷偷不计名额”绕过限制。累计创建数量也需要有界，避免不断重试创建新身份。

模型在途请求、命令执行、在线实例配额分别计算。父 owner 等待已经启动的 child 时，不持有模型调用许可；每个 child 的请求也要经过原有最坏费用预留和结算。只在子任务结束后汇总 token 不足以执行预算约束。父页面可以显示全家费用，但账本按实际请求记一次，不能再次把 child 汇总额算入父用量造成重复计费。

预算预检只是提示；原子费用授权才是准入。费用 unknown 按原规则保守扣留，不自动返还；纯费用未知与未确认的外部执行副作用分别处理。

图中例子是 24 道题：16 道离线就绪、8 道需要在线实例，实例配额为 3。若其他准入均允许，先运行 19 个 owner；其余 5 道在线题等待实例。最后一个名额可用于一个明确、离线的子任务，成为 19 owner + 1 child。这个例子只解释配额，不是性能实测或固定部署配置。

## 5. 子 Agent 权限

有效权限为：任务授权 ∩ owner 被允许委派的权限 ∩ child 模板 ∩ 本次请求。该交集由服务端计算和校验，owner 只能收窄，不能靠参数或提示词扩大。

| 能力 | 建议默认 |
| --- | --- |
| 读取本题题面/附件/记录 | 允许已授权引用；不默认拷贝父完整会话。 |
| 执行分析命令 | 按需开放受控 envd 命令；独立可写输出目录，输入快照只读。 |
| 记录观察/候选/文件 | 可追加自己的记录，保留 child 与父派工来源。 |
| 改 owner、认领其他题、宣布整题完成 | 禁止；结果由 owner 合并。 |
| 直接提交 flag、管理在线实例 | 默认禁止；经 owner/授权服务执行和记录回执。 |
| 平台凭据、模型密钥、宿主文件 | 不下发；不能以父会话继承绕过。 |
| 创建孙 Agent、改预算、安装/发现任意扩展 | 禁止；平台固定配置，不读不可信附件中的 Agent 定义。 |
| 取消/联系 | owner 只能管理自己当前范围的 child；Lead/用户保留全局停止与例外权。 |

工具名白名单不等于文件系统/网络沙箱；原生 Pi `bash/read/write` 若在 runtime 宿主执行，会绕过现有 envd 边界。必须把子工具接回受控后端，并验证读写目录、网络范围和权限失效。共享靶机有副作用的操作需要明确串行范围或独立资源，不能因为同属一题就默认可同时修改同一实例。

## 6. `pi-subagents` 核对结果与接入选择

用户给出的 `pi install npm:pi-subagents` 对应 **nicobailon/pi-subagents**，不是另一个 scoped 的 `@tintinweb/pi-subagents`。本轮读取的源码版本为 0.84.0，仓库 main 观察提交 `8520aeb788ca19479baa852b5a7884a86bed813e`。[包清单](https://github.com/nicobailon/pi-subagents/blob/8520aeb788ca19479baa852b5a7884a86bed813e/package.json)

该项目说明子任务使用独立 Pi Session，前台任务与父进程同进程，后台任务由 detached runner 承载；有深度和累计创建限制。[README](https://github.com/nicobailon/pi-subagents/blob/8520aeb788ca19479baa852b5a7884a86bed813e/README.md)

它提供结构化 delegation API、启动 preflight 和 session 级 capability ceiling；多个限制取交集，能向后代传递。文档明确 capability ceiling 是同进程策略边界，不是对恶意进程代码的操作系统沙箱。[扩展 API](https://github.com/nicobailon/pi-subagents/blob/8520aeb788ca19479baa852b5a7884a86bed813e/docs/extension-api.md#capability-ceilings)

本项目只安装 `pi-agent-core/pi-ai/pi-telemetry` 1.4.0，`worker.mjs` 将显式工具和模型请求回调到 Python；没有完整的 `pi-coding-agent` 扩展宿主，也没有自动加载该包的入口。[本地依赖](../../services/agent-runtime/pi/package.json)、[受控回调](../../services/agent-runtime/pi/worker.mjs#L50)。因此版本号满足最低要求并不等于能直接安装即用。

建议先做一个受控委派适配验证：owner 请求子目标 → 平台登记身份/权限/预算 → 子 Session 通过受控工具执行 → 结果回 owner。可以评估复用该包的 delegation/preflight/capability API，但不引入其 missions、定时任务、额外 workflow 引擎和独立全局调度。如果无法让插件的每次模型/工具调用经过平台的准入、checkpoint、用量和 envd，则优先复用现有 `CtfRunner` 实现薄委派接口，避免把裸 CLI 子进程当作平台成员。

此结论是依据两边接口作出的兼容性判断，尚未运行插件集成测试。本轮没有安装 npm 包、修改依赖或启动任何实际子模型。

## 7. 接入前的最小验证

1. owner 和 child 共享上限；全员占满时不会全部等待未获准子任务。
2. child 不能更换题目 ID、扩大工具权限、请求孙 Agent 或调用宿主默认工具。
3. parent 模型回合正常续接时 child 归属不丢失；父题交接/停止后旧 child 不能继续写当前状态。
4. 模型请求逐次预留费用，child 费用持久化且父汇总不重复计费；断流未知费用保持。
5. 取消 child 能确认进程回收；父任务结束与归档不能留下脱管后台 runner。
6. 两个 child 不覆盖彼此脚本；共享实例操作受控；一次 terminal 结果只入账/通知一次。
7. 同题比较 owner 单独求解与有限子任务协作，测首个可信结果、费用、重复尝试和 owner 合并时间；不承诺分层后必然更快。

## 8. 实际完成与验证

已生成两页 native draw.io，并导出两张带可编辑数据的 PNG；完成结构校验和两轮视觉检查，修正图例交叠、紧凑卡片布局和子任务释放表述。最终 `validate.py --strict` 为 0 errors、0 warnings；PNG 执行修复/完整性检查。图中沿用真实结算顺序，并标记待实现能力。

仅新增本报告与图文件，生产源码和配置不变。前轮同一源码的回归结果不冒充本轮插件验证；本轮没有真实模型/靶场/性能测试，没有修改 `docs/design/`。图的生成脚本和预览保留在忽略目录 `.data/ctf-task-architecture-20261103/`。

建议单独提交：`docs: diagram CTF scheduling and scoped subagent delegation`，包含本报告、`.drawio` 与两张 PNG。
