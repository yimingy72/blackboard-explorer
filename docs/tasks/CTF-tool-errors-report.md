# CTF 工具失败反馈与工具循环中断审查报告

日期：2026-10-14
状态：只读审查（未改代码、未提交）
样本任务：

- `718a7b76-9310-4aa9-bd89-4aa67ead8af9`（复测 2 题：pickle-revenge / eval-me；已完成：13:36:03 UTC `finished`，2.14 CNY，输出 204,330 tokens，两题 `work_status=completed`、`verification=candidate`，另含 2 道模型自建后删除的探针题）
- `e4d07053-f253-48d3-a8a9-79ecd0520fd3`（上一轮 3 题，235 次工具调用 / 6 次失败）

审查范围：CTF 模式下内建工具（board tools + ctf tools）的错误回传链路，以及 MAF 函数调用循环的错误上限行为。

---

## 1. 结论摘要

两条都必须修复，优先级都高于"性能优化"：

1. **工具失败的真实原因到不了模型**。除白名单里的少数几类，其余领域错误的语义文案（例如 `Reopen the challenge first`）不会进入工具结果，模型只收到 `Error: Function failed.`
2. **连续 3 次工具错误后，runtime 掐断该请求剩余的全部工具调用**（MAF 默认上限 3）。与第 1 条叠加后形成"错误不可读 → 原参数重试 → 连撞 3 次 → 被掐断 → 重新规划"的闭环，每次白烧一整次模型往返。
3. **后果已可量化，且责任只能定一半**：因为拿不到失败原因，Lead 在 `718a7b76…` 里自建两道临时探针题试错后才把交付提交成功（2.4 节）；而“这次调用为什么被拒”至今**无法判定**（2.5 节）。确定的是错误反馈链路，不确定的是被拒本身。

定位说明：这两条**不是"跑得慢"的主因**（主因是模型串行往返，见第 5 节），但它们会稳定制造**无进展的往返**，并且阻断模型的自我纠正。修第 1 条收益最大，同时消解第 2 条的主要触发条件。

---

## 2. 问题一：工具失败详情被丢弃

### 2.1 现象

模型调用 `record_candidate` / `append_record` / `update_challenge` 失败时，收到的工具结果是：

```
Error: Function failed.
```

服务端其实有可操作的明确文案，但一个字都没到模型。runtime 日志只有：

```
Function 'record_candidate' raised an exception; returning an error result to the model. Set include_detailed_errors=True for the full detail.
```

### 2.2 根因链（两层叠加）

第一层，我们侧白名单窄 —— `services/agent-runtime/src/bbx_runtime/ctf/tool_results.py:77-78`：

```python
        else:
            raise
```

`json_tool_result()` 只翻译少数几类（404 `Challenge not found`、409 `revision_conflict`、409 `message_confirmation_conflict`、422 `invalid_solution_delivery`，以及 `_ARGUMENT_ERRORS` 中的 422 文案），其余 `RemoteError` 一律 `raise`。

第二层，MAF 默认不外泄异常详情 —— `.venv/lib/python3.12/site-packages/agent_framework/_tools.py`：

- 默认配置 `"include_detailed_errors": False`（约 1839 行）
- `_function_execution_error_result()`（约 1855–1884 行）据此返回

  ```python
  message = "Error: Function failed."
  if config.get("include_detailed_errors", False):
      message = f"{message} Exception: {exception}"
  ```

被 `raise` 出来的 `RemoteError` 就落进这里，异常对象只写日志。

我们侧的配置点：`services/agent-runtime/src/bbx_runtime/models.py:341-344` 只设了 `max_iterations` / `max_duration_seconds`，未覆盖 `include_detailed_errors`。

### 2.3 与既有设计的关系（重要）

`docs/tasks/CTF-prompts-tools-report.md` 第 2 节明确记录过这是**有意选择**：

> 仅白名单中的参数校验 422 转成固定安全提示；权限 403、旧 generation、停止、预算、未知 422/409、5xx 和网络异常按原路径传播。没有开启全局详细错误或回显原始服务异常。

本报告**不主张**推翻该安全边界（不回显原始服务异常、不向模型复制未审查字段）。建议是在保持该边界的前提下，把"模型能够自我纠正"的**领域语义错误**纳入经过审查的结构化映射；真正的未知 409/422/5xx/网络异常继续按原路径传播。

### 2.4 证据

上一轮 `e4d07053…`：235 次工具调用中 6 次失败，其中 4 次服务端有明确文案却未到模型：

| 次数 | 调用方 | 工具 | 服务端真实文案 | 位置 |
|---|---|---|---|---|
| 3 | lead | `append_record` | `Artifacts must not belong exclusively to another challenge` | `services/blackboard/src/bbx_blackboard/ctf_board.py:597` |
| 1 | member-1 | `record_candidate` | `Reopen the challenge first`（409） | `ctf_board.py:397,414,425`；`ctf_platform.py:256` |
| 2 | member-1 | `record_candidate` | 客户端参数校验（`_FunctionArgumentValidationError`，无字段级提示） | runtime 侧 |

本轮 `718a7b76…`：开局 6 分钟内已出现 2 次同类日志（均为 lead 的 `record_candidate`）：

```
Function 'record_candidate' raised an exception; returning an error result to the model. Set include_detailed_errors=True for the full detail.
```

旁证：`docs/tasks/CTF-turn-transport-failure-report.md:93,228` 已记录过一次同类现象（`Function failed. Error: Only Lead, owner or collaborators may append records`，24 秒内 ×5）。说明这是**复发问题**，不是单次偶发。

旁证（本轮，最强，且模型自己给出了复现条件）：Lead 在 `718a7b76…` 里**自建了两道临时探针题**去逆向工具语义。时间线（UTC）：

| 时间 | 事件 |
|---|---|
| 13:22:03 | Lead `record_candidate` 提交 pickle 题候选 → 失败，只看到 `Error: Function failed.` |
| 13:23:06 | 重试 `record_candidate` → 同一句 |
| 13:23:52 | 改试 `update_challenge` → 同一句 |
| 13:24:58 | 建 `DIAG-temp-toolcheck`（探“工具能否提交”） |
| 13:25:24 | 建 `DIAG-temp2-verifycheck`（探“必验项是否阻塞完成”） |
| 13:26:35 | 试出绕行写法，取消两题并广播给 member-1 |

两道探针题（`DIAG-temp-toolcheck` = `75ac969a…`、`DIAG-temp2-verifycheck` = `66c8f4d9…`）描述即 `Temporary … will be deleted. No real target.`，`target` 为空，`owner=lead`，随后 `cancelled` + `tombstone=true`，各自留下 1–2 条 record，其中 `solution` 正文用的是同一串已解出的候选 flag（用来驱动提交路径，也是看板上“多道题看着一样”的原因）。它写给 member-1 的结论（原文摘录，flag 已脱敏）：

> `record_candidate` / `update_challenge` 的 `solution.steps` 里若带结构化 `record_ids` / `artifact_ids` 数组，会反复返回 'Function failed' 无法完成；去掉这两个数组、把记录/artifacts ID 以文字写进 summary 或 reproduction，即可正常提交……另确认：completed 提交本身可用，verification required=true 不阻塞。

代价与佐证：本任务 runtime 日志共 5 条同类失败（2× `record_candidate`、3× `update_challenge`）；本轮用户输入只有初始任务输入（13:16:46），**这两道题不是被要求的**——是模型为绕过不可读错误而花掉的真实预算与看板噪音。注意：模型给出的“带结构化 refs 就失败”是**试错推断**，不是服务端原文（见 2.5 节）。

### 2.5 被拒原因目前无法判定（契约层证据：模型传的字段合法）

要判断“工具拒得对不对”，先看模型传的参数是否合法。结论是**合法**：

- `CtfSolutionStep` 是契约里 `CtfSolution.steps` 的正式元素类型，本身就带这两个字段（`packages/contracts/src/bbx_contracts/ctf.py:448-454`）：

  ```python
  class CtfSolutionStep(ContractModel):
      kind: CtfProgressStage
      title: CtfShortTitle
      result: Literal["supported", "ruled_out", "inconclusive"]
      summary: CtfShortSummary
      record_ids: list[UUID] = Field(default_factory=list, max_length=8)
      artifact_ids: list[UUID] = Field(default_factory=list, max_length=8)
  ```

- 工具签名也声明接收整个契约对象，并在调用前自行 validate（`services/agent-runtime/src/bbx_runtime/ctf/tools.py:172-176,184-187`）：

  ```python
  solution: Annotated[
      CtfSolution | None,
      Field(description="Public solution delivery; at most 1600 text characters in total."),
  ] = None,
  ...
  {"solution": CtfSolution.model_validate(solution).model_dump(mode="json")}
  ```

所以**不能归为“模型乱传参数”**。为什么被拒，现在的候选方向都**无法证实**（真实原因已被丢弃）：

1. 服务端关联/归属检查（例如引用了不属于本题的 record/artifact；上一轮确实出现过 `Artifacts must not belong exclusively to another challenge`，`ctf_board.py:597`）；
2. 边界校验（每步 `record_ids` / `artifact_ids` 各自 ≤8 个；`title` / `summary` 长度上限——都定义在契约里）；
3. 客户端参数校验（`_FunctionArgumentValidationError`，无字段级提示；上一轮 6 次失败里占 2 次）。

可查到的相关事实：本任务 10 条 `artifact_registration` 记录的 `challenge_id` **全部为空**（产物挂在任务级、未归属到具体题目）。若 `solution.steps.artifact_ids` 引用的是这类产物，能否通过取决于服务端关联规则的具体实现，需要真实错误文案才能判断。

责任划分（本报告口径）：

| 事项 | 归属 | 状态 |
|---|---|---|
| 失败原因到不了模型 | 平台侧（`tool_results.py` 白名单 + MAF `include_detailed_errors` 默认） | **确凿缺陷，可直接修** |
| 这次调用为什么被拒 | 未知 | **无法判定**；而“无法判定”本身是上一条造成的 |

因此本报告不对“被拒是否合理”下结论。修完问题一（错误可读）是让这一层可诊断的**唯一路径**。

### 2.6 建议纳入映射的领域错误清单（供评审）

这些文案都表示"动作前提不满足"，模型据此可以自我纠正：

| 服务端文案 | 位置 | 模型应有的下一步 |
|---|---|---|
| `Reopen the challenge first` | `ctf_board.py:397,414,425`、`ctf_platform.py:256` | 先把题目重开（reopen），再改状态/指派 |
| `Only the owner or Lead can update work status` | `ctf_platform.py:251` | 换由 owner/Lead 操作 |
| `Only assigned members may declare a candidate` | `ctf_platform.py:246` | 先做 assign，再报候选 |
| `Challenge needs an owner` | `ctf_platform.py:253` | 先指派 owner |
| `Artifacts must not belong exclusively to another challenge` | `ctf_board.py:597` | 换用本题证据，或复制后再登记 |
| `Agents can only declare candidate or unknown` / `Invalid candidate work status` | `ctf_platform.py:150,152` | 修正 `status` / `work_status` 取值 |
| `Challenge is deleted` | `ctf_platform.py:126,243` | 停止对该题操作 |

建议实现方式：在 `tool_results.py` 给 `RemoteError` 加一个**受控兜底**分支，只回传 `{code, message}`（`code` 由服务端语义映射成稳定枚举，例如 `reopen_required`、`owner_required`、`artifact_conflict`），不拼 `str(exception)`；`raise` 仅保留给非 `RemoteError` 的真异常。若评审认为连服务端文案也不宜外露，可退化为"稳定 code + 固定提示"的映射表，效果接近但不复制服务端原文。

### 2.7 次级发现：枚举未在 schema/描述中列出确切取值

`packages/contracts/src/bbx_contracts/ctf.py`：

- `CtfProgressStage = Literal["approach", "attempt", "breakthrough", "result"]`（436 行），同时用于 `CtfProgress.stage`（443 行）与 `CtfSolutionStep.kind`（449 行）
- `CtfSolutionStep.result: Literal["supported", "ruled_out", "inconclusive"]`（452 行）

工具描述里只有散文（`board_tools.py:168-170`"meaningful approaches, effective or failed attempts, real breakthroughs and results"），没有列出确切字面量；`result` 这个同名字段更是完全没提。上一轮出现过模型用 `status="bogus"` 主动探测枚举、并多次手写 `solution` 失败的情况。建议在 schema 描述或工具描述里补齐取值。

### 2.8 验收标准

- 构造一次 409 `Reopen the challenge first`：模型侧工具结果须包含稳定 code 与该语义，不再是 `Error: Function failed.`
- 复现用例二（模型实测触发）：在 `solution.steps` 里带结构化 `record_ids` / `artifact_ids` 调用 `record_candidate`，应返回可定位到字段的错误，或按 schema 正常接受；不得是 `Error: Function failed.`
- 客户端参数校验失败时，结果须指明不合法字段/取值
- 未知 409/422/5xx/网络异常仍按原路径传播，不新增原文回显
- 被拒原因可诊断：服务端语义 code（或字段级提示）能进入工具结果并可被事件/日志捕获，而不是只留下 `Error: Function failed.` 与一条“Set include_detailed_errors=True”的提示

---

## 3. 问题二：连续 3 次工具错误掐断本请求剩余工具调用

### 3.1 现象

上一轮 `e4d07053…` runtime 日志：

```
Maximum consecutive function call errors reached (3). Stopping further function calls for this request.
```

### 3.2 根因

MAF 的"按请求连续错误上限"，默认 3 —— `.venv/lib/python3.12/site-packages/agent_framework/_tools.py`：

- `DEFAULT_MAX_CONSECUTIVE_ERRORS_PER_REQUEST: Final[int] = 3`（105 行）
- 配置键 `max_consecutive_errors_per_request`（1821、1836、1851、5403 行）
- 触发点（4090 行）：

  ```python
  "Maximum consecutive function call errors reached (%d). Stopping further function calls for this request."
  ```

我们侧未覆盖该键（`models.py:341-344`），因此使用默认值 3。

### 3.3 影响

该模型请求**剩余的工具调用全部作废**——已经规划好的动作拿不到执行结果，模型只能等下一个请求重新规划，且工具结果里没有"循环已停止"的明确信号，表现为"少了几个结果"。与问题一叠加：

```
错误不可读 → 原参数重试 → 连撞 3 次 → 工具循环被掐断 → 重新规划（再烧一次模型往返）
```

Lead 单次往返实测约 25 秒（346 秒 / 14 次请求），member-1 约 8 秒（80 秒 / 10 次请求）。

### 3.4 建议

1. 优先修问题一：错误可读后，模型不会在同一错误上连撞 3 次。
2. 若需保留容忍度，在 `models.py:341` 显式配置 `"max_consecutive_errors_per_request"`（例如 5–6），或按任务类型（CTF / 通用）区分。
3. 提示词层面补一条："同一工具连续失败 2 次即换策略并上报短进展"，降低撞上限概率。
4. 掐断发生时，给模型一个显式信号（例如在后续请求注入一条说明），避免工具结果静默缺失。

### 3.5 验收标准

- 连续 3 次同类工具错误后，该请求仍可继续后续工具调用（或断言配置值已按预期生效、日志不再出现掐断行）
- 掐断发生时模型侧可见明确说明

---

## 4. 与既有修复的关系（避免重复劳动）

- `docs/tasks/CTF-prompts-tools-report.md`：已完成 `revision_conflict` 的结构化回传，并明确"未知 409/422 按原路径传播"是刻意选择。本报告是其**扩展**：主张在同样的安全前提下，把 2.6 节列出的领域错误纳入映射（当时这些调用尚未成为高频摩擦点）。
- `docs/tasks/CTF-turn-transport-failure-report.md:93,228`：已记录一次 `Function failed.` ×5 的同类现象。本报告补充了根因（MAF 的 3 次上限）与可复现的计数口径。
- `docs/tasks/CTF-team-communication-audit-report.md`：覆盖的是协作链路的性能与入口问题，**未覆盖**本报告第 3 节的工具循环中断。

---

## 5. 性能背景：这两条不是"慢"的主因（供排优先级）

本轮同时做了阻塞排查，结论是**没有平台阻塞**：

| 环节 | 实测 |
|---|---|
| 建任务 → 派发（`task.created` → `ctf.started`） | 2.9 秒 |
| 回合启动（`ctf.turn.started`） | 与派发同一秒 |
| 工具调用服务端耗时（事件相邻差） | 0.0–0.1 秒 |
| exec 沙箱 provisioning | ≈20 秒（一次性） |
| postgres / blackboard / exec 沙箱 CPU | 0.17% / 1.06% / 0.21% |
| 宿主 | 20 核，load 4.3 |

墙钟时间主要花在**模型串行往返**：Lead 单回合 346 秒 / 14 次请求（约 25 秒一次），member-1 80 秒 / 10 次请求（约 8 秒一次）；相邻工具调用间隔实测 2s / 2s / 97s / 13s / 58s。即每个工具调用都要一次完整模型往返，延迟随上下文增长。

---

## 6. 已执行、未检查与偏差

已执行：只读查询运行中/上一轮任务的 PostgreSQL 事件、回合、成员、挑战/记录/消息/工具调用表（本任务 102 次工具调用）；读取 runtime 容器日志；通读 `tool_results.py`、`board_tools.py`、`ctf/tools.py`、`ctf_platform.py`、`ctf_board.py`、`packages/contracts/.../ctf.py` 与 MAF `.venv` 源码；核对契约字段以确认模型参数合法（2.5 节）；比对既有报告避免重复。

未检查：未复现实验（未构造 409/422 用例验证修复效果）；未测试提高 `max_consecutive_errors_per_request` 后的行为；未统计模型侧 tool_result 全文（只依据日志与事件计数）；未取得 trace payload 做参数级复核（`tool_calls.args` 只留 `full_payload_uri`，本任务 5 次失败的实参未逐字段核对），**因此 2.5 节无法给出被拒的服务端真因**；未评估把服务端文案映射进模型结果的提示词注入风险（2.6 节的 `code + 固定提示` 方案可规避，但需评审确认）。

偏差：本报告不修改 `docs/design/`，不改代码、不提交。问题一的具体映射范围（2.6 节清单是否全收）建议由设计方确认后再实施。

---

## 7. 对下一步的建议

1. 先修问题一（`tool_results.py` 受控兜底 + 2.6 节清单 + 2.7 节枚举补全）。它同时消解问题二的主要触发条件，并让“调用为什么被拒”从不可判定变为可诊断（2.5 节）。
2. 问题二作为第二优先级：显式配置上限 + 掐断时给模型明确信号 + 提示词补一条重试上限规则。
3. 修完后用同一任务规格复跑一次，对比三个指标：失败工具调用次数（上轮 6/235；本轮 5 次，且集中在交付阶段）、被拒原因是否可读、同规模回合的模型往返数（Lead 14 次 / 346 秒）。修复目标：失败调用压到 0–1 次、错误可定位、无掐断。


## 复核补充（2026-10-15）

已按当前部署及原生 MAF 调用路径完成复核与修复，详见 [CTF-tool-errors-fix-report.md](CTF-tool-errors-fix-report.md)。

- 带引用失败的可复现根因是观测映射含 UUID，HTTP JSON 编码在实际工具请求前失败；已修复，历史每条失败仍不能仅凭固定异常标记逐条归因。
- 当前 schema 已有 kind/result 枚举；连续错误上限按错误批次计数，邻近事件时间戳和总时长均不能替代具体调用耗时。
- 另修执行未知副作用重试、平台 preflight 状态、成员移除/重开派工及终态保护；完成会话窄读取与费用清理保存合并。
- 最终普通测试 1878 项、集成测试 325 项、前端测试 108 项通过；本地已更新，13 张业务表内容摘要不变。性能操作数、证据及 SDK 清理请求限制见补充报告。
