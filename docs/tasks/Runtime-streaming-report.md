# Runtime-streaming 实施报告

## 完成情况

1. **后端流式**：Explore、Derive、Close 和结束后只读对话按模型连接类型选择 MAF `Agent.run(stream=True)`，并调用原生 `ResponseStream.get_final_response()` 完整消费后才解析回执或保存答复。启用范围为 `deepseek`、`openai_chat`、`openai_compatible`、`openai_responses`、`azure_openai_chat`、`azure_openai_responses`。其他 provider 仍走原调用。工作台仍展示完整结果；本轮没有逐字推送前端或新增配置、依赖。
2. **逐调用处理**：MAF 自己执行流式工具循环和参数拼接。`BoardSyncMiddleware` 在实际消费每个模型响应时继续注入黑板增量，并在完成后记录 model_output trace、每次 heartbeat、token/缓存与费用；惰性流读取错误进入脱敏 model_error trace。只读对话的 `ReviewBillingMiddleware` 与 `CheckpointHistoryProvider` 继续按每次调用记录价目、usage 和会话。图片临时字节延至流消费结束清理，异常或取消时保留待发送引用。
3. **完整性与错误**：OpenAI Chat Completions 系仅接受 `stop` / `tool_calls` 终态；Responses 仅接受 `response.completed` 且状态为 completed。`response.failed`、`response.incomplete`、错误事件、长度截断和无终态 EOF 均阻止工具执行与正常回执。无终态 EOF 归类连接错误；Responses `rate_limit` / `rate_limit_exceeded` 和 `server_error` 固定码分别归类限流、服务端错误，未知码保留 unknown；传输层 `httpx2` 超时/连接异常沿原故障分类。兼容网关在 Responses `response.content_part.added` 的 output_text 占位里返回 `text:null` 时，仅将该占位归一为空串，以便 MAF 原生聚合后续文本增量。DeepSeek 多个 `reasoning_content` 分片汇总至最终响应的非正文附加属性，保留完整 trace，不混入普通答复及后续模型消息。Responses 继续强制 `store:false`，本地函数调用历史正常回放。
4. **离线测试**：新增 MockTransport SSE 实际 MAF 工具往返、分片工具参数、逐轮 usage、DeepSeek 推理分片、Responses 的 null 占位与本地回放、失败/不完整/EOF 的拒绝和错误码分类测试；新增脚本客户端逐调用账本与 trace、惰性流失败、图片取消、旧 runner 取消原因测试。只读对话测试新增实际 `stream=True` 断言；集成场景中用于控制并发时序的两个假客户端也改为在流实际拉取时等待原有 gate。

## 实际验证

在仓库根目录执行：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
```

本工作树已执行：依赖同步成功；`make check` 全绿，ruff format/lint 通过，pyright 0 错误，671 个普通测试通过（69 个 integration/live 测试按规则跳过）。`git diff --check` 通过。主代理独立复核也运行 `make check`，结果相同，日志位于 `/private/tmp/bbx-runtime-streaming-check-final.log`。所有新增自动化模型测试均使用 MockTransport 或脚本客户端，不调用真实模型。

主代理以最终源码重建四个测试镜像并运行 `make test-integration`，65 个集成测试全部通过（675 个非集成项跳过，185.58 秒），日志位于 `/private/tmp/bbx-runtime-streaming-integration-final.log`。第一次集成套件运行时，旧进程已导入会在流式请求时跳过等待的测试 Gate 假客户端，导致 7 个场景等候超时；随后仅将 Gate 假客户端改为在流实际拉取时等待并重新运行完整套件，原有业务完成断言均保留。最终主代理审核通过。

主代理另外用平台现存模型配置，对百智云 Responses 网关做了独立收费烟测：两个 HTTP 请求均为 `stream:true`、`store:false`，没有 `previous_response_id`；第二轮输入包含一个 `function_call_output`，内存测试工具只执行一次，最终完整答复为 `STREAM_OK`。累计缓存命中 384、未命中 370、输出 61（其中推理 17）token，按平台快照估算费用 ¥0.00124336。此前两次真实探测已进入第二轮但遇到网关 `output_text` 空占位触发的 MAF 聚合 TypeError；本轮针对该形态加最小归一和离线回归后重新验证成功。上述烟测不属于 `make check`。

## 偏差与待决

- 设计正本的第 6 节仍以非流式 `run()` 描述组装，第 7 节以直接 `ChatResponse` 描述中间件。本任务按现有后端请求修改实现；没有改动 `docs/design/`。建议后续设计同步时注明 `ResponseStream` 完整消费及每次模型响应后计费。
- 只验证并启用上述六类 OpenAI 系 provider。Anthropic、Bedrock、Gemini、Ollama、Mistral、Foundry 等连接器尚未逐一验证流式终态、用量与工具循环，因此维持既有调用，待有对应离线传输测试后再扩展。
- 流中断若没有收到供应商 usage 事件，无法准确计算该次请求的 token/费用；失败分类和 trace 仍保留，不能据本地账本声称供应商计费完整。中断后不自动重放已执行的工具。
- 百智云已完成主代理的最小两轮真实烟测，但尚未用真实模型跑 Explore/Derive/Close 完整业务闭环；其他收费网关也未验证。前端仍只显示完整结果。

## 下一步与建议提交划分

已按以下划分提交并快进合并到 main：实现提交 `5c1126c`，任务说明与验证报告提交 `ea30dc7`。

1. `Enable complete model streams in runtime workers`：`services/agent-runtime/src/bbx_runtime/{runner,chatworker,models,middleware,image_view,model_errors}.py` 及对应测试、脚本客户端测试辅助修改。
2. `Document runtime streaming verification`：`docs/tasks/Runtime-streaming.md` 和 `docs/tasks/Runtime-streaming-report.md`。

提交正文按 `AGENTS.md` 补 `Implemented by Codex (gpt-6-sol) for task Runtime-streaming.`。

## 本地部署复核（2026-09-29）

- 部署前确认没有 running / closing / provisioning 任务；仅重建启动 agent-runtime，没有自动续跑历史任务。
- 使用最终集成测试构建的镜像 `90adfedf1379`。容器运行正常、重启次数 0，PostgreSQL 中仅一个已授予的运行锁；工作台 `/login` 返回 HTTP 200。
- 容器内 models、runner、middleware、image_view、model_errors、chatworker 六个模块的 SHA-256 与 main 源码逐项相同。
- 现在新建或显式续跑的任务，以及结束后的只读对话，在上述六类连接器下使用模型流式请求。前端仍按完整响应展示；未推送远程。
