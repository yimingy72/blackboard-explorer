# Responses 本地回放工具顺序修复报告

## 完成情况

1. **诊断**：任务 `4891d8c7-7720-45de-8aec-5562809c57a3` 的 12 个 Explore 均在已成功完成 1–37 次模型调用后，以外层 HTTP 502 结束；内部 MaaS 错误为 `invalid_request_error`、`code=400001`、`source=client`。`model_error_metadata` 按外层 502 归为 `server_error`，该分类本身不能证明模型服务故障。只读会话结构核查发现，12 个失败 Agent 的末次工具批次都同时含非空 assistant 正文和函数调用；同任务大量只含函数调用的单/多工具批次正常继续。260 个函数调用与 260 个结果按 Agent 和 call_id 全部一一配对，ID 非空且无重复；66 个多工具批次最多 6 个调用，结果顺序与调用顺序一致。
2. **根因验证**：锁定 MAF 的 Responses 转换器将单条 assistant 消息中的普通正文暂存至循环末尾，却立即输出函数调用。在 `store:false` 本地回放时，请求项因此变成 `function_call → assistant message → function_call_output`，与会话中正文先于函数调用的内容顺序不一致。主代理用真实网关返回的中性 echo 调用构造同一份第二轮 HTTP 请求，仅改变这条 assistant 普通消息的位置：原顺序返回 502；移到函数调用前返回 200 completed；删除该普通消息也返回 200 completed。A/B 保持模型、工具 schema、call_id、fc_id、其他请求内容和 `stream:true/store:false` 相同，确认此请求项顺序是本次拒绝的触发条件。该验证未重发目标操作，也未实际执行第二轮工具。
3. **最小修复**：只在 `CompleteResponsesClient._prepare_message_for_openai` 中复用 MAF 原转换结果。对单条原始 assistant 消息，且仅当 `store:false` 转换结果同时包含函数调用及非空 `output_text` 普通消息时，把该普通消息移到本条消息首个函数调用之前。推理项、函数调用、函数结果及其它消息的内容和相对顺序保持原样；不跨原始消息重排，不修改持久 Session、黑板、前端、提示词、并发或其他 provider。
4. **离线回归**：新增单/双函数调用、带/不带 reasoning、跨原始消息边界、无正文、空正文、服务端存储路径以及真实 MAF SSE 两轮工具回放测试。第二轮请求仍保留原 call_id 与 `function_call_output`，工具只调用一次。

## 如何验证与实际结果

在仓库根目录执行：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run pytest -q services/agent-runtime/tests/test_runtime_streaming.py
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
```

本工作树实际运行：定向 19 项通过；`make check` 中 ruff format/lint 通过、pyright 0 错误、677 个普通测试通过，69 个 integration/live 测试按规则跳过。新增自动化测试只用本地转换器、MockTransport 和中性脚本客户端，不调用真实模型。

主代理独立重跑 `make check`：677 项通过；重建镜像后运行 `make test-integration`：65 项全部通过，耗时 171.38 秒。日志分别为 `/private/tmp/bbx-responses-tool-order-check.log` 和 `/private/tmp/bbx-responses-tool-order-integration.log`。

主代理还通过完整 MAF Agent 工具循环做真实百智云烟测，要求模型在调用中性 echo 工具前输出文字，以覆盖此前遗漏的混合输出。两轮请求均为 `stream:true/store:false`，第二轮实际顺序为 `user → reasoning → assistant message → function_call → function_call_output`，工具只执行一次，最终助手答复为 `STREAM_OK`，两个响应均 completed。该次验证累计缓存命中 384、未命中 462、输出 139（含推理 90）token，按平台费率估算 ¥0.00205136；不涉及靶场操作，也不修改原任务。真实 A/B 和本次烟测均独立于自动化检查。

## 偏差与待决

- 此修复覆写锁定 MAF 版本的内部转换方法；离线回归固定其方法签名与输出顺序。升级 `agent-framework-openai` 时需重新核对该兼容点是否已由上游修复。
- `message_count=1,text_chars=0` 来自 MAF 工具循环将上一轮仅含函数结果的单条 tool 消息传给外层 BoardSync；随后本地历史中间件才注入完整持久会话。因此该错误元数据不是实际 HTTP 请求的消息数或长度。
- 本轮只修复已通过 A/B 证实的 Responses 混合正文/工具顺序。既有任务已经结束，不自动重放失败的目标操作；是否重试由用户后续决定。

## 下一步与建议提交划分

建议主代理独立审核与必要验证后分两次提交：

1. `Keep assistant text before replayed Responses tool calls`：`services/agent-runtime/src/bbx_runtime/models.py` 与 `services/agent-runtime/tests/test_runtime_streaming.py`。
2. `Document Responses tool replay ordering fix`：`docs/tasks/Responses-tool-order-report.md`。

提交正文按 `AGENTS.md` 补 `Implemented by Codex (gpt-6-sol) for task Responses-tool-order.`。

## 本地部署（2026-09-29）

- 实现 `ac08090`、报告 `98a2749` 已快进合并到 main。部署前无运行或收尾中的任务，已用最终测试镜像 `c7111cf5449c` 更新 agent-runtime。
- 容器 running、重启次数 0、运行锁仅 1 个，工作台 `/login` 返回 200；容器内 models.py 的 SHA-256 与 main 一致。
- 原任务于 14:12:18 因用户手动停止进入收尾，14:16:21 完成收尾。本轮没有自动续跑或重放目标操作；后续显式续跑会使用修复后的 API 转换。
