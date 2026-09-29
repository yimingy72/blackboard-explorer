# Runtime-streaming：后端模型流式调用

1. 在 Explore、Derive、Close 与结束后只读对话中，对 DeepSeek、OpenAI Chat、OpenAI 兼容、OpenAI Responses 及 Azure Chat/Responses 六类连接方式启用 MAF 原生流式请求；完整消费后继续使用现有完整回执与回复协议。其他连接方式维持现有调用。
2. 保持每次模型调用的黑板注入、trace、usage/费用/缓存计数、工具循环、会话 checkpoint、错误及取消/超时处理。Responses 必须保留 `store:false` 与本地工具历史回放；失败、不完整或无合法终态的流不得成为有效回执。
3. 使用 MockTransport 和脚本客户端离线验证实际 MAF 流式工具往返、usage、会话历史、故障及取消；`make check` 不调用真实模型。
4. 记录实现、验证、局限、设计偏差与建议提交划分。本任务不修改前端、不部署、不合并。
