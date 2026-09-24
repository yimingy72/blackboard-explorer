# M3a · 调度器、清扫与恢复报告

日期：2026-09-24。

## 完成内容与规则对应

| 任务 | 实现与验证 |
|---|---|
| S.1 | 7 类冻结 Action + 纯 decide(state, params, now)，37 项决策测试，覆盖率 100% |
| S.2 | ActionExecutor：登记、预认领、建用户、启动运行器、conclude、SystemClose、状态迁移；认领竞争结束失利 Agent，停止期间已登记但未启动的 Agent 正确 finish |
| S.3 | 每任务 SchedulerLoop，SSE 500ms 防抖 + 每 2 秒硬兜底、tick 串行；TaskSupervisor 全分页、FIFO、任务容量和后台清理 |
| S.4 | 每 30 秒 Sweeper：heartbeat/grace_timeout 取消，runner 透传固定原因且只 finish 一次；本地协程丢失时按黑板记录兜底 |
| S.5 | 重启结束旧运行记录、接管健康容器、保留尚未创建的排队任务、运行中丢容器明确失败；PG 单会话 advisory lock 与有界探测；失锁立即停止派发 |
| S.6 | 独立终态清理：补 conclude→等待原始宽限→超时取消→finish→归档上传→task.archived 事件持久化→销毁。失败保留容器重试，清理未完仍占容量 |
| S.7 | runtime Dockerfile、bbx-runtime serve、compose 控制/执行双网络及 Docker socket、构建目标、进程代理、120 秒优雅停机窗口 |

决策测试与设计逐条对应：

| 规则 | 测试内容 |
|---|---|
| 空黑板 | 单种子；未认领超 seed_max_steps conclude；曾认领后保持普通 explore 上限 |
| 意图派发 | unmet 优先再按 version；槽位上限；close 不占 explore/derive 槽；已达 attempts 上限的意图先 SystemClose，重读版本后再决策 |
| 限制 | explore 步数/上下文上限；金额探索份额、时间、derive 空产达到阈值进入 closing |
| 失败 | 连续 runtime_error、两次种子空产；重启中断不伪造成一次业务失败或空产 |
| 裁定 | 新 satisfies 触发；已有 judge 不重复；静默且黑板变化触发裁定 |
| derive | 没有 worker/开放意图、已裁定且无新变化时触发 |
| 收尾 | 全部 met 进入 closing；活动 Agent 未结束不建 final；无活动 Agent 建 final；未成功提交报告的 final 有效尝试达到 max_consecutive_failures 后 failed，runtime_restart 不计入 |
| 终态 | 不再决策；禁止迟到登记；同状态迁移幂等、跨终态拒绝 |

## 实际检查结果

- `uv sync` 成功，仅把已存在于锁文件中的 asyncpg 显式列为 runtime 依赖；没有新增 provider 或模型。
- `make check`：**293 passed、22 deselected**（4.75 秒），格式/Ruff/Pyright 全绿，无真实模型调用。
- 决策覆盖率命令：`.venv/bin/pytest -q services/agent-runtime/tests/test_scheduler_decision.py --cov=bbx_runtime.scheduler.decision --cov-report=term-missing --cov-fail-under=95`：**37 passed，81/81 行，100%**。
- `make test-integration`：**18 passed、297 deselected**（39.39 秒），全部通过、无跳过；执行环境、代理、runtime 镜像构建通过。
- 新集成片段使用真实黑板/PG/MinIO + FakeEnvd + ScriptedChatClient：种子提出 3 个意图、3 个 explore 同时在途、每意图唯一 holder、峰值不超槽、用量累计；运行中重启后 I4 attempts 保持 0，被新监管器派发并关闭。附带 judge/derive 没有意外 runtime_error。
- PG 集成验证第二实例不能拿锁、强制断开首个数据库会话后 lost 通知、替代实例可获锁。真实 runtime 镜像用无模型任务的测试服务启动，拒绝重复实例，SIGTERM 退出码 0、锁已释放。
- 归档 store 集成验证事件投影/replay；重启计数 store/replay 验证保留失败连击、不增加种子空产和 attempts。
- `pnpm --dir web check`：ESLint、TypeScript、**11 项测试通过**；`pnpm --dir web build` 成功。保留此前 ELK/画布懒加载 chunk 大小告警。
- `docker compose --env-file .env.example config --quiet` 通过，只加载占位配置，未读取真实 `.env`。
- `make image-blackboard` 构建成功，包含新归档 API 与本轮前端产物；测试容器/网络已清理。

本任务没有调用真实 DeepSeek；M2b 的真实种子检查结果仍见前序报告。

## 接口、部署与复现

- `bbx-runtime serve` 运行单实例服务；独立 PG 连接只获取 advisory lock/探测，不写领域表。
- `TaskSupervisor(settings, service, manager, runner)` 提供 run/stop/tick/recover；`SchedulerLoop` 提供 run/tick/request_stop/stop；`ActionExecutor` 提供 execute/cancel/shutdown。
- `POST /api/tasks/{id}/archive`：服务身份，body 为 `{uri, size, fallback}`，仅终态且无活动 Agent；uri 必须是 `workspace/{id}.tar.zst`，对象必须存在，fallback 为 none/agents-only。相同 URI 幂等，事件更新 workspace_uri。
- BlackboardClient 新增 record_archive、完整分页 list_tasks 和完整事件 dict 的 stream；ExecEnvManager 暴露 wait_healthy 用于恢复。
- 单 Agent CLI 仍可独立调试，但不能与同一任务的 serve 同时运行。

命令及代理配置见 `services/agent-runtime/README.md`。在设置 AGENTS 第 6 节代理/UV_CACHE_DIR 和 envd README 的 DOCKER_BUILD_ARGS 后，执行 `make check`、`make test-integration` 即可复现。部署在主工作树执行 `make image-agent-runtime` 和 `docker compose --project-name blackboard-explorer up -d`；自定义项目名要同步 EXEC_NETWORK，避免连接错误的网络。

## 共享文件与设计同步

- runtime pyproject / uv.lock：显式 asyncpg；Makefile 新镜像目标并加入集成前置。
- compose / .env.example：新增 runtime 服务与独立代理变量，不把 AGENT_TOKEN_SECRET 传给 runtime，不把控制密钥传给 exec-env。
- contracts EventType / Event.json：新增 task.archived；无数据库迁移（workspace_uri 原已存在）。
- blackboard 领域/投影/服务/API/OpenAPI：归档、终态守护、恢复计数修复。
- 前端仅同步归档命名事件、reducer、API 类型和已有下载按钮的数据来源，不提前实现 M4 界面。
- runtime 集成依赖 fixture 从原单测试文件提取到 tests/conftest.py，供场景重用。
- 设计均先独立提交：e144c86、06ceee4、1867ff7、db5a0f6、65a9aae、fe54cee；分别明确恢复/归档、final 重试边界、清理容量、终态不可重新派发、种子首次认领后的上限，以及重启中断计数。

## 偏差与待决

1. v1 采用原设计允许的部署级全局 EGRESS_ALLOWLIST，任务字段仅记录需求，不动态生效/收窄策略；README 已明确，M4 创建页需同样说明。
2. 归档 size/fallback 由可信 runtime 上报；黑板验证对象存在但不另取存储元数据核对。容器已丢失时无法补造归档，会明确记录错误，保留已持久化的证据。
3. S3 片段为避免 httpx ASGITransport 对无限 SSE 缓存，用真实 events API 短轮询作测试通知；生产使用 SSE，真实 SSE 传输由既有 API 集成测试守护，去抖/洪流/重连由循环普通测试守护。
4. 单实例锁增加有界探测与失锁停止，仍依赖 PG 会话锁；不是跨区域共识系统。v1 不支持多个调度实例同时运行。
5. M3b 需验证 13 个完整场景、真实 e2e，以及任务时间上限和交接宽限与 M2b 外层 run 超时的配合，避免到探索时间上限就提前切断交接。

## 建议提交划分

1. `Persist workspace archives and preserve restart counters`：contracts、blackboard、API 类型与前端归档接线。
2. `Schedule and recover isolated agent tasks`：runtime 调度、控制服务、镜像/compose、依赖、测试、README 和报告。

设计已单独提交。合并后更新 HANDOFF 并进入 M3b；M3b 合并后才打 m3 标签。本任务无需要用户补跑的开发检查。
