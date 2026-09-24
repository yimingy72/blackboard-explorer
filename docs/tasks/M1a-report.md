# M1a 任务报告：黑板的存储与领域规则

## 完成了什么

| 编号 | 完成内容 |
|---|---|
| 1.1 | 建立 SQLAlchemy Core 表定义与异步 Alembic 迁移；包含设计 3.1 的全部表、`events(task_id, version)` 索引、`vector` 扩展和 512 维向量列。 |
| 1.2 | 实现任务内行锁、事件追加、统一投影器、事务内取号、提交后 NOTIFY 和按事件逐条重建折叠表的 `replay`。 |
| 1.3 | 实现事实与意图的引用、证据 URI、推断、resolves、satisfies、retry_of、holder、原子认领等校验；工具调用同作者匹配决定 provenance；dry run 返回包含已关闭意图在内的相似项前三名且不写入。 |
| 1.4 | 实现按事实创建顺序计算的完整争议递归、`relied_by`、争议深度及事实状态派生事件；争议链超过配置深度后不点名。 |
| 1.5 | 实现 `claim`、`claim_for`、带交接 note 的 `release` 和尝试次数达到上限后的 inconclusive 关闭；同一 Agent 同时至多持有一条意图。 |
| 1.6 | 实现验收状态初始化、裁定、终结报告与 finished 状态；被争议的支撑事实触发自动回退；维护两个版本字段及 `pending_claims`。 |
| 1.7 | 实现 Agent 登记、心跳记账、conclude、原子宽限扣减、finish 时自动释放及各类失败和空种子计数；实现 derive 回执事件与空推导计数清零。独立审查修正了收尾 conclude 不计 attempts、上限 conclude 后带未完成意图退出应计 attempts 的规则；失败、重启或无有效回执的 derive 不再误记为空结果。并拒绝已结束 Agent 再写事实、意图或工具调用。 |
| 1.8 | 实现 TaskSpec 校验、任务创建与状态迁移、工具调用记录；连续运行错误或两次空种子触发任务 failed。 |
| 1.9 | 实现调度聚合状态、按版本和点名过滤的事件查询、对象全文与一跳关联查询。 |

`Embedder.embed(texts)` 和 `ObjectStore.exists(uri)` 是注入接口。本任务测试分别使用固定 512 维字符 n-gram 假向量和内存 URI 集合；没有调用真实模型或对象存储。

## 如何验证

在仓库根目录运行：

```sh
uv sync
make check
make test-integration
.venv/bin/pytest -m 'not integration' services/blackboard/tests --cov=bbx_blackboard.domain --cov-report=term -q
```

独立复核结果：`make check` 中 Ruff、Pyright（0 错误）及普通测试 **110 passed**；`make test-integration` **6 passed**；领域层行覆盖率 **91%**。集成测试使用随机主机端口的 `pgvector/pgvector:pg16` testcontainer，实际执行 Alembic `upgrade head` 和 `downgrade base`，并验证重放逐字段一致、50 个并发认领恰有一个成功、并发取号连续、两种认领竞争及 NOTIFY 版本。测试容器已自动清理。检查未调用真实大模型 API。

若需在自行准备的空 PostgreSQL 库上单独执行迁移，可设置 `BBX_DATABASE_URL` 为 `postgresql+asyncpg://…` 连接串，再运行：

```sh
.venv/bin/alembic -c services/blackboard/alembic.ini upgrade head
.venv/bin/alembic -c services/blackboard/alembic.ini downgrade base
```

### BoardService 公开方法

以下签名省略 `self`；`tid` 均为任务 UUID。写方法返回事件列表时，列表项是已落库事件（含 version）。

| 签名 | 说明 |
|---|---|
| `BoardService(engine: AsyncEngine, embedder: Embedder, objects: ObjectStore)` | 注入数据库、向量器和对象存在性检查器。 |
| `create_task(spec: TaskSpec \| dict, *, profile_version: int = 1) -> UUID` | 校验规格并创建任务及验收初始状态。 |
| `transition(tid: UUID, status: str, *, actor: str = "scheduler", reason: str \| None = None) -> list[dict]` | 按生命周期迁移任务状态。 |
| `post_fact(tid: UUID, agent_id: str, request: PostFactRequest \| dict, *, dry_run: bool = False) -> dict` | 校验证据和引用，写事实或只返回相似事实。 |
| `post_intent(tid: UUID, agent_id: str, request: PostIntentRequest \| dict, *, dry_run: bool = False) -> dict` | 写意图，可在同一事务中认领；dry run 返回相似意图。 |
| `claim(tid: UUID, agent_id: str, intent_id: str) -> list[dict]` | Agent 认领 open 意图。 |
| `claim_for(tid: UUID, intent_id: str, agent_id: str) -> list[dict]` | 调度器为指定 Agent 预认领。 |
| `release(tid: UUID, agent_id: str, intent_id: str, note: str) -> list[dict]` | 持有者带交接说明释放意图。 |
| `system_close(tid: UUID, intent_id: str) -> list[dict]` | 系统关闭达到尝试上限的意图。 |
| `register_agent(tid: UUID, task_type: str, *, is_seed: bool = False, close_mode: str \| None = None) -> str` | 取 Agent 编号并登记运行。 |
| `heartbeat(tid: UUID, agent_id: str, *, steps: int, context_tokens: int, usage: Usage \| dict, last_seen_version: int, price: Price \| dict \| None = None) -> dict` | 累加本次步数与用量，记录最新上下文长度和版本，返回金额及未配置价格提示。 |
| `conclude(tid: UUID, agent_id: str, reason: str) -> list[dict]` | 点名请求 Agent 收尾并设置宽限额度。 |
| `take_grace(tid: UUID, agent_id: str) -> int` | 原子扣减一次宽限调用并返回本次扣减后的余额。 |
| `finish_agent(tid: UUID, agent_id: str, receipt: dict, end_reason: str) -> list[dict]` | 记录退出、释放持有意图并更新失败、种子和 derive 计数。 |
| `submit_close(tid: UUID, agent_id: str, request: SubmitCloseRequest \| dict, *, report_uri: str \| None = None) -> list[dict]` | 写验收裁定；终结模式验证报告 URI 并完成任务。 |
| `record_tool_call(tid: UUID, agent_id: str, call: dict) -> list[dict]` | 记录工具调用，供事实来源核验；结果摘要限制为前 4 KB。 |
| `state(tid: UUID) -> dict` | 返回任务、事实、意图、Agent、计数器与调度计算字段。 |
| `events(tid: UUID, since: int = 0, for_agent: str \| None = None) -> list[dict]` | 返回增量事件；指定 Agent 时只含广播和点名给它的事件。 |
| `get_object(tid: UUID, oid: str, depth: int = 1) -> dict` | 返回事实或意图全文及一跳关联。 |
| `replay(tid: UUID) -> None` | 在行锁下清空该任务折叠表并按事件重建。 |

## 偏差与待决

- **任务创建的引导写入**：`events.task_id` 外键要求任务行先存在。本实现先在同一事务插入任务行，再追加 `task.created` 并通过同一投影器应用；后续变更全部由事件投影。已在实现架构 3.1 说明唯一引导例外。
- **工具调用事件**：工具调用需参与 provenance 判定且重放后仍应存在。本实现追加 `tool_call.recorded` 事件与共享 EventType；已同步到实现架构 3.3。
- **证据持久化边界**：M1a 只通过注入的 `ObjectStore.exists` 检查 URI；执行环境文件上传、真实对象存储客户端和真实 embedding 属于 M1b。M1b 需在调用 `post_fact` 前完成上传，并向 `heartbeat` 传入对应 profile 的价格表；未提供价格时金额为 0，返回“价格未配置”。
- **相似度**：当前对已存 512 维向量做线性余弦相似度排序，只提供 top-3 提示；数据量增大时可改为 pgvector 索引检索，不影响公开方法。已同步到实现架构 4.3。
- **终态事件测试**：`finished`、`failed`、`stopped` 是互斥任务结局，无法在同一任务中构造全部终态；集成测试用不同任务分别验证这些事件的投影与重放。

共享文件修改清单：根 `pyproject.toml` 追加 testcontainers、pytest-cov 与 Pyright 虚拟环境定位；`uv.lock` 随依赖更新；`packages/contracts/src/bbx_contracts/models.py` 追加 `tool_call.recorded`；`packages/contracts/schemas/Event.json` 同步生成该枚举值。另按交接文档 6.1 更新 `docs/design/黑板系统实现架构.md` 的三处 M1a 决策并单独提交。工作树原有的 `AGENTS.md` 副本修改未提交。

## 对下一步的建议

M1b 可直接用上述方法接 HTTP 层和 SSE；创建任务时传 profile 版本，事实提交前持久化证据，心跳时传 profile 价格。保留当前的任务行锁和统一投影器作为写入入口，避免 HTTP 层直接更新折叠表。

提交划分：设计决策同步单独提交（`Document M1a event and similarity decisions`）；代码与本报告、`services/blackboard` 的新增及修改文件、上述四处共享文件作为第二个提交（`Implement M1a blackboard storage and domain rules`）。
