# M2-env 任务报告

## 完成了什么

| 编号 | 结果 |
|---|---|
| E.1 | 实现官方 MCP SDK 的 `/mcp`，仅暴露 `execute_command`；按 `X-Agent-Id` 切换用户，校验超时和 privileged 简单命令白名单，超时终止进程组，64 KiB 头尾截断并将全文存入该 Agent 的 `.outputs/`。结果字段为 `exit_code`、`stdout`、`stderr`、`truncated`、`full_output_path`；两个输出字段均用 `<command_output>` 包裹。 |
| E.2 | 实现 Bearer token 保护的幂等 `/users`；启动时创建 `/workspace`、`agents`、`shared`，分别设置 755、755、1777；Agent 工作目录属主为本人且权限 755。 |
| E.3 | 实现 `/files` 字节流和 `/stat`；限制路径在 `/workspace` 下，拒绝 `..` 和指向外部的符号链接；文件超限返回 413。 |
| E.4 | 实现 `/archive` 的 tar.zst 流式响应、默认排除规则、大小超限时只打包 `agents/`，用 `X-Archive-Fallback` 标记。 |
| E.5 | 添加 Ubuntu 24.04 执行镜像，安装工具链和 tini；envd 位于 Agent 不可写的 `/opt/envd` 虚拟环境；增加 `make image-exec-env`。 |
| E.6 | 添加 Alpine 3.20 + tinyproxy 镜像，按 `EGRESS_ALLOWLIST` 生成域名过滤配置；增加 `make image-egress-proxy`，在 Compose 中将代理连接到 `exec` 与默认网络。 |
| E.7 | `services/envd/README.md` 记录接口、配置、隔离参数与可直接运行的 Docker 示例。 |

### 给 agent-runtime 的接口

所有接口都要求 `Authorization: Bearer <ENVD_TOKEN>`。MCP 连接还需 `X-Agent-Id: agent-<1–6 位数字>`，调用命令前先创建用户。

| 方法 | 路径 | 输入 | 输出 |
|---|---|---|---|
| GET | `/health` | 无 | `{"status":"ok"}` |
| POST | `/users` | `{"agent_id":"agent-1"}` | `agent_id`、`home` |
| GET | `/files?path=<absolute>` | `/workspace` 内文件 | `application/octet-stream`；超过 `EVIDENCE_MAX_BYTES` 返回 413 |
| GET | `/stat?path=<absolute>` | `/workspace` 内路径 | `exists`；存在时附 `size`、`is_file`、`is_dir` |
| POST | `/archive` | 无 | `application/zstd` 的 tar.zst；响应头 `X-Archive-Fallback: none|agents-only` |
| MCP streamable HTTP | `/mcp` | `execute_command(command, cwd=None, timeout_sec=120, privileged=False)` | `exit_code`、`stdout`、`stderr`、`truncated`、`full_output_path` |

容器运行参数：`cap_drop=ALL`，`cap_add=CHOWN,DAC_OVERRIDE,FOWNER,SETUID,SETGID`，`no-new-privileges`，可写根文件系统，按 profile 设 CPU、内存、PID 上限，只接入内部 `exec` 网络，并把 HTTP(S) 代理环境变量指向同时连接 `exec` 与外部网络的 egress-proxy。README 中的示例使用 2 CPU、4 GiB、256 PID。

## 如何验证及实际结果

```sh
uv sync
make check
make image-exec-env
make image-egress-proxy
make test-integration
```

- `make check`：实际通过，ruff format/lint、pyright 0 错误、54 个普通测试通过；其中 MCP 测试通过官方 SDK 初始化、工具枚举及未知 Agent 拒绝；没有调用真实模型 API。
- `pytest --collect-only -q -m integration`：实际成功收集本任务 2 个集成测试。集成测试覆盖用户目录隔离、shared sticky bit、超时子进程、截断全文、privileged 白名单、文件路径及上限、归档、MCP 鉴权和代理拒绝。
- 本机归档烟雾检查：实际成功生成和解压 tar.zst，保留普通文件并排除 `node_modules`。
- `docker compose config --no-interpolate --quiet` 与 `sh -n services/egress-proxy/entrypoint.sh`：实际通过。
- `docker pull ubuntu:24.04`、`docker pull alpine:3.20`：实际成功。
- `make image-exec-env`、`make image-egress-proxy`：已尝试，均在 Docker buildx 写入 `/Users/yym/.docker/buildx/activity/` 时被沙箱拒绝（`operation not permitted`），未进入镜像构建。`make test-integration` 已尝试，在相同的前置构建步骤停止；容器测试尚未运行。请在可写 Docker 配置目录的环境中执行上面的三个 `make` 命令。按仓库规则未绕过沙箱，也未修改全局 Docker 配置。

## 偏差与待决

- `docs/design/黑板系统开发方案.md` 第 9 节沿用 `ENVD_TOKEN_SECRET`，而本任务明确要求容器接收 `ENVD_TOKEN`。本实现遵循任务说明，保留原占位符并在 `.env.example` 增加 `ENVD_TOKEN=replace-me`。后续 agent-runtime 应为每个任务生成 token 并传给 envd；建议同步修订设计文档中的变量用途。未修改 `docs/design/`。
- 因沙箱限制，完成标准中的两个镜像构建与真实容器集成测试无法在本工作树内确认。代理 403、Linux capability 下的用户创建及 MCP 容器调用仍需按上一节手动复验。
- envd 不使用跨服务 contracts，移除了其原有未使用依赖；无需新增共享数据结构。

### 共享文件修改清单

- `.env.example`：追加 `ENVD_TOKEN` 占位符。
- `Makefile`：追加两个镜像构建目标，并让 `test-integration` 先构建镜像。
- `docker-compose.yml`：追加 `egress-proxy`，连接 `exec` 和默认网络。
- 根 `pyproject.toml`：追加集成测试依赖 `testcontainers`、`httpx`、`zstandard`，并指定 pyright 使用项目 `.venv`（否则本机 pyright 错用全局 Python 3.14，无法解析 MCP 包）。
- `uv.lock`：锁定上述依赖及 envd 的 MCP、Starlette、Uvicorn 依赖。

## 对下一步的建议

1. 在沙箱外执行 `make image-exec-env image-egress-proxy && make test-integration`，确认所有容器断言通过；若有镜像或平台差异，优先修复本任务范围内的实现与测试。
2. agent-runtime 后续批次按本文接口创建容器、设置独立 `ENVD_TOKEN` 和 MCP 静态请求头；不要在此批次提前实现 ExecEnvManager。
3. 建议一个提交包含本报告、`services/envd/**`、`services/egress-proxy/**`、`.env.example`、`Makefile`、`docker-compose.yml`、根 `pyproject.toml` 和 `uv.lock`；英文提交信息：`Implement envd execution environment and egress proxy`。

## M2-env-fix 补充

### 完成情况

本次收尾时间：2026-09-24。按顺序阅读交接文档、工作树 `AGENTS.md`、原任务与报告，再依据主仓库的 `M2-env-fix.md` 完成收尾。

1. 检查时 `services/envd/Dockerfile` 已有默认空值的 `APT_MIRROR`、`PIP_INDEX_URL`：前者非空时替换 Ubuntu 主仓库与安全更新源，后者非空时为 pip 增加 `--index-url`。保留并验证已有实现。
2. 检查时 `services/egress-proxy/Dockerfile` 已有默认空值的 `APK_MIRROR`，非空时替换 Alpine 仓库域名。保留并验证已有实现。
3. 检查时 `Makefile` 已有三个阿里云源默认值、对应 `--build-arg` 及设为空使用官方源的注释。验证默认值与空值的命令展开正确；本轮没有再次修改该共享文件。
4. 在 `services/envd/README.md` 新增“构建”一节，说明变量用途、格式、默认值、覆盖方式、切回官方源的命令，以及 `test-integration` 会先构建两个镜像。
5. 在 README 明确 token 约定：agent-runtime 持有 `ENVD_TOKEN_SECRET`，按任务派生 token，创建容器时通过 `ENVD_TOKEN` 注入；envd 只读取 `ENVD_TOKEN`。本轮不实现后续 agent-runtime 的派生逻辑。

本轮实际编辑文件仅为 `services/envd/README.md` 与本报告；保留工作树原有未提交实现。

### 实际检查结果

- `make check`：通过。ruff format 检查 39 个文件、lint 通过；pyright 为 0 errors / 0 warnings；pytest 为 **54 passed, 2 deselected**，未调用真实模型 API。
- `sh -n services/egress-proxy/entrypoint.sh`：通过。
- 使用本地 Python 脚本展开 Dockerfile 续行，对所有 `RUN` 内容执行 `sh -n`，并解析 `ENTRYPOINT` / `CMD` 的 JSON：通过。这是局部静态检查，不等同于 Docker 构建器完整解析或镜像构建成功。
- 对 Dockerfile 中已有替换逻辑使用内存中的 Ubuntu / Alpine 源列表样例检查：非空镜像地址替换正确，空值跳过替换；用 shell 函数代替 pip 检查参数展开，非空时正确加入 `--index-url`，空值时不加入。未执行包安装或下载。
- `make -n image-exec-env image-egress-proxy`：在清除同名外部环境变量后，确认三个默认源均正确传入；显式传入 `APT_MIRROR= PIP_INDEX_URL= APK_MIRROR=` 时均传入空值。
- `make -n test-integration APT_MIRROR= PIP_INDEX_URL= APK_MIRROR=`：确认先构建两个镜像，再执行 `.venv/bin/pytest -m integration`。仅打印命令，没有运行构建或容器测试。

可重复执行的普通检查：

```sh
cd ~/bbx-wt/m2env
make check
sh -n services/egress-proxy/entrypoint.sh
make -n image-exec-env image-egress-proxy APT_MIRROR= PIP_INDEX_URL= APK_MIRROR=
```

### 需要用户在沙箱外执行

```sh
cd ~/bbx-wt/m2env
make image-exec-env image-egress-proxy
make test-integration
```

`test-integration` 会再次调用镜像构建目标，通常复用构建缓存。若要验证官方源，两条命令都应显式清空参数：

```sh
make image-exec-env image-egress-proxy APT_MIRROR= PIP_INDEX_URL= APK_MIRROR=
make test-integration APT_MIRROR= PIP_INDEX_URL= APK_MIRROR=
```

### 偏差、待决与下一步

- 遵照本次任务限制，未尝试镜像构建或依赖镜像的集成测试，也未绕过沙箱限制；软件源实际可达性、依赖安装与容器行为仍需上述外部验证。M2-env 原完成标准中的镜像构建和集成测试尚待验收。
- 未读写 `.env`，未修改 `AGENTS.md`、`docs/design/` 或全局配置，未执行 git 写操作。本轮无需新增依赖或配置变量；设计文档中的 token 用途仍由后续维护者同步。
- 建议先完成沙箱外验证，再审查并提交；agent-runtime 后续任务按 README 的 token 与隔离约定接入。

### 建议的提交划分

当前 M2-env 实现整体仍未提交，建议作为一个完整提交审查：`services/envd/**`、`services/egress-proxy/**`、`.env.example`、`Makefile`、`docker-compose.yml`、根 `pyproject.toml`、`uv.lock` 和 `docs/tasks/M2-env-report.md`。建议英文提交信息：`Implement envd execution environment and egress proxy with configurable build mirrors`。

如果审查者先提交原 M2-env 实现，则将本次修正独立为后续提交，包含两个 Dockerfile 的软件源参数、Makefile 的变量与参数传递、README 构建及 token 说明、本报告补充；英文提交信息：`Support configurable image build mirrors and document envd token handoff`。

## 接手审查检查点（2026-09-24，待批准）

- 按用户新的推进流程重新阅读本工作树规则、M2-env 任务、实现架构第 2 / 10 / 11 节、开发方案 M2 与第 9 节，并复核 Dockerfile、Makefile、envd 实现与容器测试。
- 实际执行 `uv sync --locked` 成功；重新运行 `make check`，ruff 通过、pyright 无错误及警告、普通测试 54 passed / 2 deselected。代理入口脚本 `sh -n` 与镜像构建命令的 `make -n` 检查通过。
- 设计待同步：开发方案第 9 节仍将 `ENVD_TOKEN_SECRET` 的使用方列为 agent-runtime、envd。拟按 HANDOFF 6.1 调整为仅 agent-runtime 持有 secret，按任务派生 `ENVD_TOKEN = HMAC-SHA256(secret, task_id)` 并注入容器，envd 只读取派生 token；在实现架构 2.2 明确同一 token 保护 MCP 与内部接口，且不向 Agent 命令子进程传递控制面 token。此方案尚未批准，尚未修改设计。
- 代码审查发现：`core.py` 创建命令子进程时未显式设置 `env`，会继承 envd 进程环境，存在将 `ENVD_TOKEN` 传入 Agent 命令的风险；现有测试没有覆盖。待设计同步获批并单独提交后，修复环境变量传递并补充回归测试；此项尚未修复或在容器内验证。
- 待批准操作：在 `m2-env` 分支单独提交上述两份设计文档；修复并通过普通检查后构建 `bbx-exec-env:latest`、`bbx-egress-proxy:latest` 并运行 `make test-integration`。未执行 Docker 构建、容器测试或任何 Git 写操作。
- 当前不是最终验收点；代码修复及容器验证完成后另行提交验收报告。建议提交顺序：`Clarify per-task envd token ownership and isolation`（设计），随后为完整 M2-env 实现提交；合并 main、打标签及删除工作树仍等待用户在最终检查点确认。


## 授权后最终收尾结果（2026-09-24）

用户已授权后续开发、设计同步、构建与 Git 操作。本节取代前文“待批准”和“镜像未验证”的当前状态；前文保留为历史记录。

### 完成与修复

- 两个软件源可配置镜像均已构建，Makefile 增加默认为空的 `DOCKER_BUILD_ARGS`，可向两个镜像目标传入构建代理与主机映射，集成测试沿用相同参数。
- root `.dockerignore` 排除 `.env`、Git 元数据、虚拟环境等；未读取或修改真实 `.env`。
- token 约定已先同步设计并单独提交 `ff8565f`。命令子进程过滤 `ENVD_TOKEN` / `ENVD_TOKEN_SECRET`，保留代理环境；补回归测试。空的 privileged 白名单项不再放行任意命令。
- `/files` 与 `/stat` 改用工作目录文件描述符逐层打开，防止校验后符号链接替换；证据读取以大小上限生成临时快照，处理文件增长、目录/FIFO、HEAD 和断连清理。归档 fallback 的排除规则已补测试。
- 容器实测发现原隔离 capability 列表无法跨 UID 杀进程：root 包装进程退出后，Agent 的后台 sleep 仍存活。先以 `1d5f4c0` 单独修正设计，增加 `KILL`，再同步 README 与测试容器配置，超时进程组清理测试现已通过。最终能力列表为 `CHOWN,DAC_OVERRIDE,FOWNER,SETUID,SETGID,KILL`，其余仍全部去掉。
- Docker 当前内部网络不发布宿主机端口。测试增加只转发到 envd 的临时中转容器，执行容器仍只接入 internal 网络；两者均随 testcontainers 上下文清理。测试 httpx 客户端使用 `trust_env=False`，避免本机 SOCKS 环境干扰本地连接。

### 实际验证

- `uv sync --locked` 成功。
- 最终 `make check`：Ruff format/lint 通过，Pyright 0 错误/0 警告，**61 passed, 2 deselected**；未调用真实模型。
- 两个镜像构建成功；最终 `make test-integration`（使用下方构建参数）成功，**2 passed, 61 deselected**。覆盖鉴权、MCP、用户隔离、shared sticky bit、超时清理、截断全文、privileged 白名单、token 不传入命令、路径/大小限制、归档排除及代理 403。
- 初次直连阿里云的大索引下载失败；显式构建代理时首次因 BuildKit 无法解析 `host.docker.internal` 失败，增加 `--add-host ...:host-gateway` 后成功。未改全局 Docker 或网络设置。

### 可复验命令

```sh
cd ~/blackboard-explorer  # 合并后；合并前在 ~/bbx-wt/m2env
export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897
export all_proxy=socks5://127.0.0.1:7897 no_proxy=localhost,127.0.0.1,::1,host.docker.internal
export UV_CACHE_DIR=/private/tmp/bbx-uv-cache
export DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal'
uv sync --locked
make check
make image-exec-env image-egress-proxy
make test-integration
```

### 提交划分与后续

1. token 归属设计：`Clarify per-task envd token ownership and isolation`（已提交）。
2. 跨 UID 信号能力设计：`Require signal capability for envd command timeouts`（已提交）。
3. envd、egress-proxy、测试、README、本报告与共享文件：`Implement envd execution environment and egress proxy`。

共享改动除原报告列出的文件外，新增 `.dockerignore` 和 `DOCKER_BUILD_ARGS`；设计文档已按先设计后实现的顺序同步。M2-env 的构建和自动化验证均已完成，没有遗留的用户必跑命令。后续 ExecEnvManager 必须沿用新增的 `KILL` 能力；宿主机开发访问内部执行容器时需要与容器内 runtime 等效的网络路径。
