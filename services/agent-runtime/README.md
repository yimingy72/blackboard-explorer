# agent-runtime

提供 Agent 工具循环、黑板同步、证据持久化、用量记录，以及任务排队、调度、清扫和重启恢复。

配置从进程环境读取，不自动加载 `.env`。必需项为 `MINIO_ROOT_PASSWORD`、`SERVICE_TOKEN`、`ENVD_TOKEN_SECRET`；执行模型调用还需可信部署配置的 `RUNNER_GATEWAY_URL` 与任务冻结的 AgentGateway 模型。Runtime 不接收上游供应商密钥。JWT 签名密钥仅由 blackboard 持有，runtime 使用登记 Agent 时取得的 token。

`state` 和 `conclude` 仅需 `BLACKBOARD_URL` 与 `SERVICE_TOKEN`，不要求模型密钥或 Docker。

## 单 Agent 命令

```sh
uv sync --locked
make pi-install
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
make image-exec-env image-agent-runtime
make web-install web-build image-blackboard
# 主工作树默认项目名对应 blackboard-explorer_exec；使用其他项目名时同步 EXEC_NETWORK
docker compose --project-name blackboard-explorer up -d --wait
# 宿主机调试：配置好进程环境（含 PG 锁连接与 relay 网络）后
.venv/bin/bbx-runtime serve
```

可选代理模式另需 `make image-egress-proxy`。内置评测目标需叠加 `docker-compose.dev.yml`；完整启动和切换命令见[使用与部署](../../docs/使用与部署.md)。镜像构建代理参数见 `services/envd/README.md`。`serve` 在 Settings 之外要求 POSTGRES_HOST/PORT/USER/PASSWORD/DB，独立 PG 会话仅用于 advisory lock 和健康探测，不直接写黑板表；锁已被占用则退出，丢失连接立即停止派发。Compose 挂载 Docker socket，runtime 是可信控制服务，不把 socket、服务密钥或模型密钥传给执行环境。

`TaskSupervisor` 每 2 秒遍历任务列表所有分页。provisioning 按创建时间排队，running/closing、启动中与尚未完成归档清理的任务占容量；`MAX_RUNNING_TASKS` 默认 2，普通黑板与 CTF 共用这一容量，每个任务只计一次。每任务独立使用执行容器、黑板、Session、预算、流量与归档；停止或清理一个任务不会停止其他任务。调低容量不强停已有任务，等待容量释放后再派发。每任务 SchedulerLoop 用 SSE（500ms 去抖）唤醒，持续事件也不会延后超过 2 秒的兜底 tick；每 30 秒清扫心跳与交接超时。`decide(state, params, now)` 是纯函数，动作由 ActionExecutor 执行。

停止服务会先阻止新派发，再以 runtime_restart 取消并等待 finish；保留执行容器。重启结束旧记录（不增加 attempts/种子空产，不清零失败连击），健康容器接管；尚无容器的 provisioning 继续排队，running/closing 丢失或无法恢复容器则 failed。终态任务先完成交接，然后归档上传、服务 API 登记 task.archived、最后销毁；登记失败保留容器重试。归档事件会让工作台的下载入口即时出现。

执行容器默认直接出网：`EXEC_EGRESS_MODE=direct`配合带网关的exec网络（`EXEC_NETWORK_INTERNAL=false`），运行时不给Agent命令注入代理变量。任务的`egress_allowlist`只记录需求，不限制默认出网。需要白名单时显式设`EXEC_EGRESS_MODE=proxy`、`EXEC_NETWORK_INTERNAL=true`，并用`docker compose --profile egress-proxy up -d`启动可选代理；此时部署级`EGRESS_ALLOWLIST`实际生效，任务字段不会动态改变它。两种模式配错网络时运行时拒绝创建执行容器。Runtime 发往固定 AgentGateway 的私有模型请求不读取环境代理，也不跟随重定向；上游网络由 AgentGateway 管理。

## 执行与同步

`AgentRunner(settings, service, objects, manager).run_agent(task_id, agent_id, task_type, intent_id=None, mode=None, *, agent_token)` 运行一个已登记 Agent。它读取任务固定版本 profile、组装工具与模板，并使用显式持久会话接入 PI 原生循环。单次模型 HTTP 请求超时不超过 120 秒，整个 run 的硬时限为任务时长加交接宽限及一分钟清扫余量；调度器仍按原探索预算进入 closing。正常、拒绝、运行错误都调用 finish；取消会完成 finish 与资源清理后再传播。测试可以注入 ScriptedChatClient 和 FakeEnvd HTTP 客户端。

工具按 explore / derive / close 分别装配，只有 explore 有 MCP execute_command。post_fact 直接提交，Agent 自行判断重复；证据文件上限 50 MiB，由 envd 检查路径与权限，上传后替换为对象 URI。模型不能自报 `uri` / `auto`。携带真实 call_id 的证据会附加完整工具记录。read_evidence 返回带边界且转义的证据数据。

ToolLog 是最外层函数中间件：写完整记录、登记 call_id、追加返回 ID；普通工具异常也落日志。GraceGate 通过服务端原子接口消耗额度，最后一次成功返回 0 仍允许执行。BoardSync 在每次 explore 模型请求前追加增量到公开 Content，保留全文点名/裁定、压缩其他事件；derive/close 仅心跳记账。所有角色都从固定版本的模板正文渲染，Jinja 使用 sandbox 与严格变量检查。

回执按 explore / derive / close 的 contracts 校验，支持代码块与尾部 JSON；无效文本记为规定的回退回执并保留 raw_text，不把回执当作事实发布。

每个新 Agent 还记录实际渲染的初始上下文、真正追加的黑板增量和逐轮模型输出。正文写入 `traces/{task_id}/{agent_id}/`，通过服务接口登记 `agent.trace.recorded` 元数据事件，工作台按需读取。DeepSeek 实际返回的 `reasoning_content` 作为记录元数据保留，不混入下一轮请求的普通回复文本；没有返回推理文本就不显示推理。Trace 写入失败记录警告，不妨碍模型用量记账和交接；旧任务无记录时不能事后还原。该记录机制不改变 Fact/Intent 和调度判断。

## 价格

AgentGateway 是新模型费率和原生 USD 费用的唯一来源。黑板旧 CNY 账本按历史冻结配置保留；新黑板角色估算来自冻结的网关费率与可信汇率，不与原生账单相加。任务和成果整理分别使用原额度的原生密钥；终态复盘使用独立的共享统计密钥。缺少或不一致的用量保持未知。

## 模块接口

- `models.make_gateway_client(…)`：使用任务冻结模型，通过原 Pi 协议适配向指定 AgentGateway 发送。每次请求在实际发送前取得原范围的可信授权与费用关联身份。
- `models.load_runtime_profile(Path | Mapping)`：本地目录或黑板固定版本快照；快照包含提示词正文。
- `clients.BlackboardClient(url, token)`：异步上下文管理器，封装任务、状态、事件、快照、对象、证据、事实/意图、Agent 生命周期与用量接口。`with_token(agent_token)` 共用连接池、绑定 Agent 身份；服务 token 不能代替 Agent 写入身份。
- `clients.EnvdClient(url, token)`：健康、用户创建、文件 stat/read/stream、archive stream。
- `clients.object_store(settings)`：共用 `bbx_objects.ObjectStore`。
- `execenv.ExecEnvManager(settings)`：`provision(task, profile)`、`find(task)`、`create_user(handle, agent)`、`archive_to_store(handle)`、`destroy(task)`。归档返回 URI、字节数与 fallback；不隐式改变黑板状态。
- `testing.ScriptedChatClient`：执行PI 原生工具循环的脚本模型，支持工具、文本、用量、条件跳转与输入记录。
- `testing.FakeEnvd`：ASGI/MCP 替身，预设命令结果，临时目录文件，不执行 shell。

## 执行容器网络

生产使用`EXEC_ACCESS_MODE=network`，runtime与envd在`EXEC_NETWORK`指定的exec网络内通信，默认该网络有网关。宿主机开发可设`relay`：单独中转容器只转发到该任务envd:8080，宿主端口随机分配且只绑定127.0.0.1。执行容器不发布端口；relay只改变宿主机访问envd的方式，不改变所选出网模式。执行容器的 `pids_limit` 不低于 `EXEC_MIN_PIDS`（默认 1024），为同一任务内的多 Agent 命令、抓包和扫描进程保留足够配额。

容器按完整任务 UUID 标记归属，短名称冲突会报错；provision 失败清理本次新建容器。`ENVD_TOKEN` 由 runtime 的 secret 和规范 UUID 字符串经 HMAC-SHA256 派生。工具进程看不到该 secret/token。

## 验证

```sh
make check
# Docker 构建代理参数见 services/envd/README.md
make test-integration # 同时构建执行环境、代理与运行器镜像；不调用模型
```

PI 三个组件固定为 1.4.0，Node.js 最低 22.21。`make check` 同时执行 Node 原生组件检查、Ruff、Pyright 和脚本模型回归，不调用真实模型。升级时重跑协议、会话、执行围栏与 Docker 垂直检查。工具循环实际运行在 `pi-agent-core`，模型协议由 `pi-ai` 完成；Python 的 `pi/` 目录只保留持久格式、业务 hook 和进程边界适配。


## 持久会话与干预

探索运行保留平台 `AgentSession` 快照与逐模型调用持久化，兼容旧 MAF 历史并保存 PI 原生元信息；初始指令单独保存，工具执行中断的未配对调用明确标为结果未知，恢复时不重跑旧命令。模型调用前认领用户消息，以稳定消息 ID 注入；消息确认与 checkpoint 同事务。CAS、claim token 与租约防止覆盖或重复回复；不确定提交读回对账。

终态复盘对不同 Agent 最多四路并行，同一 Agent 串行。用户可向已结束的 Agent 或终态任务发起复盘，工具限于 get/search/read_evidence，恢复原会话回答用户；没有原生会话时标为 legacy。复盘单次最长180秒、最多8轮工具迭代，用量保存于消息，不重开任务或改变验收。

Supervisor 处理删除标记时停止残余cleanup、销毁执行容器，再调用幂等purge；普通归档不删除会话。derive 有新事实与空闲槽位时可提前并行，登记时锁内核对阶段和事实版本；无效的过期派发跳过。
