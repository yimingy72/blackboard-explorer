# Explore 失败后续跑报告

日期：2026-10-17。触发问题：`0ef17097-b431-49cf-b431-46d94c67f2e6` 中 agent-65 在 30 分钟心跳保护后以 `费用登记待恢复` 结束，释放的 Intent 已达到尝试上限并被系统关闭，看起来像探索被 Agent 失败直接切断。

## 原行为

- `finish_agent(runtime_error)` 会释放 Explore 持有的 Intent，并删除 Agent 的内存运行协程。
- 调度器随后会为 open Intent 派发新 Explore；因此方向本身通常能继续，但新 Agent 没有原 Session 和上下文。
- 如果 `counted=true`，每次失败会增加 Intent attempts；达到上限后 Intent 被系统以 `inconclusive` 关闭，调度器不再派发。
- Derive 已有同 Agent 复用，Explore 没有；这是两条角色路径行为不一致的根源。

## 修复

1. 调度器登记 Explore 时携带目标 `intent_id`。黑板在 open Intent 的释放 notes 中找到最近失败的原 Agent，并产生 `agent.reactivated`，复用原 Agent ID、Session、步数、上下文、用量和累计贡献。
2. 没有匹配失败记录时仍创建新 Agent；不同 Intent 不会被错误绑定到同一失败 Agent。
3. Reactivation 清空 Agent 行上的终态 receipt，把上次 receipt 保存在 `previous_receipt`，并清除 Session 中旧的 `bbx_final_receipt`，避免旧失败证明覆盖新运行。
4. 复用运行追加唯一的 `bbx-explore-resume-*` 消息，明确从最后已确认工具结果继续，禁止重复执行已执行或结果未知的命令。
5. `费用登记待恢复` 现在生成结构化 `usage_settlement` 暂时错误，归入既有 120 秒故障窗口；不增加 Intent attempts 或种子空产。普通无效请求、内容审核和其他本地错误仍按原规则计数。
6. 达到 Intent 尝试上限或任务连续失败阈值时仍停止，防止同一 Session 无限重试。

## 验证

- 领域测试：失败 Explore 按 Intent note 复用同一 Agent；其他 Intent 仍新建 Agent。
- PG 集成：失败、Intent 释放、同 Agent reactivation、重新 claim、receipt/previous_receipt 和 replay 一致。
- 调度级集成：同一个 Explore Agent 连续失败三次后仍触发任务失败保护；事件中有两次以上 `agent.reactivated`。
- `make check`：2158 项通过，355 项排除。
- `make test-integration`：351 项通过，2162 项排除，548.19 秒；包含失败 Explore 复用和 usage-settlement 场景。

## 边界

- Intent 已被系统关闭后不会自动重开；修复只保证仍处于 open 的失败方向复用原 Agent。
- 同一个 Agent Session 长期增长仍受上下文阈值控制；超过上限由既有分段/收尾规则处理。
- reactivation 不重放已执行工具；未知副作用仍依赖单次执行日志和 Session 中的已确认结果。

## 公网部署

- 代码 `a4bb552`、文档 `755d795` 已合并并推送 `main`。
- 更新前确认无运行中任务，数据库备份到服务器 root 私有目录 `update-a4bb552-20261017T120633Z/database.dump`。
- `bbx-blackboard:a4bb552` 与 `bbx-agent-runtime:a4bb552` 已传输到 `101.201.116.63`，release 切换到 `755d795`；只重建两个应用容器，Postgres、MinIO 和其他项目未动。
- 容器内 runner/rules SHA-256 与提交逐项一致；两服务 healthy，`/login` 返回 200，部署前后无活动任务被中断。
