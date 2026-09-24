# 任务 M2-env · 执行环境与出网代理

依据：`docs/design/黑板系统实现架构.md` 第 2 节（执行环境）、第 10 节（安全）、第 11 节（部署）；`docs/design/黑板系统开发方案.md` M2 的 2.1–2.4 与检查点中关于 envd 的部分。

先阅读 `AGENTS.md`（尤其第 7、8 节）。

## 范围

只做 `services/envd`（envd 服务与执行环境镜像）和 `services/egress-proxy`，以及它们的测试。**不做** agent-runtime 侧的 ExecEnvManager（M2 后续批次），不接触黑板服务。

## 已定的实现决定

1. envd 是一个 ASGI 应用（Starlette 或 FastAPI），监听 8080：
   - `/mcp`：用官方 MCP Python SDK（`mcp` 包）的 streamable HTTP 传输，只暴露一个工具 `execute_command`。
   - 内部接口 `/health`、`/users`、`/files`、`/stat`、`/archive`：`Authorization: Bearer <ENVD_TOKEN>`，token 由容器环境变量提供；`/mcp` 同样校验该 token（agent-runtime 连接时带上）。
2. Agent 身份来自请求头 `X-Agent-Id`；只接受 `^agent-[0-9]{1,6}$`。未知 agent（未经 `/users` 创建）调用 `execute_command` 时拒绝。
3. 命令执行：`runuser -u <agent_id> -- bash -lc <command>`，新会话（独立进程组），超时向整个进程组发 SIGTERM，2 秒后 SIGKILL。
4. 执行环境镜像基于 `ubuntu:24.04`，自带 Python 3.12；envd 安装在 `/opt/envd` 的独立虚拟环境中，Agent 用户不可写。镜像名 `bbx-exec-env:latest`。
5. 出网代理用 tinyproxy（基于 `alpine:3.20`），白名单来自环境变量 `EGRESS_ALLOWLIST`（逗号分隔的域名）。镜像名 `bbx-egress-proxy:latest`。

## 任务

| # | 内容 |
|---|---|
| E.1 | `execute_command(command, cwd=None, timeout_sec=120, privileged=False)`：实现架构 2.2 的全部语义——默认 cwd、`timeout_sec` 上限（配置项，默认 1200）、64KB 截断（头尾各 32KB）并把全文写入 `/workspace/agents/<id>/.outputs/<seq>.txt`（属主为该 agent）、`privileged=true` 仅放行白名单前缀（配置项，默认 `apt-get install`、`apt-get update`、`pip install`、`npm install -g`）且以 root 执行、返回 `{exit_code, stdout, stderr, truncated, full_output_path}`，其中 stdout/stderr 包裹在 `<command_output>…</command_output>` 中 |
| E.2 | `/users`：创建 Linux 用户（家目录 = 工作目录），幂等；启动时建立 `/workspace`、`/workspace/shared`（1777）、`/workspace/agents`；目录与权限按实现架构 2.3 |
| E.3 | `/files?path=`（字节流；超过 `evidence_max_bytes`，默认 50MB，返回 413；只允许 `/workspace` 下的路径，拒绝穿越与指向外部的符号链接）、`/stat?path=` |
| E.4 | `/archive`：tar.zst 流式返回；排除规则默认 `**/.git/objects/`、`**/node_modules/`、`**/__pycache__/`、`**/target/`、`**/*.o`；超过 `archive_max_bytes`（默认 2GB）时退化为只打包 `agents/`，并在响应头中标明 |
| E.5 | 执行环境镜像 Dockerfile（git、curl、python3、jq、build-essential、ca-certificates、zstd、tini 作为 PID 1）；构建命令加到 `Makefile`（`make image-exec-env`） |
| E.6 | 出网代理：Dockerfile、按 `EGRESS_ALLOWLIST` 生成配置、非白名单请求返回 403；构建命令 `make image-egress-proxy`；在 `docker-compose.yml` 中追加 `egress-proxy` 服务（接入 `exec` 与默认网络） |
| E.7 | 文档：`services/envd/README.md` 写明运行容器所需的隔离参数（实现架构 2.4：cap_drop ALL + 所需 capability、no-new-privileges、资源限制、只接入 exec 网络并设置 HTTP(S)_PROXY），并给出一条可直接执行的 `docker run` 示例。agent-runtime 后续按此创建容器 |

## 测试

- **普通测试**：截断算法、白名单前缀匹配（注意 `apt-get install x; rm -rf /` 这类拼接必须被拒绝——只允许不含 shell 元字符的简单命令）、路径校验、agent id 校验、配置解析。
- **集成测试**（`@pytest.mark.integration`，先构建镜像，再以实现架构 2.4 的隔离参数运行容器，容器名加前缀 `bbx-m2env-` 并在测试后清理）：
  - agent-1 不能写 agent-2 的目录，但能读
  - 在 shared 中 agent-2 不能删除 agent-1 创建的文件
  - 超时杀掉整个进程组（命令里起一个后台子进程，超时后它也不存在）
  - 超过 64KB 的输出被截断，`full_output_path` 可读且内容完整
  - 非白名单的 privileged 命令被拒绝；白名单内的命令以 root 执行
  - `/files` 超限返回 413，路径穿越被拒绝
  - `/archive` 结果可解压，排除规则生效
  - 通过 MCP 客户端（`mcp` 包，streamable HTTP，带 `X-Agent-Id` 与 token）调用 `execute_command` 成功；缺 token 或未知 agent 被拒绝
  - 出网代理：非白名单域名返回 403（不需要外网即可验证）

## 完成标准

1. `make check` 全绿。
2. `make image-exec-env`、`make image-egress-proxy` 成功。
3. `make test-integration` 中本任务的测试全绿（报告中给出结果）。
4. 报告写到 `docs/tasks/M2-env-report.md`：envd 接口清单（给 agent-runtime 使用）、容器运行参数、对共享文件的修改清单、建议的提交划分、偏差与待决。
