# CTF 上下文耗尽修复复核 —— 未关闭项报告

> **2026-10-13 开发复核与处理已完成**：详见 [CTF-context-unclosed-fix-report.md](CTF-context-unclosed-fix-report.md)。以下保留原审查文本。U1 的历史快照推断、U3 的释放入口、U5 的键存在验收口径及 V4 的同 run 推断已更正；新配置、诊断与终态清理已本地发布。真实缓存/费用效果未重跑验证，不以代码回归代替实测。

- **用途**：交开发 Agent 审核并继续优化。本报告**只列未关闭项**；已修复部分的复核证据见附录 A，事故原始分析见 [CTF-context-exhaustion-report.md](CTF-context-exhaustion-report.md)
- **复核对象**：`HEAD = 118b393`（工作树干净）。相对上一份报告的基线 `0115a19`，修复由三个提交构成：`ded93ee`（Harden CTF cooperation, context recovery, and closing）→ `1f4341c`（Clarify CTF evidence handoff and bounded recovery）→ `118b393`（Document CTF reliability fixes and verification）
- **复核方式**：源码符号级比对 + 容器内文件哈希对照 + 单元测试 + CTF 集成测试 + 静态检查 + 平台只读 DB 查询。**未发起真实 CTF 运行，未判分**
- **行号说明**：本报告行号取自 `118b393`（该提交工作树干净，因此行号在该提交上**稳定可复现**）；但请仍以**符号**为主锚点
- **约束**：报告不含任何真实 flag 值；平台数据全程只读；本次复核未修改任何代码、配置或 profile

---

## 0. 复核结论一览

| 原缺陷 | 判定 | 备注 |
|---|---|---|
| D1 判死/放弃过程不可观测 | ✅ 已修 | 9 个具名 `branch`、`eligible/cut/protected_hits` 全落库、`logger.warning`、回执带原因、可经 HTTP 读出 |
| D2「全有或全无」压缩 | ✅ 已修 | 触发点提前到 90%；受保护输入可**整条搬迁**（`_can_relocate_input`）；未达 60% 低水位也接受 |
| D3 阈值语义未显式化 | 🟡 **部分** → **U2** | 已加注释；但 `0.8`/`0.9` 仍硬编码，有效上限仍为窗口的 32%（产品决策未做） |
| D4 `context_blocked` 用尽静默 | ✅ 已修 | 两类 `end_reason` 都发耗尽通知；5 条早退路径都有事件 + WARNING |
| D5 续跑消息可能永不投递 | 🟡 **部分** → **U3** | 由「静默卡死」改为「显式告警 + 需决策」；**已延迟输入仍无自动释放路径** |
| D6 判死前不保护在办成果 | 🟡 **部分** → **U1** | 提示词已写对，但**没进实际生效的 profile 家族**（发布缺口） |
| D7 残留与 `null` 元素 | ✅ 已修 | 写入前净化 + `clear_context_blocks` 转存历史并清空 + 终态/修复路径都调用 |
| D8 `limit_chars` 命名/单位 | ✅ 已修 | 已改为 `limit_tokens`，`remaining_chars` 名副其实 |
| D9 `turn_retries` 不可审计 | ✅ 已修 | 每次决策都发事件（含 attempt/limit/reason）+ `context_blocked_history` 留档；重置机制已定位 |
| （附带）`ctf.coordination.reminded` 不在契约枚举 → 事件流永久 500 | ✅ 已修 | 枚举已补（`packages/contracts/src/bbx_contracts/ctf.py`）；扫描确认**无**"已写入但未声明"的 ctf 事件类型 |

**剩余工作量估计**：U1 是一次 profile 发布（无代码）；U2 是一次配置变更 + 一次实跑验证（无代码）；U3/U4 需要你决策后再改；U5 是可选的清理脚本。

---

## 1. 未关闭项

### U1 【发布缺口·高】新的成果保护指导语没有进入实际生效的 profile

**事实（已核验）**

所有 CTF 团队任务的 `tasks.agent_profile` 都是 **`ctf-task-settings`**：

| profile | 版本 | 创建时间 | `prompt_templates` md5 | 是否含新指导语 |
|---|---|---|---|---|
| `ctf-task-settings` | **v6（全部任务实际在用）** | 2026-10-13 02:41:27 | `efef8cb33a1f642b7f9fe2e77e5849ad` | ❌ **无** |
| `ctf` | v12 | 2026-10-13 06:54:31 | `f887e852ac5c233dcc968e2c5d3b1b0f` | ✅ 有 |
| `ctf` | v13 | 2026-10-13 06:54:31 | `c6a03a78146f3431d0f26014b7d6d41c` | ✅ 有 |

- `ctf-task-settings` v6 的模板 md5 与**修复前**的 `ctf` v11 完全相同（`efef8cb3…`）→ 提示词层面的修复**没有随任务配置发布**。
- 该家族的发布是"跟随 `ctf` 模板"的节奏（`ctf-task-settings` v4 的 md5 = `ctf` v7；v5/v6 的 md5 = `ctf` v9–v11），因此这是**漏发了一版**，不是有意的差异。

**缺失的文本（逐字，来自 `ctf` v13 → 应进入 `ctf-task-settings` v7）**

- Lead 模板新增：> `context_blocked表示本次模型输入无法安全容纳，不等于题目不可解或成员没有成果。停止、移除或重派阻塞成员前，先盘点相关题目的共享记录、已登记产物和派工结果；确有缺口时，给原成员一条较小的明确指令补交接，沿用已有Session，不重复发同一指令或替未知结果下结论。`
- Teammate 模板新增：> `一旦形成可复核的阶段成果或完成候选，先登记必要产物并提交对应记录或record_candidate，再继续下一步调查；不要把全部交付留到最终回答或上下文耗尽时。已有记录直接引用；这不要求重复广播，也不把未经核实的线索自动视为完成。`

**为什么这是"高"**：这正是 run 4 里 `member-5`（已解出 Crushing 却来不及登记）那一类损失的唯一防线。`ctf-task-settings` v6 里虽然已有"收到恢复耗尽通知后，再依据共享记录决定显式续接、增援或重派"，但**没有**"`context_blocked` ≠ 成员没有成果"和"停止/移除前先盘点"这两句针对性指导。

**最小修法**（无平台代码改动）

1. 用平台自身发布路径（`ProfileStore.create`，扩展既有 `publish_ctf_profile.py`，**不要发裸 SQL**）发布 `ctf-task-settings` **v7**：
   - `prompt_templates` = `ctf` v13 的两份模板；
   - 其余字段**逐字沿用 v6**：`context_threshold=400000`、`max_steps=4000`、`max_teammates=12`、`exec_image=bbx-exec-env-kali:latest`、`exec_resources` 不变。
2. 顺手堵住漂移：发布脚本增加一条断言——任务 profile 的模板 md5 必须等于同批 `ctf` 版本的模板 md5，否则拒绝发布（本次缺口就是因为两个家族静默分叉）。

**验收**

- `select md5(prompt_templates::text) from agent_profiles where name='ctf-task-settings' order by version desc limit 1` == `c6a03a78146f3431d0f26014b7d6d41c`；
- 新任务快照的 `agent_profile_version` = 7；
- 两条新增指导语在模板中各命中 1 次（`不等于题目不可解` / `登记必要产物`）。

---

### U2 【配置 + 产品决策·高】有效上下文上限仍只有窗口的 32%，且触发点已提前、目标水位更低

**现状（配置，未改）**

- `ctf-task-settings` v6 仍是 `context_threshold = 400000`；`ctf` v13 是 `128000`（团队模式不用它）。
- 现行算术（`input_token_limit`）：`min(400000, 1_000_000 − 393216) × 0.8 = 320000` → **1M 窗口只用了 32%**。
- 修复把**触发点提前到 90%**、**目标水位仍是 60%**，于是本次修复后又多出两个数字：

| 量 | 值 | 出处 |
|---|---:|---|
| 判死门限（硬上限） | 320000 | `input_token_limit(model, context_threshold)` |
| 压缩触发点 | **288000**（= 90%） | `prepare_provider_messages` 的 `estimated > int(self.input_limit * 0.9)` |
| 压缩目标水位 | **192000**（= 60%） | `target = fixed + int((input_limit − fixed) × 0.6)` |
| 显然仍在窗口内的富余 | 约 680k tokens | 1M 窗口 − 320k 上限 |

**影响**：修复解决了"贴线即死"，但没有解决"安全机制工作范围过窄"。压缩仍会频繁发生，而每次压缩都会 `epoch += 1`、推进 `excluded_before`、重写模型视图前缀 → 缓存前缀变化、模型往返与成本增加。run 4 是 8 次判死；若把上限提到窗口量级，同样的会话可以一次都不触发。

**这需要产品决策（三选一）**

1. **提高阈值到用满窗口**：把 `context_threshold` 设为约 **606784**（= 1M − 393216）→ 上限 485427。代价是每次调用的输入更长（因前缀稳定，多数应为 cache_hit，成本影响需按你的计费口径复核）。
2. **维持 320000，但把余量参数化**：把 `0.8`（输入余量）与 `0.9`（触发点）抽成配置/常量并写明理由，便于按成本策略调整。
3. **维持现状**：如果 32% 是刻意的成本闸门，则应把这条理由写进代码注释与 profile 描述，并接受较高的压缩频率。

**不要做的事**：不要为了让数字好看而调低 `ctf_max_output_tokens` 或绕过 `input_token_limit` 的硬门限——那条 0.8 余量是"provider 真实 usage 可能高于启发式估计"的唯一保护。

**验收**（选 1 或 2 后）

- 一次实跑中：同一回合的压缩次数显著下降（对照量：`agent_sessions.session.state.ctf_history_view.epoch` 的增量、`ctf_context_compaction` 出现次数）；
- 无新增的 provider 侧拒绝（context window 溢出类 4xx）；
- 用 `ctf_turns.usage` 的 `cache_hit_tokens + cache_miss_tokens` 对比改动前后单任务总输入，给出成本差异；
- 保留 `test_threshold_output_reserve_and_safety_margin_remain_hard_budget` 的语义（算术若变，测试要**按新口径重写**而不是删掉）。

---

### U3 【机制·中】"已延迟输入"没有边界化的终态出口（D5 的保留项）

**现状**

- 被延迟的消息现在**完全可见**：`_settle` 重排租约消息时发 `coordination.reminded`（`status="recovery_deferred"`，`ctf.py:1937`）；`_defer_inputs` 对 `turn_recovery` 消息同样发事件；idle 看门狗把"有延迟消息"写进给 Lead 的正文，并明说 **`Deferred work needs a relevance decision; no messages were released.`**
- 被阻塞成员现在可以收到**新**指令（`dispatch_message_condition` 放行 `kind == "instruction"` 或 `sender_kind == "user"` 且 id 不在 `input_message_ids` 的消息；`post_message` 会返回 `dispatch_status = "context_recovery_requested"`）。
- **但**：`deferred = true` 的消息不在任何领取查询里（`deferred.is_(False)` 是所有 claim 的前置条件），**没有任何自动释放路径**。成员若不自证"有新指令"，就停在 idle；这一步完全依赖 Lead 主动动作。

**需要决策的点**

1. **保持现状**（推荐）：不自动释放是正确的——避免静默重放"结果未知的工具调用"。但请把这一点写进 Lead 提示词（`ctf-task-settings` v7 时一并加），否则 Lead 看到"deferred"仍可能误判为"成员卡死"。
2. **加一条显式的释放入口**：让 Lead 用一个显式操作（带 `message_id` 与理由）释放指定延迟消息，释放动作发事件留痕。
3. **对 `turn_recovery` 类延迟消息做有界自动释放**：仅限 `notification_purpose == "turn_recovery"` 且非 `context_blocked` 来源的消息，在第 N 次提醒后释放一次并记录原因。**不要**对普通派工/工具结果类输入做这件事。

**待复核（本次未确认）**：`status="cancelled"` 的 `turn_recovery` 消息（run 4 里 `member-8` 那一例）现在是否会被重新投递？`_settle` 的重排循环只处理**租约内**消息，`_defer_inputs` 只处理 `queued` 消息，二者都不覆盖 `cancelled`。**请开发 Agent 先确认这条路径，再决定是否需要补。**

**验收**

- 构造"被阻塞 + 存在 deferred 的 `turn_recovery`"场景：断言（a）产生 `recovery_deferred` 事件；（b）Lead 收到包含该延迟消息 id 的提示；（c）若有释放入口，释放后该消息可被领取且事件留痕。

---

### U4 【策略·中】`context_blocked` 自动续跑仍然只有 1 次

**现状**：`_retry_limited_turn` 中 `limit = 1 if end_reason == "context_blocked" else 3`（未改）。这是**有意保留**——用尽后会通知 Lead（`*_recovery_exhausted`）+ 发事件 + WARNING，不再是静默。

**建议**：不要在本次改动里直接调大。**先测量**：

- 若修复后一轮任务里 `context_blocked` 仍 > 0：说明 D2 的搬迁修复还没覆盖到那类会话形态，应**先查 `branch`**（现在有了）而不是加次数；
- 若 `context_blocked` 归零，则 1 次足够，维持现状；
- 若出现"仅因 1 次用尽而停摆"的实例（事件里 `status="recovery_exhausted"` 且 `attempt=1`），再考虑提到 2–3 次以对齐 `provider_transient`。

**验收**：一条能覆盖"同一成员同一 run 内两次 `context_blocked`"的测试或实跑记录，证明要么获得第 2 次有界恢复，要么产生耗尽通知（两者现在都可观测）。

---

### U5 【残留数据·低】历史任务的旧残留不会被主动清理

**现状**

- 新增的 `clear_context_blocks`（`ctf.py`）会在**终态转移**（`finalize_close` 正常收尾路径）与**"已 closed 但仍有残留"的修复路径**上转存历史并清空 `context_blocked_members`；新 run 进入 `provisioning` 时也会清（`ctf_lifecycle.py:255-265`）。
- **但是**：已经处于终态的**历史任务**不会触发这些路径。例如第 1 轮 `d11f03ea` 至今仍残留 `tasks.ctf_control.context_blocked_members.lead`（含 `limit_chars: 102400` 这类旧字段名与 `null` 元素）。
- 净化逻辑只在**写入时**生效（`_settle` 落库前过滤非空字符串），因此**不会回填**已有数据。

**这道"残留"的害处有限**（旧任务不再运行），但会持续污染事后复盘（我上一份报告就被它误导过一次）。

**最小修法（可选）**：提供一次性修复脚本/命令：对 `status ∈ {finished, failed, stopped}` 且 `ctf_control ? 'context_blocked_members'` 的任务，调用 `clear_context_blocks` 落库。或至少在文档里写明"历史残留是已知现象，不代表当前代码行为"。

**验收**：`select count(*) from tasks where ctf_control ? 'context_blocked_members' and status in ('finished','failed','stopped')` = 0（或该差异被显式记录为不修）。

---

### U6 【观测·低】Lead 没有"全部阻塞成员"的聚合视图（可选改进）

**现状（已满足的部分）**：判死回执现在会把 `branch / eligible / cut / estimated / limit` 写进 `final_answer`，而 `_settle` 会把它作为 `turn_finished` 消息发给 Lead → **Lead 能直接看到单个成员为什么被判死**（这已解决我原报告里"回执固定文案误导诊断"的问题）。

**残留**：Lead 没有一个"当前有哪些成员处于 `context_blocked`、各自停在哪个分支"的聚合读取面；`context_blocked_history` 存在于任务控制里，但没有面向 Lead 的直读工具。

**建议**：低优先级。若要补，最省的做法是在给 Lead 的**恢复耗尽通知**正文里直接带上 `branch/eligible/cut`（现在只写了"inspect ... context diagnostics"），避免 Lead 再去翻记录。

---

## 2. 需要产品/你拍板的三项（不是缺陷）

| # | 问题 | 选项 | 影响面 |
|---|---|---|---|
| P1 | `context_threshold` 400000（→ 320000）是否就是期望值？ | 提到 ~606784 / 参数化余量 / 维持 32% 并写明理由 | U2。纯配置，无代码 |
| P2 | 被延迟输入是否允许**有界自动释放**？ | 保持仅人工决策（推荐）/ 加 Lead 显式释放入口 / 仅对 `turn_recovery` 有界自动释放 | U3 |
| P3 | `context_blocked` 恢复次数维持 1 还是 2–3？ | 先测量再定（推荐） | U4 |

---

## 3. 本次复核**没有**验证的部分（如实标注）

| # | 缺口 | 说明 |
|---|---|---|
| V1 | **无端到端实证** | 未发起真实 CTF 运行、未判分。"下次实战不再贴线判死"目前是**代码 + 测试**结论，不是线上实证。建议修复后跑一轮 canary，并复核：`end_reason='context_blocked'` 的回合数、`context_blocked_history` 是否为空、以及每回合 `ctf_history_view.epoch` 的增量 |
| V2 | 集成套件未跑全量 | 全仓集成用例 222 条，我跑了与本次修复相关的 **133 条 CTF 用例**（全部通过）；`real_vertical`、`vertical`、`access` 等需要完整靶机/镜像环境的用例**未跑** |
| V3 | 修复后的真实 per-call usage 未重新测量 | 原报告里"真实输入 62k–175k vs 门限 320k"是**修复前**的推算（当时 `usage_requests` 不存逐次 token）。现在 `ctf_context_blocked.last_provider_usage` / `ctf_last_provider_usage` 已落库，**跑一轮即可直接读出来**，建议作为 U2 决策的输入 |
| V4 | `turn_retries` 的"run 内重置"仍未完全解释 | 已定位"新 run 进入 provisioning 清空"（`ctf_lifecycle.py:255-265`），但第 3 轮 `cda295d2` 的 `member-1` 在**同一 run 内**两次判死而计数为 1，仍需要一个同 run 内的清理点来解释。现在有事件留痕（`status=recovery_requested/exhausted`），建议用事件流复核该样本而非只看计数 |

---

## 4. 边界（本次不要动）

1. **不改 MAF 版本/依赖**（`agent-framework-core==1.19.0` 固定）。
2. **不得静默丢弃输入**：`deferred` 不可自动消失、不可静默重放"结果未知的工具调用"——这是既有设计，U3 的任何方案都必须保留这条。
3. **不得放宽硬门限**来规避 U2（见 U2 的"不要做的事"）。
4. **不要为了改绿而改测试**：`test_threshold_output_reserve_and_safety_margin_remain_hard_budget`、`test_unknown_parallel_tool_group_remains_uncut_with_exact_failure_diagnostics`、`test_small_overflow_with_safe_boundary_accepts_suffix_above_low_watermark` 都是**事故回归**，口径变化时应重写断言而不是放宽。
5. 平台侧 profile 一律走 `ProfileStore.create` 发布，**不发裸 SQL**。

---

## 5. 给开发 Agent 的执行清单（按优先级）

1. **U1**：发布 `ctf-task-settings` v7（模板取 `ctf` v13，其余字段沿用 v6），并在发布脚本加"模板 md5 必须与 `ctf` 同批一致"的断言。
2. **U2 / P1**：拿到产品决策后改 `context_threshold`（或参数化 `0.8`/`0.9`），然后跑一轮实跑验证（验收见 U2）。
3. **V3**：在实跑里读 `last_provider_usage`，把"估计 vs 真实"的差值落成一组数字，作为 U2 的决策依据。
4. **U3 / P2**：先复核 `cancelled` 的 `turn_recovery` 是否会被重投；再按决策实现（若维持人工决策，则至少把"deferred 需 Lead 决策"写进 v7 提示词）。
5. **U4 / P3**：用一轮任务的事件流统计 `context_blocked` 与 `recovery_exhausted` 数量，再决定是否调整次数。
6. **U5**（可选）：一次性残留清理脚本。
7. **U6**（可选）：把 `branch/eligible/cut` 直接放进给 Lead 的耗尽通知正文。

---

## 附 A：本次复核的证据

### A.1 部署一致性（不是"只提交未发版"）

| 文件 | 容器内 sha256 | 工作树 sha256 | 一致 |
|---|---|---|---|
| `services/agent-runtime/src/bbx_runtime/ctf/middleware.py` | `42e5e9212c6e85fa44169666c15f3f2a624f1c8b6933b011e8d91afcabe03472` | 同左 | ✅ |
| `services/blackboard/src/bbx_blackboard/ctf.py` | `e34c5fa64b65192d75118f4912ac651441ef44bd1360298691a62d9cbd3d741b` | 同左 | ✅ |

容器 `bbx-ctf-team-mode-agent-runtime-1` / `bbx-ctf-team-mode-blackboard-1` 于复核前约 28 分钟重启。

### A.2 测试与静态检查

| 项 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `.venv/bin/pytest -m "not integration and not live" -q` | **1359 passed**, 232 deselected |
| CTF 单元测试 | `… -k ctf` | **392 passed** |
| CTF 集成测试 | `… -m "integration and not live" -k "ctf and not real_vertical and not vertical"` | **133 passed** |
| 诊断可读性（真实 HTTP） | `pytest services/agent-runtime/tests/test_ctf_context_observation_http_integration.py` | **1 passed** |
| lint | `make lint` | 283 files already formatted / All checks passed |
| 类型检查 | `make typecheck` | **0 errors, 0 warnings** |

### A.3 事故回归用例（`services/agent-runtime/tests/test_ctf_context_exhaustion.py`）

```python
@pytest.mark.parametrize("overflow", [770, 1206, 1438, 2402, 2913, 3131, 4110, 4150])
async def test_small_overflow_with_safe_boundary_accepts_suffix_above_low_watermark(overflow):
    ...
    projected = value.prepare_provider_messages([*view, fresh])
    assert fresh in projected
    assert value.input_limit * 0.6 < estimate_messages_tokens(projected, ...) <= value.input_limit
```

即：**这 8 个数字正是 run 4 那 8 条回合的超出量**（+770 ~ +4150 tokens），修复后必须能压过去而不是判死。同文件另有：

- `test_partial_compaction_below_limit_is_valid_without_reaching_low_watermark`（未达 60% 低水位也接受）
- `test_hard_overflow_loading_can_relocate_confirmed_text_assignment_without_losing_it`（受保护派工可搬迁且不丢失）
- `test_protected_first_assignment_moves_whole_to_recovery_and_survives_restart`（重启后仍成立）
- `test_blocked_leaf_explains_compaction_branch_and_records_safe_ids`（6 个 branch 逐个断言 + `caplog`）
- `test_unknown_parallel_tool_group_remains_uncut_with_exact_failure_diagnostics`
- `test_block_retains_latest_single_provider_usage_instead_of_turn_average`

### A.4 profile 对照（U1 的证据）

```
ctf                v13  2026-10-13 06:54:31   th=128000  max_steps=60    max_teammates=4   tmpl=c6a03a78… （新模板）
ctf                v12  2026-10-13 06:54:31   th=128000  …                tmpl=f887e852…  （新模板）
ctf                v11  2026-10-13 02:33:08   th=128000  …                tmpl=efef8cb3…  （旧模板）
ctf-task-settings  v6   2026-10-13 02:41:27   th=400000  max_steps=4000  max_teammates=12  tmpl=efef8cb3…  ← 全部任务在用，仍是旧模板
ctf-task-settings  v5   2026-10-13 00:56:53   th=400000  …                tmpl=efef8cb3…
```

> `tasks.agent_profile` 全为 `ctf-task-settings`（最近 8 个 CTF 任务无一例外），最新版本 v6。

### A.5 符号索引（按符号定位，避免行号漂移）

**运行时（`services/agent-runtime/src/bbx_runtime/ctf/`）**

- `CtfHistoryProvider.prepare_provider_messages`（90% 触发、判死入口）
- `CtfHistoryProvider._compact_confirmed_prefix`（诊断字典、`eligible`、`cut`、`target`）
- `CtfHistoryProvider._can_relocate_input` / `_recovered_inputs` / `_protected_input_ids`
- `CtfHistoryProvider.block_context`（WARNING、`ctf_context_blocked`、`last_provider_usage`）
- `CtfHistoryProvider.get_messages`（`unshrinkable_loaded_history`、`unsafe_loaded_boundary`、搬迁循环）
- `input_token_limit` / `estimate_messages_tokens`（`ctf/context.py`）
- `runner.py` 的 `TurnBoundary("context_blocked")` 分支（回执文案）

**平台（`services/blackboard/src/bbx_blackboard/`）**

- `ctf.clear_context_blocks` / `dispatch_message_condition`
- `ctf.Blackboard.post_message`（`dispatch_status`）
- `ctf.Blackboard._settle`（`context_blocked` 分支、`context_blocked_history`、租约重排）
- `ctf.Blackboard._retry_limited_turn`（`limit`、耗尽通知、事件、WARNING）
- `ctf.Blackboard.request_member_operation`（`stop`/`resume`/`remove`，**无**在办成果检查）
- `ctf.Blackboard.record_observation`（诊断作为有界引用持久化）
- `ctf.Blackboard._defer_inputs` / idle watchdog 分支
- `ctf_lifecycle.py` 的 `provisioning` 分支（清 `context_blocked_members`/`turn_retries`）
- `packages/contracts/src/bbx_contracts/ctf.py` 的 `CtfEvent.type`（已含 `ctf.coordination.reminded`）

---

## 附 B：一句话总结

修复方向正确、落到了实处（部署哈希一致、1359 单测 + 133 集成全绿、8 个事故溢出值有专门回归），**D1/D2/D4/D7/D8/D9 可以关闭**；剩下的主要是三类事：**一次 profile 发布（U1）**、**一个需要你拍板的阈值决策（U2/P1）**、**一条仍需决策的延迟输入出口（U3/P2，含 `cancelled` 重投路径待复核）**。
