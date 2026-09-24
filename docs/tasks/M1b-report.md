# M1b 任务报告：黑板接口层

## 完成情况

用户在开发过程中明确修改设计：Agent 已经通过启动快照与增量同步获知黑板，重复判断由 Agent 自己完成。因此本阶段最终实现**不包含本地 embedding、向量列、相似度 top-3 或二次确认流程**。三份设计正本先于实现修改并单独提交，M1b/M2b 任务说明已同步。

| 编号 | 结果 |
|---|---|
| B.1 | 实现任务创建、列表、详情、启动和停止；停止运行中任务进入 closing 并发 conclude，未运行任务进入 stopped，重复停止幂等。 |
| B.2 | 实现 state、YAML snapshot、events、SSE、对象关联、关键词 search、证据读取，以及报告和归档下载。 |
| B.3 | 实现事实、意图、原子认领、释放和 close；身份仅取 Agent JWT，证据 URI 必须属于当前任务；dry_run 只校验不写入。 |
| B.4 | 实现系统状态、Agent 登记/心跳/conclude/grace/finish、system_close、tool_calls、claim_for 和服务身份上传。 |
| B.5 | default 按内容幂等建版本；版本包含提示词正文快照，正文变化产生新版本；任务固定版本，profile 默认参数与任务覆写合并。 |
| B.6 | 用户 Cookie、服务 token、Agent JWT 三种身份；过期、audience、跨任务读写、伪造身份均有测试。中文凭据和非法 Unicode token 不导致 500。 |
| B.7 | 单进程一个专用 asyncpg 连接，动态监听精确任务通道；先监听后补查，按 version 去重，重连补查，15 秒 SSE 心跳。 |
| B.8 | 新增 bbx_objects：MinIO exists/put/get/stream/list，阻塞 I/O 在线程，未知长度分片上传，流结束或取消时释放连接。 |
| B.9 | 按用户新设计完成无向量提交与关键词查询；移除模型和 native 依赖、清理本次下载的 91 MB 模型缓存；旧事件的废弃 embedding 字段不影响重放。 |
| B.10 | 快照优先保留 open/claimed 意图及依据事实，再保留近期内容；陈述截断 80 字，控制行数并显示省略数量。 |
| B.11 | Dockerfile、迁移后启动入口、Compose blackboard 服务、image-blackboard/openapi 目标、OpenAPI 快照守护；web/dist 存在时托管 SPA。 |
| B.12 | HTTP demo 模拟器：多个 Agent、并发认领、争议与反争议、satisfies、judge 与 final，支持可调间隔。后续修复补齐 Agent 生命周期：每次 judge 提交后 finish；进入 closing 后向 explore 发 conclude 并 finish；final 报告提交后 finish final Agent。 |

## 接口与鉴权

`create_app(settings, *, engine=None, objects=None, dispatcher=None)` 支持测试注入；创建应用本身不联库、不下载模型。`BoardService` 当前构造函数为 `BoardService(engine, objects)`。完整定义以 `services/blackboard/openapi.json` 为准。

| 身份 | 允许范围 |
|---|---|
| 用户（HttpOnly Cookie） | 任务管理、读取全部、profile 管理 |
| 服务（Bearer SERVICE_TOKEN） | 任务管理、读取、profile、系统接口、上传 |
| Agent（JWT，aud/exp/tid/aid） | 本任务读取与 Agent 写入；事件只见广播和对自己的点名 |

服务端不接受调用方自报 aid 来写事实：系统也应使用登记返回的 Agent token。普通领域错误映射 422 并保留中文 code/message；不存在 404，缺失/失效凭据 401，越权 403，宽限耗尽 409。

| 方法 | 路径 | 身份 | 请求模型 | 响应模型 |
|---|---|---|---|---|
| GET | `/api/evidence` | 用户 / 服务 / 本任务 Agent | `查询 / 路径参数` | `流 / 空响应` |
| POST | `/api/login` | 公开（登录校验静态账号） | `LoginBody` | `object` |
| POST | `/api/logout` | 公开（登录校验静态账号） | `查询 / 路径参数` | `object` |
| GET | `/api/profiles` | 用户 / 服务 | `查询 / 路径参数` | `list[ProfileName]` |
| GET | `/api/profiles/{name}/versions` | 用户 / 服务 | `查询 / 路径参数` | `list[ProfileVersion]` |
| POST | `/api/profiles/{name}/versions` | 用户 / 服务 | `AgentProfile-Input` | `ProfileDocument` |
| GET | `/api/profiles/{name}/versions/{version}` | 用户 / 服务 | `查询 / 路径参数` | `ProfileDocument` |
| GET | `/api/tasks` | 用户 / 服务；任务详情和产物也允许本任务 Agent | `查询 / 路径参数` | `list[TaskView]` |
| POST | `/api/tasks` | 用户 / 服务；任务详情和产物也允许本任务 Agent | `TaskCreateBody` | `TaskCreated` |
| GET | `/api/tasks/{task_id}` | 用户 / 服务；任务详情和产物也允许本任务 Agent | `查询 / 路径参数` | `TaskView` |
| POST | `/api/tasks/{task_id}/agents` | 服务 | `AgentRegisterBody` | `AgentRegistered` |
| PATCH | `/api/tasks/{task_id}/agents/{agent_id}` | 服务 | `HeartbeatBody` | `object` |
| POST | `/api/tasks/{task_id}/agents/{agent_id}/conclude` | 服务 | `ConcludeBody` | `list[object]` |
| POST | `/api/tasks/{task_id}/agents/{agent_id}/finish` | 服务 | `FinishBody` | `list[object]` |
| POST | `/api/tasks/{task_id}/agents/{agent_id}/grace` | 服务 | `查询 / 路径参数` | `object` |
| POST | `/api/tasks/{task_id}/claim_for` | 服务 | `ClaimForBody` | `list[object]` |
| POST | `/api/tasks/{task_id}/close` | 绑定任务的 Agent | `CloseBody` | `list[object]` |
| GET | `/api/tasks/{task_id}/events` | 用户 / 服务 / 本任务 Agent | `查询 / 路径参数` | `list[Event]` |
| POST | `/api/tasks/{task_id}/facts` | 绑定任务的 Agent | `PostFactRequest` | `object` |
| POST | `/api/tasks/{task_id}/intents` | 绑定任务的 Agent | `PostIntentRequest` | `object` |
| POST | `/api/tasks/{task_id}/intents/{intent_id}/claim` | 绑定任务的 Agent | `查询 / 路径参数` | `list[object]` |
| POST | `/api/tasks/{task_id}/intents/{intent_id}/release` | 绑定任务的 Agent | `ReleaseBody` | `list[object]` |
| POST | `/api/tasks/{task_id}/intents/{intent_id}/system_close` | 服务 | `查询 / 路径参数` | `list[object]` |
| GET | `/api/tasks/{task_id}/objects/{object_id}` | 用户 / 服务 / 本任务 Agent | `查询 / 路径参数` | `object` |
| GET | `/api/tasks/{task_id}/report` | 用户 / 服务；任务详情和产物也允许本任务 Agent | `查询 / 路径参数` | `string` |
| GET | `/api/tasks/{task_id}/search` | 用户 / 服务 / 本任务 Agent | `查询 / 路径参数` | `list[SearchHit]` |
| GET | `/api/tasks/{task_id}/snapshot` | 用户 / 服务 / 本任务 Agent | `查询 / 路径参数` | `string` |
| POST | `/api/tasks/{task_id}/start` | 用户 / 服务；任务详情和产物也允许本任务 Agent | `查询 / 路径参数` | `StatusResult` |
| GET | `/api/tasks/{task_id}/state` | 用户 / 服务 / 本任务 Agent | `查询 / 路径参数` | `object` |
| POST | `/api/tasks/{task_id}/status` | 服务 | `StatusBody` | `list[object]` |
| POST | `/api/tasks/{task_id}/stop` | 用户 / 服务；任务详情和产物也允许本任务 Agent | `查询 / 路径参数` | `StatusResult` |
| GET | `/api/tasks/{task_id}/stream` | 用户 / 服务 / 本任务 Agent | `查询 / 路径参数` | `流 / 空响应` |
| POST | `/api/tasks/{task_id}/tool_calls` | 服务 | `ToolCallBody` | `list[object]` |
| POST | `/api/tasks/{task_id}/uploads` | 服务 | `原始字节；key 查询参数` | `UploadResult` |
| GET | `/api/tasks/{task_id}/workspace` | 用户 / 服务；任务详情和产物也允许本任务 Agent | `查询 / 路径参数` | `流 / 空响应` |

## 实际验证

- `uv sync --locked` 成功；Python 依赖使用项目级阿里云索引，无全局配置改动。
- 最终 `make check`：Ruff format/lint、Pyright 0 错误，**141 passed, 11 deselected**；不调用真实模型。
- 最终 `make image-blackboard test-integration`：镜像构建成功，**11 passed, 141 deselected**。包含真实 PostgreSQL/MinIO HTTP 全流程、跨任务证据拒绝、SSE 实时通知与 Last-Event-ID 续传，原 M1a/M2-env 测试，以及无向量旧事件重放。
- 实际用独立 PostgreSQL、MinIO 和黑板镜像执行启动烟雾验证：镜像自动迁移到 head，`/api/profiles` 返回 default v1。容器、网络和应用测试线程均已清理；未使用 main 的 `.env` 或固定数据库端口。
- `python -m bbx_blackboard.openapi --check`、`git diff --check`、`docker compose --env-file /dev/null config --no-interpolate --quiet` 均通过。
- 原 embedding 路线在用户改设计前曾完成一次本地模型测试；随后已撤除代码、依赖、测试与工作树模型缓存。最终应用不包含模型下载步骤。

## 可复验命令

```sh
cd ~/blackboard-explorer
export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897
export all_proxy=socks5://127.0.0.1:7897 no_proxy=localhost,127.0.0.1,::1,host.docker.internal
export UV_CACHE_DIR=/private/tmp/bbx-uv-cache
export DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal'
uv sync --locked
make check
make image-blackboard test-integration
.venv/bin/python -m bbx_blackboard.openapi --check
```

人工启动平台可用 `make up`（由操作人准备环境配置）；已导出 SERVICE_TOKEN 后运行：

```sh
.venv/bin/python -m bbx_blackboard.simulator --base-url http://127.0.0.1:58000 --scenario demo --interval 0.5
curl -N -H "Authorization: Bearer $SERVICE_TOKEN" 'http://127.0.0.1:58000/api/tasks/<task-id>/stream?since=0'
```

上述模拟器不调用 DeepSeek；真实 Agent 验证属于后续 M2。完整模拟器和 SSE 行为已由本阶段自动化集成验证，不需要额外授权才能进入 M1-W。

## M1b 模拟器生命周期补修

M1-W 浏览器检查发现，原 demo 虽已写 `task.finished`，但 7 个 Agent 仍为 `running`，缺少 `agent.finished`，无法演示 Agent 节点退场。模拟器现按既定接口完成 3 个 judge、3 个 explore 和 1 个 final Agent：explore 在 closing 时先收到 `agent.conclude_requested`，再以正常回执结束；final 在提交报告后结束。集成测试确认任务 finished 时全部 7 个 Agent 为 finished，事件中恰有 7 条 `agent.finished`、3 条 `agent.conclude_requested`，SSE 从 `since=0` 回放逐条匹配完整有序事件流。独立复核命令 `uv sync --locked`、`make check`、`make test-integration` 全部通过，分别为普通测试 **141 passed**、集成测试 **11 passed**；没有调用真实模型。该补修只涉及模拟器、其 HTTP 集成测试和本报告；不改变服务接口或领域规则。

## 偏差与待决

- 用户取消 embedding 的决定覆盖原 M1a/M1b/M2b 向量预检要求。关键词查询只帮助定位原文，不进行语义判重。dry_run 保留为确定性校验接口。
- PostgreSQL 的 LISTEN 没有通配符，采用单连接按活跃订阅增删精确通道，已同步设计。
- profile 路径不能冻结历史模板正文，因此新增 `prompt_templates` 合同字段及 0002 迁移；本地 loader 读取正文，版本哈希包含正文。旧版本中无法恢复的历史正文不凭空补写；default 启动时创建带正文的新版本。
- 新增 0003 迁移移除派生向量列；旧事件保留但重放忽略该字段。初始迁移仍保留旧 vector 扩展兼容性，当前数据库镜像沿用已验证的 pgvector/pgvector:pg16；应用不依赖 pgvector Python 包，也不存向量。
- 原任务没有模拟器 HTTP 上传端点，增加受服务身份保护的 uploads；真正 runtime 仍可用 bbx_objects 直接上传。
- 人工 stop 直接进入既定 closing 流程，未增加未定义的 stop_requested 存储字段。
- workspace 目录树/文件预览按 M4 U.8 留待后续；当前仅提供归档下载。workspace_uri 的收尾写入由 runtime 生命周期阶段衔接。

## 共享文件与提交划分

共享修改：根 pyproject/uv.lock（objects workspace、HTTP/存储依赖、项目索引，移除向量依赖）；Makefile（检查objects、镜像与OpenAPI目标）；Compose（blackboard）；.dockerignore（排除开发缓存）；.env.example（移除EMBED配置）；contracts/profile loader/JSON Schema（提示词正文快照）；设计与M1b/M2b任务说明、M1a历史注记及HANDOFF。

已先提交设计：`6371507`（接口和生命周期）、`561103c`（profile正文快照与身份）、`7de008a`（Agent自主判重）。实现、迁移、测试、OpenAPI、文档与上述共享文件建议作为一个完整提交：`Implement M1b HTTP API and agent-owned duplicate judgment`，随后合并 main，再进入 M1-W。

后续模拟器生命周期补修在独立分支提交，只包含 `simulator.py`、`test_api_integration.py` 和本报告，不修改共享配置。
