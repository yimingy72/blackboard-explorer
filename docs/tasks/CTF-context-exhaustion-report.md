# CTF 上下文耗尽事故报告（run 4：8 名成员在"仅超 0.24%–1.30%"时被判死）

- **状态**：已审核并完成本轮优化，报告事实更正、验证与发布见 [修复报告](CTF-close-context-fix-report.md)。下文保留原事故分析及不确定性。
- **严重度**：高 —— 8 名成员的回合被判死，其中 6 名被移除、2 名被停止；在办成果随人丢失
- **分析对象**：任务 `b92a831f-f007-40cf-9aa3-bae3783d31e8`（第 4 轮 / run 2）；对照组 `d11f03ea-e182-4a3a-8bf9-511e9995c490`（第 1 轮）、`cda295d2-82ce-4cdf-bcad-dce5eb191433`（第 3 轮）
- **设计基线（务必先读）**：`docs/tasks/CTF-context-recovery-report.md`（2026-10-12）。**本报告不推翻它的设计决策**，而是给出该设计在真实运行中被触发时的后果与缺口
- **代码基线**：工作树 `HEAD = 0115a19`（dirty；`ctf.py` / `runner.py` / `session.py` / `middleware.py` 均为"已修改未提交"状态，且分析期间仍在被编辑）
- **行号使用须知**：⚠️ 本报告行号取自**某一时刻的工作树**（同一文件在 13:11–13:14 之间被改动，两次读取行号相差约 39 行）。请一律**按符号定位**（`input_token_limit`、`ctf_max_output_tokens`、`CtfHistoryProvider.prepare_provider_messages`、`_compact_confirmed_prefix`、`_recovery_seed`、`block_context`、`_protected_input_ids`、`_retry_limited_turn`、`_settle`、`finish_turn`）；行号仅作参考
- **证据来源**：平台 DB（只读，`bbx-ctf-team-mode-postgres-1` / `blackboard` 库）、agent-runtime 容器内 MAF 1.19.0 源码、工作树源码
- **约束**：本报告不含任何真实 flag 值；平台数据仅只读；未运行判分

---

## 0. 一句话结论

平台的上下文压缩是**"撞线才压、一次压不动就判死整条回合"**。run 4 里 8 条回合的**超出幅度只有 +770 ~ +4,150 tokens（0.24%–1.30%）**，全部在第一轮压缩尝试后直接 `TurnBoundary("context_blocked")` 结束；平台对 `context_blocked` 只给 **1 次**自动续跑、用尽后**不发任何通知**，并把该成员的派工挂成 `deferred`；Lead 只能看到一句「Lead action is required」，据此判断成员"stuck/looping"并停用重派 —— **8 名成员全部离场（6 removed + 2 stopped），在办成果一并丢失（member-5 已解出 Crushing 但来不及登记）。**

---

## 1. 观测事实与量化

### 1.1 8 次判死（`ctf_turns.end_reason = 'context_blocked'`）

| 成员 | gen | 回合开始 | 回合结束 | 回复长度 | 模型调用数 | 平台估计输入 | 相对上限 320000 |
|---|---|---:|---:|---:|---:|---:|---:|
| member-5 | 1 | 00:59:31 | 01:12:19 | 235 | 64 | 322402 | +2402（**+0.75%**） |
| member-1 | 1 | 00:59:31 | 01:14:30 | 235 | 23 | 324110 | +4110（+1.28%） |
| member-2 | 1 | 00:59:31 | 01:16:55 | 235 | 39 | 324150 | +4150（+1.30%） |
| member-3 | 1 | 00:59:31 | 01:23:05 | 235 | 37 | 322913 | +2913（+0.91%） |
| member-7 | 1 | 01:22:19 | 01:38:49 | 235 | 52 | 320770 | **+770（+0.24%）** |
| member-8 | 1 | 01:22:19 | 01:38:44 | 235 | 18 | 321438 | +1438（+0.45%） |
| member-11 | 1 | 01:23:47 | 01:46:42 | 235 | 33 | 321206 | +1206（+0.38%） |
| member-12 | 4 | 02:44:41 | 03:10:50 | 235 | 45 | 323131 | +3131（+0.98%） |

**8 条的回复是同一句模板，只有数字不同**（`runner.py` 内硬编码，见 §3.5）：

```
Context blocked before provider dispatch. Full history and evidence remain saved.
The remaining message batch cannot fit the model view without splitting tool pairs:
<estimated> estimated input tokens, limit 320000. Lead action is required.
```

> **关键**：没有一条是"上下文真的塞满"（例如 500k/1M 撞墙）。**全部是贴着上限的 0.24%–1.30%。**

### 1.2 成员去向（`ctf_members.lifecycle`）

| 成员 | lifecycle | removed_at |
|---|---|---|
| member-1 / member-2 / member-3 | **removed** | 01:39:45 |
| member-5 / member-7 / member-8 | **removed** | 01:47:04–01:47:05 |
| member-11 | **stopped** | — |
| member-12 | **stopped**（先成功恢复过一次） | — |

⇒ **8 名被阻塞的成员无一继续工作**；只有 member-12 在判死后的续跑回合里成功（03:12:24 完成）。

### 1.3 压缩没发生过的直接证据（会话视图 epoch）

`agent_sessions.session.state.ctf_history_view.epoch` = 成功重建模型视图的次数：

| epoch = 1（从未成功压缩） | epoch ≥ 2 |
|---|---|
| member-1, 2, 3, 5, 7, 8, 11（**恰好是被阻塞的 7 名**） | member-12 = 3、member-4 = 4、member-9 = 4、lead = 4、member-10/13/15 = 3、member-6/14 = 2 |

⇒ **"被阻塞"与"epoch 停在 1"完全重合**：这些成员的模型视图**自始至终没有成功压缩过一次**。

### 1.4 平台承诺的"自动续跑"实际投递情况（run 4 共 12 条 `turn_recovery`）

| 收件人 | 触发原因 | 状态 | 生成时刻 | 投递到的回合 |
|---|---|---|---|---|
| member-5 / 1 / 2 / 3 / 7 | context_blocked | delivered | 与判死同秒 | ✅ |
| member-8 | context_blocked | **cancelled** | 01:38:44 | ❌ |
| member-11 | context_blocked | **queued（`deferred=true`）至今** | 01:46:42 | ❌ |
| member-12 | context_blocked | delivered | 03:10:50 | ✅（随后完成） |
| member-9 / 14 / 15 | provider_transient | delivered | — | ✅ |
| lead | provider_transient | **queued（`deferred=true`）至今** | 03:08:46 | ❌ |

⇒ **member-11 的续跑消息在判死后 6 毫秒生成，`deferred=true`，直到任务结束从未投递**；它的下一回合 gen2 **模型调用数为 0**，随后被判死方停止。**平台承诺的"有界恢复"对该成员从未发生，且没有任何告警。**

### 1.5 跨轮次对照

| 任务 | context_blocked 回合 | provider_transient 回合 | turn_recovery 消息 | provider_recovery_exhausted 消息 | 结局 |
|---|---:|---:|---:|---:|---|
| `b92a831f`（run 4） | **8** | 4 | 12 | 0 | failed / 未收尾 |
| `cda295d2`（run 3） | **7** | 0 | 7 | 0 | OOM |
| `d11f03ea`（run 1） | **3** | 0 | **0** | 0 | finished |
| 其余 6 个任务 | 0 | 0 | 0 | 0 | — |

两条重要的**反例事实**（务必注意，它们影响结论的边界）：

- `d11f03ea` 的 **lead 被判死 2 次（12:06:20、12:23:50），却一条续跑消息都没有**；该任务至今残留 `tasks.ctf_control.context_blocked_members.lead`（见 §6 D7、§11）。
- `cda295d2` 的 member-1 被判死 2 次（15:20:33、23:22:45），**两次都拿到了续跑消息**（delivered 15:45:06、23:38:33），而任务快照里 `turn_retries` 只记了 `{"member-1": {"context_blocked": 1}}`。
  ⇒ **`turn_retries` 会被重置或清理**（谁清理需复核，见 D9），因此**不能**用"计数满上限"解释第 2 次判死；该成员的两次判死相隔 8 小时，中间大概率经历过 Lead 重下指令。

---

## 2. 设计意图 vs 实际后果

`CTF-context-recovery-report.md`（2026-10-12）明确了三条**有意为之**的设计，本报告**不主张推翻**它们：

| 设计决策 | 出处 | 设计理由 |
|---|---|---|
| 不用 MAF 默认的工具结果压缩、不用 `SummarizationStrategy` | 该报告 §3.1 前段 | "它会随着历史组变旧改写旧输入"；"需要额外模型调用"；预算、持久 cutoff、稳定预览、generation/receipt 是平台业务约束 |
| `context_blocked` 最多自动续跑 **1** 次 | 该报告 §3.3 | 抑制循环；"不是循环绕过超大输入" |
| 没有安全边界时**明确阻挡**，不静默丢弃 | 该报告 §3.3 | "不能重新引入旧调用或伪造结果" |

**问题不在"该不该挡"，而在三处工程缺口**：

1. **挡的阈值贴着 0（0.24%–1.30%）**，意味着"可压空间"在设计上就没有余量，安全机制会在长会话里**频繁触发**（run 4：8 次）。
2. **挡之后是终态**：1 次续跑 + 静默 + 派工 `deferred` ⇒ 成员实际停摆，Lead 只能停用重派 ⇒ **把"上下文问题"放大成"成果丢失"**。
3. **没有任何仪器能告诉你"为什么挡"**：判死回执是**固定文案**（§3.5），6 条放弃分支无一打日志/发事件，`ctf_context_blocked` 快照在后续成功回合被覆盖清除 ⇒ **连事后归因都要靠读源码推理**（本报告 §3 就是这样写出来的，见 §11 的诚实标注）。

对照该报告 §4 的验证记录（"离线恢复视图检查……Lead 约 77103、crypto 约 86873"）：那次审计**只计 opening prompt、未装配工具 schema**，因此**结构上无法发现**"真实请求窗口贴上限"这一类问题。

---

## 3. 失败机制（代码路径）

### 3.1 触发点：只在撞线那一刻尝试压缩一次

`CtfHistoryProvider.prepare_provider_messages`（`ctf/middleware.py`）：

```python
messages = stable_tool_previews(outgoing, self.checkpoint.session.state, self.input_limit)
estimated = self.fixed_input_tokens + estimate_messages_tokens(messages, provider=...)
if estimated > self.input_limit:                       # ← 唯一的压缩入口：已经超了才压
    compacted = self._compact_confirmed_prefix(outgoing, messages)
    if compacted is not None:
        messages = compacted
        estimated = ...                                # 重算
if estimated > self.input_limit:                       # ← 压完还超 → 判死
    self.block_context(messages, self.input_limit, ...)
    raise TurnBoundary("context_blocked")
```

- **没有预警阈值**（如 70%/80%）触发，没有分步压；
- `self.input_limit = input_token_limit(model, context_threshold)` 在构造时算定（`CtfHistoryProvider.__init__`）；
- 判死用**估计值**，不是 provider 报告的权威 usage（见 §3.4）。

### 3.2 压缩的全部放弃分支（`_compact_confirmed_prefix`）

该方法只有"成功"与"返回 `None`"两种结局。**6 条 `return None`**（按工作树行号，请以符号为准）：

| # | 条件 | 含义 |
|---|---|---|
| R1 | `not self._loaded_messages or len(outgoing) < len(self._loaded_messages)` | 本 leaf 没有已加载前缀 |
| R2 | 逐条前缀比对失败：`len(contents)` 不等 / `a is not b`（**对象同一性**）/ `message_signature` 不等 / `_attribution.source_id != self.source_id` | 前缀身份不一致 |
| R3 | `confirmed_history` 与 `in_memory.messages` 前缀签名不逐条相等，或 `excluded_before != self._loaded_start` | 确认历史与持久历史不一致 |
| R4 | `durable.get("ctf_context_recovery_seed")` 为空 | **没有恢复种子**（正常路径下由 `_recovery_seed()` 在 `get_messages` 阶段写入并持久化，故通常不成立） |
| R5 | 裁剪边界与 `align_history_start(...)` 不吻合 | 边界会切断工具组 |
| R6 | `not cut or remaining > self.input_limit` | **一个合法边界都没有，或压完仍超上限** |

**裁剪空间的定义（关键）**：

```python
eligible = self._confirmed_count - self._loaded_start      # 名义可裁范围
protected = self._protected_input_ids()                    # ← 本回合 assignment / delivered / active inputs
for index, message in enumerate(raw[self._loaded_start:self._confirmed_count]):
    if message.message_id in protected:
        eligible = index                                   # ← 受保护消息把它截断
        break
...
for boundary in safe_history_boundaries(body, 0):
    if boundary > eligible: break                          # ← 越不过 eligible
    ...
if not cut or remaining > self.input_limit: return None     # R6
```

⇒ **可裁空间 = "已确认前缀中第一条受保护消息之前"的部分。** `_protected_input_ids()` 含本回合的 `assignment_message_ids`、`delivered_message_ids`、`deliveries`、`ctf_active_input_ids`。**每回合都会下发新的派工/指令，它们一旦落在已确认前缀靠前的位置，`eligible` 就趋近 0 ⇒ `cut == 0` ⇒ R6 放弃。** 这能解释为什么"只超 0.75% 也压不动"。

> **这是本报告对直接失败点的最强推断，但不是已证实的结论**：`ctf_context_blocked` 快照已被后续回合清除（run 4 全部 16 个会话 state 中已无该键），无法从库中复原每次放弃的分支与 `eligible` 实际值。**修 D1（可观测性）后应能一次测出。**

### 3.3 压缩目标与"一次批量裁到低水位"

```python
target = self.fixed_input_tokens + int((self.input_limit - self.fixed_input_tokens) * 0.6)
...  # 沿 safe_history_boundaries 逐段裁，直到 remaining <= target
if not cut or remaining > self.input_limit: return None      # R6
```

设计意图（前序报告 §3.2）：**一次裁到 60% 低水位**，留出后续追加空间。因此 R6 的实际含义是"**要么裁到 60%，要么整条回合判死**"——**没有"先裁一点、勉强塞进 100%"这个中间档**。这是本事故最直接的结构性原因：**全有或全无**。

### 3.4 判死依据是启发式估计，不是真实 usage

`estimate_text_tokens` 的 docstring 自己写着：

> "This is a sizing heuristic, **not the provider's tokenizer**. The input budget also leaves a 20% margin, and **reported provider usage remains authoritative**."

而 `prepare_provider_messages` 判死用的是 `estimate_messages_tokens(...)`（启发式：`16 + 估计可见文本 + 每个 data/uri 预留 4096`），**并未与 provider 报告的真实 usage 对照**。

- **真实用量可得但未用于判死**：`ctf_turns.usage` 里有 `cache_hit_tokens + cache_miss_tokens`（真实输入）。8 名成员该回合的真实输入总量 ÷ 调用数 = **62k ~ 175k/次**（member-1 62k、member-8 84k、member-2 111k、member-11 114k、member-7 116k、member-5 131k、member-3 134k、member-12 175k）。
- 若上下文随调用近似线性增长，末次真实输入约为均值的 2 倍量级 ⇒ **member-1（均值 62k）末次真实输入很可能仅 ~100k 级，而平台在"估计 324110"上判死**。
- ⚠️ **此为推断，需复核**：`ctf_turns.usage_requests` 只存 request id，**不存逐次 token**，因此无法从 DB 直接取末次真实输入。复核路径：MinIO 的 model_output（按 call_id）/ 账本；**并建议在判死时把"估计值 + 末次真实 usage"一起落库**（见 D1）。

⇒ 结论：**"320,000 的上限"再叠加"保守启发式估计"，可能让真实输入只有上限 1/3 的回合被判死。**

### 3.5 判死回执是固定文案（会误导诊断）

`ctf/runner.py`（`except TurnBoundary` 分支）：

```python
if reason == "context_blocked":
    blocked = checkpoint.session.state.get("ctf_context_blocked", {})
    answer = (
        "Context blocked before provider dispatch. Full history and evidence "
        "remain saved. The remaining message batch cannot fit the model view "
        f"without splitting tool pairs: {blocked.get('estimated_input_tokens', 0)} "
        f"estimated input tokens, limit {blocked.get('limit_tokens', 0)}. "
        "Lead action is required."
    )
```

**无论命中 R1–R6 哪一条分支，回执都声称原因是 "without splitting tool pairs"** ⇒ 该文案不是诊断信息。run 4 里 8 条回执全部如此，**把"没有合法裁剪边界/受保护消息夹死"误标成"工具对不可切"**，直接误导了 Lead 与后续分析。

### 3.6 平台侧：1 次续跑、用尽静默、派工挂起

`services/blackboard/src/bbx_blackboard/ctf.py` `_retry_limited_turn`（现文）：

```python
limit = 1 if end_reason == "context_blocked" else 3
if count >= limit:
    if end_reason == "provider_transient":        # ← 只有这一支会通知
        await self._message(..., notification_purpose="provider_recovery_exhausted")
    return                                        # ← context_blocked 用尽：直接 return，零通知、零事件
```

另有 **4 条静默早退**（同样无日志、无事件、无通知）：`lifecycle != active` / `pending_operation` / `phase != "running"` / `replacement.phase != "ready"`（前置检查），以及 `check_budget(task)` 抛 `HTTPException`。**唯一会发事件的是"成功发放续跑"那一支**（`_emit("coordination.reminded")`）。

配套行为：`_settle` 在 `context_blocked` 时把该成员的派工消息标 `deferred=true` 重新入队（**不再自动派发**），并把成员记入 `ctf_control.context_blocked_members`；只有 Lead 主动下发新指令（`kind="instruction"`）才会把它移出该集合、返回 `dispatch_status="context_recovery_requested"`。

⇒ **平台在"决定不救"或"救不动"时完全不留痕**：这正是 Lead 把现象判断为 "stuck/looping" 的原因（见 run 4 的 Lead 会话原文）。

### 3.7 Lead 侧证据（run 4 会话原文，脱敏）

- 「member-5 (rev-forensics) got **context_blocked** … **This means member-5 hit context limit and couldn't continue.** I need to handle this: possibly resume with fresh context?」
- 「It's **probably stuck/looping or was auto-resumed**. To prevent duplication and loop, I should **stop member-5 and reassign** its two remaining tasks.」
- 「member-1 / member-2 … Both hit the same 320k context limit. **This is a systemic issue: long-running teammate sessions exceed context.**」
- 「member-5 **actually SOLVED Crushing before getting context-blocked** … But it **didn't get to register_artifact/record_candidate for it**.」

（Lead 会话内 `context_blocked` 出现 58 次、上下文相关表述 62 次；含真实 flag 的原文已脱敏，未收录。）

---

## 4. 阈值算术：为什么是 320,000，而不是你设置的 1,000,000

**你的 1M 确实生效了，但它只是三道门里的第一道。**

运行期实际配置（`agent_profiles`，`ctf-task-settings` v6，2026-10-13 02:41 发布；任务快照 `tasks.ctf_options` 与之相同）：

| 项 | 值 | 来源 |
|---|---|---|
| `models.lead/teammate.context_window` | **1000000** | 你的设置（`deepseek-flash`、`provider=openai_responses`、`reasoning_effort=max`；1M 与最大输出 393216 的保留见前序报告 §3.1） |
| `params.context_threshold` | **400000** | 同上；任务快照 `{"max_steps": 4000, "max_teammates": 12, "context_threshold": 400000}` |
| `ctf_max_output_tokens(model)` | **393216** | 代码硬编码（DeepSeek 系列，含 reasoning tokens） |

计算（`ctf/context.py` `input_token_limit`）：

```python
available = threshold
if model.context_window is not None:
    available = min(available, model.context_window - ctf_max_output_tokens(model))
return max(0, int(available * 0.8))          # ← 硬编码 20% 安全余量
```

代入：

```
available = min(400000, 1000000 − 393216 = 606784) = 400000
limit     = 400000 × 0.8 = 320000            ← 与 8 条回执的 "limit 320000" 完全吻合
```

**三个必须被明确认知的事实**：

1. `context_threshold = 400000` 是**真正卡住的那道门**（不是 1M，也不是 393216）。
2. 即便把阈值提到模型窗口（1M），上限也只是 `(1000000 − 393216) × 0.8 = 485427`（≈ 窗口的 **48.5%**）——`×0.8` 与输出预留仍在。
3. 现实是 **320000 ≈ 1M 的 32%**。

> 不同轮次的"撞墙点"不同，因为**平台有两个 profile 家族**：`ctf` 家族各版本（v8/v9/v10/v11 实测，`profiles/ctf/profile.yaml` 同值）的 threshold 是 **128000**（→ 上限 **102400**，与 `d11f03ea` 残留快照里的 `limit_chars: 102400` 吻合；该轮跑了 `ctf` 家族的早期版本，发布记录见 `CTF-context-recovery-report.md` §5 的 revision 5→7），`ctf-task-settings` 家族（v5/v6）是 **400000**（→ 320000，run 4 用的就是 v6）。
> 附带字段问题：`ctf_context_blocked` 里 `limit_chars` 的数值等于 **token 上限**（102400 = 0.8×128000），命名与单位疑似不符（见 D8）。

---

## 5. 对照：MAF 1.19.0 原生提供了什么

`services/agent-runtime/pyproject.toml` 钉定 `agent-framework-core==1.19.0`；`agent_framework/_compaction.py` 导出（节选）：

| MAF 原生 API | 语义 |
|---|---|
| `CompactionProvider(ContextProvider)` | 挂在 agent 上，**`before_run`** 压"即将送模型的上下文"、**`after_run`** 压"已持久化的历史" ⇒ **原生即"每回合前后持续压"**，不是撞线才压 |
| `ContextWindowCompactionStrategy(max_context_window_tokens, max_output_tokens)` | **两阶段比例阈值**：工具结果摘要驱逐 @ `tool_eviction_threshold=0.5`，再截断 @ `truncation_threshold=0.8`；驱逐后**重新计数**再决定是否截断；受保护组仍超预算时**保留并发出结构化告警**（而不是判死回合） |
| `TokenBudgetComposedStrategy(token_budget, strategies, early_stop)` | 多策略按序跑到满足预算，**并有确定性兜底**（淘汰最旧分组，最后才动锚点） |
| `SummarizationStrategy` / `TruncationStrategy` / `SlidingWindowStrategy` / `ToolResultCompactionStrategy` / `SelectiveToolCallCompactionStrategy` | 摘要 / 截断 / 滑窗 / 只压旧工具组 |
| `apply_compaction` / `COMPACTION_STATE_KEY` / `included_token_count` / `TokenizerProtocol` | 压缩状态可持久化、token 可计量、摘要可追溯（`_summary_of_message_ids` 等） |

**平台用法**：只继承 MAF 的 `InMemoryHistoryProvider`（`CheckpointHistoryProvider`，`ctf/middleware.py` 的 `CtfHistoryProvider`），压缩逻辑自写。在 `services/` + `packages/` 的非测试代码中检索 `CompactionProvider|apply_compaction|CompactionStrategy|SummarizationStrategy|TokenBudgetComposedStrategy|ContextWindowCompactionStrategy`：**零命中**。

**这不是疏忽**（前序报告 §3.1 已列理由：MAF 策略会改写旧输入 → 破坏缓存前缀与持久 cutoff；`SummarizationStrategy` 还要额外模型调用）。**但可以只借用它的"阈值纪律"，而不引入它的改写行为** —— 见 §7 的 T2/T3。

---

## 6. 缺陷清单

> 位置一律给**符号**；行号仅参考（工作树 dirty）。

### D1【运行时·必修·其他修复的前提】判死与放弃过程不可观测
- **位置**：`ctf/middleware.py` `_compact_confirmed_prefix`（R1–R6 六条 `return None`）、`_recovery_seed`、`block_context`；`ctf/runner.py` `except TurnBoundary` 分支
- **问题**：6 条放弃分支**没有任何日志/事件**；回执照抄固定文案 "without splitting tool pairs"（§3.5），与实际分支无关；`ctf_context_blocked` 快照被后续成功回合覆盖清除（run 4 已无留存）⇒ 事后无法归因，只能读源码推理。
- **最小修法**：① 每条 `return None` 前打一条 WARN，字段至少含 `branch`、`eligible`、`cut`、`remaining`、`input_limit`、`protected_hits`（受保护消息在已加载前缀中的下标）；② 回执改为携带 `branch` 与 `eligible`（**不要把固定文案当诊断**）；③ 把该 WARN 汇总为一条 `ctf.context.blocked` 事件（注意：事件类型须先加入契约枚举，见 §10 边界）；④ `block_context` 同时记录**该回合末次真实 usage**（来自 `ctf_turns.usage`/MinIO），与估计值并列。
- **验收**：一次真实判死后，DB/日志里能直接读出"哪条分支、eligible 是多少、估计 vs 真实 usage 各是多少"。

### D2【运行时·根因】"全有或全无"的压缩
- **位置**：`_compact_confirmed_prefix` 的 R6（`if not cut or remaining > self.input_limit: return None`）
- **问题**：必须**一次**裁到 60% 低水位（`target`），否则整条回合判死；**没有"先裁到勉强低于 100%"的中间档**。run 4 的 8 次判死超出幅度仅 0.24%–1.30%。
- **最小修法**：把 R6 拆成两步：先尝试 `remaining <= input_limit`（尽力而为），只有连"低于上限"都做不到才判死；并让 `target` 成为"理想值"而非"必要条件"。同时按 §3.2，`eligible` 受保护消息夹死时应能**部分裁剪**（裁到 `eligible` 为止）而不是 `cut == 0` 直接放弃。
- **验收**：构造"超出 <2%、可裁空间 >0"的场景 ⇒ **必须压缩通过**（不再 `context_blocked`）。

### D3【配置/语义·需产品确认】阈值与窗口的关系未显式化
- **位置**：`packages/contracts/src/bbx_contracts/ctf.py`（`CtfOptions.context_threshold`）、`packages/contracts/src/bbx_contracts/models.py`（`Params.context_threshold`："触发探索交接的上下文 token 数"）、`ctf/context.py` `input_token_limit`、`ctf/budget.py` `ctf_max_output_tokens`
- **问题**：`context_threshold` 在**非 CTF 路径**是"到阈值就主动交接/开新段"的触发器（`scheduler/decision.py` 的 `Conclude(agent, "limit")`、`runner.py` 的 `limit_reached → handoff`、`start_derive_segment`）；在 **CTF 路径**它只剩"输入上限参数"与"成本预留"两个用途——**"提前交接"的语义在团队模式里消失了**。同时 `×0.8` 硬编码使 1M 窗口只能用 32%–48.5%。
- **最小修法**：① 在契约/文档中写明 CTF 语义：`输入上限 = min(threshold, window − max_output) × 0.8`，并给出"想用满窗口该填多少"的推荐值；② 明确 400000 是否为期望值（若意图是用满 1M，应设到 ~606784）；③ 考虑把 `0.8` 提为可配置的 `input_safety_margin`。
- **验收**：`input_token_limit` 的单测覆盖（threshold=400000 → 320000；threshold=window → 485427；小窗口 → 受输出预留约束），且**文档与配置一致**。

### D4【平台·必修】`context_blocked` 用尽后续跑静默
- **位置**：`services/blackboard/src/bbx_blackboard/ctf.py` `_retry_limited_turn`
- **问题**：`limit = 1 if end_reason == "context_blocked" else 3`；用尽时只有 `provider_transient` 会发消息，`context_blocked` **直接 `return`**；另有 4 条早退路径（`lifecycle`/`pending_operation`/`phase`/`replacement.phase`）与"预算不足"同样静默。
- **最小修法**：① 用尽时为 `context_blocked` 也发一条面向 Lead 的通知（`notification_purpose="context_recovery_exhausted"`）+ emit 事件；② 早退路径至少 WARN + emit（说明"为什么不救"）；③ 评估把 `context_blocked` 的 `limit` 提到 3（与 `provider_transient` 同级）是否更合适（参见 D2：修好压缩后，续跑次数可回调）。
- **验收**：任何"平台决定不再救援"的情形都能在事件流里查到；`context_blocked` 用尽必有 Lead 可见通知。

### D5【平台·必修】续跑消息可能永不投递且无声
- **位置**：`ctf.py` `_message` / 派发（`dispatch_message_condition`、`_message` 的 `deferred` 处理、§3.6 的 `context_blocked_members` 投递路径）
- **问题**：run 4 中 member-11 的续跑消息 `queued + deferred=true` **直到任务结束未投递**，member-8 的续跑被 `cancelled`，lead 的 `provider_transient` 续跑同样 `queued + deferred=true` 至今。三例均无告警。
- **最小修法**：续跑消息是"平台对成员的承诺"，应：① 不设 `deferred=true`（或设了也要有到期升级告警）；② 被 `cancelled`/超时未投递时 emit 事件并通知 Lead；③ 在 Lead 的看板/汇总里体现"该成员有未投递的恢复指令"。
- **验收**：`turn_recovery` 消息的生命周期在事件流中可查；不存在"生成后静默消失"的路径。

### D6【产品·高价值】判死前不保护在办成果
- **位置**：`ctf/runner.py` 结算路径 + Lead 提示词/工具面
- **问题**：member-5 **已解出 Crushing 但未登记**（Lead 原话）即被判死、随后被移除 ⇒ 成果随之丢失，需重派重做。
- **最小修法**：① 判死 / 停止 / 移除成员前，要求或自动触发一次"成果登记"（`register_artifact`/`record_candidate` 的最小快照），或在 Lead 的判死通知里明确携带"该成员最后 N 条产出线索"；② 在 Lead 提示词中把"阻塞成员的成果必须先盘点"写成硬步骤。
- **验收**：阻塞/移除成员时，其未登记成果在黑板上有据可查。

### D7【平台·数据一致性】`context_blocked_members` 残留与 `null` 元素
- **位置**：`tasks.ctf_control.context_blocked_members`（写入 `ctf.py` `_settle`；清除在派发路径）
- **问题**：
  - `d11f03ea`（**已 finished**）至今残留 `context_blocked_members.lead`，字段为 `{"turn_id": …, "fingerprint": "704b8bca…", "limit_chars": 102400, "excluded_before": 245, "remaining_chars": 137137, "message_ids": [uuid, null, null], "input_message_ids": [uuid, uuid, null]}` ⇒ ①任务结束未清理；②**数组内含 `null` 元素**（投递路径虽有 `isinstance(value, str)` 过滤，但写入端的 `null` 说明有占位写入）。
- **最小修法**：任务进入终态时清空该字段；写入端过滤 `None`（或改用非空约束/显式占位类型）。
- **验收**：终态任务 `context_blocked_members == {}`；数组中不含 `null`。

### D8【命名/单位】`limit_chars` 实为 token 上限
- **位置**：`ctf/middleware.py` `block_context`（写 `limit_tokens` 的同时写 `remaining_chars`）
- **问题**：`ctf_context_blocked.limit_chars = 102400` 与 `0.8 × 128000` 相等，**数值口径是 token**；`remaining_chars` 才是字符数。命名混用会让后续排查（包括本报告）误判单位。
- **最小修法**：重命名/注释明确单位，历史数据在报告中标注；新写入统一 `limit_tokens` / `remaining_chars`。
- **验收**：字段名与单位一致，有单测固定口径。

### D9【待复核】`turn_retries` 计数会被清理/重置
- **位置**：`turn_retries` 的写入（`_retry_limited_turn`）与任何清除路径（Lead 新指令/成员移除/phase 变更……）
- **问题**：run 4 只有 7 名被阻塞成员**一条计数都没留下**（快照仅 `{"lead": {...}, "member-12": {...}, "member-14": {...}, "member-15": {...}}`）；`cda295d2` 的 member-1 判死 2 次却只记 1 次。⇒ **无法用 DB 证据复核"自动续跑是否被耗尽"**。
- **最小修法**：明确并记录计数的重置语义（何时清、由谁清），保留可审计轨迹（事件而非仅计数器）。
- **验收**：任一成员的续跑发放/耗尽历史可从事件流完整复原。

---

## 7. 给开发 Agent 的执行清单（按依赖排序）

> **原则：先测量，再改策略。** D1 不做，后续任何优化都无法验证，也解释不了 run 4 的 8 次判死到底卡在哪条分支。

| 序 | 任务 | 对应缺陷 | 说明 |
|---|---|---|---|
| 1 | **加仪器**：6 条放弃分支 + 4 条早退路径的 WARN/事件；回执携带分支与 eligible；落库"估计 vs 真实 usage" | D1 | 其余任务的前置；也是最快能收敛的产出 |
| 2 | **让判死不再是"0.24% 就死"**：先尽力裁到低于上限，再谈 60% 低水位；`eligible` 夹死时允许部分裁剪 | D2 | 本事故最直接的止血点 |
| 3 | **阈值语义显式化**（文档 + 注释 + 单测），确认 400000/0.8 是否为期望 | D3 | 可能与用户预期（1M）冲突，需产品确认 |
| 4 | **`context_blocked` 的续跑与通知对齐 `provider_transient`**；早退也要留痕 | D4 | 消除"静默不救" |
| 5 | **续跑消息的投递保证** | D5 | 消除"承诺未兑现" |
| 6 | **判死/移除前盘点在办成果** | D6 | 直接止损分数 |
| 7 | **清理 `context_blocked_members` 残留与 `null` 元素** | D7 | 数据一致性 |
| 8 | **单位/命名与计数语义** | D8, D9 | 可维护性 |

**关于是否接 MAF（可选，T2/T3 二选一）**：

- **T2（推荐，保守）**：保留平台自写压缩，但**移植 MAF 的阈值纪律**：把"0.6 工具结果驱逐 / 0.8 前缀裁剪"两阶段比例阈值、驱逐后重新计数、以及"受保护组仍超预算时保留 + 结构化告警"这套语义照搬过来（`ContextWindowCompactionStrategy` 的 `DEFAULT_TOOL_EVICTION_THRESHOLD=0.5` / `DEFAULT_TRUNCATION_THRESHOLD=0.8` 是现成参考）。**不引入任何改写历史的行为**，因此不破坏缓存前缀与持久 cutoff。
- **T3（激进）**：直接在 `CtfHistoryProvider` 之外挂 `CompactionProvider(before_strategy=TokenBudgetComposedStrategy(token_budget=…, strategies=[ToolResultCompactionStrategy(...), TruncationStrategy(...)]))`。**必须先解决前序报告 §3.1 列出的四个冲突**（预算、持久 cutoff、稳定预览、generation/receipt），并确认它不会改写已确认前缀（否则缓存命中率会再降——前序报告已量化过这条路线的代价）。

---

## 8. 回归测试要求

### 必须新增
1. **可压缩性**：`estimated = limit × 1.005`（模拟 run 4 的 0.24%–1.30%）且存在合法裁剪边界 ⇒ **不得** `context_blocked`，必须压缩通过。
2. **分支可观测**：构造 R1–R6 各自的触发条件，断言 WARN/事件里 `branch` 正确、`eligible` 有值、**回执不再谎报 "splitting tool pairs"**。
3. **受保护消息夹死**：把本回合 assignment 放在已确认前缀首位 ⇒ 断言"部分裁剪"（`cut` 落在 `eligible` 之前）或至少 WARN 指出 `eligible == 0`。
4. **平台侧**：`context_blocked` 续跑用尽 ⇒ 断言向 Lead 发出通知 + 事件（D4）。
5. **投递**：构造 `deferred=true` 的续跑消息 ⇒ 断言不会静默消失（D5）。
6. **终态清理**：任务终态后 `context_blocked_members == {}`，且数组不含 `null`（D7）。

### 必须修改（现有测试把当前行为当成了契约，**不要只改绿**）
| 文件 | 位置/现状 | 期望 |
|---|---|---|
| `services/agent-runtime/tests/test_ctf_context.py` | 断言 `input_token_limit(configured, 400_000) == 320_000`、`== int((1_000_000 - 393216) * 0.8)`、`== 102400` | **保留数值断言**（口径正确），但**追加注释/用例说明这是"20% 余量 + 输出预留"的结果**，并补"想用满窗口应配置多少"的用例（D3） |
| `services/agent-runtime/tests/test_ctf_provider_boundary.py` | `end_reason == "context_blocked"`、`ctf_context_blocked.message_ids` | 保留"真不可压时阻塞"，**补**"可压时必压过去"（新增用例 1/3） |
| `services/agent-runtime/tests/test_ctf_leaf_context.py` | 5 处 `pytest.raises(TurnBoundary, match="context_blocked")` | 逐条区分：哪些是"真不可压"（保留）、哪些是"只超一点就该压过去"（改为通过） |
| `services/agent-runtime/tests/test_ctf_budget_bounds.py` | `input_token_limit(model, 128000) == 51200` | 与 D3 的口径注释保持一致 |
| `services/blackboard/tests/test_ctf_usage_outbox_integration.py` | `finish_turn(..., "context_blocked")` 后行为 | 补 D4/D5 的通知与投递断言 |

### 验收标准
1. 上述新增场景全部通过；
2. run 4 的 8 个判死场景在修复后**至少能被压缩救回**（可用回执里的估计值构造 fixture）；
3. `make check` 与 `make test-integration` 全绿（前序基线：1247 passed / 180 passed）；
4. 任一真实判死都能在事件流中读到"分支 + eligible + 估计 vs 真实 usage"。

---

## 9. 复现方法

**单元级（最快）**

```python
# 1) 阈值口径
input_token_limit(configured_model, 400_000) == 320_000
input_token_limit(configured_model, 1_000_000) == 485_427      # (1M - 393216) * 0.8

# 2) 只超 0.5% 应当压过去
provider.input_limit = 320_000
messages = build_messages(estimated=321_600, with_legal_boundary=True)
assert provider.prepare_provider_messages(messages)      # 修复前：抛 TurnBoundary("context_blocked")

# 3) 受保护消息夹死
turn["assignment_message_ids"] = [first_confirmed_prefix_message_id]
# 修复前：eligible == 0 → cut == 0 → R6 → 判死
```

**集成级**：`test_ctf_provider_boundary.py` 现有形态（小窗口 + 大输入）即可复现；把"大于窗口但可裁"与"大于窗口且不可裁"分开断言。

**配置级**：用 `context_threshold ∈ {400000, 1000000}` 各跑一次，比较 `CtfHistoryProvider.input_limit` 与首次判死的估计值（应为 320000 / 485427）。

---

## 10. 边界（本次不要动）

- **不要改** `agent-framework-core==1.19.0` 的钉定，也不要升级依赖。
- **不要改** 判分/flag 相关逻辑；本报告不含 flag，也不要求读取官方答案。
- **不要顺手修** `ctf.coordination.reminded` 不在契约枚举导致 `GET /api/tasks/{id}/events` 永久 500 的缺陷（D1 若要 emit 新事件**必须先**单独把事件类型加进 `packages/contracts/src/bbx_contracts/ctf.py` 的枚举并重建，**建议另案**）。
- **不要**为了"不判死"而静默丢弃输入或重放未知副作用的工具调用（前序报告的硬约束，继续生效）。
- **不要**把 60 分钟级别的收尾死锁与本次问题混为一谈——那是另一份报告（`CTF-close-deadlock-report.md`）的对象。

---

## 11. 残留不确定性（如实标注）

1. **8 次判死具体命中哪条分支，未证实。** `ctf_context_blocked` 快照被后续回合清除，run 4 已无留存；本报告 §3.2 的"`eligible→0`（受保护消息夹死）"是**代码路径推理的最强候选**，需 D1 落地后实测。
2. **"判死用的是保守估计、真实输入远低于上限"是推断。** 依据是"每成员真实输入总量 ÷ 调用数 = 62k–175k"，以及估计函数自述"不是 tokenizer"。`usage_requests` 只存 request id，逐次真实 token 需从 MinIO/账本复核。
3. **`limit_chars` 的单位**（疑为 token）与 **`message_ids` 中的 `null`** 来源未定位。
4. **`d11f03ea` 的 lead 两次判死为何 0 条续跑消息**未证明；候选是 `_retry_limited_turn` 的 4 条早退路径或预算不足，**均为静默**，需 D1/D4 补痕后复核。
5. **`turn_retries` 的清理/重置时机未定位**（`cda295d2` member-1 两次判死只记 1 次；run 4 七名成员无计数）。
6. 工作树在分析期间仍在被改动（`ctf.py` / `runner.py` / `session.py` / `middleware.py`，13:11–13:14），**本报告所有行号可能已漂移**，一律以符号为准；若相关符号已在新代码中被重构，请以语义对应关系重新定位。

---

## 附 A：原始数据

### A.1 8 条判死回执（逐字，仅数字不同）

```
Context blocked before provider dispatch. Full history and evidence remain saved. The remaining message batch cannot fit the model view without splitting tool pairs: 320770 estimated input tokens, limit 320000. Lead action is required.
Context blocked before provider dispatch. Full history and evidence remain saved. The remaining message batch cannot fit the model view without splitting tool pairs: 321206 estimated input tokens, limit 320000. Lead action is required.
Context blocked before provider dispatch. Full history and evidence remain saved. The remaining message batch cannot fit the model view without splitting tool pairs: 321438 estimated input tokens, limit 320000. Lead action is required.
Context blocked before provider dispatch. Full history and evidence remain saved. The remaining message batch cannot fit the model view without splitting tool pairs: 322402 estimated input tokens, limit 320000. Lead action is required.
Context blocked before provider dispatch. Full history and evidence remain saved. The remaining message batch cannot fit the model view without splitting tool pairs: 322913 estimated input tokens, limit 320000. Lead action is required.
Context blocked before provider dispatch. Full history and evidence remain saved. The remaining message batch cannot fit the model view without splitting tool pairs: 323131 estimated input tokens, limit 320000. Lead action is required.
Context blocked before provider dispatch. Full history and evidence remain saved. The remaining message batch cannot fit the model view without splitting tool pairs: 324110 estimated input tokens, limit 320000. Lead action is required.
Context blocked before provider dispatch. Full history and evidence remain saved. The remaining message batch cannot fit the model view without splitting tool pairs: 324150 estimated input tokens, limit 320000. Lead action is required.
```

### A.2 真实用量（`ctf_turns.usage`，真实输入 = cache_hit + cache_miss）

| 成员 | 真实输入合计 | 调用数 | 均值/次 | 该回合输出 token | reasoning |
|---|---:|---:|---:|---:|---:|
| member-1 | 1,428,922 | 23 | 62,127 | 127,250 | — |
| member-8 | 1,514,172 | 18 | 84,121 | 137,631 | — |
| member-2 | 4,330,281 | 39 | 111,033 | 123,453 | — |
| member-11 | 3,748,190 | 33 | 113,582 | 146,834 | — |
| member-7 | 6,036,298 | 52 | 116,083 | 124,383 | 101,072 |
| member-5 | 8,383,056 | 64 | 130,985 | 95,251 | — |
| member-3 | 4,973,341 | 37 | 134,415 | 155,442 | — |
| member-12 | 7,881,153 | 45 | 175,137 | 134,820 | — |

### A.3 `d11f03ea` 残留快照（已 finished 的任务）

```json
{"lead": {"turn_id": "f3f6000f-…", "fingerprint": "704b8bca…", "limit_chars": 102400,
          "excluded_before": 245, "remaining_chars": 137137,
          "message_ids": ["380f9b23-…", null, null],
          "input_message_ids": ["75482bac-…", "380f9b23-…", null]}}
```

## 附 B：符号索引（避免行号漂移）

| 文件 | 符号 |
|---|---|
| `services/agent-runtime/src/bbx_runtime/ctf/context.py` | `estimate_text_tokens`、`estimate_message_lengths`、`estimate_messages_tokens`、`input_token_limit`、`fixed_prompt_tokens`、`stable_tool_previews`、`safe_history_boundaries`、`align_history_start` |
| `services/agent-runtime/src/bbx_runtime/ctf/budget.py` | `CTF_MAX_OUTPUT_TOKENS`、`ctf_max_output_tokens`、`ctf_call_reservation` |
| `services/agent-runtime/src/bbx_runtime/ctf/middleware.py` | `CtfHistoryProvider`（`__init__`、`get_messages`、`prepare_provider_messages`、`_compact_confirmed_prefix`、`_protected_input_ids`、`_confirmed_history_count`、`_recovery_seed`、`block_context`） |
| `services/agent-runtime/src/bbx_runtime/ctf/runner.py` | `CtfHistoryProvider(...)` 构造点、`except TurnBoundary` 回执分支、`retry_checkpoints` |
| `services/agent-runtime/src/bbx_runtime/session.py` | `CheckpointHistoryProvider(InMemoryHistoryProvider)`、`SessionCheckpoint` |
| `services/agent-runtime/src/bbx_runtime/scheduler/decision.py` | `context_threshold → Conclude(agent, "limit")` |
| `services/agent-runtime/src/bbx_runtime/runner.py` | `context_threshold` 交接/开新段分支 |
| `services/blackboard/src/bbx_blackboard/ctf.py` | `_retry_limited_turn`、`_settle`、`finish_turn`、`context_blocked_members`、`turn_retries`、`_message` 派发与 `context_recovery_requested` |
| `packages/contracts/src/bbx_contracts/ctf.py` | `CtfOptions.context_threshold`、`resolve_ctf_options` |
| `packages/contracts/src/bbx_contracts/models.py` | `Params.context_threshold`、`ModelConfig.context_window` |
| MAF（容器内 `/app/.venv/.../agent_framework/_compaction.py`） | `CompactionProvider`、`ContextWindowCompactionStrategy`、`TokenBudgetComposedStrategy`、`SummarizationStrategy`、`TruncationStrategy`、`SlidingWindowStrategy`、`ToolResultCompactionStrategy`、`SelectiveToolCallCompactionStrategy` |
