# Provider-failure-coalescing · 模型瞬时故障合并

来源：任务 `ae3919e0-ffca-47d0-954f-3dccb3a5c5f6`。四个并行 Agent 的模型请求各自耗尽 5 次 SDK 重试，在 13 秒内以 `connection` 失败；旧策略把前三个并发结果当成三次独立连续失败，直接终止任务。代理和 DeepSeek 随后恢复，余额、预算、上下文均不是终止原因。

## 完成标准

1. Runner 的 runtime_error 回执保留现有可读 `reason`，并增加脱敏 `error` 元数据；`transient` 只对 connection、timeout、rate_limit、conflict、server_error 为 true。非模型运行错误不伪装成供应商故障。
2. 同一任务中，120 秒窗口内完成的 transient 模型错误合并为一个 failure streak 增量。窗口以第一条失败为锚点，不被后续并发失败向后滑动；下一窗口再增加一次，连续三个窗口仍按原规则终止，防止无限运行。
3. transient 模型错误不增加 Intent attempts，不增加种子空产计数；相关方向保持可重派。400、认证、余额、内容审核及本地 runtime_error 继续逐次计数。
4. 任一正常 Agent 结束会清空 failure streak 和瞬时窗口；人工/预算/时间/完成裁定规则不变。任务进入终态后，在途 Agent 的后续正常交接不得把触发终止时的失败计数清零。
5. 数据库新增瞬时窗口种类与起点，事件 payload 保存已判定的 failure increment/window，保证 replay 与原运行一致；续跑清空窗口。历史任务兼容为空。
6. make check、make test-integration 全绿；脚本化并发场景覆盖四个同窗错误只计一次、跨窗累计、成功重置、永久错误仍快速失败、Intent/种子不被误罚、终态冻结和 replay。

## 边界

- 合并的是已经耗尽客户端重试的模型基础设施错误，不修改模型调用的 SDK 重试次数。
- 窗口固定 120 秒，不新增配置项；需要在真实运行中观察后再决定是否开放设置。
- 本任务不改变默认并发、提示词或任何领域策略，不自动续跑失败任务。
