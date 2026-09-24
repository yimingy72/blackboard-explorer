# 任务 M3b · 裁定、收尾与完整闭环

依据：`docs/design/黑板式探索架构设计.md` 第 5.4–5.6 节（derive、裁定、收尾与失败）、第 7 节（运行流程示例）、8.2–8.3（derive、close 提示词）；`docs/design/黑板系统实现架构.md` 第 3.4 节（验收状态）、5.2（`enter_closing` / `drive_closing`）；`docs/design/黑板系统开发方案.md` M3 的 3.4–3.6、3.12–3.13 与场景测试表。

前置：M3a 已合并。先阅读 `AGENTS.md`。

## 范围

derive 与 close 在调度中的完整流程：derive 回执与空计数、裁定（judge）与终结（final）两种 close 的运行、裁定反馈推送、成功收尾与终结收尾、报告存储；开发方案 M3 的 13 个场景测试；端到端命令。

## 已定的实现决定

1. **收尾流程**按实现架构 5.2 的 `enter_closing` / `drive_closing`：进入 closing → 向所有运行中 Agent 发 conclude（reason=closing，不计 attempts）→ 等它们结束（Sweeper 负责宽限超时）→ 启动终结 close → `submit_close` 后黑板置 finished。
2. **第二次 derive** 的附加输入：上一次 derive 回执中的 `excluded`，并要求检查 inconclusive 意图（`retry_of`）、disputed 事实、最近一次裁定的 `missing`（模板已在 M2b 完成，本任务负责把这些数据传进渲染上下文）。
3. **裁定反馈**：`acceptance.judged` 事件由 M2b 的 BoardSync 以全文推送给运行中的 explore——本任务验证端到端生效。
4. **场景测试**使用 ScriptedChatClient + FakeEnvd + 真实 blackboard（testcontainers），每个场景一个脚本化剧本；标记 `integration`。
5. **端到端**：`make e2e`（不属于 `make check` / `make test-integration`）——用 compose 起全栈，创建玩具任务，等待 finished，打印报告与花费。需要 `DEEPSEEK_API_KEY`，由用户运行。

## 任务

| # | 内容 |
|---|---|
| C.1 | derive 在调度中的运行与回执处理；第二次 derive 的附加输入 |
| C.2 | close 的两种模式在调度中的运行；终结模式的报告上传 |
| C.3 | 收尾：成功收尾与终结收尾两条路径 |
| C.4 | 13 个场景测试（开发方案 M3 场景表，逐条对应）：顺利完成、裁定不通过、derive 两次为空、有毒意图、模型不可用、种子空产、认领竞争、宽限超时、预算用尽、支撑事实被争议、裁定期间有新声明、重启恢复、人工停止 |
| C.5 | `make e2e` |

## 完成标准

1. `make check`、`make test-integration` 全绿，13 个场景全部通过（报告中逐条列出）。
2. 用户可执行的检查点：`make e2e` 在玩具任务上由 DeepSeek 跑通完整闭环，产出报告与归档；工作台（M1-W）上能看到全过程。
3. 报告 `docs/tasks/M3b-report.md`：场景与设计规则的对照、e2e 用法、共享文件修改、建议提交划分、偏差与待决。
