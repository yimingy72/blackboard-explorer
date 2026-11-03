# CTF 团队模式架构复核（2026-11-03）

本轮只读 `main`（`55044a9220cd40e1f13cce1044447e42b54a4fb8`）上的实现，并对照两份未进入本仓库的审核原文：策略审查、以及在其之上的调度圆桌。不改运行逻辑。下文“代码”均指这个 `main`；两份报告里的任务耗时、费用和 Pi 进程数据来自作者本机，本仓库没有那两份提交（`54c624f`、`56fa9e0`），本文不把它们当成已复核的测量。

## 结论摘要

圆桌保留「1 个 Lead + 代码调度器 + 持久队友池」，并复用 PostgreSQL、邮箱、Session 和现有执行环境。这个方向与代码相符，也够用。不需要常驻管理 Agent、消息队列或工作流框架。

主要分歧有三处：

1. **「正常结束要到下一轮 `prepare` 才清理旧执行」与 `main` 不符。** 成员回合在调用 `finish_turn` 之前已经 `stop_member`；envd 在上一代未 drain 时拒绝登记新 generation。当前不存在「等清理才派下一题、等下一题才清理」的环。若新门禁要求先 drain，却把 drain 留在下一次 `prepare`，才会造出这个死锁。
2. **「条件允许就铺满 20 名队友」不是当前资源模型能支持的求解并发。** 20 只是某次任务写进 `max_teammates` 的名册上限。仓库默认是 4 名队友、每任务一个 2 CPU / 4 GiB 容器；默认同账号在途调用按合同约预留 ¥3.40。圆桌用来估内存的 Pi/Node「每人一个进程」在这个 `main` 里不存在。`main` 也没有「每成员一个异步长命令」。
3. **第一批不该做新派工表、`settle_dispatch`、两个新模型工具和模型公平队列。** 当前提示词已经要求逐题、先派首批；模型仍可以把多题打成一个 owner。缺口是服务端不变量，不是再加一层策略工具。

建议先做三件事：

1. 一名成员同时只负责一道未终态题；当前 turn 只领取这道题的执行消息。
2. 未被归类的单成员异常不再把整场打成 `system_failure`；停止流程与正常结算抢写结束原因时，也不要因此收整场。自动调度启用后，仍有未分配题目时，空闲看门狗不得把整场收成 partial。关闭阶段若执行注册表已无法证明，走现有换容器，不能在 `closing` 里空转。
3. 仅在显式 `automatic` 配置里，用现有分配事务给空闲且已 drain 的成员补一道题。求解名额单独封顶，默认对齐现有容器，不把 20 当目标。

## 1. 证据怎么分级

| 等级 | 含义 | 本文中的例子 |
| --- | --- | --- |
| 已核对 | 本仓库当前函数的控制流 | 回合结束先 drain 再 `finish_turn` |
| 由合同推算 | 用仓库默认字段算出的数字，没有跑真实任务 | 一次在途调用约 ¥3.40 |
| 报告所述、此处不能复核 | 作者本机数据库或另一分支 | 任务 `400d5309` 的 16 分首次派工；Pi Bridge |
| 待实测 | 要在实现后用假模型和隔离环境测量 | 就绪到派工的延迟、N 路并发的 RSS |

策略审查的运行样本早于其所述的 Pi 部署，圆桌也写了不能把该样本当成新版本性能。本文只采用与 `main` 源码一致的机制判断。本仓库这次 `make check` 是 2463 passed、424 deselected；圆桌写的是 2605 passed、438 deselected，两边不是同一棵树。

## 2. 当前机制

### 2.1 派工是 Lead 的工具调用，调度器只消费邮箱

`CtfCoordinator._tick` 在任务处于 `running` 时，只对 `ready_member_ids` 里的成员调用 `claim_turn`（`services/agent-runtime/src/bbx_runtime/ctf/coordinator.py` 约 583–605 行）。`ready_member_ids` 来自该成员邮箱里尚未推迟的 `purpose=execution` 消息（`services/blackboard/src/bbx_blackboard/ctf.py` 约 778–780 行）。没有人读「哪些题还没有 owner」。

`scheduling_event` 只订阅消息、成员、turn、结论、控制和执行事件（`coordinator.py` 31–42 行）。题目更新本身不唤醒调度器；今天能唤醒，是因为分配同时写了消息。

题目的 owner 有两个写入者。Lead 或用户调用 `update_challenge(action=assign)` / `assign_challenges`。成员自己可以 `update_challenge(action=claim)`，但只能认领无主且 `pending` 的题（`services/blackboard/src/bbx_blackboard/ctf_board.py` 617–631 行）。单条 `assign` 若带 instructions，派信与 owner 写入在同一事务里，带 CAS 和幂等 `request_id`（同文件 624–734 行）。不带 instructions 就只改 owner，不发消息，成员不会被唤醒。`assign_challenges` 不是一批一个事务：`_challenge_batch` 逐项调用 `update_challenge`，前面的项提交之后，后面的项仍可失败（295–353 行）。这些写入都不检查该成员是否已经拥有另一道未终态题，也不要求独立平台题各占一行。`external_id` 可空。

`create_member` 统计所有未 `removed` 的队友，含 `stopped`（`ctf.py` 1091–1094 行）。`CtfOptions` 只有 `max_teammates`（默认 4）、`max_steps`、`context_threshold`（`packages/contracts/src/bbx_contracts/ctf.py` 79–82 行）。名册上限不是模型并发，也不是命令并发。

`idle_watchdog` 在任一成员仍有 turn、非 idle、待执行消息或费用预留时直接返回（`ctf.py` 824–842 行）。全队空闲、仍有未完成题目、又没有可执行消息时，它提醒 Lead，超过次数后以 partial 收尾（同函数 896–953 行）。有人忙时，积压题目不会触发它。

Lead 模板已经要求：可独立验收的目标单独建账，有可执行目标就先派一小批，目录核对与首批派工并行（`profiles/ctf/prompts/lead.md.j2` 第 5–12 行）。2026-10-28 的架构改造把这句写进了提示词，并加上了批量工具。服务端仍接受「一个 challenge 代表多题」和「一个 owner 持有多题」。策略审查若属实（冻结模板与仓库模板一致，模型仍交出 9 个大包），说明这句话没有变成不变量。

### 2.2 一轮怎么开始，执行何时清理

`claim_turn` 在成员没有当前 turn 时，把 generation 加一，并只把最早一条排队消息记入 `assignment_message_ids`——而且仅当它的 `kind` 是 `instruction`（`ctf.py` 1372–1428 行）。随后 `claim_messages` 将该收件人所有 `queued`/`leased`、同一 purpose 的消息全部租给这个 turn（`ctf.py` 1450–1508 行）。检查点再把租到的每条 instruction 追加进 `assignment_message_ids`（`ctf.py` 1747–1756 行）。因此这个集合是回执，不是唯一工作范围。

`dispatch_message_condition` 只影响谁进入 `ready_member_ids`，以及 `claim_turn` 能不能看见某条消息（`ctf.py` 81–96 行）。回合已经在跑时，`claim_messages` 不再套这道门（1450–1476 行）。只在开回合时做题目过滤，挡不住同一次 turn 后面的模型步把其他消息租进来。

结算时仍处于 `leased`、还没写进 Session 的 instruction，会被算进给 Lead 的结果通知，随后又改回 `queued`（2186–2274、2316–2343 行）。checkpoint 已经把它们标成 `delivered` 的正常结束不会走到这里。流在 checkpoint 之前停下时，Lead 会先收到结果通知，同一条指令还会再开一回合。

回合协程的顺序是：

1. `prepare` 向 envd 登记本 generation（`services/agent-runtime/src/bbx_runtime/ctf/runner.py` 200–201 行）。
2. 跑 MAF。模型调用前，中间件先做费用准入；预算不够时在进程内睡眠，睡眠发生在标记「已发送」之前（`services/agent-runtime/src/bbx_runtime/ctf/middleware.py` 205–239、264–280 行）。
3. 协程结束后 `settle`。在仍拥有该 generation 和 turn 时，若执行绑定尚未 drain，先 `stop_member`，再 `finish_turn`（`runner.py` 746–784 行）。`DrainPending` 时结算返回，成员留在 `pending_settlements`，调度器不会为他再 `claim_turn`（`coordinator.py` 587–592 行）。
4. `finish_turn` 发现 `pending_operation` 会拒绝，避免与人工停止抢写（`ctf.py` 2485 行）。成功后成员 `current_turn_id` 清空、`run_state=idle`，generation 保持为刚结束的那一代（`ctf.py` 2292–2302 行）。

envd 的 `CtfCommandRegistry.stop` 取消该 generation 的命令任务，并最多再等 2 秒确认进程组退出（`services/envd/src/bbx_envd/core.py` 27、456–471 行）。`register` 在上一代未进入 stopping 或未 drain 时直接拒绝（同文件 388–399 行）。`prepare` 里对更老 generation 的 drain（`services/agent-runtime/src/bbx_runtime/ctf/execution.py` 159–188 行）只出现在「envd 仍登记着更小的 generation」时，注释写的是丢失绑定后的交接，不是正常收尾的主路径。

人工停止走 `pending_operation`，由 `member_operations` 调用同一个 `stop_member`（`coordinator.py` 285–322 行）。`DrainPending` 是 `RemoteError`，这里被接住后下一轮再试，不挡住其他成员进入后续 tick。整场关闭和 runtime `recover` 也会 drain（`coordinator.py` 346–354、680–682 行）。

仍有一条窄缝：`settle` 发现 generation 或 `current_turn_id` 已经变了，会改走完成确认，不再 drain（`runner.py` 754–761 行）。进程组被拆散后逃出登记表的孙进程，也不在 `_drained` 的进程组探测里。这两处是残留，不是「正常结束要等下一轮才清理」。

### 2.3 换题、求助和串题

`release` 立刻把 owner 清空、状态改回 `pending`，不等 drain，也不写 `previous_owner_id`（`ctf_board.py` 653–659 行）。`request_help` 要求五段说明，并把该题标为 `blocked`，同时给 Lead 发 `help_request`（`ctf_board.py` 1041–1097 行）。owner 还在，没有「已采纳 / 已否定」的结案字段，也没有 `blocks_owner` 开关：求助固定挡住这道题的完成状态。帮手只是 Lead 事后写上的 collaborator。

工具权限看的是调用时的 owner / collaborator / generation，不看「本 turn 唯一的 challenge」。工作目录是 `/workspace/agents/<成员>` 和 `/workspace/shared`，按成员而不是按题分开（`runner.py` 254–256 行）。同一 Session 跨回合保留。所以即便以后把工具调用绑到一道题，旧脚本、旧文件和旧对话仍在。

平台结果方面，`result_adapter` 只有 `none` 和 `fake_ctf_v1`（`packages/contracts/src/bbx_contracts/ctf.py` 147 行）。命令输出里的 `correct:true` 不会变成可信 `accepted`。这点两份报告都对。

### 2.4 容量实际卡在哪里

每场 CTF 一个执行容器。镜像资源来自 profile：默认 2 CPU、4 GiB、profile 内 `pids: 256`（`profiles/ctf/profile.yaml` 27–30 行）。runtime 创建容器时用 `max(profile.pids, exec_min_pids)`，`exec_min_pids` 默认 1024（`services/agent-runtime/src/bbx_runtime/execenv/manager.py` 257–259 行，`settings.py` 27–32 行）。默认部署下进程数上限是 1024，先紧的是 2 个 CPU 和 4 GiB。容器内命令没有单独的任务级并发闸门；同步命令占着该成员的这一轮工具调用。本仓库的 CTF 工具列表里没有异步长命令工具。runtime 调 envd 的 HTTP 超时是 650 秒（`execution.py` 370–372 行），envd 允许的命令超时默认到 1200 秒。模型若请求更长的 `timeout_sec`，客户端会先断开，命令仍留在容器里；之后只有同一个 `command_id` 能再查询，不能换参数重放。

runtime 同时跑的任务数默认 2（`settings.py` 32 行）。两场 CTF 会在同一个 Python 进程里各跑自己的 coordinator，并各占一个容器。

模型调用没有跨成员、跨任务的公平队列。黑板模式的 `ModelRecoveryGate` 在 `services/agent-runtime/src/bbx_runtime/runner.py`，CTF 回合不经过它。CTF 每个回合自己建客户端，结束时关掉（`ctf/runner.py` 266–280、532–546 行）。DeepSeek 走的 OpenAI 兼容客户端默认最多重试 4 次（`services/agent-runtime/src/bbx_runtime/models.py` 578–582 行）。429 不在队友之间共享冷却。空闲成员不保留模型客户端；内存随**正在跑的 turn** 里加载的 Session 增长。这是代码结构，不是本轮量过的 RSS。

费用预留在输入上限存在时启用（`services/agent-runtime/src/bbx_runtime/ctf/budget.py` 16–37 行）。仓库 `ModelConfig.context_window` 默认 1_000_000（`packages/contracts/src/bbx_contracts/models.py` 464–466 行）。`deepseek-flash` 未写 `max_output_tokens` 时，输出上限回退到 393216（`packages/contracts/src/bbx_contracts/model_limits.py` 6–24 行）。预留使用 `context_threshold`（默认 128000）和两边价格的较大值。按 `profiles/ctf/profile.yaml` 的价格（缓存未命中 ¥2 / 百万，输出 ¥8 / 百万）：

```text
输入 128000 × 2 / 1e6 = 0.256
输出 393216 × 8 / 1e6 = 3.145728
一次在途调用 ≈ ¥3.401728
20 路同时预留 ≈ ¥68.03
```

全新 ¥100 预算在无人已花费时大约能同时放下 29 次这样的预留，20 路在开场放得下；账上再花掉约 ¥32 之后就放不下 20 路。输出回退占了预留的大头。`services/blackboard/tests/test_ctf_budget_admission.py` 把 `3.401728` 写死成一次准入样例，与这份默认合同一致；它不是从某次平台快照算出来的。平台模型会在创建任务时替换画像里的模型。快照若没有 `context_window` 且没有 `max_input_tokens`，预留函数返回 `None`，并行调用不再占额度。写了更小的 `max_output_tokens` 时，金额也会变。契约里的 `CTF_BUDGET_RESERVE_RATIO = 0.20` 和 `ctf_spend_limit`（`packages/contracts/src/bbx_contracts/ctf.py` 52–58 行）没有调用方，准入比对的是完整 `max_cost`。一个回合同时只能有一条未结预留（`ctf.py` 469–472 行）。协调器判断要不要因预算收场时，只加已花费和未知预留，不加仍挂在活回合上的预留（`coordinator.py` 151–166 行）。

预算等待不发出模型请求，但 `prepare` 已经完成：等待中的成员占着已登记的 generation、已加载的 Session 和一条活 turn。它不占「模型许可」，因为 CTF 现在没有这张许可。

## 3. 圆桌六个决定

判断用词：对、部分对、错、证据不足。行号会随后续提交移动；以函数名和上述路径为准。

### 3.1 题目表就是队列；原子派工；能覆盖就上 20 人

**部分对。**

对的部分：今天没有面向题目的补位。coordinator 只启动「邮箱里已有执行消息」的人。任务行锁 `Repository.lock`（`services/blackboard/src/bbx_blackboard/store/repository.py` 642–647 行）已经把同一任务的写入串行化。分配与派信已经同事务。用查询表达「无主且可做」，比再存一份 ready 列表干净。

错或过重的部分：

- 20 是名册配置，不是求解槽。默认 profile 为 4。在 2 CPU / 4 GiB 和约 ¥3.40 的在途预留下，把「20 道题就绪」实现成「创建并跑满 20 个求解 turn」会同时打满容器和预算。圆桌后文自己也把 20 降成名册上限，但 M1 验收仍写成「容量允许时 20 人唯一领取」，容易在实现时被当成产品目标。
- 「选题、建成员、授权、派信」合成一个新事务，只有在**同一条连接**上复用现有写入时才原子。现有 `create_member` 和 `assign_challenges` 各自 `engine.begin()` 再抢同一行锁。外层已持锁时调用它们，第二条连接会一直等这把锁：这是新的自死锁，当前 Lead 工具路径没有这个问题。
- 再加 `current_dispatch_message_id`、`scope_revision`、题目上的 dispatch 引用和成员槽，会和现有 `owner_id`、`assignment_message_ids` 形成两套归属。claim 若不同时改，成员可以在旧槽未结时靠任意排队消息开工，随后旧槽结算把槽清掉。

建议：第一批不新增派工表。不变量是「一名 active 成员最多一道未终态 owner 题」。补位直接调用与 `update_challenge(action=assign)` 相同的写入，并且只在显式新配置中启用。

### 3.2 有可执行步骤就开工，目录核对与推进并行

**对，而且提示词里已经是这个策略。**

`lead.md.j2` 第 5 行已经禁止「等全目录、等全员创建、等材料下完再首次派工」。队友模板也要求独立验收目标分开交付。策略审查把样本失败归为「只有策略文字、没有可检验约束」，这个判断与当前代码相符：服务端不拒绝大包，也不拒绝一人多题。

因此下一刀不是重写这段提示，也不是先做完整材料状态机。无稳定平台目录时，系统无法证明「题已经拆干净」；能证明的是「这一行 challenge 只有一个 owner，且该 owner 没有第二道未终态题」。目录覆盖仍是收尾条件，保持现状即可。

材料是否够解题，继续由模型和现有题目字段表达。本仓库没有可信的平台就绪枚举，不能从标题里的「附件」「逆向」推断离线。圆桌把「缺材料且没有获取步骤才等待」留在模型判断里，这是对的。

### 3.3 换题必须交接；每轮只有一个执行范围

**部分对。范围约束对；完整 yield 状态机过重，并且可能引入新的互相等待。**

串题是实的。`claim_messages` 不按 `challenge_id` 过滤。进度通知、旧派工和求助都是同一邮箱里的执行消息，会在下一次模型调用边界注入当前 Session。`assignment_message_ids` 只累积 instruction，不能阻止模型读到其他题的正文后去跑命令。命令的网络目标也无法靠一个新 JSON 字段隔离；圆桌 5.4 节最后对此是老实的。

换题方面，generation drain 已经挡住「旧进程还在，新 generation 就登记」。缺的是题目责任，不是再做一套执行清理。`release` 今天不等待本轮结束，所以 Lead 可以在成员仍跑着 A 的 turn 时把 B 派给他；下一次 `claim_messages` 会把两题的消息放进同一轮。

圆桌要求 yield 在帮手的派工也安全结束之前不释放 owner。帮手若还在等 owner 的下一条说明，owner 又在等帮手结束，两个人都不再有可执行 turn。这是新设计才有的等待环。当前 `request_help` 没有这环，因为它不占帮手的生命周期。

建议的交接：

- 成员只有在本人当前 turn 已结束、该 generation 已 drain、没有 `pending_operation` 时，才能成为另一题的 owner。进行中的换题继续用现有 `release`，但服务端拒绝「旧 turn 未结束就派新题」。
- 释放时把该题仍排队、属于旧范围的 instruction 标成已有的 deferred/cancelled 语义，避免下一轮自动执行。普通进度通知可以留下，但不得以 instruction 的身份进入新 turn。
- 不等帮手。帮手的迟到写入继续靠题目 `revision` CAS 失败。需要停掉帮手时，走现有 `stop_teammate`，不要把人工 stop 和 yield 合成一种 `pending_operation`。圆桌「不扩展 `pending_operation`」这句是对的：`finish_turn` 已经因 `pending_operation` 拒绝结算，再叠一种操作会和停止互相 409。

### 3.4 调度器应主动清理；现状是下一轮才清理

**错。** 正常路径已经在 `finish_turn` 之前清理，见第 2.2 节。

圆桌把「黑板 `_settle` 不调用 envd」和「`prepare` 里有一段旧 generation drain」连成了主路径。黑板本来就不能连容器。runtime 的 `settle` 才是调用方，而且它先 `stop_member`。`prepare` 的那段是丢绑定后的补洞。

因此不需要第二套「后台 future + `settle_dispatch`」才能避免死锁。当前顺序是：回合协程结束 → 锁外 drain → 短事务 `finish_turn` → 成员变为可领取。下一题的消息若在 drain 完成前写入，`pending_settlements` 仍占着该成员，`claim_turn` 不会开始新 generation。

若实现时把「未 drain 不能派下一题」加进 `claim_turn`，同时删掉 `settle` 里的 `stop_member`、改到下一次 `prepare` 里 drain，就会得到圆桌想避免的环。约束应写成：**drain 的责任留在结束本轮的 runtime 结算里，派下一题只检查「结算已经成功」。** 不要为了同一件事再引入一个服务端清理状态机。

人工停止已经优先：存在 `pending_operation` 时 `settle` 直接返回，`finish_turn` 也会拒绝。这条保持。

值得单独修、但不是新调度器的窄缝：身份已经变掉时 `settle` 跳过 drain（`runner.py` 754–761 行）。那条路径应继续对**旧** generation 调用 `stop_member`，而不是靠下一题的 `prepare` 碰运气。

### 3.5 协作要围绕产出闭环；最多两个新工具

**部分对。闭环缺结案记录；两个新工具不是首批。**

`request_help` 已经强制问题、失败路线、证据和阻塞说明，并通知 Lead。缺的是帮手交付之后的 adopted / rejected / cancelled，以及「求助是否必须把题目打成 blocked」。今天一律 blocked。

`resolve_help` 有用，但不必是新工具名。它是帮助记录上的一次结案写入，和 `update_challenge` 的动作同一风格，沿用 request_id 与 CAS。没有成果引用就不得标 adopted。结案不占用帮手名额：帮手 turn 结束后，名额随现有 drain 释放。

`set_team_plan` 可以不做。固定规则「无主的未终态题、成员空闲且已 drain、未超过求解槽，就派一道」不需要模型先写计划。计划工具会多一次模型回合，也会和调度器抢写优先级。真出现饿死或错误偏好，再给 Lead 一个可选的优先级字段；默认没有它也能跑。

`blocks_owner` 若要做，默认保持今天的行为（求助使该题 blocked）。非阻塞求助是后一步，并且不得在 owner 仍被同一 turn 占用时，让帮手再占第二个执行身份。一人一个执行槽。

### 3.6 名册、模型、命令、靶机分开限流；预算等待不占模型名额

**部分对。分开限流这个方向对；圆桌描述的现况有两处和 `main` 不符，公平队列也不是首批。**

对的部分：名册、在途模型调用、容器命令、外部靶机是四件不同的资源。`max_teammates` 只限制第一件。预算等待发生在发送之前，不该被做成「占着供应商并发许可睡觉」。停止必须能取消尚未发送的排队。

与 `main` 不符的部分：

- 「每个 `BaseChatClient` 持有一个 Pi Bridge / 独立 Node」依赖 `services/agent-runtime/src/bbx_runtime/pi/`。该目录不在 `main`。当前是 Python MAF，客户端随回合创建和关闭。用 Node 数验收当前主线没有对象。
- 「同成员最多一个异步长命令已经存在」在 CTF 工具与 runtime 中没有对应实现。命令是同步的 `execute_command`，超时上限默认 1200 秒（`services/envd/src/bbx_envd/settings.py` 的 `command_timeout_max`）。
- 预算等待今天不占模型许可，因为许可不存在。它占的是已 `prepare` 的 generation 和内存中的 Session。先加许可、再在持有许可时进入现有睡眠循环，才会出现圆桌警告的问题。

命令闸门值得做，但应落在 envd 真正 `spawn` 之前，并先用默认 2 CPU 容器量一个很小的上限。在量到之前，自动补位的求解槽应配置成小整数，而不是等于 `max_teammates`。未知的供应商并发上限保持不猜：没有配置就不发明「7 路」或「10 路」。429 的共享冷却可以复用错误分类里已有的 `rate_limit`，等并发真的打出 429 再加；现在就做跨任务轮转队列，收益依赖尚未在本仓库观察到的供应商行为。

跨任务的外部靶机名额圆桌说得对：单任务行锁保护不了同一平台账号的配额。这要等真实适配器存在后再做持久占用记录。`fake_ctf_v1` 不能充当证明。

## 4. 策略审查里其余判断

| 判断 | 结论 | 理由 |
| --- | --- | --- |
| 继续堆提示词不能保证逐题覆盖 | 对 | 当前模板已要求逐题和先派首批，服务端没有对应不变量 |
| 一人一个主工作项，未开工题留在共享队列 | 对 | `assign` 不检查该成员是否已有未终态题 |
| 准备 / 求解 / 验证拆成带租约的阶段派工 | 不采纳 | turn、generation、邮箱租约已经是执行事实。再加 phase lease 会和它们竞争。验证继续用现有候选与可信适配，不另建阶段工人 |
| 在线名额按阶段占用，上限 3 | 证据不足 | 仓库没有真实平台配额合同。3 来自某次任务的脚本约定。有适配器之后用适配器的配置，不写死 |
| 单成员可隔离失败不应结束整场 | 对，且应提前 | 见第 5 节。圆桌的 M1–M4 没有把这件事列为交付 |
| 代码做补位，只有需要选择时才叫 Lead | 对 | 与「不增加调度 LLM」一致。`set_team_plan` 不是每题的必经步骤 |
| 20 项就绪且资源允许就扩到 20 人 | 部分对 | 只在求解槽、预算预留和容器同时允许时成立。默认容器和默认预留下，20 不成立 |
| 样本中的墙钟、并行度和费用 | 不在此复核 | 数据不在本仓库。即便属实，也只能说明旧任务的 Lead 行为，不能当作 Pi 或本方案的性能基线 |

策略审查图里的「材料准备流水线」和「平台资源控制」是目标职责，不是现成模块。圆桌把它们放到真实适配器阶段，这个顺序比「先做目录导入再允许自动派工」稳：没有适配器时，用户和 Lead 仍可用现有建题入口，自动补位只消费已经写成独立 challenge 的行。

## 5. 更影响正确性、两份报告没有放到第一批的问题

### 5.1 一个未归类异常会结束整场

`runner` 把 `provider_transient`、上下文边界和传输中断收成回合结束原因。除此之外，turn 协程上的异常会在 `coordinator._tick` 里变成 `request_close(..., "system_failure")`（`coordinator.py` 524–538 行）。tick 自身未分类异常同样收场（815–819 行）。共享账本损坏、容器身份不明、鉴权失败，应当停整场。某一名成员的工具包装错误或未捕获的运行时错误，不应当拆掉其他独立题目的进程。

这比调度器是否 1 秒内派工更直接影响「能不能继续解题」。隔离时要留下该成员的检查点；执行副作用不明时只停该 generation，不重放命令。其他成员的 turn 继续。

还有一条会把整场打成 `system_failure` 的窄路径。`finish_turn` 发现回合已经结束就原样返回（`ctf.py` 2481–2483 行）。停止流程若已把同一回合结算成 `stopped`，runner 的 `_validate_finished` 仍要求 `end_reason` 等于自己手里的 `completed`（`runner.py` 706–714 行）。对不上就抛错，协调器收整场。停止标志若在 `settle` 进入 `stop_member` 之前就可见，`settle` 会直接返回，这条是安全的（`runner.py` 752–753 行）。危险窗口在 drain 已经开始之后。

### 5.2 空闲看门狗会在仍有题目时把整场收成 partial

看门狗的触发条件是「没有可执行消息」，不是「没有未完成的题」。全队一空闲，未完成题目只导致几次提醒，然后 partial 关闭。自动补位若和看门狗并行，启动后的空窗会把「还没来得及派的无主题」收掉。启用自动模式时，只要还有无主的未终态题，看门狗就不得进入 partial。已有 owner、只是成员停住的题，仍可以提醒 Lead，这是现有语义。

### 5.3 输出上限回退把预算并发卡死，也让等待中的回合占着容器登记

第 2.4 节的 ¥3.40 里，约 ¥3.15 来自 393216 的输出回退，不是来自 128k 输入。20 个已经 `prepare` 的成员在预算等待里睡眠时，不打模型，但占着 envd generation。公平队列解决不了这件事。应当：

- 任务快照若没有明确 `max_output_tokens`，预留继续用保守上限，避免改成平均值后超支。
- 自动补位在 `check_budget(reservation)` 失败时不创建、不派工，并把原因写成「预算预留不足」，而不是再开一个 turn 进去睡。
- 是否把默认输出上限改成模型真实窗口，属于模型配置，不塞进调度器。改之前要用现有计费测试证明预留不会低于单次最大输出。

### 5.4 同一 Session 和同一工作目录会把上一题带进下一题

消息范围修好之后，模型仍能读到历史工具结果，并在同一家目录里执行。drain 只负责登记过的进程组。首批应把「新派工不得自动执行旧题 instruction」做严；不要承诺 shell 已经按题隔离。按题分目录可以作为后续，它会碰归档路径和现有 `/workspace/agents/agent-N` 约定，不与派工不变量绑在一个变更里。

### 5.5 新调度器自己的一致性风险

实现补位时必须同时满足：

1. 不在持有任务行锁的事务里调用会新开事务并再次 `lock` 的公开方法。
2. 不在该事务里等待 envd。drain 保持在锁外，和今天的 `settle` 一样。
3. 补位与 Lead 的 `assign`、成员的 `claim` 使用同一 owner 检查。自动模式下这三条写入都要满足「一人一题、旧执行已结束」。
4. 范围过滤做在 `claim_messages`，并且和补位同一批交付。只在 `claim_turn` 过滤，同回合的下一步仍会把其他消息租进来。
5. 成员 `claim` 和 Lead `assign` 都走一人一题。漏掉 `claim`，自动补位之外仍有第二条占题路径。

### 5.6 关闭阶段不再换容器，进程退出也不 drain

`_tick` 一看到 `phase==closing` 就进入 `_close_tick`，后面的健康检查和 `replace_execution` 不会跑（`coordinator.py` 435–436 行，健康检查在 441 行之后）。registry 为 `unknown` 时 `drain()` 直接返回 false（`execution.py` 242–248 行）。关闭超时也不绕过这次 drain。于是关闭完不成，销毁容器又要等任务终态。这不是派工环，但整场会停在 `closing`。

runtime 的 `close` 和 `detach` 只取消本地回合并关掉 HTTP 客户端，不调用 envd drain（`coordinator.py` 949–1003 行）。回合协程在具备 `stop_member` 时收到 `CancelledError`，自己也不 drain（`runner.py` 424–439 行）。命令留在容器里，直到该任务仍是 `running` 时的 `recover`，或容器被销毁。邮箱租约没有按 `lease_until` 退回 `queued` 的清扫；只有回合结算、移除或续跑会清租约。

## 6. 建议的架构

### 6.1 目标

让已经写成独立 challenge 的题，在成员空闲且资源允许时被分到一名队友身上；一名队友一次只执行一道未终态题；旧题的命令不会留到新题的 generation；一个成员的普通故障不结束其他题。Lead 继续处理建队例外、求助取舍和收尾结论。

不做：保证 20 路加速、按题隔离任意 shell 的网络目标、通用资源池、调度专用模型、阶段租约、技能市场。

### 6.2 职责

| 组件 | 继续负责 | 增加的边界 |
| --- | --- | --- |
| Lead | 建题、例外、求助取舍、整场结论 | 自动模式下不再是唯一派工者；手工派工与补位同一套检查 |
| `CtfCoordinator` | 邮箱驱动的 `claim_turn`、停止、关闭、恢复 | 自动模式下每轮 tick 最多补有限道题；不在 tick 里 drain |
| 黑板题目事务 | owner、CAS、派信、记录通知 | 一人一题；按当前题过滤可领取消息 |
| runtime `settle` | 锁外 drain，然后 `finish_turn` | 身份已变时仍 drain 旧 generation |
| envd | generation 围栏、进程组停止 | 首批不改。命令并发上限留到量完容器之后 |
| 费用账本 | 最坏预留、unknown 保留 | 补位前用同一次预留估算做准入，不把等待放进新 turn |
| 平台适配 | `fake_ctf_v1` 仅测试 | 真实目录和靶机配额另阶段，不阻塞补位 |

### 6.3 状态机

题目继续使用 `pending` / `in_progress` / `blocked` / `completed` / `cancelled`。不增加 ready 表。自动补位眼中的「可派」是：未删除、`pending`、`owner_id` 为空、任务在运行、没有替换容器或未决检查点挡住全场。`blocked` 仍表示有 owner 的题在等外部条件或求助，不视为空闲工作。

成员继续使用 lifecycle、`run_state`、generation、execution 绑定。可接受新题的条件：`active`、没有当前 turn、没有 `pending_operation`、没有未完成结算、执行绑定为空或已对当前 generation drain、未终态 owner 题数量为 0。`stopped` 不自动恢复。

派工不新造实体。一次派工就是现有的 assignment instruction，带 `challenge_id`。该消息 ID 就是这次派工的标识。重试复用 `request_id`，不换 ID。

求助继续是 `help_request` 记录加给 Lead 的消息。结案是记录上的状态，不是帮手的第二套 run_state。帮手占用的是普通成员槽：他被派到这道题或作为 collaborator 执行时，同样一人一题。

```text
无主 pending
    │ 补位、Lead 派工或成员 claim（同一人一题检查）
    ▼
in_progress（唯一 owner，一名成员至多一道）
    ├─ request_help → blocked（owner 保留，成员槽仍被这道题占用）
    ├─ release 且本 generation 已 drain → 无主 pending
    └─ completed / cancelled → 终态，owner 作为历史留下，槽释放
```

有界恢复（`provider_transient`、`context_blocked`、`step_limit`）期间 owner 不变，补位不得把这道题交给别人，也不得给该成员第二道题。

### 6.4 调度

只在新任务显式选择 `dispatch_mode=automatic` 时运行。旧快照缺字段视为 `manual`，行为与今天相同。

每一轮 coordinator tick 在启动已有邮箱之后，最多处理固定小批（建议 4，避免一次 tick 占住全部成员）：

```text
锁外读短状态
对每个候选：
  短事务内锁任务
  再次确认：automatic、running、无替换/关闭、题目仍无主、成员仍可接收
  写入 owner=该成员、work_status=in_progress、一条 instruction
  提交
现有 claim_turn 在提交后的唤醒里启动该成员
```

只改 owner、不写 instruction 的 `assign` 今天不会把成员放进 `ready_member_ids`。补位必须带上那条 instruction。成员 `claim` 在 automatic 下也要服从一人一题；否则成员可以绕过补位自己再占一道。

人选：已经空闲且满足第 6.3 节的成员。没有这样的成员、求解槽未满、名册未满时，才创建一名队友。求解槽是新的可选上限，默认取 profile 的 `max_teammates` 与一个保守常数中的较小者；在命令闸门落地前，保守常数取 4，与今天的默认名册一致，不取 20。同题优先交还给 `previous_owner` 仅在该成员空闲且已 drain 时发生；`release` 需要开始记录 `previous_owner_id`。没有空闲的原成员就交给其他空闲成员。

不按分类把未开工题预先分给某人。不根据模型自报的轻重活来加减槽位。

`claim_messages` 在自动模式和手工模式下都增加同一过滤：执行 turn 只租借 `challenge_id` 为空（纯协调）或等于该成员当前未终态 owner 题的消息。其他题的 instruction 保持排队或按现有 deferred 规则放下，不进入这个 Session 的工具循环。用户直接发给该成员的消息保留，这是现有用户插话，不是派工。

Lead 摘要由代码生成，只含计数和 ID：无主题数、进行中、blocked、可派成员、因预算或求解槽没派出去的原因、未结求助。不向模型报告剩余金额和剩余时间。摘要写入若没有新的版本，不重复唤醒 Lead。看门狗的「全队空闲」提醒保留给 manual；automatic 在仍有无主题时不 partial 收尾。

### 6.5 并发与容量

四层各自计数：

| 层 | 首批行为 | 后继 |
| --- | --- | --- |
| 名册 | 继续 `max_teammates`，含 stopped，不含 Lead | 不变 |
| 求解槽 | 自动创建和自动派工遵守；默认不超过 4 | 有容器测量后再调 |
| 模型在途 | 继续最坏预留；预留失败则不新开 turn | 出现 429 后再做同账号共享冷却 |
| 命令 | 首批不改 envd | spawn 前的任务级上限，排队可被 stop 取消 |
| 靶机 | 无真实适配器则不限额、不假装已限额 | 按资源范围持久占用，跨任务 |

预算等待继续留在已经跑起来的回合里，并且发生在发送之前。自动补位不要制造这种等待：派之前预算放不下一次预留，就停止派工并显示原因。

未知费用预留继续占用预算。执行已经 drain 的成员可以在剩余预算仍能覆盖一次新预留时接下一题。执行结果不明时不派新题、不重放命令。

### 6.6 失败与恢复

| 情况 | 行为 |
| --- | --- |
| 单成员未分类异常，检查点可保存 | 结束该成员本轮，保留 Session，题目保持 owner，其他成员继续。不 `request_close(system_failure)` |
| 账本、检查点或容器 boot 无法确认 | 保持今天的整场暂停 / 容器替换 |
| provider / 上下文的有界恢复 | 保持现有次数和延迟；恢复完成前不换题 |
| drain 未确认 | 保持该成员占用，下一次 tick 重试同一 generation。不派新题来触发清理 |
| 停止插在 drain 和 `finish_turn` 之间 | 现有 `pending_operation` 接管，不自动恢复 |
| runtime 重启 | 现有 `recover`：先证明 drain，再允许新 generation |
| 看门狗 | manual 保持；automatic 有无主题则不 partial |

### 6.7 观测

工作台和任务状态增加代码写出的等待原因，而不是 Lead 的自述：名册使用量、求解槽使用量、无主题数、预算预留不足、成员未 drain、看门狗被挡住的原因。20 个已创建成员不算 20 路求解。

指标只在有基线之后比较：可信完成数、首个 flag 时间、费用、无主等待时长。就绪到派工、派工到 turn 启动的分位数是实现后的测量项。圆桌写的 p95 ≤ 1 秒和 ≤ 5 秒是目标提案，不是本仓库已测结果；在默认 2 秒兜底轮询（`coordinator.py` 的 `FALLBACK_SECONDS`）仍在时，事件丢失路径会比 1 秒宽松。首批验收用行为，不用这两个数卡合并。

### 6.8 明确不建

不建 assignment 表、工作租约、调度 LLM、`set_team_plan`、阶段 DAG、第二套清理状态机、Pi/Node 形态的进程模型（除非另有迁移任务真的合入）。不把 `docs/design/` 下的通用黑板设计改成 CTF 专则。

## 7. 分阶段与验收

每阶段都能用假模型和 `make check` 里的脚本化客户端验证。真实模型和靶机不在这些阶段里。

### P1 不变量，不自动铺人

改动：一人一道未终态题，`assign` 和成员 `claim` 都检查；`claim_messages` 按当前题过滤；`release` / 派工在旧 generation 未 drain 时拒绝新 owner；`settle` 在身份已变时仍 drain 旧 generation；单成员未分类异常不收整场，停止与正常结算的 `end_reason` 不一致也不收整场；`closing` 在 registry 无法证明时走现有换容器；为失败成员保存可区分的诊断类型。

验收：

- 已有 owner 的成员再被 `assign` 或自己 `claim` 第二道未终态题时，第二次失败，第一题 owner 不变。
- 邮箱里同时有 A、B 两题的 instruction 时，正在做 A 的 turn 不会租到 B 的 instruction。
- 正常结束的 turn 在 `finish_turn` 之前对**本** generation 得到 drain 证明；下一轮 `prepare` 看到的是已 drain 的上一代，而不是由它第一次去杀进程。
- 身份被停止流程改写后，旧 generation 仍有 drain 尝试，而不是只依赖下一轮 `prepare`。
- 一名成员抛出未归类异常后，另一名成员的 turn 仍可开始并完成。共享容器 boot 在任务仍为 `running` 时不明，整场仍停止并换容器。任务已经 `closing` 且 registry 为 `unknown` 时，也要换容器或留下可结束的失败，不能一直返回 drain 未完成。
- 停止流程把回合结算成 `stopped` 之后，runner 再读到的 `finish_turn` 结果不得因为 `end_reason` 不同而把整场打成 `system_failure`。
- `manual` 旧测试、旧消息 `challenge_id` 为空、看门狗 partial 路径保持。

### P2 显式自动补位

改动：`dispatch_mode`，默认 manual。automatic 下用第 6.4 节的短事务补位，求解槽默认不超过 4。看门狗在仍有无主题时不 partial。状态里带未派原因。Lead 手工派工走同一人一题检查。

验收：

- 4 道无主题、求解槽 4、名册允许：4 名不同成员各一题。第 5 道保持无主，并写出槽位原因。
- 名册 20、求解槽 4、20 道题：最多 4 个求解 turn，不多建到 20。
- 完成并 drain 之后，同一成员自动接到下一道无主题；turn 未结束时不接。
- 两个补位者并发：一道题只有一个 owner，一条派信。
- 预算预留放不下时不创建 turn，原因可见。
- 有无主题时看门狗不把任务打成 partial。
- 旧 snapshot 不带 `dispatch_mode` 时不补位。

### P3 求助结案，以及测过之后的资源闸门

改动：帮助记录可结案；结案不新增工具名，除非现有动作无法表达引用完整性。用脚本化供应商验证 429 时，再加同账号共享冷却，并保证预算睡眠不持有该许可。用容器测量后，再在 envd spawn 处加命令上限。

验收：

- 无成果引用不能 adopted。帮手 turn 结束后其求解槽释放，不必等 owner 结案。
- 结案前 owner 仍是唯一未终态负责人。
- 若做了模型许可：等待预算时许可已归还；两个任务的在途请求不超过配置；未配置则测试证明「没有发明默认并发」。
- 若做了命令上限：同一 command id 不执行两次；stop 能取消尚未 spawn 的排队；已 spawn 而结果不明的不自动重放。

### P4 一个真实平台适配器

目录分页、附件校验、资源占用和提交回执分三小步。配额以适配器配置为准。未知回执先查询。这一阶段不反向修改 P2 的补位条件，只把「可派」从「无主 pending」收紧为「无主且适配器标记材料或题目可执行」。没有适配器的任务保持 P2 语义。

### P5 对照

同题集、同模型、同预算，比较当前 manual、只改提示词、以及 P2 自动补位。看可信完成数、首 flag 时间、费用和波动。创建了多少成员不是指标。

## 8. 事实与假设

已核对：邮箱调度、一人可多题、成员可 `claim` 无主题、`assign_challenges` 逐项提交、`claim_messages` 全量租借且不再套延迟门、`settle` 先 drain、envd 拒绝未 drain 的新 generation、`closing` 不再换容器、`close`/`detach` 不 drain、看门狗条件、`system_failure` 的整场范围、默认 2 CPU / 4 GiB、pids 地板 1024、`max_running_tasks` 默认 2、CTF 不走 `ModelRecoveryGate`、无 Pi 目录、无异步长命令工具、0.20 预留比例没有调用方、预留公式与 `fake_ctf_v1` 的范围。

由默认合同推算、未跑任务：一次在途调用约 ¥3.401728，20 路约 ¥68.03。准入测试把这个数写成样例。换价格、写上 `max_output_tokens`，或平台快照失去输入上限后作废。

待实测：N 个同时加载的 Session 使 runtime RSS 增加多少；2 CPU 容器在几路 `execute_command` 时饱和；供应商在几路并发时返回 429；自动补位相对「只改提示词」是否提高得分。在这些数出来之前，求解槽保持小，20 只作为名册配置的合法取值，不作为默认并发。

## 9. 和现有设计文档的关系

本文件是复核结论，不是新的设计正本。`docs/design/` 未改。若进入 P1，先把「一人一题（含成员 claim）、按题过滤领取、drain 仍在回合结算、单成员故障隔离、closing 无法证明时换容器」写进 `docs/proposals/CTF-team-mode-design.md` 并单独提交，再改代码。圆桌草案里的 dispatch 列、`settle_dispatch` 和 `set_team_plan` 不进入那次设计修订，除非 P2 之后的测量说明第 6 节的不变量不够用。
