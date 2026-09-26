# envd 执行环境

`bbx-exec-env:latest` 在容器内以 root 运行 envd，监听 8080。每任务传入独立的 `ENVD_TOKEN`；`/mcp` 和内部接口均需 `Authorization: Bearer <ENVD_TOKEN>`。`/mcp` 的 `execute_command` 还需 `X-Agent-Id: agent-1` 一类请求头，用户须先通过 `/users` 创建。

token 约定：agent-runtime 持有 `ENVD_TOKEN_SECRET`，以任务 UUID 的标准小写连字符字符串为消息，计算 HMAC-SHA256 的十六进制摘要作为 token，并在创建容器时以环境变量 `ENVD_TOKEN` 注入。envd 只读取 `ENVD_TOKEN`，不接收 `ENVD_TOKEN_SECRET`；命令子进程会移除这两个环境变量，同时保留出网代理配置。派生逻辑由后续 agent-runtime 任务实现。

## 构建

在仓库根目录执行以下命令，默认通过阿里云软件源构建两个镜像：

```sh
make image-exec-env image-egress-proxy
```

| Make 变量 / Docker 构建参数 | Make 默认值 | 用途 |
|---|---|---|
| `APT_MIRROR` | `http://mirrors.aliyun.com/ubuntu` | 替换 Ubuntu 主仓库与安全更新仓库的地址；值为不带末尾 `/` 的仓库 URL。 |
| `PIP_INDEX_URL` | `https://mirrors.aliyun.com/pypi/simple` | envd 的 pip 安装使用此索引 URL。 |
| `APK_MIRROR` | `mirrors.aliyun.com` | 替换 Alpine 仓库域名；值为不带协议和路径的主机名。 |

Make 将这些变量通过 `--build-arg` 传给对应的 Dockerfile；可以在命令行覆盖任意一项。将三项显式设为空即可使用官方源：

```sh
make image-exec-env image-egress-proxy APT_MIRROR= PIP_INDEX_URL= APK_MIRROR=
```

两个 Dockerfile 的参数默认均为空，直接运行 `docker build` 时默认保留官方源。这些参数只用于镜像构建，不配置容器运行时的出网代理。

若 Docker 构建直连镜像站超时，而宿主机通过本地 `7897` 代理可访问，可显式传入构建代理。BuildKit 中若无法解析 `host.docker.internal`，同时加上主机网关映射：

```sh
export DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal'
make image-exec-env image-egress-proxy
make test-integration
```

`DOCKER_BUILD_ARGS` 默认为空，用于向两个镜像目标传入额外 Docker 构建参数；构建和集成测试使用相同值可复用缓存。它不配置容器运行时环境。

镜像构建需要 Docker 可用；受限沙箱不能构建时需申请在沙箱外执行。`make test-integration` 依赖上述两个镜像构建目标；如需切换软件源，应同样传入对应变量。仓库 `.dockerignore` 排除真实 `.env`、Git 元数据与本地虚拟环境，避免它们进入构建上下文。

## 接口

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/health` | 无 | `{"status":"ok"}` |
| POST | `/users` | `{"agent_id":"agent-1"}` | `{"agent_id", "home"}`，幂等 |
| GET | `/files?path=` | `/workspace` 下绝对路径 | 文件字节；超限 413 |
| GET | `/stat?path=` | `/workspace` 下绝对路径 | `exists`、存在时的 `size`、`is_file`、`is_dir` |
| POST | `/archive` | 无 | `application/zstd` 的 tar.zst；`X-Archive-Fallback: none` 或 `agents-only` |
| POST/GET/DELETE | `/mcp` | MCP streamable HTTP | 仅 `execute_command(command, cwd, timeout_sec, privileged)` 工具 |

`execute_command` 返回 `exit_code`、`stdout`、`stderr`、`truncated`、`full_output_path`。stdout/stderr 以 `<command_output>` 标签包裹。默认工作目录是 `/workspace/agents/<id>`。输出总量超过 64 KiB 时显示头尾各 32 KiB，完整输出写入该 Agent 的 `.outputs/`。超时的退出码为 124。`privileged=true` 仅允许简单的白名单命令，默认前缀为 `apt-get install`、`apt-get update`、`pip install`、`npm install -g`。

配置环境变量：`ENVD_TOKEN`（必填）、`COMMAND_TIMEOUT_MAX`（默认 1200 秒）、`EVIDENCE_MAX_BYTES`（默认 50 MiB）、`ARCHIVE_MAX_BYTES`（默认 2 GiB）、`PRIVILEGED_PREFIXES`（逗号分隔）、`ARCHIVE_EXCLUDE`（逗号分隔）。

## 隔离运行

agent-runtime创建容器时应使用可写根文件系统、`cap_drop=ALL`，仅加`CHOWN,DAC_OVERRIDE,FOWNER,SETUID,SETGID,KILL`，`KILL`用于在超时时终止其他UID的Agent进程组；启用`no-new-privileges`，并按profile设置CPU、内存、PID上限。默认执行容器只接入有网关的exec网络，不设置代理。下例为**显式代理隔离模式**：exec网络改为internal，HTTP(S)代理变量指向同时接入exec与有网关网络的egress-proxy。

以下示例可直接运行；使用示例 token，请在实际任务中替换。结束后执行末尾清理命令。

```sh
make image-exec-env image-egress-proxy
docker network create --internal bbx-m2env-exec
docker run -d --name bbx-m2env-proxy --network bbx-m2env-exec --network-alias egress-proxy -e EGRESS_ALLOWLIST=example.com bbx-egress-proxy:latest
docker network connect bridge bbx-m2env-proxy
docker run -d --name bbx-m2env-envd --network bbx-m2env-exec --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SETUID --cap-add SETGID --cap-add KILL --security-opt no-new-privileges --cpus 2 --memory 4g --pids-limit 256 -e ENVD_TOKEN=replace-me -e HTTP_PROXY=http://egress-proxy:8888 -e HTTPS_PROXY=http://egress-proxy:8888 -e http_proxy=http://egress-proxy:8888 -e https_proxy=http://egress-proxy:8888 bbx-exec-env:latest
docker exec bbx-m2env-envd curl -fsS -H 'Authorization: Bearer replace-me' http://127.0.0.1:8080/health
docker rm -f bbx-m2env-envd bbx-m2env-proxy
docker network rm bbx-m2env-exec
```
