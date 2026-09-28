# Runtime-failure-hardening · 运行失败收敛

来源：任务 `425ceb85-9ece-4b22-82fc-ab0d338d1e5e`。该任务因三个模型请求在 32 秒内连续失败而终止；另有 13 个 Explore 因命令输出中的 NUL 字符无法写入 PostgreSQL JSONB 而结束，复用的 derive 在第 2–18 轮丢失了初始上下文 trace。

## 完成标准

1. 工具调用全文在对象存储中保持可追溯；进入 PostgreSQL 文本/JSONB 与持久 Session 前，将 U+0000 转写为可见的 `\\u0000`，不让二进制命令输出终止 Agent。转换覆盖嵌套 Session、工具摘要和 trace 摘要，其他文本不变。
2. OpenAI 兼容连接器使用 SDK 自带的标准重试语义，将瞬时连接错误、408/409/429 与 5xx 的最大重试次数从 2 提高到 4；400、401/403、402 和内容审核不额外重试。不得在应用层重放工具调用。
3. 模型请求最终失败时，保存一条 `model_error` trace。只记录脱敏诊断：错误类别、HTTP 状态、供应商错误码、请求 ID、Retry-After、请求耗时、消息数及文本字符估算；不保存提示词、响应正文、凭据或异常原文。Agent 回执使用错误类别，不再只显示 `ChatClientException`。
4. derive 的 `initial_context` trace 按 `(agent_id, derive_round)` 唯一；同一轮幂等，后续轮次均可记录。非 derive 保持每 Agent 一条。trace payload 保存轮次，前端可显示“模型请求失败”。
5. make check、make test-integration、前端检查/构建和浏览器测试全绿；普通测试只用假客户端，不访问模型。新增回归覆盖 NUL 嵌套数据、瞬时/永久模型错误分类与重试配置、derive 多轮 trace。

## 边界

- SDK 重试仍可能在“服务端已完成、响应丢失”时产生重复模型费用；账本只记录收到响应的调用，供应商账单仍是实扣正本。
- 最终失败是否累计到任务的连续错误保护保持原规则；四次 SDK 重试耗尽后才形成一次 Agent runtime_error。
- 不借本任务改变探索提示词、完成条件、并发默认值或靶场专属策略。
