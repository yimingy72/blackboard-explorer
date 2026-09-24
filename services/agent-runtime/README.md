# agent-runtime

提供 Agent 工具循环、黑板同步、证据持久化、用量记录，以及任务排队、调度、清扫和重启恢复。M3b 补齐完整闭环场景与显式付费 e2e。

配置从进程环境读取，不自动加载 `.env`。必需项为 `DEEPSEEK_API_KEY`、`MINIO_ROOT_PASSWORD`、`SERVICE_TOKEN`、`ENVD_TOKEN_SECRET`；地址与网络项见 `.env.example`。JWT 签名密钥仅由 blackboard 持有，runtime 使用登记 Agent 时取得的 token。

`state` 和 `conclude` 仅需 `BLACKBOARD_URL` 与 `SERVICE_TOKEN`，不要求模型密钥或 Docker。

## 单 Agent 命令

```sh
uv sync --locked
bbx-runtime run-agent --task <任务UUID> --type explore --seed
bbx-runtime run-agent --task <任务UUID> --type explore --intent I1
bbx-runtime run-agent --task <任务UUID> --type derive
bbx-runtime run-agent --task <任务UUID> --type close --mode judge
bbx-runtime run-agent --task <任务UUID> --type close --mode final
bbx-runtime conclude --task <任务UUID> --agent agent-1
bbx-runtime state --task <任务UUID>
```

可将 `bbx-runtime` 替换为 `.venv/bin/bbx-runtime`。如由使用者显式选择加载本地配置，可使用 `uv run --env-file <本地配置文件> bbx-runtime …`；程序本身不自动读取文件，也不打印密钥。

任务需先通过黑板 API/网页创建。单 Agent CLI 在 explore 首次运行时启动任务、创建执行容器，登记 Agent、认领指定意图并建立 Linux 用户；会先输出 Agent ID，方便另一个终端发送 conclude。默认容器保留供后续 Agent 复用。final close 只允许在 closing 任务启动。单 Agent CLI 用于调试，不应与同一任务的调度服务同时运行。

## 调度服务与部署

```sh
make image-exec-env image-egress-proxy image-agent-runtime
make web-install web-build image-blackboard
# 主工作树默认项目名对应 blackboard-explorer_exec；使用其他项目名时同步 EXEC_NETWORK
docker compose --project-name blackboard-explorer up -d
# 宿主机调试：配置好进程环境（含 PG 锁连接与 relay 网络）后
.venv/bin/bbx-runtime serve
```

镜像构建代理参数见 `services/envd/README.md`。`serve` 在 Settings 之外要求 POSTGRES_HOST/PORT/USER/PASSWORD/DB，独立 PG 会话仅用于 advisory lock 和健康探测，不直接写黑板表；锁已被占用则退出，丢失连接立即停止派发。Compose 挂载 Docker socket，runtime 是可信控制服务，不把 socket、服务密钥或模型密钥传给执行环境。

`TaskSupervisor` 每 2 秒遍历任务列表所有分页。provisioning 按创建时间排队，running/closing 与尚未完成归档清理的任务占容量；`MAX_RUNNING_TASKS` 默认 1。每任务 SchedulerLoop 用 SSE（500ms 去抖）唤醒，持续事件也不会延后超过 2 秒的兜底 tick；每 30 秒清扫心跳与交接超时。`decide(state, params, now)` 是纯函数，动作由 ActionExecutor 执行。

停止服务会先阻止新派发，再以 runtime_restart 取消并等待 finish；保留执行容器。重启结束旧记录（不增加 attempts/种子空产，不清零失败连击），健康容器接管；尚无容器的 provisioning 继续排队，running/closing 丢失或无法恢复容器则 failed。终态任务先完成交接，然后归档上传、服务 API 登记 task.archived、最后销毁；登记失败保留容器重试。归档事件会让工作台的下载入口即时出现。

v1 **实际出网限制是部署级 EGRESS_ALLOWLIST**，所有执行容器共享；任务的 egress_allowlist 记录需求，不动态改代理，也不额外收窄部署策略。可把 eval-targets 和依赖镜像源列在部署白名单中。runtime 访问模型可使用独立 RUNTIME_HTTP_PROXY/RUNTIME_HTTPS_PROXY（本机 Docker Desktop 例：`http://host.docker.internal:7897`）；黑板与 envd HTTP 客户端不读取外部代理变量。

## 执行与同步

`AgentRunner(settings, service, objects, manager).run_agent(task_id, agent_id, task_type, intent_id=None, mode=None, *, agent_token)` 运行一个已登记 Agent。它读取任务固定版本 profile、组装工具与模板，并创建显式 MAF session。单次模型 HTTP 请求超时不超过 120 秒，整个 run 的硬时限为任务时长加交接宽限及一分钟清扫余量；调度器仍按原探索预算进入 closing。正常、拒绝、运行错误都调用 finish；取消会完成 finish 与资源清理后再传播。测试可以注入 ScriptedChatClient 和 FakeEnvd HTTP 客户端。

工具按 explore / derive / close 分别装配，只有 explore 有 MCP execute_command。post_fact 直接提交，Agent 自行判断重复；证据文件上限 50 MiB，由 envd 检查路径与权限，上传后替换为对象 URI。模型不能自报 `uri` / `auto`。携带真实 call_id 的证据会附加完整工具记录。read_evidence 返回带边界且转义的证据数据。

ToolLog 是最外层函数中间件：写完整记录、登记 call_id、追加返回 ID；普通工具异常也落日志。GraceGate 通过服务端原子接口消耗额度，最后一次成功返回 0 仍允许执行。BoardSync 在每次 explore 模型请求前追加增量到公开 Content，保留全文点名/裁定、压缩其他事件；derive/close 仅心跳记账。所有角色都从固定版本的模板正文渲染，Jinja 使用 sandbox 与严格变量检查。

回执按 explore / derive / close 的 contracts 校验，支持代码块与尾部 JSON；无效文本记为规定的回退回执并保留 raw_text，不把回执当作事实发布。

## 价格

默认 profile 使用 2026-09-24 [DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/)的 USD 高峰单价，按每百万 token 计价：缓存命中 0.006、未命中 0.30、输出 1.20。推理 token 已含在输出中，不重复收费。这是固定版本的保守预算估算；错峰费率为一半，需在运行用 profile 中明确选定对应价格再核对账单。`off_peak` 是该价格快照的标记，不会动态切换时段。

## 模块接口

- `models.make_client(model, api_key=…, explore_max_steps=…, conclude_grace_calls=…, max_duration_seconds=…)`：固定版本 MAF 的 DeepSeek Chat Completions 客户端，保留缓存 token 计数。`model_run_options` 传入 profile 的 `reasoning_effort`。
- `models.load_runtime_profile(Path | Mapping)`：本地目录或黑板固定版本快照；快照包含提示词正文。
- `clients.BlackboardClient(url, token)`：异步上下文管理器，封装任务、状态、事件、快照、对象、证据、事实/意图、Agent 生命周期与用量接口。`with_token(agent_token)` 共用连接池、绑定 Agent 身份；服务 token 不能代替 Agent 写入身份。
- `clients.EnvdClient(url, token)`：健康、用户创建、文件 stat/read/stream、archive stream。
- `clients.object_store(settings)`：共用 `bbx_objects.ObjectStore`。
- `execenv.ExecEnvManager(settings)`：`provision(task, profile)`、`find(task)`、`create_user(handle, agent)`、`archive_to_store(handle)`、`destroy(task)`。归档返回 URI、字节数与 fallback；不隐式改变黑板状态。
- `testing.ScriptedChatClient`：执行真实 MAF 工具循环的脚本模型，支持工具、文本、用量、条件跳转与输入记录。
- `testing.FakeEnvd`：ASGI/MCP 替身，预设命令结果，临时目录文件，不执行 shell。

## 执行容器网络

生产使用 `EXEC_ACCESS_MODE=network`，runtime 与 envd 在 `EXEC_NETWORK` 指定的 internal 网络内通信。宿主机开发可设 `relay`：单独中转容器只转发到该任务 envd:8080，宿主端口随机分配且只绑定 127.0.0.1。执行容器始终只连 internal 网络、不发布端口，出网仍经过 egress-proxy。

容器按完整任务 UUID 标记归属，短名称冲突会报错；provision 失败清理本次新建容器。`ENVD_TOKEN` 由 runtime 的 secret 和规范 UUID 字符串经 HMAC-SHA256 派生。工具进程看不到该 secret/token。

## 验证

```sh
make check
# Docker 构建代理参数见 services/envd/README.md
make test-integration # 同时构建执行环境、代理与运行器镜像；不调用模型
# 仅在进程环境已有 DEEPSEEK_API_KEY 时运行，会产生模型费用
make test-live
```

普通与集成测试排除 `live` 标记。MAF 固定为 core 1.19.0、openai 1.14.4；升级时重跑 `tests/verification`。默认 enqueue 注入不能跨工具轮次保留，运行器使用公开 Content.text/result 追加黑板增量，不挂 MessageInjectionMiddleware。详见 `docs/tasks/M2a-report.md` 和 `docs/tasks/M2b-report.md`。


## 真实闭环检查（会调用 DeepSeek）

```sh
# 密钥已由外部注入进程环境时
make e2e
# 或由 uv 子进程显式加载用户选择的本地配置文件
make e2e E2E_ENV_FILE=~/blackboard-explorer/.env
# 保留隔离工作台供浏览器复盘；--timeout 只限制任务等待时间
make e2e E2E_ENV_FILE=~/blackboard-explorer/.env E2E_ARGS='--keep --timeout 1800'
```

先按上面的部署命令构建前端和四个镜像；每次修改实现后重新构建对应镜像。该命令生成已有玩具素材，使用已构建镜像，以唯一 `bbx-e2e-*` Compose 项目启动全栈。黑板仅发布 localhost 随机端口，PG/MinIO 不发布宿主端口，控制凭据为本次随机生成，只有模型配置使用提供的 DEEPSEEK_API_KEY。Compose 文件不保存密钥值；每条 Compose 命令另有 180 秒上限。

控制台会显示任务/工作台地址与进展。隔离测试账号是 `e2e / local-e2e-only`，只用于这套临时服务。任务结束后输出报告和账本费用估算，并在 `.data/e2e/<项目名>/` 保存报告、事件、状态、工作区压缩包和脱敏日志。默认停止服务并清理本项目卷与执行容器；`--keep` 仅在任务已终结并登记归档后保留工作台，并打印清理命令。

生产请求去重仍由 Agent 判断；e2e 不使用 embedding，也不会让普通检查调用模型。固定 profile 的价格是预算估算，不能代替供应商实际账单。
