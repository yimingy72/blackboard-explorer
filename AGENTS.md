# AGENTS.md — 给 Codex 的项目规则

本仓库实现"黑板式多 Agent 探索系统"。开发由用户使用 Codex 推进：Codex 按任务说明实现，用户在检查点验收，并批准需要越过沙箱的操作。

## 1. 设计正本

- `docs/design/黑板式探索架构设计.md`：概念与规则（Fact / Intent、调度、裁定、提示词、参数）
- `docs/design/黑板系统实现架构.md`：容器、数据模型、接口、MAF 用法
- `docs/design/黑板系统开发方案.md`：仓库结构、里程碑、测试策略

实现以这三份文档为准。**未经用户同意，不要修改 `docs/design/` 下的文件。** 需要改设计时（包括 `docs/HANDOFF.md` 6.1 列出的待同步事项），先向用户说明改哪里、怎么改，同意后先改设计文档（单独提交），再改代码。发现设计有矛盾、遗漏或无法实现之处，按你的最佳判断实现一个最小、可替换的方案，并在任务报告的"偏差与待决"一节写清楚：哪一处、为什么、你怎么做的、建议怎么改设计。

## 2. 任务

- 当前任务的说明在 `docs/tasks/<里程碑>.md`，只做说明范围内的事，不提前实现后续里程碑的功能。
- 任务结束时把报告写到 `docs/tasks/<里程碑>-report.md`（中文），内容：
  1. 完成了什么（按任务编号逐项）
  2. 如何验证（用户可以照着执行的命令）以及你实际运行的检查结果
  3. 偏差与待决
  4. 对下一步的建议
- 报告写完后，把同样的摘要作为最终回复输出。

## 3. 代码约定

- Python 3.12 语法（本机可能是 3.13 解释器运行，不要使用 3.13 专有特性）；依赖与虚拟环境用 uv workspace 管理。
- 格式与静态检查：ruff（format + lint）、pyright（standard 模式）。
- 测试：pytest、pytest-asyncio。跨服务共享的数据结构只在 `packages/contracts` 中定义。
- 代码标识符、注释、docstring、提交信息用英文；面向用户的文档与报告用中文。
- 保持简单：不引入任务不需要的依赖、抽象或配置项。

## 4. 测试与模型调用

- `make check` 必须在提交前全绿，且**不得调用任何真实的大模型 API**（测试中使用脚本化的假客户端）。
- 真实模型（DeepSeek）只在显式的端到端 / 评估命令中使用，这些命令不属于 `make check`。

## 5. 密钥与安全

- 不要在任何文件、测试、日志、提交中写入真实密钥。`.env` 在 `.gitignore` 中；`.env.example` 只放占位符。
- 不要读取或输出 `.env` 的内容。
- 不要修改仓库以外的文件或全局配置（全局 git 配置、shell 配置、Docker 设置等）。

## 6. 环境

- 本机：macOS（Intel，amd64），Docker Desktop 4 核、约 9.7GB 内存。
- 外网访问经代理：`http_proxy` / `https_proxy` = `http://127.0.0.1:7897`，`all_proxy` = `socks5://127.0.0.1:7897`，`no_proxy` = `localhost,127.0.0.1,::1,host.docker.internal`（本机地址必须在 `no_proxy` 中）。
- uv 下载缓存使用 `UV_CACHE_DIR=/private/tmp/bbx-uv-cache`（沙箱写不了 uv 默认缓存；多个 Codex 共用）。Python 3.12 解释器已安装在 uv 默认位置，直接 `uv sync` 即可，不要把解释器装到临时目录。
- 如果当前环境缺少上述变量（例如从桌面应用启动），在命令前显式设置，不要修改全局配置。
- Docker：本机配置了镜像加速源，它不提供 MinIO 官方镜像（`minio/minio`、`minio/mc` 均返回 422）。对象存储统一使用 `pgsty/minio:RELEASE.2026-04-17T00-00-00Z`（自带 `mc`）。新增任何镜像前，先确认能通过 `docker pull` 拉取。
- 本地 `.env` 只存在于主工作树，不要修改或读取；你的代码与测试不能依赖 `.env`。需要新变量时加到 `.env.example` 并在报告中说明。
- 沙箱阻止的操作（git 写操作、`docker build`、依赖新镜像的测试等），向用户申请在沙箱外执行；不要用其他手段绕过沙箱（例如把 `.git` 或 `~/.docker` 加入可写目录）。无法申请或未获批准时，在报告中写明哪一步未能执行、原因、以及用户需要手动执行的命令。

## 7. Git

- 可以执行 git 写操作：建分支与工作树、暂存、提交、合并、打标签、删除已合并的工作树与分支。沙箱把 `.git` 设为只读，这些命令需向用户申请在沙箱外执行。
- **合并到 `main`、打标签、删除工作树或分支之前，必须得到用户的明确确认。** 在任务自己的分支上提交，用户批准该命令即可。
- 不要改写已有提交（`commit --amend`、`rebase`、`reset --hard` 等），不要推送到远程。
- 提交前 `make check` 全绿；只提交任务范围内的文件，绝不提交 `.env`。
- 报告中给出建议的提交划分（每个提交包含哪些文件、英文提交信息），并按它提交；提交信息正文末尾注明 `Implemented by Codex (gpt-6-sol) for task <T>.`

## 8. 并行开发

- 可能有多个 Codex 同时在不同分支上开发。每个任务在自己的 git 工作树与分支上进行（`~/bbx-wt/<name>`，或桌面应用自建的工作树），只修改任务说明列出的目录与文件；不直接在 `main` 上改代码（合并后更新 `docs/HANDOFF.md` 进度除外）。
- 共享文件（根 `pyproject.toml`、`uv.lock`、`Makefile`、`docker-compose*.yml`、`packages/contracts`）只做必要的、尽量追加式的修改，并在报告中逐条列出，方便合并。修改 `AGENTS.md` 与 `docs/design/` 前需用户同意。
- 测试分两类：
  - 普通测试：不依赖 Docker、网络，由 `make check` 运行。
  - 需要 Docker 的测试：标记 `@pytest.mark.integration`，由 `make test-integration` 运行；使用 testcontainers（随机主机端口、结束后自动清理），**不要使用 `make up` 或固定主机端口**，以免与其他 Codex 冲突。
- 你构建的镜像、创建的容器与网络，名称加任务前缀（例如 `bbx-m2env-…`），测试结束后清理。
