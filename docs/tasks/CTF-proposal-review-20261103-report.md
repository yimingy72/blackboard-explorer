# 外部 CTF 架构复核提案审查与本地方案更正

日期：2026-11-03，北京时间。审查用户提供的[提案](https://github.com/yimingy72/blackboard-explorer/blob/bb95460d4f65a071065c89dd31ec7b9c589ee057/docs/proposals/CTF-architecture-review-20261103.md)，固定到提交 `bb95460d4f65a071065c89dd31ec7b9c589ee057`，文档 blob `454253d86da2301b85fa8b7c274eb93c0c04e7f3`。只做审查、本地报告更正与离线核验，没有修改生产逻辑或在 GitHub 发布评论。

## 1. 结论

这份提案正确指出了本地圆桌的一处事实错误，并提出值得提前处理的停止竞态、成员故障范围、closing 恢复问题。应吸收它“复用已有结算链、先明确不变量”的方向，但其消息过滤、旧任务兼容、helper 交接和预算预检方案仍有具体缺口，不能原样实施。

外部文档明确以 `55044a9` 为基线；当前本地部署与主线代码基线是 `56fa9e0`，包含后续异步命令和 Pi 迁移。两者不一致并不使其全部审查无效，但“没有 Pi”“没有异步工具”“固定 650 秒调用超时”等旧树事实不能用于否定本地已部署实现。平台默认 2 CPU/4 GiB 也不能代替此前任务实际冻结的资源配置。实施前应选定同一提交逐项复核，保留其已有证据分级。

## 2. 接受的纠正与优先项

### 2.1 撤回“正常结束只在下一轮清理”的结论

当前 [runner.settle](../../services/agent-runtime/src/bbx_runtime/ctf/runner.py#L863)读取成员状态，必要时先 `stop_member`，随后才请求 `finish_turn`。上一轮只追到 `run` 的最终调用和 `execution.prepare`，漏读了 `settle` 的完整控制流，这是本地审查错误，不是版本差异。旧 `55044a9` 中也有相同顺序。

本轮离线脚本调用实际 `CtfRunner.settle`，记录到 `read_member → drain_member → finish_turn`。已在[圆桌报告](CTF-dispatch-roundtable-20261103-report.md)开头加入优先级最高的更正，撤回以该错误为依据提出的第二条清理路径和独立 `settle_dispatch` 收尾服务。新题目责任/交接收敛应优先接到现有结算事务；执行范围绑定仍有独立用途，需要单独论证最少字段。

### 2.2 停止结束原因竞态在当前版本仍可复现

如果停止操作在 runner 读到仍运行的成员之后、drain 返回之前完成，`finish_turn` 会原样返回已持久化的 `stopped` turn；runner 随即用本地 `completed` 调用 `_validate_finished`，抛 `RuntimeError`。[读取返回校验](../../services/agent-runtime/src/bbx_runtime/ctf/runner.py#L895)、[终态直接返回](../../services/blackboard/src/bbx_blackboard/ctf.py#L2788)。

当前 `_confirm_finish` 已能识别带证据的权威停止，但这一直接成功响应路径没有走它。本轮用确定性 fake board/envd 复现了该窗口。最小修复应统一权威停止结果核对，不能简单忽略所有 end_reason 不一致；也不能在身份已换代后无条件用当前绑定清理，以免影响新一代工作。

### 2.3 故障隔离和 closing 恢复应列入早期交付

未分类异常目前会触发整场 `system_failure`；可证明是成员局部、检查点与执行状态已得到处理的错误应隔离。无法证明范围的错误仍要暂停准入或进入既有恢复，不能仅因“能保存 Session”就认定无共享影响。

当前 `_close_tick` 中 `envd.drain()` 返回 false 会继续等待；registry unknown 也返回 false。提案要求明确关闭阶段的容器销毁/替换和失败收敛路径有价值。必须保留精确容器身份、归档和检查点屏障，不能以超时当 drain 已证实。本轮没有对真实容器破坏恢复做测试。

## 3. 必须修改的提案内容

### 1. [P1] 按当前 owner 题过滤消息，会挡住 Lead 与 helper

位置：[第 305 行](https://github.com/yimingy72/blackboard-explorer/blob/bb95460d4f65a071065c89dd31ec7b9c589ee057/docs/proposals/CTF-architecture-review-20261103.md#L305)。规则只允许空 challenge_id 或成员当前未终态 owner 题的消息。

反例：A 的 owner 是 X，helper 是 H，Lead 不担任 owner。X 发出的 `help_request(challenge_id=A)` 会被 Lead 的过滤器排除；发给 H 的支援 instruction 也被排除。当前 `claim_turn` 又会先从排队消息启动 H，随后 `claim_messages` 拒绝租走消息，可能产生没有实际输入的 turn，结束后同一排队消息继续唤醒。

最小修改：Lead 保留跨题协调消息；worker 以本轮明确派工范围处理消息，owner/helper 都可合法绑定；`claim_turn`、`claim_messages` 和有副作用工具使用同一范围判断。空 challenge_id 不能一律认作纯协调，因为历史派工和当前 `send_message(instruction=true)` 都允许缺失题目引用。不必建立 assignment 表，但不能只靠可变的 owner 查找定义当前执行范围。

### 2. [P1] P1 同时改变 manual 规则，与旧任务兼容承诺冲突

位置：[P1 实施与验收](https://github.com/yimingy72/blackboard-explorer/blob/bb95460d4f65a071065c89dd31ec7b9c589ee057/docs/proposals/CTF-architecture-review-20261103.md#L351)、[manual 也启用过滤](https://github.com/yimingy72/blackboard-explorer/blob/bb95460d4f65a071065c89dd31ec7b9c589ee057/docs/proposals/CTF-architecture-review-20261103.md#L305)。

反例：旧冻结任务里，一个成员已拥有 A、B 两题并排队了两条旧 instruction。新的一人一题/按单一 owner 过滤无法同时维持该任务原本允许的行为；它与第 285 行“旧快照 manual，行为与今天相同”不能同时成立。

最小修改：新的单工作范围与认领约束按冻结的 automatic 版本启用，manual 保持旧语义。通用故障修复可以覆盖两种模式。旧任务若要迁移，必须在显式续跑且已清理执行的边界完成范围对账；不要将“旧测试通过”作为迁移合同。

### 3. [P1] helper 的迟到操作不能仅由题目 CAS 阻断

位置：[第 140 行](https://github.com/yimingy72/blackboard-explorer/blob/bb95460d4f65a071065c89dd31ec7b9c589ee057/docs/proposals/CTF-architecture-review-20261103.md#L140)。提案建议不等待 helper，迟到写入依靠 revision 冲突。

反例：X 释放 A，Y 成为新 owner，旧 helper H 的 collaborator 身份仍保留且 turn 还在运行。普通 `append_record` 没有题目 CAS 检查，只要 H 仍在 collaborator_ids 就能写入；执行命令也不会检查题目 revision。即使候选提交有 CAS，H 读取新 revision 后仍能提交，revision 不是旧执行权限的撤销证明。[记录追加检查](../../services/blackboard/src/bbx_blackboard/ctf_board.py#L884)。

最小修改：责任移交前关闭旧支援准入，明确撤销旧范围权限并完成相关执行清理，或用可验证的派工代次隔离旧结果；原始历史观察可保留，不能当当前派工成果。不要等待 owner“采用”结果才释放已完成 helper，但也不能把仍执行的 helper 当已经结束。只等待而不提供终止/交接路径确实可能互相等待，这一点应吸收提案的提醒。

### 4. [P2] 派工前调用预算检查，不足以保证新 turn 不会等待预算

位置：[第 218 行](https://github.com/yimingy72/blackboard-explorer/blob/bb95460d4f65a071065c89dd31ec7b9c589ee057/docs/proposals/CTF-architecture-review-20261103.md#L218)、[第 321 行](https://github.com/yimingy72/blackboard-explorer/blob/bb95460d4f65a071065c89dd31ec7b9c589ee057/docs/proposals/CTF-architecture-review-20261103.md#L321)。

反例：余额只够一次预留，有两个候选。`check_budget(reservation)` 只读检查，不登记预留；两个派工事务先后都能通过，而第一笔真正预留要到 runner 模型准入才发生，第二个新 turn 仍会进入 budget_wait。任务行锁不能消除跨这两个阶段的空窗。[预算检查](../../services/blackboard/src/bbx_blackboard/ctf.py#L120)。

最小修改二选一：将预检明确限定为调度提示，继续允许已有的安全 budget_wait，不承诺“新 turn 不等待”；若必须保证该行为，则在现账本原子占用可转交首调用的额度，并定义取消和重试结算。不能仅增加一次 check 就声称已完成准入。

## 4. 应调整的范围与表述

- 自动模式初始使用较小、公开可调的求解容量，可作为灰度策略；但 4 不是测得的性能极限，不能由“默认 2 CPU”推出所有 20 路模型/I/O 工作都不可行。独立容量应在界面和有效配置中可见，用户设 20 后只跑 4 必须能解释且可按测量调整。
- 一人一条 challenge 并不能阻止一个 challenge 包含 18 道独立题。允许先交付自动补位，但要说明它尚未完成全量逐题覆盖；真实目录适配/来源映射仍是原始目标的一部分。
- 延后两个模型工具、先复用现有记录与题目更新接口是可采纳的简化。是否需要新工具取决于最小合同的可表达性，工具数量本身不能证明性能收益。
- 不新增 assignment 表与第二套执行清理可采纳。本地圆桌本来就没有要求新增 assignment 表或阶段租约，批评时应区分已有提案与假设实现，避免为不存在的分歧做设计。
- “身份已改变就再 drain 旧 generation”需保留原 `boot/generation` 与权威停止证明核查；当前 `_confirm_finish` 和相关测试已有精确旧回合核对。不能误用新成员绑定去停止新工作。

## 5. 建议合并后的第一批范围

1. 对齐当前部署源码；先修复已复现的停止结算竞态，以及证明范围后的成员故障隔离和 closing 收敛。
2. 只在新 automatic 配置中增加明确的单工作范围，覆盖 owner/helper、消息领取与工具权限，保留 Lead 的跨题协调能力。
3. 复用现有分配事务、任务锁和 `settle → finish_turn` 链路做自动补位；派工范围与责任释放在已有结算边界收敛，避免第二条清理路径。
4. 对容量、预算等待和无主等待给出真实原因。随后接入逐题目录/材料准备，按实测添加模型/命令准入。

先解决这些实际控制流问题，再比较自动调度对首 flag 时间、得分和费用的影响。不能以简化状态数量为理由删掉必要执行身份，也不能以补齐身份为理由重复现有清理机制。

## 6. 实际验证与限制

- GitHub 插件读取并固定文档提交/blob；本地读取 `55044a9` 与当前源码对应函数，确认基线差异。
- `.data/ctf-proposal-review-20261103/check_settlement.py` 使用实际 `CtfRunner.settle` 与内存 fake 服务：确认正常 drain 顺序，并复现 drain 后权威停止与本地 completed 校验冲突。没有模型、数据库或容器调用。
- 同目录 `check_boundaries.py` 调用实际 `check_budget` 和 `_append_record`：确认重复预检不占额、owner 更换后仍保留身份的 helper 可追加记录。只操作内存替身，未写业务数据。
- `.venv/bin/pytest -q services/agent-runtime/tests/test_ctf_turn_transport.py services/agent-runtime/tests/test_ctf_close_coordinator.py -m 'not integration and not live'`：**52 passed，3.68 秒**。已有测试没有覆盖上面刚复现的直接成功响应竞态，测试通过不表示该缺口不存在。
- 前轮同一生产源码的完整 `make check` 已有 **2605 passed、438 deselected**；本次只改文档，不重复整套检查，不将旧结果记为本轮新增运行。
- 新提案尚无实现，所以消息过滤和预算派工缺口按明确规则与当前 API 合同推演，未声称运行了不存在的自动调度器。未实测真实供应商、并发解题收益或破坏性容器恢复。

本轮文档提交建议：`docs: review the external CTF proposal and correct drain analysis`，包含本报告与圆桌报告更正。诊断脚本和远端快照保留在忽略目录，不修改对方分支，不发布 review/comment，不部署。
