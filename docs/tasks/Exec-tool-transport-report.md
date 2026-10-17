# 内置执行工具响应串扰修复报告

日期：2026-10-17。工作树：`/Users/yym/bbx-wt/exec-tool-transport`，分支：`fix/exec-tool-transport`。

## 故障结论

任务 `0ef17097-b431-49cf-b431-46d94c67f2e6` 在 run 2 于 08:47:07 UTC 以“连续运行失败达到保护阈值”结束。三次连续失败发生在 agent-41、agent-40、agent-46：

- 08:42:15 内置执行工具先报告“命令结果未确认”；
- 08:42:18，已在环境中完成并返回的工具响应才到达，MCP 客户端因请求已结束抛出 `Received response with an unknown request ID`；
- 08:44 和 08:47 又出现同类未确认命令结果，三个 Agent 均以 `RuntimeError` 退出，达到 `max_consecutive_failures=3`。

这不是预算耗尽，也不是预算调整导致。根因是普通黑板内置命令复用长期存在的 MCP 会话：一次命令响应延迟或丢失后，晚到响应会污染同一会话的请求编号，导致后续 Agent 运行被错误终止。

## 修复内容

1. envd 新增受鉴权的普通任务 `/execute` 接口，接收 `command`、`cwd`、`timeout_sec`、`privileged`，Agent 身份继续只从认证后的 `X-Agent-Id` 获取。CTF 模式拒绝该接口，仍使用带 generation/metadata 的 MCP 执行链。
2. runtime 的内置 `execute_command` 改为一次独立、有界的 HTTP POST；不再通过长期 MCP 会话发送命令。MAF 仍负责工具声明和 Agent 生命周期，命令执行不再与 MCP JSON-RPC 请求编号共享状态。
3. 保留单次派发和“结果未确认时禁止盲重试”的边界；HTTP/传输失败只会结束本次 Agent，不会污染其他 Agent 的会话。
4. 测试用 envd 增加相同的 `/execute` 路由；单次派发回归覆盖直接 HTTP 成功、响应丢失和 MCP 兼容回退路径。

## 验证

- 相关单测：`services/agent-runtime/tests/test_single_dispatch.py` 与 `services/envd/tests/test_envd_traffic.py` 共 **40 passed**。
- `make check`：**2152 passed、353 deselected**；Ruff、Pyright 通过。
- Docker 流量/命令集成测试 `services/envd/tests/test_traffic_integration.py`：**1 passed**，其中直接通过 `/execute` 执行合成命令。
- 失败任务已完成归档：`cleanup_ready=true`，工作区 URI 为 `workspace/0ef17097-b431-49cf-b431-46d94c67f2e6/run-2.tar.zst`。

## 部署与续跑

修复镜像构建后，将更新服务器 `bbx-exec-env:latest`（保留旧标签）和 runtime 镜像；更新前再次确认无 `provisioning/running/closing` 任务。随后使用平台续跑接口恢复同一任务，保留黑板、会话、附件、预算和运行历史。

## 偏差与待决

- CTF 团队模式的执行链没有切换到 `/execute`，继续保持其已有的 generation 与命令去重契约。
- 未修改 `docs/design/`、没有读取 `.env`、没有新增依赖或数据库迁移。
