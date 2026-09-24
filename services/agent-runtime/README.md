# agent-runtime

M2a 提供客户端、任务执行容器管理和 MAF 验证基础；Agent 工具与运行入口由 M2b 补齐。

配置从进程环境读取，不自动加载 `.env`。必需项为 `DEEPSEEK_API_KEY`、`MINIO_ROOT_PASSWORD`、`SERVICE_TOKEN`、`ENVD_TOKEN_SECRET`；地址与网络项见 `.env.example`。JWT 签名密钥仅由 blackboard 持有，runtime 使用登记 Agent 时取得的 token。

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
make test-integration
# 仅在进程环境已有 DEEPSEEK_API_KEY 时运行，会产生模型费用
make test-live
```

普通与集成测试排除 `live` 标记。MAF 固定为 core 1.19.0、openai 1.14.4；升级时重跑 `tests/verification`。默认 enqueue 注入不能跨工具轮次保留，M2b 应使用公开 Content.text/result 追加黑板增量；不挂 MessageInjectionMiddleware。详见 `docs/tasks/M2a-report.md`。
