# 黑板 HTTP 服务

服务以 `python -m bbx_blackboard.server` 启动：先执行 Alembic 迁移，再在 8000 端口启动 FastAPI。配置来自环境变量，不自动加载 `.env`。Compose 将服务发布到 `127.0.0.1:58000`，`web/dist` 存在时同时托管前端。

黑板在完整探索中的作用、Fact/Intent 写入与 Close 裁定规则见[项目 README](../../README.md#探索如何运转)；本页说明服务接口与开发检查。

## 模型与重复判断

Agent 使用 DeepSeek，并根据启动快照与增量黑板信息自行判断是否重复。黑板不下载模型或计算向量；`/search` 按关键词查找原文。事实与意图通过证据、引用、身份和状态校验后直接写入。可选的 `dry_run=true` 只返回校验结果 `{valid: true}`，不写入。

## 构建和检查

```sh
uv sync --locked
make check
make openapi
.venv/bin/python -m bbx_blackboard.openapi --check
make image-blackboard
make test-integration
```

Python 依赖使用项目配置的阿里云索引；镜像的 `DEBIAN_MIRROR`、`PIP_INDEX_URL` 可以覆盖。需要本机代理时，使用 envd README 中的 `DOCKER_BUILD_ARGS`；构建不预下载任何模型。`make check` 使用假客户端，不调用真实模型。

## 鉴权与模拟器

- 用户通过 `/api/login` 登录，使用 HttpOnly Cookie 管理任务、读取黑板和管理 profile。
- 服务 token 用于系统接口、配置和上传。
- 登记 Agent 时返回绑定任务与 Agent ID 的 JWT。Agent 写入身份只取自此 token；服务身份写事实时也应使用登记得到的 Agent token。

平台启动后，可通过纯 HTTP 模拟器检查事件流，不会调用 DeepSeek：

```sh
.venv/bin/python -m bbx_blackboard.simulator --base-url http://127.0.0.1:58000 --scenario demo --interval 0.5
curl -N -H "Authorization: Bearer $SERVICE_TOKEN" \
  "http://127.0.0.1:58000/api/tasks/<task-id>/stream?since=0"
```

模拟器从已导出的 `SERVICE_TOKEN` 环境变量取凭据，输出任务 ID 和检查结果。SSE 的 `id` 是事件版本，续传可用 `Last-Event-ID`；事件日志是数据源，PostgreSQL 通知只唤醒补查。客户端需监听具体事件名称，如 `fact.posted`、`intent.claimed`。

profile 版本保存提示词正文快照，内容变化产生新版本；任务创建时固定版本，并把 profile 默认参数与任务覆写合并。运行中的任务不跟随文件变化。

完整请求与响应定义见 `openapi.json`；接口、验证结果及迁移说明见 `docs/tasks/M1b-report.md`。

## 会话与定向消息

`agent_sessions` 保存原生 MAF Session、初始指令和revision；`agent_messages` 保存幂等用户消息、投递状态、复盘回复及用量。用户通过任务/Agent范围的messages接口发送和读取；session checkpoint、认领、完成与恢复接口只允许服务身份。checkpoint与消息确认同事务，复盘每次写入同时校验claim token与租约。黑板投影回放不会清除这些会话数据。

删除终态任务采用deleting标记与runtime协调，执行环境销毁后再幂等清理对象和数据库；普通任务结束与归档保留会话。并行derive登记也在任务锁内核验期望阶段，过期派发返回stale_derive。完整合同见[Interactive-agents](../../docs/tasks/Interactive-agents.md)。
