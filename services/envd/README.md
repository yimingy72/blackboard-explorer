# envd 执行环境

`bbx-exec-env:latest` 在容器内以 root 运行 envd，监听 8080。每任务传入独立的 `ENVD_TOKEN`；`/mcp` 和内部接口均需 `Authorization: Bearer <ENVD_TOKEN>`。`/mcp` 的 `execute_command` 还需 `X-Agent-Id: agent-1` 一类请求头，用户须先通过 `/users` 创建。

token 约定：agent-runtime 持有 `ENVD_TOKEN_SECRET`，以任务 UUID 的标准小写连字符字符串为消息，计算 HMAC-SHA256 的十六进制摘要作为 token，并在创建容器时以环境变量 `ENVD_TOKEN` 注入。envd 只读取 `ENVD_TOKEN`，不接收 `ENVD_TOKEN_SECRET`；命令子进程会移除这两个环境变量。默认直连模式不注入代理变量；显式代理隔离模式才保留代理配置。派生逻辑由 agent-runtime 实现。

## 构建

在仓库根目录构建执行环境镜像；只有使用代理隔离模式时才额外构建可选代理镜像：

```sh
make image-exec-env
# 显式代理隔离模式另运行：make image-egress-proxy
```

| Make 变量 / Docker 构建参数 | Make 默认值 | 用途 |
|---|---|---|
| `APT_MIRROR` | `http://mirrors.aliyun.com/ubuntu` | 替换 Ubuntu 主仓库与安全更新仓库的地址；值为不带末尾 `/` 的仓库 URL。 |
| `PIP_INDEX_URL` | `https://mirrors.aliyun.com/pypi/simple` | envd 的 pip 安装使用此索引 URL。 |
| `APK_MIRROR` | `mirrors.aliyun.com` | 替换 Alpine 仓库域名；值为不带协议和路径的主机名。 |

Make 将这些变量通过 `--build-arg` 传给对应的 Dockerfile；可以在命令行覆盖任意一项。将三项显式设为空即可使用官方源：

```sh
make image-exec-env APT_MIRROR= PIP_INDEX_URL=
# 可选代理镜像：make image-egress-proxy APK_MIRROR=
```

两个 Dockerfile 的参数默认均为空，直接运行 `docker build` 时默认保留官方源。这些参数只用于镜像构建，不配置容器运行时的出网代理。

若 Docker 构建直连镜像站超时，而宿主机通过本地 `7897` 代理可访问，可显式传入构建代理。BuildKit 中若无法解析 `host.docker.internal`，同时加上主机网关映射：

```sh
export DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal'
make image-exec-env
# 可选代理模式还需：make image-egress-proxy
make test-integration
```

`DOCKER_BUILD_ARGS` 默认为空，用于向镜像构建目标传入额外 Docker 构建参数；构建和集成测试使用相同值可复用缓存。它不配置容器运行时环境。

镜像构建需要 Docker 可用。`make test-integration` 会按 Makefile 构建集成测试需要的执行环境、代理、运行器与评测环境镜像，即使日常部署默认直连；如需切换软件源，应同样传入对应变量。仓库 `.dockerignore` 排除真实 `.env`、Git 元数据与本地虚拟环境，避免它们进入构建上下文。

## 接口

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/health` | 无 | `{"status":"ok","runtime_audit":true}` |
| GET | `/audit` | 无 | `{"format":"bbx.runtime-audit.v1","commands":"<JSONL>","sudo":"<text>"}`；日志缺失 503、合计超过 32 MiB 返回 413 |
| POST | `/users` | `{"agent_id":"agent-1"}` | `{"agent_id", "home"}`，幂等 |
| GET | `/files?path=` | `/workspace` 下绝对路径 | 文件字节；超限 413 |
| GET | `/stat?path=` | `/workspace` 下绝对路径 | `exists`、存在时的 `size`、`is_file`、`is_dir` |
| POST | `/archive` | 无 | `application/zstd` 的 tar.zst；`X-Archive-Fallback: none` 或 `agents-only` |
| POST/GET/DELETE | `/mcp` | MCP streamable HTTP | 仅 `execute_command(command, cwd, timeout_sec, privileged)` 工具 |

`execute_command` 返回 `exit_code`、`stdout`、`stderr`、`truncated`、`full_output_path` 和 `execution`（命令 ID、Agent、目录、起止时间、耗时、状态、提权请求标记）。stdout/stderr 以 `<command_output>` 标签包裹。默认工作目录是 `/workspace/agents/<id>`。输出总量超过 64 KiB 时显示头尾各 32 KiB，完整输出写入该 Agent 的 `.outputs/`。超时的退出码为 124。普通命令以 Agent 用户运行；`privileged=true` 由该用户通过 `sudo -n` 执行任意 shell 命令。Agent 也可在普通命令中自行调用 `sudo -n`。

envd 将每次命令的开始与结果写入 root 持有的 `/var/log/bbx/commands.jsonl`；sudo 将实际提权调用和退出状态写入 `/var/log/bbx/sudo.log`。两个文件在服务启动时建立，`/audit` 返回其文本快照。若文件后来被 root 删除，接口返回 503，不能视作完整审计。已获 root 的进程可篡改容器内日志；sudo 对 shell 或脚本记录一次调用及退出，不会逐条记录内部子命令。归档前由 runtime 持久化快照，`execution` 随已有工具记录持久化。

配置环境变量：`ENVD_TOKEN`（必填）、`COMMAND_TIMEOUT_MAX`（默认 1200 秒）、`EVIDENCE_MAX_BYTES`（默认 50 MiB）、`ARCHIVE_MAX_BYTES`（默认 2 GiB）、`ARCHIVE_EXCLUDE`（逗号分隔）。旧 `PRIVILEGED_PREFIXES` 仅保留配置读取兼容，不再限制执行。

## 隔离运行

agent-runtime 创建容器时使用可写根文件系统，保留 Docker 默认 capabilities、增加 `NET_ADMIN`，挂载 `/dev/net/tun`，并按 profile 设置 CPU、内存、PID 上限。执行容器不设置 `no-new-privileges`；不使用 Docker privileged 模式、宿主网络或 Docker socket。默认执行容器只接入有网关的 exec 网络，不设置代理。下例为**显式代理隔离模式**：exec 网络改为 internal，HTTP(S) 代理变量指向同时接入 exec 与有网关网络的 egress-proxy。

以下示例可直接运行；使用示例 token，请在实际任务中替换。结束后执行末尾清理命令。

```sh
make image-exec-env image-egress-proxy
docker network create --internal bbx-m2env-exec
docker run -d --name bbx-m2env-proxy --network bbx-m2env-exec --network-alias egress-proxy -e EGRESS_ALLOWLIST=example.com bbx-egress-proxy:latest
docker network connect bridge bbx-m2env-proxy
docker run -d --name bbx-m2env-envd --network bbx-m2env-exec --cap-add NET_ADMIN --device /dev/net/tun --cpus 2 --memory 4g --pids-limit 256 -e ENVD_TOKEN=replace-me -e HTTP_PROXY=http://egress-proxy:8888 -e HTTPS_PROXY=http://egress-proxy:8888 -e http_proxy=http://egress-proxy:8888 -e https_proxy=http://egress-proxy:8888 bbx-exec-env:latest
docker exec bbx-m2env-envd curl -fsS -H 'Authorization: Bearer replace-me' http://127.0.0.1:8080/health
docker rm -f bbx-m2env-envd bbx-m2env-proxy
docker network rm bbx-m2env-exec
```
