# 任务 M1a · 黑板的存储与领域规则

依据：`docs/design/黑板式探索架构设计.md` 第 3、5、6 节；`docs/design/黑板系统实现架构.md` 第 3 节（数据模型）、4.3（事实写入流水线）、4.4（快照）、5.2–5.3（调度器依赖的状态与 finish 规则）；`docs/design/黑板系统开发方案.md` M1 的 1.1–1.7 与检查点。

> 2026-09-24 后续设计调整：Agent 根据同步黑板自行判断重复。下文 Embedder、512 维向量与 dry_run 相似度为原任务历史要求，已由 M1b 的无向量实现与迁移取代。

先阅读 `AGENTS.md`（尤其第 7、8 节：你不做 git 写操作；有其他 Codex 并行开发）。

## 范围

只做 `services/blackboard` 的**存储层与领域层**，以及它们的测试。**不做 HTTP 接口、SSE、鉴权、MinIO 客户端、真实 embedding、快照 YAML 输出、模拟器**——这些属于 M1b。可以在 `packages/contracts` 中追加事件载荷等共享模型（追加式，报告中列出）。

## 已定的实现决定

1. **技术**：SQLAlchemy 2.x Core（async，asyncpg 驱动）+ Alembic（async env）。不用 ORM 映射类。
2. **分层**（`services/blackboard/src/bbx_blackboard/`）：
   - `domain/`：纯函数，不接触数据库。输入是某任务的内存状态 `BoardState`（事实、意图、Agent 运行、验收状态、计数器）和一个命令；输出是要追加的事件列表，或抛出带错误码的 `RuleViolation`（错误码稳定、消息是给模型读的中文说明，写清"错在哪、怎么改"）。争议状态、relied_by、dispute_depth、pending_claims、黑板是否为空等**计算字段**也在这里。
   - `store/`：事务、加载 `BoardState`、追加事件、**投影器** `apply(event) → 折叠表更新`。实时写入与重放必须走同一个投影器。
   - `service.py`：`BoardService`，把"开事务 → 锁任务行 → 加载状态 → 领域规则 → 追加事件并投影 → 提交 → NOTIFY"串起来。
3. **并发**：每个写操作的事务开头 `SELECT … FROM tasks WHERE id = :task_id FOR UPDATE`，同一任务的写入串行化。认领、取号、验收都依赖这一点，不需要其他锁。
4. **对象编号**：F1、I1、agent-1 … 由 `task_counters` 在同一事务内分配。
5. **依赖注入的接口**（M1b 提供真实实现）：
   - `Embedder`：`async embed(texts) -> list[list[float]]`；测试用确定性的假实现（例如按字符 n-gram 哈希得到固定维度向量，维度 512）。
   - `ObjectStore`：`async exists(uri) -> bool`；测试用内存假实现。
6. **NOTIFY**：提交后 `NOTIFY bbx_task_<task_id 去掉连字符>, '<version>'`（M1b 的 SSE 会监听）。

## 任务

| # | 内容 |
|---|---|
| 1.1 | Alembic 迁移：实现架构 3.1 的全部表与索引；`CREATE EXTENSION vector`；向量维度 512 |
| 1.2 | 事件存储：追加事件、投影器、取号、NOTIFY；`replay(task_id)` 能清空该任务的折叠表并按事件重建 |
| 1.3 | 写入规则：设计文档 3.5 全部规则；事实（含证据 uri 存在性校验、provenance 计算：证据 call_id 在 `tool_calls` 中且属于同一 agent 即 tool_backed）、意图（含 retry_of 规则、`claim=true` 原子认领）、争议（含撤回自己的事实）、resolves、satisfies 引用校验；`dry_run` 时返回最相似的已有项（事实 top-3；意图 top-3，含已关闭）而不写入 |
| 1.4 | 计算字段：争议状态完整递归（设计文档 3.2）、relied_by、dispute_depth；`fact.disputed` / `fact.undisputed` 派生事件，点名深度 ≤ `dispute_notify_depth` 时写 `addressed_to=[被争议事实的作者]` |
| 1.5 | 认领：`claim`、`claim_for`（调度器预认领）、`release`（写 note）、`system_close`（attempts 上限，关闭为 inconclusive）；resolves 事实关闭意图并清 holder |
| 1.6 | 验收（实现架构 3.4）：`acceptance_state`；`submit_close`（judge 只更新状态；final 写报告 uri 并置任务 finished）；met 项的支撑事实变为 disputed 时回到 unmet 并写 `acceptance.reverted`；维护 `last_change_version`、`last_judgment_version`（取 close 运行的 `judge_from_version`）；`pending_claims` 计算 |
| 1.7 | Agent 生命周期：`register_agent`（取号、种子标记、close 模式与 judge_from_version）、`heartbeat`（steps、context_tokens、usage 累加到 agent 与任务、last_seen_version、last_heartbeat_at）、`conclude`（点名事件、置 concluding、grace_calls_left）、`take_grace`（原子扣减，用尽返回失败）、`finish_agent`（实现架构 5.3 的规则：释放持有的意图与 attempts 计数、failure_streak、seed_empty_count、derive 回执 → `derive.result` 与 derive_empty_streak；黑板有变化时清零 derive_empty_streak） |
| 1.8 | 任务：`create_task`（校验 TaskSpec、初始化 acceptance_state）、状态迁移（created → provisioning → running → closing → finished；任何状态 → failed / stopped；非法迁移拒绝）、`record_tool_call` |
| 1.9 | 查询：`state(task_id)`（调度用聚合视图：任务、预算与用量、验收、意图、agents、计数器、pending_claims、board_empty、last_change_version、last_judgment_version）、`events(task_id, since, for_agent)`（广播 + 点名给该 agent）、`get_object(task_id, id, depth=1)`（对象全文 + 一跳关联） |

金额换算：`heartbeat` 收到的 usage 已包含 token 数；把 profile 价格表换算成金额的函数写成纯函数放在 `domain/`，价格表未填时金额记为 0 并在返回值中标明"价格未配置"。

## 测试

- **领域层表驱动测试**（普通测试）：设计文档 3.5 的每条规则至少一个"通过"、一个"拒绝"样例；争议递归（含撤回、四层来回、点名深度）；验收回退；finish_agent 各计数规则；derive_empty_streak 清零；pending_claims；状态迁移。
- **集成测试**（`@pytest.mark.integration`，testcontainers，镜像 `pgvector/pgvector:pg16`，迁移用 Alembic 真实执行）：
  - 重放：一个任务写入一串覆盖所有事件类型的操作后，清空折叠表按事件重放，结果与原折叠表逐字段一致
  - 并发认领：50 个并发 `claim` 同一意图，恰好 1 个成功
  - 竞争：`post_intent(claim=true)` 与 `claim_for` 并发，无重复持有
  - 取号：并发写入时编号连续、不重复
  - NOTIFY：提交后监听端收到版本号
- 覆盖要求：`domain/` 行覆盖率 ≥ 90%（在报告中给出数字）。

## 完成标准

1. `make check` 全绿（不需要 Docker）。
2. `make test-integration` 全绿（在报告中给出结果）。
3. `alembic upgrade head` 与 `alembic downgrade base` 都能在空库上执行。
4. 报告写到 `docs/tasks/M1a-report.md`，包括：`BoardService` 的公开方法清单（签名 + 一句话说明），供 M1b 做 HTTP 层时使用；对共享文件的修改清单；建议的提交划分；偏差与待决。
