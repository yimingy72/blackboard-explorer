# 任务 M2a · 技术验证与 agent-runtime 骨架

依据：`docs/design/黑板系统实现架构.md` 第 0.2–0.3 节（模型与 MAF 核对结论）、2.4–2.5（执行环境隔离与生命周期）、5.1（进程结构）、6.1、6.3（Agent 组装、模型客户端）；`docs/design/黑板系统开发方案.md` 第 6 节（技术验证清单）、M2 的 2.5–2.7；`services/envd/README.md`（接口、容器运行参数、token 约定）；`docs/tasks/M1b-report.md`（接口清单）。

前置：M1b 与 M2-env 已合并到 `main`，镜像 `bbx-exec-env:latest` 已由用户构建。先阅读 `AGENTS.md`。

## 范围

`services/agent-runtime`：技术验证测试、配置、三个客户端（blackboard、envd、对象存储）、`ExecEnvManager`、`make_client`、测试替身 `ScriptedChatClient` 与 `FakeEnvd`。**不做**黑板工具、中间件、提示词、CLI（M2b），不做调度（M3）。

## 已定的实现决定

1. **依赖**：固定 `agent-framework-core==1.19.0` 与 `agent-framework-openai==1.14.4`，显式加入 `mcp>=1.24,<2`；不使用会引入无关 provider 的 `[all]` extra。另有 `docker>=7`、`httpx`、`jinja2`、`pyyaml`、工作区包 `bbx_contracts`、`bbx_objects`。2026-09-24 已按官方包元数据确认 core/openai/MCP 兼容；新增依赖时继续确认共用 uv.lock 无冲突。
2. **模型客户端**：DeepSeek 用 `OpenAIChatCompletionClient(model, api_key, base_url)`（**不是** `OpenAIChatClient`，后者对应 OpenAI Responses 接口）。构建时设置 `function_invocation_configuration["max_iterations"] = explore_max_steps + conclude_grace_calls + 5`（MAF 默认 40）与 `max_duration_seconds`。`reasoning_effort` 从 profile 读取，通过运行选项传入（具体参数名以技术验证结果为准）。
3. **ScriptedChatClient**：继承 MAF 的 `BaseChatClient`，实现 `_inner_get_response(*, messages, stream, options, **kwargs)`（以安装版本的源码为准）。脚本由若干步组成（返回工具调用 / 返回文本），支持条件跳转（例如"收到的消息含 conclude 指令时跳到交接步骤"）；记录每次收到的完整消息列表；返回与 DeepSeek 相同结构的用量（缓存命中 / 未命中 / 输出 / 推理 token）。
4. **FakeEnvd**：实现 envd 的 MCP 与内部接口（ASGI 应用，可被 httpx 与 MCP 客户端直接调用），命令执行用预设输出表，文件落在临时目录。
5. **ExecEnvManager**（Docker SDK）：
   - 容器名 `bbx-exec-<task_id 前 8 位>`；运行参数严格按 `services/envd/README.md`；资源来自 profile 的 `exec_resources`。
   - 网络：接入 compose 的 `exec` 网络（compose 会加项目前缀，网络名来自配置项 `EXEC_NETWORK`，默认 `blackboard-explorer_exec`）；环境变量 `HTTP(S)_PROXY` 指向 egress-proxy。
   - token：`ENVD_TOKEN = HMAC-SHA256(ENVD_TOKEN_SECRET, task_id)` 的十六进制，注入容器环境变量。
   - 方法：`provision(task_id, profile)`（固定任务的 profile 版本，创建并等待 `/health`，返回 handle）、`create_user(handle, agent_id)`、`archive_to_store(handle)`（流式读 `/archive`，临时文件中转后上传 `workspace/{task}.tar.zst`）、`destroy(task_id)`、`find(task_id)`（重启恢复用）。
6. **agent-runtime 访问 envd**：生产 `exec_access_mode=network`，按容器名访问 `http://bbx-exec-<id>:8080`。Docker Desktop 的 internal 网络不发布宿主机端口，因此宿主开发使用 `relay` 模式的任务级固定目标 TCP 中转，宿主随机端口只绑定 `127.0.0.1`；执行容器不加外部网络。详细约束按实现架构 2.4–2.5。

## 任务

| # | 内容 |
|---|---|
| R.1 | 技术验证（开发方案第 6 节全部 11 项）逐项写成测试，放在 `services/agent-runtime/tests/verification/`。需要真实 DeepSeek 的项（1、2、10、11）标记 `@pytest.mark.live`，不属于 `make check` 与 `make test-integration`；新增 `make test-live`（要求环境变量 `DEEPSEEK_API_KEY`，由用户运行）。其余项用 ScriptedChatClient / FakeEnvd 验证 |
| R.2 | 配置 `Settings`（服务地址、token、MinIO、Docker 网络、`MAX_RUNNING_TASKS`、`DEEPSEEK_*`） |
| R.3 | blackboard 客户端（服务身份 + 每个 agent 的 token）、envd 客户端（内部接口）、对象存储（`bbx_objects`） |
| R.4 | `ExecEnvManager` |
| R.5 | `make_client` 与 profile 加载 |
| R.6 | `ScriptedChatClient`、`FakeEnvd` |
| R.7 | 把技术验证结论写进报告的专门一节：每项"符合 / 不符合 → 采用的退路"。**任何不符合项都要明确影响到 M2b 的哪个设计点** |

## 测试

- 普通测试：客户端（对 FakeEnvd 与 mock 的 blackboard）、token 派生、profile 加载、ScriptedChatClient 自身。
- 集成测试：`ExecEnvManager` 完整生命周期（需要 `bbx-exec-env:latest`；镜像不存在时跳过并给出明确提示）：创建 → 健康 → 建用户 → 执行一条命令（经 MCP）→ 归档上传到 MinIO（testcontainers）→ 销毁，且没有残留容器。
- live 测试：由用户运行，报告中给出命令。

## 完成标准

1. `make check`、`make test-integration` 全绿（报告中给出结果）。
2. 技术验证报告完整：每项结论 + 退路 + 对 M2b 的影响。
3. 报告 `docs/tasks/M2a-report.md`：模块清单、客户端接口、验证结论、需要用户运行的 live 命令、共享文件修改、建议提交划分、偏差与待决。
