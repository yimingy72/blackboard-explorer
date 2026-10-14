# CTF 回合结算期传输抖动导致整任务判死事故报告（run 5：单次 `RemoteProtocolError` 清零 9 个在跑回合）

> **2026-10-14 开发复核与修复已完成**：见 [CTF-turn-transport-fix-report.md](CTF-turn-transport-fix-report.md) 及 [控制链扩查](CTF-control-audit-report.md)。以下保留原事故分析。单个结算连接抖动被升级为整队失败的根因成立；空最终回答不代表已持久 Session 或全部进度被删除。修复已本地部署，旧任务/会话/配置和未知费用保持，没有真实靶场重跑。

- **用途**：交开发 Agent 审核并继续优化。本报告只描述**本次事故的机制与缺口**，不主张推翻上下文恢复的设计
- **严重度**：高 —— 一次 HTTP 连接抖动即让整任务 `system_failure`；同秒中断全部在跑回合，未提交进度丢失；同类事件在 runtime 生命周期内已出现 7 次
- **分析对象**：任务 `7408d5bc-470e-40b3-bd3a-4e19ecf3010e`（Cybench 全量 31 题，第 5 轮，2026-10-13 10:11:33 → 10:42:01 UTC）
- **对照组**：`b92a831f`（第 4 轮）、`cda295d2`（第 3 轮）、`d11f03ea`（第 1 轮）
- **代码基线**：`HEAD = c1bd222`（工作树干净）。**行号取自该提交**，请一律**按符号定位**（`CtfTurnRunner.run`、`CtfTurnRunner.settle`、`CtfClient.runtime_state`、`BlackboardClient._response`、`CTFCoordinator._tick`、`failure_diagnostic`）
- **证据来源**：agent-runtime 容器日志（`docker logs bbx-ctf-team-mode-agent-runtime-1`）、平台 DB（只读）、源码（容器内与工作树哈希一致）
- **约束**：本报告不含任何真实 flag 值；平台数据全程只读；本次**未修改任何代码、配置或 profile**；未判分

---

## 0. 一句话结论

一次 `httpx.RemoteProtocolError`（黑板 HTTP 连接被对端提前关闭）在**回合结算**阶段抛出（`runner.py:557` 的 `runtime_state` GET），而这条调用**落在回合的异常分类之外**（`runner.py:450` 的 `await self.settle(...)` 在 `try/except/finally` 之后）。异常冒泡到协调器的"活动回合"检查（`coordinator.py:328-343`），被**无条件**当成任务级失败 → `request_close("system_failure")` → 10:42:01 任务判失败，**同一秒内 9 个在跑回合全部被中断**（`final_answer` 全为空串）。

**与刚发布的上下文阈值修复无关**：本轮 `context_blocked` 回合 = 0、`model_error` 事件 = 0、压缩事件 = 0 —— **修复根本没有被触发过一次**，任务在跑到阈值之前就被这次传输抖动杀掉了。

---

## 1. 观测事实与量化

### 1.1 终态与消耗

| 项 | 值 |
|---|---|
| 开始 / 结束 | 2026-10-13T10:11:33.027Z → 10:42:01.306Z（`active_seconds` = **1828 s ≈ 30.5 min**） |
| `status` / `end_reason` | `failed` / **`system_failure`**（`requested_by = system`） |
| 成本 | **18.32781880 CNY**（上限 100，用了 18.3%） |
| 另挂 `budget_unknown_reservations` | 4 笔 × 4.359296 = **17.44 CNY**（`member-5` gen2、`member-2` gen1、`member-4` gen2、`member-7` gen1，`state="unknown"`） |
| token | `cache_hit 67,906,270` / `cache_miss 1,996,148` / `output 1,452,409`（含 reasoning 1,220,032） |
| 时长预算 | 用了 30.5 / 180 分钟（**83% 未用**） |

### 1.2 回合与成员

| 项 | 值 |
|---|---|
| 回合总数 | 27（`ctf.turn.started` 27 = 18 `completed` + **9 `interrupted`**） |
| 9 个中断回合 | **全部 `finished_at = 10:42:01`**，`final_answer` 长度**全部为 0** |
| 中断回合名单 | `member-1` gen2、`member-2` gen1、`member-3` gen1、`member-4` gen2、`member-5` gen2、`member-6` gen3、`member-7` gen1、`member-8` gen1、`lead` gen14 |
| 触发失败的可见线索 | `10:41:59.933` 日志 `CTF turn interrupted turn_id=ff082491… generation=14 status=409`（lead 回合，走的是 `RemoteError 409` 分支） |
| 成员 | 9 个仍为 `active`（lead + 8 teammate），无 `removed`/`stopped` |

### 1.3 平台上保留的成果（未丢失）

`ctf.challenge.created` 31（31 题全部登记）、`ctf.record.appended` 145、`ctf.challenge.updated` 80、`ctf.message.posted` 51。
⇒ **丢失的是 9 个在跑回合里未提交的模型进度**，板上已登记记录完好。

### 1.4 关键否证：本次不是上下文/阈值问题

| 观测 | 值 |
|---|---|
| `end_reason='context_blocked'` 的回合 | **0** |
| `model_error` 事件 | **0** |
| 压缩/恢复类事件（`ctf_context_compaction`、`*_recovery_exhausted`） | **0** |
| 生效参数 | `ctf-task-settings` **v8**，`context_threshold = 1000000`（创建时继承模型窗口）→ 有效上限 **485427**（`context.py:134-139`：`min(1_000_000, 1_000_000−393216) × 0.8`） |

### 1.5 触发点的完整栈（逐字，来自 `docker logs`）

```
CTF coordinator failure correlation_id=f50f4710-ad5d-4f87-9900-40037f9b7639 phase=turn error_type=RemoteProtocolError
stack=/app/services/agent-runtime/src/bbx_runtime/ctf/runner.py:450 in run
    ; /app/services/agent-runtime/src/bbx_runtime/ctf/runner.py:557 in settle
    ; /app/services/agent-runtime/src/bbx_runtime/ctf/client.py:18 in runtime_state
    ; /app/services/agent-runtime/src/bbx_runtime/clients/blackboard.py:133 in _json
    ; /app/services/agent-runtime/src/bbx_runtime/clients/blackboard.py:59 in _response
    ; /app/.venv/lib/python3.12/site-packages/httpx/_client.py:1540 in request
    （中间帧省略：httpx/_client.py send/_send_handling_auth/_send_handling_redirects/_send_single_request、
      httpx/_transports/default.py:393 handle_async_request、contextlib __exit__）
    ; /app/.venv/lib/python3.12/site-packages/httpx/_transports/default.py:118 in map_httpcore_exceptions
```

⇒ 抛出者是 **`httpx` 0.28.1**（平台客户端），**不是** `httpx2`（模型客户端）、**不是**模型服务。

### 1.6 `RemoteProtocolError` 在 runtime 容器生命周期内的 7 次记录

| 时刻（UTC） | 出现位置 | 后果 |
|---|---|---|
| 2026-10-12T15:50:48 | `CTF control transport retry … attempt=1` | 被退避重试吸收 |
| 2026-10-12T23:31:27 | `Review polling failed` | 记录后继续 |
| 2026-10-12T23:31:27 | `CTF control transport retry … attempt=1` | 吸收 |
| 2026-10-12T23:36:53 | `CTF control transport retry … attempt=1` | 吸收 |
| 2026-10-12T23:37:54 | `CTF review deferred for task …` | 延后 |
| **2026-10-13T10:30:26** | `Task supervision tick failed`（**本轮内**） | 记录后继续 |
| **2026-10-13T10:41:59** | `CTF coordinator failure … phase=turn`（**本轮内**） | **整任务判死** |

⇒ 同一异常类型在**监督/控制/复核路径上都被容忍**，只有"回合结算"这条路径是致命的。本轮 30 分钟内就出现 2 次，**按此频率直接重跑大概率会再次死在同一个地方**。

### 1.7 附带观测（非本次致命因，但值得一并处理）

- **成员登记被拒 5 次**：`10:39:37–10:40:01`（约 24 秒内）连续 5 条 `Function failed. Error: Only Lead, owner or collaborators may append records`（`append_record` 工具）。这与上一轮才发布的新指导语「一旦形成可复核的阶段成果或完成候选，先登记必要产物并提交对应记录或 `record_candidate`」存在**入口歧义**：teammate 显然以为可以 `append_record`。建议核对 teammate 的登记工具口径与提示词措辞。
- **老任务残留清理**：`b92a831f`（第 4 轮）在 `2026-10-13T04:45:41–04:45:46` 仍反复输出 `Cleanup failed for task b92a831f…: RemoteError`，此后未再出现。请确认是否已收敛（本次未追查）。

---

## 2. 根因链（每一步都有代码锚点与证据）

| # | 环节 | 代码锚点 | 说明 |
|---|---|---|---|
| 1 | 回合主体结束，进入结算 | `runner.py:447-450` | `finally` 关闭模型客户端后，**裸调用** `await self.settle(...)`。该行**不在**任何 `try` 内 |
| 2 | 结算内部发起黑板读取 | `runner.py:550-557` | `settle()` 先 `(await self.service.runtime_state(task_id))["members"]` 取最新成员状态 —— 这是**幂等 GET** |
| 3 | 客户端单发无重试 | `ctf/client.py:17-18` → `clients/blackboard.py:58-65,132-133` | `BlackboardClient._response` 是一次 `httpx.AsyncClient(timeout=30)` 请求；`_check` 只处理 HTTP 状态码，**传输层异常原样抛出** |
| 4 | 对端提前关闭连接 | `httpx/_transports/default.py:118` | `RemoteProtocolError`：服务端在响应完成前断开 / 响应非法。黑板访问日志中相邻请求（同 IP 同时段）均为 200，**服务端未留下可归因证据** |
| 5 | 异常穿过 `run()` 的全部处理器 | `runner.py:393-446` | `except RemoteError`（403/409→interrupted）、`except Exception`（仅**模型**错误可转 `provider_transient`）都覆盖不到第 450 行；异常直接冒出 |
| 6 | 协调器把"回合 future 抛异常"当任务级失败 | `coordinator.py:328-343` | `error = future.exception()` 非空 → `request_close(task_id, "system_failure", failure=failure_diagnostic("turn", error))` → `return True`。**没有任何分类** |
| 7 | 收尾取消整队 | `coordinator.py` 收尾路径 | 9 个在跑回合同秒以 `interrupted` 收尾，`final_answer=""` |
| 8 | 反差：同类错误在协调器自身路径上是被重试的 | `coordinator.py:527-539` | `except httpx.TransportError` → 记 WARNING、退避重试 **3 次**，超限才 `system_failure`。**同样的错误类型，"在 tick 里"能重试，"在回合里"直接杀任务** |

**推论**：这不是"某个组件坏了"，而是**失败分类缺失**——"回合内的一次瞬时传输错误"与"确定性逻辑错误"被同等对待，且结算这一步没有恢复边界。

> 注：本仓库 2026-09-29 的 [`Control-connection-resilience-report.md`](Control-connection-resilience-report.md) 已为 **scheduler tick / sweeper** 修过同一族问题（"旧代码对每任务 tick/清扫器的内部 HTTP 传输异常没有恢复边界，会进入 finally 并取消全部 Agent"）。**CTF 协调器的回合路径没有被那次修复覆盖**——本报告是其遗漏面。

---

## 3. 缺陷清单（建议修的 4 处 + 2 处附带）

### D1 【运行时·根因】结算调用没有异常边界，且黑板客户端单发无重试

- **位置**：`services/agent-runtime/src/bbx_runtime/ctf/runner.py:450`（`await self.settle(...)`）、`runner.py:550-591`（`settle` 内 `runtime_state` / `finish_turn`）、`services/agent-runtime/src/bbx_runtime/clients/blackboard.py:58-65,132-133`
- **问题**：`settle()` 是回合的收尾写回，其中第 557 行的成员状态读取是**幂等 GET**，却没有任何重试；一旦抖动，回合直接把异常抛给协调器。
- **最小修法**（任选其一，建议 1+2）：
  1. 对 `settle()` 内的**读操作**做有界重试（沿用 `ctf/client.py:20-32` 已对 `claim_turn` 使用的"传输错误→读回对账"范式）；
  2. 把 `runner.py:450` 的 `await self.settle(...)` 纳入带分类的处理块：瞬时传输错误 → 有界重试后按 `interrupted` 结算（**不重放不确定写**，沿用 `Control-connection-resilience-report.md` 的原则）。
- **不要做**：不要在重试里原样重发可能已提交的写（`finish_turn`/`checkpoint`）；已提交但响应丢失的情形必须走读回核对，而不是重复提交。

### D2 【协调器】活动回合抛异常一律升级为任务级 `system_failure`

- **位置**：`services/agent-runtime/src/bbx_runtime/ctf/coordinator.py:328-343`
- **问题**：只要回合 future 抛异常就关任务，不区分"瞬时可恢复"与"确定性错误"。这是把一次连接抖动放大成整队清零的**放大器**。
- **最小修法**：在此处加分类——
  - `httpx.TransportError`（含 `RemoteProtocolError`）等瞬时类：记 WARNING + 计数，交由该回合按有界重试/`interrupted` 处理，**不关任务**；
  - 确定性错误（如 4xx 逻辑错误、不可恢复的持久化失败）：保持现有 `system_failure` 语义。
  - 与 `coordinator.py:527-539` 的 3 次退避口径保持一致。
- **验收**：注入一次结算期传输错误 → 任务**不**进入 `system_failure`，该回合以 `interrupted`（或重试成功）收尾，其余回合不中断。

### D3 【运行时】"取消全队在跑回合"不是显式动作

- **位置**：收尾路径（`request_close` → 取消在跑回合）
- **问题**：9 个回合同秒中断，但没有任何事件说明"这是收尾取消"还是"各自失败"。
- **最小修法**：把"因任务级失败取消在跑回合"变成一个显式、带计数的动作，并落一条事件（含被取消回合 id 列表）。修 D1/D2 后此类收尾应显著减少。

### D4 【可观测性】失败现场只存在于容器日志

- **位置**：`coordinator.py:35-65`（`failure_diagnostic`）
- **问题**：`failure_diagnostic` 把完整栈写进 `logger.error`，但落库的结论只有 `error_type/phase/summary/correlation_id` 四个字段，**没有 agent_id / turn_id**，因此无法从平台数据自证"谁在哪一步失败"；本次是靠 `docker logs` 才定位到 `runner.py:557`。此外 9 个中断回合的 `final_answer` 是空串，使"丢了什么"不可见。
- **最小修法**：那行 error 日志加 `task_id/member_id/turn_id/generation`；中断收尾时把 `lost_turn_ids` 写进事件或结论（不含任何模型内容）。

### D5 【附带·类型体系分裂】`httpx2` 与 `httpx` 两套类层次并存

- **位置**：`services/agent-runtime/src/bbx_runtime/model_errors.py:11,57,65-76,240-254`
- **事实**：模型客户端用 **`httpx2` 2.13.0**（`_TRANSPORT_TYPES` 全部取自它），平台客户端（黑板/envd/执行环境）用 **`httpx` 0.28.1**；两者类层次**互不相干**：
  ```
  issubclass(httpx.RemoteProtocolError, httpx2.RemoteProtocolError) → False
  issubclass(httpx.RemoteProtocolError, httpx2.TransportError)      → False
  ```
- **影响**：平台侧的传输错误**永远不可能**被 `is_transient_model_error` 识别为瞬时错误。因此修 D1/D2 时**不能指望**往 `_TRANSPORT_TYPES` 里加类型了事——平台客户端错误需要**独立**的分类与重试边界（本报告 §3 D1/D2 正是如此）。

### D6 【附带·与同批发布的指导语冲突】teammate 的登记入口不明

- **证据**：本轮 24 秒内 5 次 `Only Lead, owner or collaborators may append records`（§1.7）
- **问题**：新指导语让 teammate「登记必要产物并提交对应记录或 `record_candidate`」，成员却反复尝试 `append_record` 并被拒。要么提示词把 `record_candidate` 说清楚，要么放宽/明确权限，否则会在关键时刻浪费模型往返。

---

## 4. 给开发 Agent 的执行清单（按依赖排序）

1. **D2（先做）**：`coordinator.py:328-343` 对回合异常分类，瞬时传输错误不升级为 `system_failure`。
2. **D1**：`runner.py:450` 的 `settle()` 加恢复边界；`settle()` 内的黑板**读**做有界重试（幂等），**写**走读回核对不重放。
3. **D3**：把收尾取消在跑回合显式化 + 落事件。
4. **D4**：失败日志补 `member_id/turn_id/generation`；中断收尾落 `lost_turn_ids`。
5. **D6**：核对 teammate 登记工具与提示词口径（1 行提示或 1 处权限判断）。
6. **回到上下文验证**：本轮**不构成**对上下文修复的任何正/反证据（`context_blocked` = 0）；修完本缺陷后重跑再按原口径观测。

---

## 5. 回归测试要求

**必须新增**

- `settle()` 期间黑板 `runtime_state` 抛一次 `httpx.RemoteProtocolError`（其余请求正常）→ 断言：任务**不**进入 `system_failure`；该回合以有界重试成功或 `interrupted` 收尾；**其他在跑回合不被打断**。
- 同类错误连续超过重试上限 → 断言：以**有界**方式退出（不无限重试），且错误分类写入事件。
- 结算期传输错误发生在 `finish_turn`（写）而非读 → 断言：**不重复提交**同一 `turn_id`（读回核对路径生效）。

**必须保持**

- 确定性错误（4xx 逻辑错误、持久化失败）仍升级为 `system_failure` 的现有语义；
- `coordinator.py:527-539` 的 tick 退避行为不变；
- `Control-connection-resilience-report.md` §5 的既有回归（控制链断连后重读恢复、已提交写不重放）继续通过。

---

## 6. 复现方法（不依赖线上数据）

1. **单元级**：用 fake `BlackboardClient`，令其 `runtime_state()` 第一次抛 `httpx.RemoteProtocolError`、之后正常；构造一个 `CtfTurnRunner` 走 `run()` → `settle()`；旧代码应在协调器层得到 `system_failure` + 全队取消，新代码应在本回合内恢复。
2. **集成级**：在 CTF 集成用例中让黑板端点在结算窗口返回一次"连接被关闭"（而非 5xx），断言任务终态与回合终态。
3. **注入点提示**：可在 `clients/blackboard.py:58-65` 的请求入口注入一次性异常，避免改动被测逻辑。

---

## 7. 边界（本次不要动）

- **不要改 `context_threshold` / profile**：本轮参数是**正确**的（v8、`1000000` → 上限 485427），与本缺陷无关。
- **不要为了"更耐抖"而放宽写语义**：不确定写（`finish_turn` / `checkpoint`）仍不得原样重放；这会把一次抖动换成数据不一致。
- **不要把"9 回合同秒中断"当成性能问题**：它是分类缺失的后果，不是超时或吞吐问题。
- 本报告不涉及 CTF 事件契约枚举、exec 镜像、判分口径。

---

## 8. 残留不确定性（如实标注）

1. **无法确认连接为何被关闭**：黑板访问日志中同 IP 同时段的相邻请求均为 200，服务端未留下 keep-alive 竞态 / 代理 / 连接池的证据。与 `Control-connection-resilience-report.md` 的结论一致，**不建议**据此断言某一具体接口成因。
2. **无法确认是哪个成员的结算触发**：9 个回合的中断时间同为 `10:42:01`，且 `failure_diagnostic` 未记录 `member_id/turn_id`。另有一条 `10:41:59.933` 的 `interrupted status=409` 属于 lead gen14，与 `10:41:59.570` 的失败**相距 0.36 s**，二者是否为同一回合的不同阶段**无法从现有证据判定**（D4 建议补齐该字段后即可确定）。
3. **`budget_unknown_reservations` 的 17.44 CNY 是否会回冲**未验证；本轮账面成本 18.33 CNY + 预留 17.44 CNY 的关系需财务口径确认。
4. **本轮 `context_blocked` = 0 只能说明"没跑到阈值"**，不能作为"上下文修复有效"的证据（运行仅 30.5/180 分钟）。
5. **D6 的 5 次 `append_record` 被拒**：仅从日志可见回执，未复核该成员当时的完整提示词上下文与工具清单，故只标为"入口歧义"，未定性为缺陷。

---

## 附 A：事故时间线（可直接用作测试 fixture 的观测原始值，UTC）

```
10:11:33.027  任务开始（ctf-task-settings v8，threshold=1000000，limit=485427，budget 100/180）
10:12:11.5x   9 名成员就绪（lead + member-1…8），lead 开始登记题目
10:12:30      首批 agent.trace.recorded / tool_call.recorded
10:30:26.326  Task supervision tick failed: RemoteProtocolError        ← 被容忍
10:39:37.906  Function failed. Error: Only Lead, owner or collaborators may append records（×5，至 10:40:01）
10:41:59.570  CTF coordinator failure correlation_id=f50f4710-ad5d-4f87-9900-40037f9b7639
              phase=turn error_type=RemoteProtocolError
              stack: runner.py:450 run → runner.py:557 settle → client.py:18 runtime_state
                     → blackboard.py:133 _json → blackboard.py:59 _response → httpx/_client.py:1540
10:41:59.636  ctf.conclusion.requested（failure，requested_by=system）
10:41:59.933  CTF turn interrupted turn_id=ff082491-8d32-4a24-acef-0d61e0017f79 generation=14 status=409
10:42:01.0x   9 个在跑回合同时 finished（end_reason=interrupted，final_answer 空）
10:42:01.306  ctf.conclusion.finalized（status=failed，end_reason=system_failure）
10:42:04      task.cleanup_ready → task.archived
```

## 附 B：与既有报告的关系

| 报告 | 与本事件的关系 |
|---|---|
| [`Control-connection-resilience-report.md`](Control-connection-resilience-report.md)（2026-09-29） | 已为 **scheduler tick / sweeper / session** 建立传输错误恢复边界；**CTF 协调器的回合路径未覆盖**，本报告是其遗漏面 |
| [`CTF-close-deadlock-report.md`](CTF-close-deadlock-report.md)（D5：应重试却直接判死） | 同一族问题的另一面：那里的传输错误被记成 `system_failure` 后进入死锁，这里是被记成 `system_failure` 后直接清零全队 |
| [`CTF-context-exhaustion-unclosed-report.md`](CTF-context-exhaustion-unclosed-report.md) U1/U2 | 同批发布的修复（profile v7/v8、阈值与指导语）。本轮已确认**参数生效**，但因本缺陷未被触发过，其效果仍待验证 |
