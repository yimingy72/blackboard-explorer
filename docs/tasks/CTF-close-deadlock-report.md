# CTF 收尾死锁事故报告（run-2 任务在 `closing` 阶段空转 60 分钟）

- **状态**：已审核并完成本轮修复，验证与发布见 [修复报告](CTF-close-context-fix-report.md)。下文保留原事故分析及不确定性。
- **严重度**：高 —— 任务级完全卡死、时间与预算白耗、平台侧每秒数十次无效写；且与上一轮（run 1）的失败是**同一个洞的镜像两面**
- **分析对象**：任务 `b92a831f-f007-40cf-9aa3-bae3783d31e8`（第 4 轮 / run 2）
- **代码基线**：工作树（`HEAD = 0115a19`，含大量未提交改动）
- **行号使用须知**：⚠️ 本报告所有行号取自**当前工作树**，而 `runner.py` / `coordinator.py` / `ctf.py` / `session.py` / `coordinator` 相关文件**本身就处于"已修改未提交"状态**（`git status` 可见）。请一律**按符号/函数名定位**（如 `CtfCheckpoint.save`、`retry_checkpoints`、`_fence`、`finish_turn`、`_settle`、`finalize_close`），行号仅作参考；若与工作树不符，以符号语义为准。
- **证据来源**：blackboard 访问日志（`127.0.0.1:58000`）、平台 DB（只读）、agent-runtime 容器日志、`ctf_turns` / `events` / `tool_calls` 记录
- **约束**：本报告不含任何真实 flag 值；平台数据仅只读；未运行 `grade.py`

---

## 0. 一句话结论

一次 checkpoint 写失败（被拒绝或响应丢失）后，平台把该 turn 记入 `ctf_control.unresolved_checkpoints`；**随后该 turn 正常结束，而平台没有任何"turn 结束后清除未决项"的路径**。收尾屏障 `finalize_close` 要求该集合为空，于是：

```
未决项永不清空  →  finalize_close 永不执行  →  coordinator 的 closing 分支无条件 return True
              →  ［收尾循环］每秒约 3.5 次重试同一个已结束的 turn，成功率 0
```

死锁自 03:36:56 起持续 **60 分钟**，直到外部强制置为 `failed`/`closed` 才停止。**团队不是慢，是收尾流程走不完。**

---

## 1. 观测事实与量化

| 项 | 值 |
|---|---|
| 团队停手 | 03:36:55（lead 主动请求结论，`end_reason=partial`） |
| 任务卡死窗口 | 03:36:56 → 04:37:27，**≈3631 秒** |
| `checkpoint` 409 | **12611** |
| `checkpoint_failed` 409 | **12611**（与上行 1:1 成对，说明每次重试都连撞两次 fence） |
| `checkpoint` 200 | 2179（**全部发生在 03:36:56 之前**） |
| `checkpoint_failed` 200 | **1**（仅 03:33:05.200） |
| 死锁期有效进度 | **0** |
| 重试速率 | 12611 / 3631s ≈ **3.5 次/秒** |
| 任务终态 | `status=failed`、`phase=closed`、`end_reason=partial`、`active_seconds=10136`、成本 48.09/150（成本一直冻结累积） |
| 运行时错误日志 | 03:30–03:36:55 **完全空白**（见缺陷 D4） |

---

## 2. 根因链（每一步都有代码锚点与证据）

| # | 时刻 | 事件 | 证据 |
|---|---|---|---|
| 1 | 03:33:05.0x | lead 的 turn `c970327c` 执行 `await checkpoint.save()`（`runner.py:309`）。平台侧已提交该次写入（revision → 649），但客户端判定失败：**被 4xx 拒绝，或响应丢失且对账未通过** | `turn.checkpoint_revision = 649`（存的是本次值）+ 失败请求在访问日志中无对应行 |
| 2 | 03:33:05.200 | `session.py:20-38` `CtfCheckpoint.save()` 捕获异常 → `self.failed = True` → 调平台 `checkpoint_failed` → **200 OK（全运行期唯一一次成功的 `checkpoint_failed`）** → 平台把 `c970327c` 写入 `unresolved_checkpoints` | `checkpoint_failed` 200 = 1，就在此刻；写入逻辑 `ctf.py:1393-1407` |
| 3 | 03:33:05–09 | 异常在 `runner.py:312-330` 被当作可重试 → **静默重试**（attempt 0，预算未占用，不打日志） | 运行时该窗口无任何日志 |
| 4 | 03:33:0x | 第 2 次尝试在 `for attempt in range(2)` **循环内**重建 checkpoint（`runner.py:228-231`）→ 得到**全新对象**：`failed` 复位为 `False`（`session.py:16` 是类属性） | `runner.py:229` 位于循环体内 |
| 5 | 03:33:07.592 | 重放期间一次调用被 409 拒绝 | `record_observation` 409 全运行期**仅 2 次**：03:11:28.654、03:33:07.592（均来自 192.168.0.5 = runtime 容器） |
| 6 | 03:33:09.204 | 该 409 以 `RemoteError` 浮出 → `runner.py:363-369`：`status in {403,409}` 且此时 `checkpoint.failed == False`（第 4 步刚复位）→ `reason="interrupted"` → `settle(..., answer=第 1 次尝试留下的文本)`。**该分支没有任何日志** | turn 行：`end_reason=interrupted`、`final_answer` 非空且内容正常 |
| 7 | 03:33:09 后 | **矛盾状态成型**：平台同时持有该 turn 的 checkpoint（rev 649）**和** `unresolved_checkpoints=[c970327c]`，而该 turn 已 `finished` | `turn.checkpoint_revision=649` + 事故前快照中的 `unresolved_checkpoints` |
| 8 | 03:36:55.039 | lead 请求结论（`partial`）→ coordinator 进入 `phase=closing` | 全运行期 `ctf.conclusion.requested` 仅 2 条：01:50:33（run 1 的 system）、03:36:55.039（lead） |
| 9 | 03:36:56.158 起 | 收尾：`coordinator.py:354-363` 检测到未决项 → 调 `retry_checkpoints` → `runner.py:412` **先 `await checkpoint.save()`、成功后才 `del`**（418）。save 被 `_fence` 拒绝：`ctf.py:205` 要求 `turn["status"] == "running"`，实际是 `finished` → **409** | 409 从 03:36:56.158 精确开始，且 `checkpoint`/`checkpoint_failed` 计数 1:1 |
| 10 | 之后 | `unresolved_checkpoints` 的**唯一**清空路径是"对该 turn 的一次成功 `checkpoint` 保存"（`ctf.py:1475-1490`，只移除本次 `turn_id`）；该路径永不可能成功 → `finalize_close` 的门禁（`ctf.py:2022`）永不过 → `coordinator.py:363` 无条件 `return True` → **死循环** | — |

> **推论**：这不是"某个组件坏了"，而是**状态机缺少一条边**——"turn 已结束"这个事实没有传播到"未决项"上。

---

## 3. 缺陷清单（要修的 5 处）

### D1 【平台·根因】未决项没有"turn 结束"出口
- **位置**：`services/blackboard/src/bbx_blackboard/ctf.py`
  - 写入：`checkpoint_failed(...)` = 1393-1407（追加 `turn_id`）
  - 清空（唯一）：`checkpoint(...)` = 1475-1490（仅当该 turn 保存成功）
  - 门禁：`finalize_close` = 2022（`require(not control.get("unresolved_checkpoints"), "Final checkpoint remains unresolved")`）
  - 结算：`finish_turn(...)` = 1907-1919 → `_settle(...)` = 1714+（**注意 1915-1916：turn 非 running 时直接 `return turn`，不做任何清理**）
- **问题**：一个 turn 结束（结算）后，它的 `unresolved_checkpoints` 条目在语义上已经**失去意义**（该 turn 的最终快照已成定局，重新写它永远不可能），但没有任何代码把它删掉；而关闭屏障又把它当硬门禁。
- **最小修法**：在 `_settle()`（或 `finish_turn` 的结算主路径）里，把已结算的 `turn_id` 从 `ctf_control.unresolved_checkpoints` 中移除。**这一条即可根治本事故。**
- **验收**：`checkpoint_failed` 后结算该 turn → `unresolved_checkpoints` 不含它 → `finalize_close` 可通过。

### D2 【运行时·放大器】收尾重试无分类、无上限、无退避
- **位置**：`services/agent-runtime/src/bbx_runtime/ctf/runner.py:408-418`（`retry_checkpoints`）+ `coordinator.py:345-363`（closing 分支）
- **问题**：
  - `retry_checkpoints` 把**任何**异常都当"暂时失败"，且 `del`（418）在 `save()` 之后，异常时条目永不被移除 → 每个 tick 重试一次同一个注定失败的 turn；
  - `coordinator.py:354-363` 只要还有未决项/pending 就 `return True`，**没有次数上限、没有退避、没有升级出口**；
  - 对"turn 已结束"这种**终态**错误（409）与"瞬时抖动"没有区分。
- **最小修法**：
  1. `retry_checkpoints` 捕获 save 异常后判定是否终态（turn 已结束 / `_fence` 拒因）；终态则 `del self.pending_checkpoints[key]` 并记一条 WARN，停止重试；
  2. closing 分支加**退避 + 次数/时长上限**；超限时用明确的 `end_reason`（如 `system_failure` 或专用原因）走正常终态收尾，**不再无限 `return True`**；
  3. 参考已有范式：`coordinator.py:523-528`（归档清理路径）已经有"丢弃 pending_checkpoints"的处理，收尾路径缺少同类出口。

### D3 【运行时】把"被拒绝的写"当成"丢失的写"
- **位置**：`services/agent-runtime/src/bbx_runtime/ctf/session.py:20-38`（`CtfCheckpoint.save`）+ 基类 `services/agent-runtime/src/bbx_runtime/session.py:150-180`
- **问题**：基类已经把两种情况分开了——`session.py:167-169`：**`RemoteError` 且 `status < 500` 立即 `raise`（不进入响应丢失对账）**；只有传输错误/5xx 才走 150-180 的"对账"逻辑。但 `CtfCheckpoint.save` 的 `except Exception` **不加区分**：一律 `failed = True` + 调 `checkpoint_failed`。于是一次**被平台明确拒绝**的写（fence/claim 失效）也被记成"持久状态未决"，进而参与关闭屏障 —— 而这类写**永远不可能再成功**。
- **最小修法**：只有"可能已落库"的错误（传输错误 / 5xx / 对账失败）才标 `failed` 并调 `checkpoint_failed`；4xx 拒绝语义应为"本次写作废"，不进入关闭屏障（或在 D1 落地后至少不阻塞收尾）。
- **附带**：`session.py:31-37` 里 `checkpoint_failed` 自身的异常会**顶掉原始异常**（原始 409 被 `_fence` 的 409 覆盖），丢掉第一现场；建议 `try/except` 包住并记录原始错误。

### D4 【运行时】结算分支静默，事故不可观测
- **位置**：`runner.py:363-369`
- **问题**：`reason="interrupted"` 的结算**不打任何日志**。本次事故中 03:30–03:36:55 运行时日志完全空白，事后只能靠平台侧 turn 行与访问日志反推。
- **最小修法**：该分支加一条 WARN，记录 `turn_id` / `generation` / `error.status` / `answer` 长度 / `checkpoint.failed`。

### D5 【附带·run 1 的镜像缺陷】应重试却直接判死
- **位置**：`runner.py:314-330`（传输错误分支：第 2 次传输错误或预算已 `sent` → 直接 `raise`）
- **问题**：run 1 中**全运行期仅 1 次** `httpx.ReadError`（2026-10-13T01:50:33.900Z）就被判为 `end_reason=system_failure`（01:50:39.782 结束）。
- **与 D2 的关系**：run 1 = "该重试却放弃"，run 2 = "该放弃却永远重试"。**同一个洞：重试/失败没有分类（可重试 vs 终态）。** 建议一并修：对带 `expected_revision` 的幂等写（checkpoint）放行 1–2 次退避重试，传输错误不直接 `system_failure`。

---

## 4. 给开发 Agent 的执行清单（按依赖排序）

1. **平台侧（根治，优先）**：`_settle` / `finish_turn` 结算时移除该 turn 的未决项（D1）。
2. **运行时侧（兜底）**：`retry_checkpoints` 对终态错误丢弃条目并 WARN（D2.1）。
3. **收尾有界化**：closing 分支加退避 + 上限 + 升级出口（D2.2）。
4. **失败语义收敛**：`CtfCheckpoint.save` 区分 4xx 拒绝与真实丢失（D3）。
5. **可观测性**：补 WARN 日志（D4）。
6. **run 1 镜像问题**：幂等写放行退避重试、传输错误不判 `system_failure`（D5）。

---

## 5. 回归测试要求

**必须新增**（本次事故的直接回归）：

- `finished turn + unresolved_checkpoints 含该 turn + phase=closing` → 收尾**必须能完成**（`finalize_close` 被调用）且**不发生无限重试**。
- `checkpoint_failed(tid, agent, turn, gen)` → `finish_turn(...)` 之后断言 `unresolved_checkpoints` 不含该 turn。

**必须修改（现有测试把缺陷当成了契约，别只“改绿”）**：

| 文件 | 位置 | 现状 | 期望 |
|---|---|---|---|
| `services/blackboard/tests/test_ctf_integration.py` | 502-508 | `checkpoint_failed` 后断言 `finalize_close` 抛错、`unresolved_checkpoints == [turn["id"]]` | 保留"未决时不可关闭"的语义，**补**"该 turn 结算后未决项被清除、关闭可通过" |
| `services/agent-runtime/tests/test_ctf_runtime.py` | 971-995（`test_unconfirmed_checkpoint_blocks_goal_claimed_finalization`） | `phase=closing` + `unresolved=["turn"]` → 断言 `tick() is True` 且不 `finalize_close` | 保留"真未决不 finalize"，**补**"未决项已失效或重试超限 → 有界退出（不再恒 True）" |
| `services/blackboard/tests/test_ctf_reliability_integration.py` | 180-200 | 用 `unresolved_checkpoints` 作为 blocker 场景 | 保持语义；如结算清理会改变该场景，需显式改为"未结算的 turn" |

**验收标准**：

1. 第 5 节新增场景：有限 tick 内到达终态，`finalize_close` 被执行；
2. 平台侧：turn 结算后未决项被清除（新增断言）；
3. 运行时侧：对已结束 turn 的 checkpoint 重试被丢弃且只记一条 WARN（有测试覆盖）；
4. CTF 相关测试全绿（含上述修改项）。

---

## 6. 复现方法（不依赖线上数据）

- **单元级**：直接构造 `ctf_control = {"phase": "closing", "unresolved_checkpoints": ["<已结算 turn id>"]}` + `pending_checkpoints` 含该 turn，断言 coordinator 收尾有界收敛、最终调用 `finalize_close`。
- **集成级**：`checkpoint_failed(...)` → `finish_turn(...)`（或 `_settle`）→ `finalize_close(tid, True)` 应成功。
- **在旧代码上应能复现**：旧代码会在该场景下持续 `return True` 且永不 `finalize_close`（`test_ctf_runtime.py:971` 就是这个形态的最小化）。

---

## 7. 边界（本次不要动）

- **不改 CTF 事件契约枚举**（另有独立缺陷，建议单独修）：平台自身写入的事件类型 `ctf.coordination.reminded`（`ctf.py` 的 idle watchdog）不在契约枚举 `packages/contracts/src/bbx_contracts/ctf.py:406-411` 中，导致 `GET /api/tasks/{id}/events` 自首次提醒起**永久 500**。
- **不改 exec 镜像 / profile**（与本缺陷无关，v11 已另案发布）。
- 不要把本报告任何"60 分钟空转"当作性能问题——它是**状态机缺陷**。

---

## 8. 残留不确定性（如实标注）

1. 03:33:05 那次失败究竟是"平台 200 但客户端读响应失败（同一次调用）"还是"两次调用中第二次被拒/失败"，仅凭访问日志无法逐字节区分；**两种情形对结论无影响**——平台最终同时持有 `rev 649` 与 `unresolved` 标记（有 `turn.checkpoint_revision=649` 与事故前快照为证）。
2. 03:33:07.592 的 409 是否属于 lead 本次重放（而非其他成员的收尾竞态）无法 100% 确定；即便属后者，第 4 步"重试重建对象使 `failed` 复位"与第 6 步的结算条件不变。
3. 另有一段更早的 **≈7 分钟 turn 层卡顿（03:16:30–03:23:10）**，结束于 03:23:11.028 的唯一一条 `recover` 广播（该广播中断 5 个在跑回合、并按成员 generation+1）；触发者未完全证实（与 03:22:56 的黑板热补丁/重启窗口重合）。**它与本次死锁无因果**，但它把队伍送进了 `c970327c`（该 turn 于 03:23:11.202 开始）。

---

## 附 A：事故时间线（可直接用作测试 fixture 的观测原始值）

```
03:23:11.028603Z  ctf.recover 广播（唯一一条，interrupted 5 turns，member generation+1）
03:23:11.202      turn c970327c 开始（lead, generation 21）
03:33:05.070      checkpoint → 200（平台已提交，revision 649）
03:33:05.200      checkpoint_failed → 200   ← 全运行期唯一一次成功；unresolved += c970327c
03:33:07.592      record_observation → 409  ← 全运行期仅 2 次之一
03:33:09.204/438  turn c970327c finished, end_reason=interrupted（无日志）
03:36:55.039      lead request_finish → end_reason=partial，phase=closing
03:36:56.158      409 风暴开始：checkpoint 409 + checkpoint_failed 409（1:1）
04:37:27          （外部）强制 status=failed + phase=closed，空转停止
```

## 附 B：事故后已执行的处置（已完成，勿重复操作）

1. 04:37:27 强制 `status=failed` + `ctf_control.phase=closed`（`active_seconds=10136`）→ coordinator 退出；
2. 04:39 清空遗留的 `unresolved_checkpoints`；
3. 04:45:41 结清孤儿 turn（`end_reason=interrupted`、成员 `current_turn_id→NULL`、`run_state=idle`）——**这是让运行时彻底恢复的关键一步**；
4. 04:46:30 重启 agent-runtime（事后证明不必要；清理事件在 04:45:52 已触发）。

当前运行时的 409 风暴为 **0**（此前约 600/分钟）。以上四次写入均已日志化，事故前状态快照见 `/tmp/run2_prestate.json`。
