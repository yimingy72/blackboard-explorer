# 任务 M3a · 调度器、清扫与恢复

依据：`docs/design/黑板式探索架构设计.md` 第 5 节（调度规则全部）、第 9 节（参数）；`docs/design/黑板系统实现架构.md` 第 5 节（agent-runtime：进程结构、决策伪代码、spawn、Sweeper、任务结束、重启恢复）；`docs/design/黑板系统开发方案.md` M3 的 3.1–3.3、3.7–3.11。

前置：M2b 已合并。先阅读 `AGENTS.md`。

## 范围

`services/agent-runtime/src/bbx_runtime/scheduler/`：决策纯函数、动作执行器、任务监管、调度循环、清扫器、重启恢复、失败保护、归档与销毁；为归档补齐 blackboard 的事件、投影、服务身份 API 与运行客户端；agent-runtime 的 Dockerfile 与 compose 服务。**不做** derive / close 的触发之外的内容——触发条件在本任务的 `decide()` 中实现，但 close 的收尾流程与完整场景测试属于 M3b。

## 已定的实现决定

1. **决策与执行分离**：`decide(state, params, now) -> list[Action]` 是纯函数，实现实现架构 5.2 的伪代码（失败判定 → 上限 conclude → 意图重试上限 → 收尾判定 → 裁定触发 → 种子 → derive → 为 open 意图派发 explore）。`Action` 为冻结的 dataclass：`SpawnExplore(intent_id | None, seed)`、`SpawnDerive()`、`SpawnClose(mode)`、`Conclude(agent_id, reason)`、`SystemClose(intent_id)`、`EnterClosing(reason)`、`Fail(reason)`。执行器负责把动作变成黑板调用与 Agent 运行。
2. **并发槽位**：只统计运行中的 explore 与 derive；close 不占名额；上限取任务 `budget.max_concurrent_agents`。
3. **预算**：探索预算 = `max_cost × (1 − close_reserve_ratio)`，另有 `max_minutes`；用量取黑板上的累计金额。价格表未配置时金额为 0，此时只按时间约束，并在日志中警告。
4. **调度循环**：每个任务一个 `SchedulerLoop`；订阅该任务的 SSE 事件唤醒（500ms 去抖），并每 2 秒兜底执行一次；每任务一把 `asyncio.Lock` 保证 tick 串行。
5. **任务监管**：轮询（每 2 秒）或订阅任务状态变化；`provisioning` 的任务在运行数未达 `MAX_RUNNING_TASKS` 时 `ExecEnvManager.provision` → 置 `running` → 启动循环；达到上限的任务保持 `provisioning` 排队。`finished` / `failed` / `stopped` → 归档上传 → 销毁执行环境。
6. **清扫器**（每 30 秒）：心跳超时（`heartbeat_timeout`）→ 取消该 Agent 的协程，finish（end_reason=heartbeat）；宽限超时（`grace_timeout`）→ 取消，finish（end_reason=grace_timeout，黑板写系统交接说明）；运行超过 `max_minutes` → 标记探索预算用尽。
7. **重启恢复**：启动时把所有 running / concluding 的 agent 运行 finish 为 `runtime_restart`（不计 attempts）；对 provisioning 无容器的任务保留排队，有容器则健康检查后接管；running / closing 无容器或不健康则 failed（原因写明）。单实例保障：启动时取 Postgres advisory lock，拿不到就退出。
8. **部署**：`services/agent-runtime/Dockerfile`（`python:3.12-slim`）；`docker-compose.yml` 追加 `agent-runtime` 服务，接入 `internal` 与 `exec` 网络，挂载 `/var/run/docker.sock`；`make image-agent-runtime`（镜像由用户在沙箱外构建）。

## 任务

| # | 内容 |
|---|---|
| S.1 | `Action` 与 `decide()`，表驱动单元测试覆盖设计文档第 5 节每条规则（至少：空黑板建种子、种子未认领超步数 conclude、按优先级为 open 意图派发、槽位上限、close 不占槽、上限 conclude、意图 attempts 上限、连续失败、种子两次空产、有 satisfies 声明触发裁定、裁定进行中不重复、探索停下来且黑板有变化触发裁定、已裁定且无变化时触发 derive、derive 空计数达上限、全部 met 进入收尾、预算用尽 / 人工停止进入终结收尾） |
| S.2 | 动作执行器（登记 agent、预认领、启动运行器、conclude、system_close、状态迁移） |
| S.3 | 调度循环、任务监管（含排队） |
| S.4 | 清扫器 |
| S.5 | 重启恢复与单实例锁 |
| S.6 | 任务结束：归档上传与销毁 |
| S.7 | Dockerfile、compose 服务、Makefile 目标 |

## 测试

- 普通测试：`decide()` 表驱动（覆盖率 ≥ 95%）；执行器对假黑板客户端的调用序列；清扫器时间判断（注入时钟）。
- 集成测试：真实 blackboard（testcontainers）+ FakeEnvd + ScriptedChatClient，跑"种子 → 提出 3 个意图 → 3 个并发 explore → 各自关闭意图"的片段，验证无重复认领、槽位不超限、心跳与用量累计正确；重启恢复（运行中停止调度器再启动，意图被重新派发，attempts 不变）。

## 完成标准

1. `make check`、`make test-integration` 全绿（报告中给出结果）。
2. 报告 `docs/tasks/M3a-report.md`：`decide()` 规则与测试用例对照表、动作清单、部署说明、共享文件修改、建议提交划分、偏差与待决。

## M3a 接口补充

按实现架构 5.5，归档采用 POST /api/tasks/{id}/archive 与 task.archived 事件，上传并登记成功后才销毁。runtime 的 PG 会话仅做单实例锁；任务列表遍历所有分页。v1 明确使用部署级全局出网白名单，任务字段不动态生效。进程停止传 runtime_restart 取消原因，Sweeper 传 heartbeat/grace_timeout，finish 仅由运行器写一次。归档事件需同步现有前端命名订阅、状态投影和下载入口的数据来源，不扩展 M4 界面。
