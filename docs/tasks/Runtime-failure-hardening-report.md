# Runtime-failure-hardening · 运行失败收敛报告

日期：2026-09-28。问题样本：任务 `425ceb85-9ece-4b22-82fc-ab0d338d1e5e`。

## 失败诊断

任务实际运行 62 分 43 秒，在失败事件前估算费用 ¥18.67641896 / ¥20，均未达到 90 分钟或金额硬上限。黑板已有 70 条 Fact、39 条 Intent，3 个不同 flag 结果及 3 个开放方向，但没有 Fact 声明 `satisfies`，因此没有启动 Close、验收仍为 unmet、没有最终报告。

直接终止链路为三个不同 Agent 在 32 秒内连续产生 `ChatClientException`：derive round 18、两个 Explore 依次形成 failure streak 1/2/3，第三次同时触发 `task.failed`。失败后同一模型配置仍有请求成功，当前账户余额可用，三个上下文规模差异也很大；现有日志丢失 HTTP 状态及请求 ID，不能再区分单请求 429、5xx、连接故障或 400。官方状态页没有该分钟的全局事故记录，但不足以排除局部瞬时问题。

另有 13 次确定的平台缺陷：13 个 `execute_command` 对象记录包含真实 NUL，对应 13 个 Agent 的 Session PUT 各返回两次 PostgreSQL `UntranslatableCharacterError`。对象存储能用 JSON 转义保留全文，MAF Session 中的真实 U+0000 则无法进入 PostgreSQL JSONB。derive round 2–18 另有 17 次 initial_context trace 被旧“每 Agent 唯一”规则拒绝；trace 失败被隔离，没有导致 Agent 结束。

## 完成内容

1. 新增跨服务 `storage_safe`：进入 PostgreSQL Event、Session 和对话文本边界时，将真实 U+0000 转写为可见的 `\u0000`。嵌套字典、列表、元组均复制处理，输入对象不原地修改；对象存储中的完整工具记录保持不变。客户端保存前也使用相同变换，使响应丢失后的 CAS 读回比较一致。
2. DeepSeek/OpenAI/OpenAI兼容/Azure OpenAI 连接器继续使用 OpenAI SDK 自带退避，将 `max_retries` 从 2 提高到 4。重试范围由 SDK 决定：连接错误、408/409/429、5xx；永久的 400、认证、余额和内容审核不额外重试。没有应用层循环，不会重放已执行工具。
3. 最终模型失败增加 `model_error` trace，保存固定类别、HTTP 状态、供应商码、请求 ID、Retry-After、耗时、消息数、文本字符估算和最多尝试数。所有标识受格式/长度限制，不保存异常原文、提示词、响应或密钥；Agent 回执显示类别，非模型运行错误仍保留原异常类型。
4. derive 的 initial_context 按 `(agent_id, derive_round)` 唯一，payload 保存轮次；旧无轮次记录视为第 1 轮，非 derive 仍保持每 Agent 一条。工作台显示“第 N 轮”以及“模型请求失败”，同一轮的多个错误 trace 均保留。

## 实际验证

- `make check`：ruff、pyright 与 570 项普通测试通过；不调用真实模型。
- `make test-integration`：62 项通过，包含 PostgreSQL NUL Session 写入、derive 分轮 trace、既有调度/闭环/归档恢复。
- SDK 离线重试回归：前四次 HTTP 503、第五次恢复，随后工具只执行一次并正常完成第二次模型调用。
- `pnpm --dir web check`：ESLint、TypeScript 与 33 项前端测试通过；生产构建成功。
- Playwright：18 项全部通过，既有工作台、对话、配置、续跑与窄屏流程无回归。

## 偏差与边界

- SDK 重试无法消除“服务端完成但响应在途中丢失”造成的重复模型费用风险；供应商账单仍是实扣正本。
- 四次重试耗尽仍形成一次 runtime_error，并继续进入既有连续失败保护，避免服务长期不可用时无限运行。
- 本轮没有改提示词、并发默认值、完成条件或任何任务专属策略，也没有调用真实模型或自动续跑失败任务。

## 本地部署

- 已合并并重建 blackboard/runtime 镜像；两容器健康、重启次数均为 0。部署前数据库备份保存在 `.data/backups/pre-runtime-failure-hardening.dump`。
- 部署烟测通过：临时任务经真实 HTTP API 保存含 U+0000 的嵌套 MAF Session，读回内容为可见 `\u0000`；临时任务及对象随后完整清理。线上前端与本地生产构建哈希一致。
- 原失败任务 `425ceb85-9ece-4b22-82fc-ab0d338d1e5e`、70 条 Fact、39 条 Intent、Agent Session 和工作区归档保持不变；未自动续跑、未产生模型费用。

## 提交划分

设计合同独立提交；存储安全、重试与错误 trace 作为一个原子后端提交；前端 trace 显示、报告和文档作为交付提交。部署前备份数据库；部署后保留现有任务及归档，由用户决定是否追加预算续跑。
