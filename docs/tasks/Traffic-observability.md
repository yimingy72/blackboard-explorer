# 任务流量采集、Agent 归属与 HTTPS 解码

用户于 2026-10-15 批准设计并开发，补充明确 HTTPS 解码也是本次交付要求。设计正本见《黑板系统实现架构》9.2。开发分支 `feature/traffic-observability`，工作树 `~/bbx-wt/traffic-observability`；此前已交付的首页样式保持。

## 范围与完成标准

1. envd 复用 dumpcap/tshark 与单个 mitmdump，采集全接口原始包、HTTP/HTTPS 请求响应，使用每命令随机代理身份关联 Agent；支持 sudo/常见客户端信任任务 CA。没有解码、归属未知、丢包、容量或工具失败必须显式展示，不能归为完整。
2. 两模式在首命令前启动、后台上传、结束先封存确认再销毁；重启/同轮容器替换去重、续跑按轮次隔离。原始片、详情与清单存 MinIO，删除任务清理全轮次；失败可重试，不静默销毁未上传文件。
3. 复用 MAF OpenTelemetry 记录服务侧模型/工具逻辑调用；记录与原始网络包有明确来源区别，Agent/轮次上下文不串扰，流式输出不因采集被缓冲。
4. 统一用户鉴权查询：`GET /api/tasks/{id}/traffic?run=&agent_id=&protocol=&q=&before=&limit=`、`/traffic/records/{record_id}?run=`、`/traffic/files/{file_id}?run=`。只接受固定命名规则，详情限额脱敏、原始包流式下载，跨任务/轮次、坏清单与缺文件正确拒绝。
5. 前端任务顶部及 Agent 会话提供流量入口，共用紧凑 Drawer：轮次/Agent/协议/搜索、分页列表、请求响应/调用详情及原始包下载；旧任务、采集中、已归档、部分缺失、错误状态清楚，保留工作台和草稿。

## 数据契约

- 根：`traffic/{task_id}/run-{run}/`。manifest 状态 `collecting|complete|partial|error|archived`，HTTPS 能力 `enabled|unavailable`，保留起止/更新时间、记录数、字节和丢包统计、原因、不可变 chunks/files。不存在的历史清单返回 `availability=not_recorded`；存储错误返回 503。
- 摘要主键 `r-{seq}`，本轮 seq 递增，包含时间、agent_id nullable、`attribution=exact|unknown`、协议、端点、method/url/status、字节与 `plaintext=decoded|encrypted|not_applicable`。
- 详情的 request/response 包含 header pairs、有界 body_preview、真实 body_bytes/encoding/truncated；每侧正文上限 16KiB。logical_call 的 observation 记录 trace/span/parent、状态、脱敏属性和 retry_visibility，不冒充 wire。
- 摘要 gzip JSON 数组按 chunk 存储，详情独立 gzip JSON；原始 PCAP 单独私有保存。source/checkpoint 支持采集进程重启，先上传依赖再发布 manifest。ID 只用 `[a-zA-Z0-9_-]{1,80}`，下载不接收任意路径。

## 验证与边界

- 普通单测覆盖解析、脱敏、归属、顺序、错误、限额、路径和生命周期；`make check` 不调用真实模型。依赖 Docker 的测试用任务前缀和随机端口，结束清理。
- 本地合成 HTTP/HTTPS 服务验证请求响应、代理身份、并发双 Agent、sudo、任务 CA、流式透传、归档及删除；不会连接真实靶机或模型，不读写 `.env`。
- Docker Desktop 上 cgroup/eBPF 和任意 TLS 实现不是预先承诺的支持范围；本次可移植基线使用代理身份，直连网络包仍保存但无法证实 Agent 时标 unknown。原始包包含原始数据，只有展示副本脱敏。
- 不更改全局 CA、宿主机网络或 Docker 配置。报告列实际检查、未覆盖项、部署与提交划分。
