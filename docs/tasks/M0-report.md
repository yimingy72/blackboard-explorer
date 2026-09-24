# M0 工程基座报告

## 完成了什么

- **0.1 仓库骨架**：建立 uv workspace、四个 Python 包、后续模块的 README 占位目录、Python 3.12 版本文件、ruff / pyright / pytest / pre-commit 配置及忽略规则。`web/` 仅有 README。受 Git 沙箱限制，未能创建 `m0` 分支或提交，详见下文。
- **0.2 基础设施容器**：编写 PostgreSQL 16 + pgvector、MinIO、幂等建桶容器，以及 `internal` / `exec` 网络、数据卷、健康检查和本机回环端口映射。未加入后续服务占位容器。
- **0.3 统一命令**：提供 `up`、`down`、`clean-volumes`、`fmt`、`lint`、`typecheck`、`test`、`check`、`schemas`。格式化和静态检查只扫描四个 Python 包，不触及设计文档。`make check` 不访问网络或 Docker。
- **0.4 共享数据模型**：用 pydantic v2 定义任务、事实、意图、证据、验收、Agent 运行、事件、回执、写入请求、配置及所需枚举；仅校验结构、单字段和同对象一致性。三种回执可以解析区分；29 个 JSON Schema 已导出。49 项测试覆盖模型合法和非法样例、枚举、回执、profile、日志和服务配置。
- **0.5 配置与日志**：各服务有环境变量 `Settings`，密钥使用 `SecretStr`；`.env.example` 列出设计所需变量并只用占位值；共享 JSON 行日志包含时间、级别、服务、消息和附加字段；`OTEL_ENABLED` 默认关闭。
- **0.6 默认 Agent 配置**：三个任务类型均指向 DeepSeek `deepseek-flash`，价格字段留空并注释按官方价格页填写；参数初值与设计一致；三个提示词模板仅保留 M2 注释。加载函数验证 profile 和模板路径，价格未填时返回三条明确提示。

## 如何验证及实际结果

在仓库根目录执行：

```sh
uv sync
make check
make schemas
docker compose --env-file .env.example config --quiet
```

实际结果：uv 使用 Python 3.12.13，解析 31 个包并安装 29 个包；再次 `uv sync --offline` 成功。`make check` 中 ruff format / lint 通过、pyright 为 0 错误 0 警告、pytest 为 **49 passed**。`make schemas` 成功，输出 29 个 JSON 文件。Compose 配置校验通过。检查确认 `docs/design/` 无改动。

本机 uv 默认缓存目录不可写，因此实际首次同步使用了以下命令；3.12 解释器下载到了临时目录，而非全局位置：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache UV_PYTHON_INSTALL_DIR=/private/tmp/bbx-uv-python uv python install 3.12
UV_CACHE_DIR=/private/tmp/bbx-uv-cache UV_PYTHON_INSTALL_DIR=/private/tmp/bbx-uv-python uv sync
```

容器验收尚未通过。已执行 `COMPOSE_ENV_FILES=.env.example make up`，Docker 镜像源在拉取 `minio/minio:latest` 时返回 HTTP 422，未启动容器，因此无法验证 PostgreSQL、MinIO 的 healthy 状态和 `blackboard` bucket。镜像源恢复后，可执行：

```sh
cp -n .env.example .env
make up
docker compose ps
docker compose exec minio mc ls local/blackboard
make down
```

## 偏差与待决

1. **Git 操作被沙箱阻止**：`git switch -c m0` 无法创建 `.git/refs/heads/m0.lock`，报 `Operation not permitted`。当前仍在 `main`，新增文件未提交、工作区不干净；因此“m0 分支上有若干提交”未达成。也无法安装 Git pre-commit 钩子。没有推送、合并或改写历史。请在允许写入 `.git` 的环境完成分支与提交。
2. **Docker 镜像拉取失败**：当前镜像源对指定的 `minio/minio:latest` 返回 422；未改变任务指定的镜像来源，也未绕过 Docker 配置。需在镜像源恢复或可访问官方镜像后重跑上述容器验证命令。
3. **参数表示**：设计将 `close_reserve_cost` 初值写为 `max_cost` 的 5%。任务 profile 尚无具体任务预算，因此在 profile 中保存比例 `0.05`；M1/M3 应在任务预算确定时换算为实际金额。建议设计文档明确该字段是比例还是金额，避免后续解释分歧。设计文件本次未修改。
4. 开发方案 M0 表格提到服务占位容器，但 M0 任务明确禁止；按任务要求只配置三个基础设施容器。

## 对下一步的建议

先在可写 `.git` 的环境切出 `m0`，将 Python workspace / contracts 与基础设施 / profile 分为逻辑提交；再重试容器验证。待 Git 和 Docker 两项验收完成后再进入 M1。示例提交命令：

```sh
git switch -c m0
git add .gitignore .python-version .pre-commit-config.yaml Makefile README.md pyproject.toml uv.lock packages services web eval profiles
git commit -m "Set up Python workspace and shared contracts"
git add .env.example docker-compose.yml docs/tasks/M0-report.md
git commit -m "Add local infrastructure and M0 report"
git status --short --branch
```

## M0-fix 补充

### 完成了什么

1. `minio` 与 `minio-init` 均改用固定镜像 `pgsty/minio:RELEASE.2026-04-17T00-00-00Z`；建桶仍使用 `mc mb --ignore-existing`。未增加环境变量。
2. `Params.close_reserve_cost` 更名为 `close_reserve_ratio`，默认 `0.05`，范围为 `[0, 1)`；`max_concurrent_agents` 仅保留在 `Budget`。同步更新默认 profile、测试、`Params.json` 与 `AgentProfile.json`。新增任务参数覆盖测试，确认并发字段及旧参数名被拒绝，比例边界按新范围校验。

### 如何验证及实际结果

在仓库根目录运行：

```sh
make check
make schemas
docker compose config --quiet
make up
docker compose ps -a
docker compose exec -T minio sh -c 'mc alias set verify http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ls verify/'
make up
docker compose ps -a
make down
```

实际结果：指定 MinIO 镜像 `docker pull` 成功；`make check` 中 ruff 通过、pyright **0 错误 0 警告**、pytest **50 passed**。`make schemas` 和 Compose 配置校验通过。首次 `make up` 后 PostgreSQL 与 MinIO 均为 **healthy**，`minio-init` **Exited (0)**，桶列表出现 `blackboard/`。第二次 `make up` 后状态相同，建桶日志再次成功，确认幂等。`make down` 成功，数据卷保留，`docker compose ps -a` 为空。

### 偏差与待决

无新增设计偏差。本节结果更新了原报告中关于 MinIO 镜像、容器验收和参数命名的历史状态；原报告正文按任务要求未改动。本次未执行任何 Git 写操作，提交由 Claude 完成。

### 建议的提交划分

1. `docker-compose.yml`：`Use pinned MinIO image for local infrastructure`
2. `packages/contracts/src/bbx_contracts/models.py`、`packages/contracts/tests/test_models.py`、`packages/contracts/schemas/Params.json`、`packages/contracts/schemas/AgentProfile.json`、`profiles/default/params.yaml`、`docs/tasks/M0-report.md`：`Align reserve ratio and task budget contracts with design`
