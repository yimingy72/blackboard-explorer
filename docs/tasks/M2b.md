# 任务 M2b · 单个 Agent 跑通

依据：`docs/design/黑板式探索架构设计.md` 第 4 节（Agent 构造与工具）、5.2–5.3（启动上下文、conclude 与宽限）、6（同步规则）、8.1–8.3（提示词）；`docs/design/黑板系统实现架构.md` 第 6–7 节（Agent 构造、黑板工具、中间件）；`docs/design/黑板系统开发方案.md` M2 的 2.8–2.12 与检查点；`docs/tasks/M2a-report.md`（**技术验证结论优先于设计文档中的默认写法**）。

前置：M2a 已合并。先阅读 `AGENTS.md`。

## 范围

`services/agent-runtime` 的黑板工具、三个中间件、启动上下文、提示词模板、回执解析、Agent 运行器、CLI，以及 `profiles/default/prompts/`。**不做**调度循环（M3）。derive 与 close 的工具和模板在本任务一并实现，但它们何时被触发属于 M3。

## 已定的实现决定

1. **黑板工具**（设计文档 4.1 的工具表，按任务类型装配）：
   - `post_fact`：Agent 根据已同步的黑板内容自行判断重复；工具不做相似度预检或二次确认。对每条证据经 envd `/stat`、`/files` 取文件（超过上限返回"证据文件过大，请截取相关部分另存后再提交"）→ 上传到对象存储 `evidence/{task}/{agent}/{sha256[:12]}-{basename}` → 对带 `call_id` 的证据自动追加一条 `command_output` 证据（uri 为 `toolcalls/{task}/{call_id}.txt`，`auto=true`）→ 正式提交。
   - `post_intent`（含 `claim`）、`claim`、`release`、`get`、`search`、`read_evidence`（返回内容包裹为 `<evidence>…</evidence>` 数据块）、`submit_close`（终结模式先上传报告到 `reports/{task}.md`）。
   - 所有工具返回给模型读的简短中文：成功给对象 id 与一句说明；失败原样给出黑板的错误消息（它已写明怎么改）。
2. **execute_command**：`MCPStreamableHTTPTool(name="exec", url=…, static_headers={"X-Agent-Id": aid, "Authorization": f"Bearer {envd_token}"})`，只装配给 explore。
3. **中间件**（执行顺序以 M2a 的验证结论为准）：
   - `ToolLogMiddleware`（函数中间件，最外层）：工具执行后生成 `call_id`（`c_` + 12 位 base32），把工具名、参数、完整结果上传到 `toolcalls/{task}/{call_id}.txt`，调用 `record_tool_call`，在结果末尾追加 `[call_id: …]`。
   - `GraceGateMiddleware`（函数中间件）：Agent 处于 concluding 时，对非 `release / post_fact / post_intent` 的调用、或宽限次数已用尽（黑板 `grace` 接口原子扣减失败）的调用，**设置结果为拒绝说明并且不调用 call_next；不要 terminate**。
   - `BoardSyncMiddleware`（chat 中间件）：仅对 explore 拉取 `events?since=last_seen&for=aid`，按设计文档第 6 节渲染（点名事件全文、裁定结果全文、其余每条一行摘要、超过 `delta_max_lines` 只给计数、过滤自己产生的事件）；有 conclude 请求且尚未注入时，把 conclude 指令放在最前；按 M2a 实测退路把有边界的增量追加到最近工具结果的公开 Content.result；首轮没有工具结果时附到已有起始 user Content.text。每条增量只追加一次，持久进入本次工具循环的后续上下文；不操作私有历史。调用完成后 `heartbeat`：steps +1、`context_tokens` 取本次输入 token、用量（缓存命中 / 未命中 / 输出 / 推理）、`last_seen_version`。derive 与 close 只做心跳与记账。
   - 不再挂 MessageInjectionMiddleware；MAF 1.19 的原持久追加方式不能跨轮保留增量。conclude 是否已追加由该 Agent 的中间件实例记录一次，重启后旧 run 按恢复规则结束。
4. **启动上下文**：`OpeningContextProvider.before_run` 用 `context.extend_instructions` 注入渲染后的模板；首条用户消息为"开始。"。explore 的 L0–L2 按设计文档 5.2（种子只有 L0）；快照取黑板 `/snapshot`。
5. **提示词模板**：把设计文档 8.1（explore，完整原文）、8.2（derive）、8.3（close）写成 `profiles/default/prompts/{explore,derive,close}.md.j2`。模板变量与实现架构 7.1 的表一致。derive 与 close 按 8.2、8.3 的要点写成完整提示词，风格与 8.1 一致（中文、先说任务、再说规则、最后说输出格式）。
6. **回执解析**：容错（去掉代码块包裹、多余文字，取最后一个 JSON 对象），按任务类型用对应的回执模型校验；解析失败时视为 `{"accepted": true, "data": {"note": "回执格式错误"}}` 并记录原始文本。
7. **Agent 运行器**：`run_agent(task_id, agent_id, task_type, intent_id=None, mode=None)`：组装 Agent → `agent.run("开始。", session=…)`（非流式）→ 解析回执 → 调用黑板 `finish`（end_reason：normal / refused / runtime_error；取消时为 grace_timeout）。
8. **CLI**（`bbx-runtime`）：`run-agent --task <id> --type explore|derive|close [--intent I3] [--seed] [--mode judge|final]`（自动登记 agent、为 explore 创建 Linux 用户）、`conclude --task <id> --agent agent-3`、`state --task <id>`。

## 任务

| # | 内容 |
|---|---|
| A.1 | 黑板工具（全部）与按任务类型装配 |
| A.2 | 三个中间件与组装顺序 |
| A.3 | 启动上下文与三份提示词模板 |
| A.4 | 回执解析 |
| A.5 | Agent 运行器与 CLI |
| A.6 | 测试（见下） |

## 测试

- **普通测试**（ScriptedChatClient + FakeEnvd + 假黑板客户端）：
  - `post_fact` 完整流程：直接提交、取文件、上传、自动附加 command_output、超限提示
  - ToolLog：call_id 追加且与记录一致
  - GraceGate：concluding 后非交接工具被拒绝、模型继续运行、次数用尽后交接工具也被拒绝
  - BoardSync：增量渲染（点名、裁定、摘要、计数截断、过滤自己）；conclude 指令只注入一次；注入的消息出现在下一次模型调用中（ScriptedChatClient 记录）
  - 模板渲染（种子 / 有意图 / 有交接说明三种 explore；derive；close 两种模式）
  - 回执解析的各种不规范输入
- **集成测试**（testcontainers 的 blackboard 依赖 + FakeEnvd）：一个 ScriptedChatClient 驱动的种子 explore 从开始到回执，黑板上出现预期的事实、意图、tool_calls 与心跳用量。

## 完成标准

1. `make check`、`make test-integration` 全绿（报告中给出结果）。
2. 用户可执行的检查点（写进报告，需要 `.env` 中的 `DEEPSEEK_API_KEY`、`make eval-targets`、`docker-compose.dev.yml` 中的 `eval-targets`、出网白名单含 `eval-targets`）：在玩具任务 `eval/tasks/flaky-order-test` 上用 CLI 跑一个真实种子 explore——它把代码拉到 `/workspace/shared`、提交至少 3 条事实（至少 1 条 tool_backed 且带自动附加的 command_output）、提出意图；手动 `conclude` 后在宽限内 release 并返回合法回执；金额记账与 DeepSeek 控制台一致（需先填价格表）。
3. 报告 `docs/tasks/M2b-report.md`：工具与中间件说明、模板清单、CLI 用法、检查点操作步骤、共享文件修改、建议提交划分、偏差与待决。
