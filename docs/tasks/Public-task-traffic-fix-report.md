# 公网任务故障与流量采集修复报告

日期：2026-10-17。工作树：`/Users/yym/bbx-wt/public-task-fix`，分支：`fix/public-task-completion`。

## 故障与定位

最新公网普通黑板任务 `0ef17097-b431-49cf-b431-46d94c67f2e6`（名称“测试”，目标“渗透靶机：123.57.58.26”）在 06:49 启动，07:31 以“连续运行失败达到保护阈值”结束。任务最终失败不是模型服务返回的 HTTP 402，而是控制面与执行环境在运行后期失联：

- runtime 日志出现 `Task supervision tick failed: ReadTimeout`、`decision tick reconnecting ... ReadTimeout`、`Traffic upload deferred ... ReadTimeout`、`Agent ... task failed: ConnectError`，以及多次 `Temporary failure in name resolution`。
- 任务后期 Agent 被登记为“会话持久化失败”，随后连续失败计数达到参数 `max_consecutive_failures=3`。
- 该任务实际产生 146,962 条流量预览、约 1.57 GB 原始抓包和 2,472 个原生抓包分片；20 个 Agent 同时运行时，执行容器内存在大量 tshark/dumpcap 进程。
- 旧版 envd 的 `seal`、`stop` 和片段列表接口默认携带完整 `chunks`、`files` 数组。runtime 每秒轮询时反复传输全量清单，容器内流量累计达到约 136 GB；runtime 内存一度升至约 3.27 GiB，控制请求出现 60/150 秒超时。
- 旧版停止路径逐个等待抓包进程退出，20 个进程的最坏等待时间超过 100 秒；自动容量停止与任务清理因此可能卡住。失败任务的执行容器在任务结束后仍存活，正是该清理链未完成的结果。

结论：模型服务不是本次任务的唯一原因。高并发抓包产生的控制面状态膨胀、进程停止串行等待和任务结束后的采集排空，是导致 runtime/网络控制链超时并被保护阈值终止的直接故障链。

## 修复内容

1. envd 的 `/traffic/status` 默认只返回计数、状态和 `pending_chunks`；只有显式 `include_items=true` 才返回完整数组。`/traffic/chunks`、`/traffic/artifacts` 增加 `limit`、`total_items` 和 `has_more`，runtime 每轮只取必要片段，避免全量对象在 HTTP 中重复传输。
2. 抓包停止改为并行向所有进程组发送 SIGINT，统一等待后只对未退出进程发 SIGKILL；正常停止不再逐个等待约 100 秒。强制终止仍会留下明确的不完整提示。
3. 流量容量提示只保留一条规范文案，去掉了 poll 与 limit 产生的重复 `Traffic capacity reached`；采集达到容量后仍保留代理转发。
4. 预览队列溢出、超大预览、预览读取中断等只增加 `preview_dropped` 计数并标记为采集不完整，不再把原始包仍完整的采集误报为致命错误。
5. TLS 解密失败仍生成逐条加密流量记录，但不再写入全局 `failure` 文件，也不会把整轮采集标成“TLS handshake failed”致命错误。部分 HTTPS 未能解密的记录可直接在流量详情中查看。
6. 任务终态清理先停止 envd 抓包，再完成未上传逻辑记录、原生分片和工作区归档；流量记录、工作区归档与任务清理顺序保持不变。

## 验证

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync --frozen
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
.venv/bin/pytest services/envd/tests/test_envd_traffic.py services/agent-runtime/tests/test_traffic.py services/agent-runtime/tests/test_traffic_packets.py -q
TESTCONTAINERS_RYUK_DISABLED=true .venv/bin/pytest services/envd/tests/test_traffic_integration.py -m integration -q
TESTCONTAINERS_RYUK_DISABLED=true .venv/bin/pytest services/envd/tests/test_live_packets_integration.py -m integration -q
```

实际结果：

- `make check`：2150 passed、352 deselected；Ruff 与 Pyright 通过。
- 流量单元与运行时回归：53 passed。
- `test_traffic_integration.py`：1 passed，覆盖 HTTPS、TLS 解密失败、TCP/UDP/ICMP、原始片、容量限制、转发保持和分 Agent 归属。
- `test_live_packets_integration.py`：1 passed，覆盖两 Agent 实时预览、原始片和资源边界。
- 测试镜像 `bbx-traffic-envd:latest` 由当前源码构建；首次未构建镜像时的测试失败属于测试环境前置条件，不是代码回归。

## 公网发布与续跑

- 服务器保留旧执行镜像标签 `bbx-exec-env:before-fix-2046acd`，新镜像 `bbx-exec-env:fix-2046acd` 同时更新为 `bbx-exec-env:latest`；runtime 使用 `bbx-agent-runtime:fix-2046acd`。
- 服务器 compose 备份为 `operations/compose.server.yml.before-fix-2046acd`；PostgreSQL、MinIO、blackboard 容器、数据卷和任务数据均未替换或清理。
- 更新前确认没有 `provisioning/running/closing` 任务。失败任务完成归档后，`cleanup_ready=true`，`workspace_uri=workspace/0ef17097-b431-49cf-b431-46d94c67f2e6.tar.zst`，残留执行容器已销毁。
- 通过平台登录态调用续跑接口，返回 HTTP 200；同一任务进入 `run_number=2`、`status=running`，原有黑板、会话、附件、预算和失败历史保留。新执行容器的 `/traffic/status` 已验证为紧凑响应，`preview_dropped=0`、`error=null`。

## 偏差与待决

- 本次没有调用新的 DeepSeek live/e2e/eval 命令；续跑通过平台原有的运行时链路自然发起模型调用。
- 原任务的 run 1 仍保留旧流量错误文本和旧失败原因，作为历史证据，不在归档中篡改；run 2 使用新镜像和新采集周期。
- 完整任务的目标结果取决于靶机网络、模型服务和后续 Agent 执行，本报告只确认故障修复、归档恢复和续跑启动，不把运行中的任务提前标记为完成。
- 未修改 `docs/design/`、未读取或写入 `.env`、未引入依赖或数据库迁移。

## 建议提交

- `fix(traffic): bound capture control traffic and cleanup`（已提交 `2046acd`）：envd、runtime 流量控制、对应单元与隔离集成测试。
- 本报告可随该提交一起归档；若按拆分提交，则单独使用 `docs(traffic): record public task recovery and traffic fix`。
