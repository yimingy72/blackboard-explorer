# 任务 M1b · 黑板服务的接口层

依据：`docs/design/黑板系统实现架构.md` 第 0.4 节（Agent 自主判断重复）、3.2（对象存储布局）、3.5（Agent 配置）、4.1–4.4（鉴权、接口、写入流水线、快照）、10（安全）、11（部署）；`docs/design/黑板系统开发方案.md` M1 的 1.8–1.14 与检查点；`docs/tasks/M1a-report.md`（`BoardService` 公开方法清单与偏差）。

前置：M1a 已合并到 `main`。先阅读 `AGENTS.md`。

## 范围

在 M1a 的 `BoardService` 之上做 HTTP 接口、鉴权、SSE、对象存储、关键词查询、快照、Agent 配置、blackboard 镜像与 compose 服务、OpenAPI 导出、黑板模拟器。**不改动 M1a 的确定性领域规则**（按 2026-09-24 用户的新设计移除向量列、Embedder 注入和相似度预检；迁移与旧事件重放需兼容）；不做前端（M1-W）、不做 agent-runtime（M2）。

## 已定的实现决定

1. **应用**：`bbx_blackboard.api.create_app(settings) -> FastAPI`，所有接口挂在 `/api` 下；`BoardService` 通过依赖注入提供。
2. **错误映射**：`RuleViolation` → 422，响应体 `{"code": "...", "message": "..."}`，`message` 原样返回（它是写给模型读的）；对象不存在 → 404；鉴权失败 → 401；越权 → 403。
3. **鉴权**（PyJWT，HS256）：
   - 服务 token：`Authorization: Bearer <SERVICE_TOKEN>`，身份为 `scheduler`，可访问系统接口。
   - agent token：`POST /api/tasks/{id}/agents` 登记时由 blackboard 签发并返回，JWT 声明 `{aud: "agent", tid, aid, exp}`，密钥 `AGENT_TOKEN_SECRET`。Agent 接口的身份**只取自 token**，不信任请求中的 agent id。
   - 用户：`POST /api/login` 校验 `ADMIN_USERS`（格式 `user:pass,user2:pass2`），签发 `aud: "user"` 的 JWT 写入 HttpOnly Cookie；`POST /api/logout`。
4. **SSE**（`sse-starlette`）：每个进程一个 asyncpg 连接 `LISTEN` 所有 `bbx_task_*` 通道，在进程内分发给订阅者（不要每个客户端开一个数据库连接）。事件 id = version；客户端用 `Last-Event-ID` 或 `?since=` 续传；每 15 秒发心跳注释。
5. **对象存储**：新建共享包 `packages/objects`（包名 `bbx_objects`），基于官方 `minio` SDK（同步调用经 `anyio.to_thread`），提供 `exists / put / get / stream / list`。blackboard 用它实现 M1a 的 `ObjectStore` 协议；M2 的 agent-runtime 会复用这个包上传证据。键布局按实现架构 3.2。
6. **重复判断与查询**：Agent 依据启动快照和增量黑板信息自行判断重复。取消本地模型、模型下载、向量列和相似度分数；`/search` 用关键词匹配事实陈述及意图 statement/expected/method，按 version 倒序返回。`dry_run` 仅作确定性校验，返回 `{valid: true}`，不写入。
7. **Agent 配置**：启动时把 `profiles/default` 以"名称 default + 内容哈希"幂等写入 `agent_profiles`（内容未变不产生新版本）；任务创建时固定 profile 名与版本；`heartbeat` 接口按任务的 profile 与 agent 的任务类型取价格表，调用方不传价格。
8. **快照**：`domain/snapshot.py` 纯函数，输入 `state`，输出实现架构 4.4 的 YAML（含裁剪规则与省略说明）。
9. **镜像与部署**：`services/blackboard/Dockerfile`（`python:3.12-slim`，uv 安装依赖；启动时执行 `alembic upgrade head` 再启动 uvicorn）；`docker-compose.yml` 追加 `blackboard` 服务（`internal` 网络，主机端口 `127.0.0.1:58000`，依赖 postgres 与 minio 健康）。如果 `web/dist` 存在则作为静态文件挂在 `/`。镜像构建由用户在沙箱外执行（参考 M2-env-fix 的国内源参数做法）。
10. **OpenAPI**：`make openapi` 导出到 `services/blackboard/openapi.json` 并提交；普通测试比对当前生成结果与该文件，不一致即失败（接口变化必须显式更新）。

## 任务

| # | 内容 |
|---|---|
| B.1 | 任务接口：创建、列表、详情（含验收状态、用量与金额、运行中 agent、report_uri、workspace_uri）、start / stop（实现架构 4.2"任务"表） |
| B.2 | 黑板读接口：`/state`、`/snapshot`、`/events?since&for`、`/stream`（SSE）、`/objects/{oid}?depth`、`/search`、`/api/evidence?uri=`（校验 uri 属于该任务） |
| B.3 | Agent 写接口：facts（含 dry_run）、intents（含 dry_run、claim）、claim、release、close（含报告：终结模式由调用方先上传报告再传 uri） |
| B.4 | 系统接口：status 迁移、agents 登记（返回 agent token）/ heartbeat / conclude / grace / finish、system_close、tool_calls、claim_for |
| B.5 | 配置接口：`/api/profiles` 列表、版本列表、新建版本（校验 `AgentProfile`）、设为某任务默认 |
| B.6 | 鉴权三种身份与权限矩阵（哪些接口允许哪类身份），测试覆盖越权访问 |
| B.7 | SSE 分发与续传 |
| B.8 | `bbx_objects` 包与 blackboard 的 `ObjectStore` 实现 |
| B.9 | 关键词查询与无向量提交；移除旧向量存储、依赖和构建步骤 |
| B.10 | 快照纯函数 |
| B.11 | Dockerfile、compose 服务、`make image-blackboard`、`make openapi` |
| B.12 | 黑板模拟器 `python -m bbx_blackboard.simulator --base-url … --scenario demo`：通过 HTTP 接口创建任务、登记多个 agent、上传假证据、提交事实与意图、认领竞争、争议来回、satisfies、裁定（judge 与 final），节奏可调（每步间隔），供 M1-W 演示 |

## 测试

- **普通测试**：鉴权（签发、过期、aud 不符、越权）、错误映射、快照裁剪、SSE 分发器（假通知源）、profile 幂等写入、OpenAPI 快照比对。
- **集成测试**（testcontainers：`pgvector/pgvector:pg16` 与 `pgsty/minio:RELEASE.2026-04-17T00-00-00Z`）：用 httpx 对真实应用走一遍"创建任务 → 登记 agent → 上传证据 → 提交事实 / 意图 → 认领 → 争议 → 裁定 → 终结"；SSE 客户端收到有序事件且断线后能从 version 续传；证据读取越权被拒绝。
- 不再下载或测试 embedding 模型；验证关键词查询、提交不返回相似度、dry_run 不写入，以及旧事件中的废弃向量字段不影响重放。

## 完成标准

1. `make check` 全绿；`make test-integration` 全绿（报告中给出结果）。
2. `services/blackboard/openapi.json` 已生成并被测试守护。
3. 用户可执行的检查点（写进报告）：`make image-blackboard && make up`，运行模拟器，`curl -N` 订阅 SSE 看到完整、有序、可续传的事件流。
4. 报告 `docs/tasks/M1b-report.md`：接口清单（方法、路径、身份、请求/响应模型）、鉴权矩阵、共享文件修改、建议提交划分、偏差与待决。
