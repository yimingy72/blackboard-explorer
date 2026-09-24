# 黑板式探索系统 · 开发交接文档

> 交接时间：2026-09-24。交接方：Claude（负责调度与审查）。接手方：用户，后续直接使用 Codex 开发。
> 本文是继续开发所需的全部信息：项目在哪、做到哪、下一步做什么、怎么用 Codex 做、有哪些坑。

---

## 0. 一页速览

- **项目**：通用的黑板式多 Agent 探索系统——给定目标（Goal）与文字验收条件，多个 DeepSeek 驱动的探索 Agent 通过共享黑板（事实 Fact、意图 Intent）协作，close Agent 裁定是否达成。
- **仓库**：`/Users/yym/blackboard-explorer`（`main` 分支，已打标签 `m0`）。
- **设计正本**：`docs/design/` 下三份文档（桌面上的同名文件是指向它们的符号链接）。实现以它们为准。
- **进度**：

  | 里程碑 | 状态 |
  |---|---|
  | M0 工程基座 | ✅ 已完成、审查、合并 |
  | EVAL 评估目标（玩具任务 + mini-shop） | ✅ 已完成、审查、合并（mini-shop 规模偏小，见 EVAL-2） |
  | M1a 黑板存储与领域规则 | 🟡 Codex 已完成，**未审查、未提交**（工作树 `~/bbx-wt/m1a`） |
  | M2-env 执行环境与出网代理 | 🟡 Codex 已完成，**镜像未构建成功、集成测试未运行、未提交**（工作树 `~/bbx-wt/m2env`） |
  | M1b、M1-W、M2a、M2b、M3a、M3b、M4、EVAL-2、M5 | ⬜ 未开始；**任务说明已全部写好**（`docs/tasks/`，派发顺序见第 6 节） |

- **下一步（按顺序）**：① 收尾 M2-env（第 3.1 节，任务说明 `M2-env-fix.md`）② 审查并合并 M1a（3.2）③ 并行派发 M1b 与 M1-W（第 6 节）。

---

## 1. 仓库与文档

```
blackboard-explorer/
├─ AGENTS.md                 # Codex 的项目规则（每次启动自动读取）——务必先读
├─ docs/
│  ├─ design/                # 设计正本（概念设计 / 实现架构 / 开发方案）
│  ├─ tasks/                 # 每个任务的说明 <T>.md 与 Codex 报告 <T>-report.md
│  └─ HANDOFF.md             # 本文
├─ packages/contracts/       # 共享数据模型（pydantic v2）与导出的 JSON Schema
├─ services/{blackboard,agent-runtime,envd,egress-proxy}/
├─ profiles/default/         # 默认 Agent 配置（deepseek-flash、参数、提示词模板占位）
├─ eval/                     # 玩具任务与评估任务（targets / tasks / answers / runner）
├─ docker-compose.yml        # postgres(pgvector) + minio(pgsty) + 建桶
├─ docker-compose.dev.yml    # eval-targets（叠加使用）
└─ Makefile
```

**常用命令**

| 命令 | 作用 |
|---|---|
| `uv sync` | 安装/同步 Python 依赖 |
| `make check` | ruff + pyright + 普通测试（不需要 Docker、不访问网络、不调用真实模型） |
| `make test-integration` | 需要 Docker 的测试（testcontainers，标记 `integration`） |
| `make up` / `make down` | 启动 / 停止本地基础设施（需要 `.env`） |
| `make schemas` | 导出 contracts 的 JSON Schema |
| `make eval-targets` / `make eval-verify` | 生成评估目标 tar 包 / 验证植入问题确实存在 |

**三份设计文档各管什么**

| 文档 | 内容 | 写任务说明时常用的章节 |
|---|---|---|
| 黑板式探索架构设计.md | 概念与规则：Fact / Intent、写入规则、调度、裁定、提示词、参数 | 3（黑板内容）、5（调度）、6（同步）、8（提示词）、9（参数） |
| 黑板系统实现架构.md | 容器、数据库表、API、MAF 用法、中间件、前端 | 0（框架判断）、2（执行环境）、3（数据模型）、4（blackboard API）、5（调度实现）、6–7（Agent 与中间件）、8（前端） |
| 黑板系统开发方案.md | 仓库结构、里程碑与检查点、测试策略、技术验证清单、评估任务 | 4（里程碑）、6（技术验证）、7（玩具/评估任务） |

---

## 2. 当前状态明细

### 2.1 git

- `main`：`996ef18`，包含 M0、M0-fix、EVAL 与所有任务说明。标签 `m0`。
- 仓库本地 git 身份为 `yym <yym@localhost>`（全局未配置），可用 `git config user.email …` 修改。
- 两个未合并的工作树（分支从 `01e13f9` 切出，落后 `main` 三个 EVAL 提交，合并时无冲突风险之外需注意 `Makefile`、`uv.lock`）：

  | 工作树 | 分支 | 未提交文件 | Codex 报告 |
  |---|---|---|---|
  | `/Users/yym/bbx-wt/m1a` | `m1a` | 14 | `docs/tasks/M1a-report.md` |
  | `/Users/yym/bbx-wt/m2env` | `m2-env` | 18 | `docs/tasks/M2-env-report.md` |

### 2.2 已验证的事实

| 项 | 结果 |
|---|---|
| `make check`（main） | 50 项通过 |
| `make up` | postgres、minio 健康；bucket `blackboard` 已建；pgvector 0.8.6 |
| mini-shop | 5 个提交；P1–P6 全部验证存在（P2、P3 各 20/20 复现）；tar 包无答案线索 |
| order-service 玩具任务 | 偶发失败率 36%（18/50）；稳定复现方法 100/100 |
| DeepSeek `deepseek-flash` | 工具调用可用；不回传 `reasoning_content` 不报错；`reasoning_effort` low/high/max 可用；用量含缓存命中/未命中 token |

### 2.3 Codex 自述、尚未独立验证的结果

- **M1a**：`make check` 97 通过、`make test-integration` 6 通过、领域层覆盖率 91%；Alembic 升降级已在集成测试中执行。
- **M2-env**：`make check` 54 通过；镜像构建与集成测试未能运行（原因见 3.1）。

---

## 3. 立即要做的事

### 3.1 收尾 M2-env（工作树 `~/bbx-wt/m2env`）

**问题一：镜像构建失败。** 两个原因叠加：

1. Codex 沙箱不允许 Docker buildx 写 `~/.docker/buildx/activity`，所以 Codex 无法构建镜像——**镜像需要你在沙箱外构建**。
2. 在沙箱外构建时，`apt-get` 直连 `archive.ubuntu.com` 极慢并最终失败（退出码 100）。需要改用国内软件源。

建议的修改（**尚未应用**）已写成任务说明 `docs/tasks/M2-env-fix.md`，可直接派给 Codex（`--cwd ~/bbx-wt/m2env`，提示语中用绝对路径 `~/blackboard-explorer/docs/tasks/M2-env-fix.md` 引用，因为该工作树切出时还没有这个文件）。修改内容如下，也可以手动改：

```dockerfile
# services/envd/Dockerfile —— 在 ENV DEBIAN_FRONTEND 之后
ARG APT_MIRROR=
ARG PIP_INDEX_URL=
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://archive.ubuntu.com/ubuntu/|$APT_MIRROR/|g; s|http://security.ubuntu.com/ubuntu/|$APT_MIRROR/|g" \
            /etc/apt/sources.list.d/ubuntu.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
    ...（原有包列表不变）
# pip 安装一行改为：
    && /opt/envd/venv/bin/pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} /opt/envd/source \
```

```dockerfile
# services/egress-proxy/Dockerfile
ARG APK_MIRROR=
RUN if [ -n "$APK_MIRROR" ]; then sed -i "s|dl-cdn.alpinelinux.org|$APK_MIRROR|g" /etc/apk/repositories; fi \
    && apk add --no-cache tinyproxy
```

```makefile
# Makefile —— 默认用国内源；设为空即回到官方源
APT_MIRROR ?= http://mirrors.aliyun.com/ubuntu
PIP_INDEX_URL ?= https://mirrors.aliyun.com/pypi/simple
APK_MIRROR ?= mirrors.aliyun.com

image-exec-env:
	docker build -f services/envd/Dockerfile --build-arg APT_MIRROR=$(APT_MIRROR) \
		--build-arg PIP_INDEX_URL=$(PIP_INDEX_URL) -t bbx-exec-env:latest .

image-egress-proxy:
	docker build -f services/egress-proxy/Dockerfile --build-arg APK_MIRROR=$(APK_MIRROR) \
		-t bbx-egress-proxy:latest .
```

然后在沙箱外执行：

```sh
cd ~/bbx-wt/m2env
make image-exec-env image-egress-proxy
make test-integration        # 注意：Makefile 中 test-integration 依赖这两个镜像目标
```

**问题二：变量命名。** 设计文档（开发方案第 9 节）只有 `ENVD_TOKEN_SECRET`；M2-env 让 envd 容器读取 `ENVD_TOKEN`。两者其实不矛盾，建议在设计文档中写明：agent-runtime 持有 `ENVD_TOKEN_SECRET`，为每个任务派生一个 token，创建执行环境容器时以 `ENVD_TOKEN` 注入；envd 只认 `ENVD_TOKEN`。

**审查要点**：集成测试（`services/envd/tests/test_integration.py`）用一个大测试覆盖了鉴权、非法 agent id、目录隔离、shared 不可删他人文件、超时杀进程组、截断与全文读取、`/files` 限制与路径穿越、privileged 白名单、未知 agent、归档排除；另有出网代理 403 测试。全部通过后按报告中建议的提交划分提交，合并到 `main`。

### 3.2 审查并合并 M1a（工作树 `~/bbx-wt/m1a`）

审查清单：

1. 读 `docs/tasks/M1a-report.md`，重点看 `BoardService` 公开方法清单（M1b 的 HTTP 层要用）与"偏差与待决"。
2. 独立运行 `uv sync && make check && make test-integration`，核对 97 / 6 / 覆盖率 91%。
3. 抽查 `services/blackboard/src/bbx_blackboard/domain/` 是否覆盖设计文档 3.5 的每条写入规则；争议状态是否完整递归；finish_agent 的计数规则是否符合实现架构 5.3 段落。
4. 确认每个写操作事务开头 `SELECT … FOR UPDATE` 锁任务行；实时写入与重放走同一个投影器。
5. 已知偏差：**任务创建是"事件是唯一写入入口"的引导例外**（先插任务行，再追加 `task.created` 并投影，因为 events 有外键）。建议在实现架构第 3.1 节补一句说明。

合并顺序：M2-env 与 M1a 互相独立，先合并哪个都可以；两者都改了 `pyproject.toml` / `uv.lock` / `Makefile` / `.env.example`，第二个合并时若 `uv.lock` 冲突，取任一版本后运行 `uv lock` 重新生成，再跑 `make check`。

合并后删除工作树：`git worktree remove ~/bbx-wt/<name> && git branch -d <branch>`。

### 3.3 第二波：M1b 与 M1-W（可并行）

任务说明已写好：`docs/tasks/M1b.md`、`docs/tasks/M1-W.md`。M1b 依赖 M1a 合并；M1-W 可以与 M1b 同时开始（先用 contracts 的 JSON Schema 与 MSW 模拟接口，M1b 完成后切到 OpenAPI 生成的类型）。

---

## 4. 用 Codex 开发的操作手册

### 4.1 派发一个任务

先把任务说明写到 `docs/tasks/<T>.md`（模板见 4.3），提交到要开发的分支上，然后：

```sh
export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897 \
       all_proxy=socks5://127.0.0.1:7897 no_proxy=localhost,127.0.0.1,::1,host.docker.internal \
       UV_CACHE_DIR=/private/tmp/bbx-uv-cache

node ~/.claude/plugins/marketplaces/openai-codex-plugin-cc/plugins/codex/scripts/codex-companion.mjs task \
  --cwd <工作树或仓库路径> --model gpt-6-sol --effort high --write --fresh \
  "请先阅读 AGENTS.md，然后按 docs/tasks/<T>.md 完成任务 <T>。严格遵守任务范围与完成标准，结束时写报告 docs/tasks/<T>-report.md 并输出摘要。" \
  > /tmp/bbx-codex-<T>.log 2>&1 &
```

| 参数 | 说明 |
|---|---|
| `--cwd` | Codex 的工作区；沙箱只允许写这个目录（及 /tmp） |
| `--model gpt-6-sol --effort high` | 本项目使用的模型与思考强度（可用模型见 `~/.codex/models_cache.json`） |
| `--write` | 允许修改文件 |
| `--fresh` / `--resume-last` | 新会话 / 接着该工作区上一个会话（用于"审查后修正"） |
| 代理变量 | 必须带上，否则 Codex 运行时的登录检查与依赖下载会失败 |
| `UV_CACHE_DIR` | Codex 沙箱写不了 uv 默认缓存，用这个共享目录 |

- 注意：`task --help` 会被当成提示词执行，不要用它看帮助。
- 查看进度：`/codex:status`，或 `tail -f /tmp/bbx-codex-<T>.log`。
- 继续某个 Codex 会话：`codex resume <session id>`（见第 8 节）。
- 也可以直接在 Codex CLI / 桌面应用里打开仓库，给出同样的提示语——`AGENTS.md` 会自动生效。

### 4.2 并行开发（多个 Codex 同时做不同任务）

```sh
cd ~/blackboard-explorer
git worktree add -b <branch> ~/bbx-wt/<name>     # 每个任务一个工作树
# 用 4.1 的命令分别派发，--cwd 指向各自工作树
```

规则（已写入 `AGENTS.md` 第 8 节）：各任务只改自己目录；共享文件（根 `pyproject.toml`、`uv.lock`、`Makefile`、`docker-compose*.yml`、`packages/contracts`）只做追加式修改并在报告中列出；需要 Docker 的测试用 testcontainers 随机端口，不用 `make up`；自建镜像/容器加任务前缀。本机 4 核 / 9.7GB，同时 3 路比较合适。

### 4.3 任务说明模板

```markdown
# 任务 <T> · <名称>

依据：<设计文档章节>

先阅读 `AGENTS.md`。

## 范围
<只做什么；明确不做什么（属于哪个后续任务）>

## 已定的实现决定
<技术选型、分层、并发策略、接口约定——不留给 Codex 自由发挥的部分>

## 任务
| # | 内容 |

## 测试
<普通测试 / 集成测试分别要覆盖什么>

## 完成标准
<可执行、可核对的条目；最后一条：报告写到 docs/tasks/<T>-report.md，含接口清单、共享文件修改、建议提交划分、偏差与待决>
```

经验：把"必须满足的数字/行为"写进完成标准（例如"50 个并发认领恰好 1 个成功"），Codex 会照着验证；把"不做什么"写清楚，能有效防止越界。

### 4.4 审查与合并

1. 读报告，尤其"偏差与待决"。
2. 独立运行 `make check`、`make test-integration`，以及任务特有的验证。
3. 对照设计抽查关键代码；发现问题写 `docs/tasks/<T>-fix.md`，用 `--resume-last` 派回同一个会话。
4. 按报告建议的提交划分提交（Codex 不能写 `.git`，见 5.1），提交信息注明 `Implemented by Codex (gpt-6-sol) for task <T>.`
5. 合并到 `main`（`git merge --ff-only` 或普通合并），删除工作树。
6. 设计需要改的，**先改 `docs/design/`，再改代码**。

---

## 5. 环境与已知坑

### 5.1 Codex 沙箱的限制

| 限制 | 影响 | 现在的做法 |
|---|---|---|
| 不能写 `.git` | Codex 不能切分支、提交 | 由人提交（`AGENTS.md` 第 7 节已写明） |
| 不能 `docker build`（buildx 要写 `~/.docker`） | 镜像需在沙箱外构建 | 构建与依赖镜像的测试由人在沙箱外运行 |
| 可以 `docker run` / `docker compose` / testcontainers | 集成测试可以在沙箱内跑（前提是镜像已存在） | M0、M1a 的容器测试均在沙箱内跑通 |
| 写不了 uv 默认缓存 | `uv sync` 失败 | 派发时设 `UV_CACHE_DIR=/private/tmp/bbx-uv-cache` |

如果改为用更宽松的沙箱（例如允许写 `.git` 与 `~/.docker`）运行 Codex，记得同步修改 `AGENTS.md` 第 7 节，否则 Codex 仍会遵守"不做 git 写操作"。

### 5.2 网络与镜像

- 外网经代理 `127.0.0.1:7897`；本机地址必须放进 `no_proxy`，否则访问本地容器会走代理。
- Docker 配置了镜像加速源（xuanyuan）。它**不提供 MinIO 官方镜像**（`minio/minio`、`minio/mc` 返回 422），对象存储统一用 `pgsty/minio:RELEASE.2026-04-17T00-00-00Z`（自带 `mc`）。已确认可拉取：`pgvector/pgvector:pg16`、`ubuntu:24.04`、`python:3.12-slim`、`alpine:3.20`、`node:22-alpine`、`testcontainers/ryuk:0.11.0`。新增镜像前先 `docker pull` 确认。
- Docker 构建中的 `apt` / `pip` / `apk` 直连官方源很慢，用国内源（3.1 的修改）。

### 5.3 模型服务的内容审核

EVAL 任务在最后阶段被 Codex 的模型服务以"可能的网络安全风险"中断——mini-shop 是故意植入越权、注入等问题的评估目标，并附有证明问题存在的验证脚本。后续涉及这类"植入安全问题的评估素材"的工作（例如 EVAL-2），可能再次被拦截。**不要改写措辞去绕过审核**；这部分工作建议人工完成，或只让 Codex 做不涉及植入问题的正常业务代码。

### 5.4 密钥与本地配置

- `.env` 只在主工作树 `~/blackboard-explorer/.env`（权限 600，已被 git 忽略）：数据库/MinIO 口令与各类 token 为随机值。
- **`DEEPSEEK_API_KEY` 尚未写入 `.env`**（仍是占位符）。M2 用真实模型之前填入。不要写进任何提交、文档或 Codex 提示语。该 key 曾出现在对话记录中，可考虑在 DeepSeek 控制台重新生成。
- `profiles/default/models.yaml` 的价格表为空，需按 DeepSeek 官方价格页填写（缓存命中输入、未命中输入、输出单价、是否错峰）；未填时金额记为 0 并有提示。

### 5.5 其他

- Python：3.12 已由 uv 安装在默认位置；`.venv` 在每个工作树中各自 `uv sync` 生成。
- `ruff format` 只针对四个 Python 包目录（否则会格式化设计文档里的代码块）。
- `eval/targets/dist/` 是构建产物，已忽略，用 `make eval-targets` 生成。

---

## 6. 后续任务与派发顺序

所有任务说明都在 `docs/tasks/`，每份都包含：依据的设计章节、范围与"不做什么"、已定的实现决定、任务表、测试要求、完成标准（含需要你在沙箱外执行的检查点）。派发方法见第 4 节。

| 顺序 | 任务 | 任务说明 | 依赖 | 可与谁并行 | 需要你在沙箱外做的事 |
|---|---|---|---|---|---|
| 0 | M2-env 收尾 | `M2-env-fix.md` | — | M1a 审查 | 构建两个镜像、`make test-integration` |
| 0 | M1a 审查合并 | 本文 3.2 | — | M2-env 收尾 | 审查、提交、合并 |
| 1 | 黑板接口层 | `M1b.md` | M1a | M1-W | 构建 blackboard 镜像；`make up` 后跑模拟器看 SSE |
| 1 | 画布最小版 | `M1-W.md` | —（M1b 完成后切真实接口） | M1b | 浏览器检查画布 |
| 2 | 技术验证与 runtime 骨架 | `M2a.md` | M1b、M2-env | EVAL-2 | `make test-live`（真实 DeepSeek） |
| 3 | 单个 Agent 跑通 | `M2b.md` | M2a | EVAL-2 | 填 `DEEPSEEK_API_KEY` 与价格表；玩具任务上跑真实种子 |
| 4 | 调度器、清扫与恢复 | `M3a.md` | M2b | — | 构建 agent-runtime 镜像 |
| 5 | 裁定、收尾与完整闭环 | `M3b.md` | M3a | — | `make e2e`（玩具任务完整闭环） |
| 6 | 工作台 | `M4.md` | M1-W、M3b | M5 | `make web-e2e`；用工作台复盘一次运行 |
| 6 | 评估与调参 | `M5.md` | M3b、EVAL-2 | M4 | `make eval-run` / `make eval-score`（花费较多） |
| 任意 | mini-shop 扩容 | `EVAL-2.md` | — | 任何 | **建议人工完成**（见 5.3） |

**每个任务的标准流程**：建工作树 → 派发 → 读报告 → 独立运行 `make check` / `make test-integration` → 执行任务说明中"需要用户执行"的检查点 → 有问题写 `<T>-fix.md` 用 `--resume-last` 派回 → 按报告建议提交 → 合并 → 删除工作树。

## 6.1 设计文档待同步

以下事项来自 Codex 报告，尚未写回 `docs/design/`（先改设计文档，再让后续任务依据它）：

| 事项 | 来源 | 建议写法 | 位置 |
|---|---|---|---|
| envd 的 token | M2-env 报告 | agent-runtime 持有 `ENVD_TOKEN_SECRET`，按任务派生 `ENVD_TOKEN = HMAC-SHA256(secret, task_id)` 注入容器；envd 只读 `ENVD_TOKEN` | 开发方案第 9 节；实现架构 2.2 |
| 工具调用事件 | M1a 报告 | 新增事件类型 `tool_call.recorded`（参与 provenance 判定、需可重放） | 实现架构 3.3 |
| 任务创建的引导写入 | M1a 报告 | 任务行先插入、再追加 `task.created` 并投影，是"事件为唯一写入入口"的唯一例外 | 实现架构 3.1 |
| 相似度检索 | M1a 报告 | v1 为线性余弦 top-3，只作提示不拒绝；数据量大时改 pgvector 索引 | 实现架构 4.3 |
| `derive_enabled` 参数 | M5 任务 | 默认 true；为 false 时不触发 derive（单 Agent 基线用） | 设计文档第 9 节、5.4 |

## 7. 已确定的关键设计决策（避免重复讨论）

- 只有三种 Agent 任务：explore（长驻有界；黑板为空时的第一个为"种子"）、derive（探索停下来且已裁定后推导新意图）、close（裁定 / 终结）。没有 verify / test / arbitrate 角色；质疑用"争议事实"表达，作者可以撤回自己的事实；争议点名深度 2。
- 事实状态只有 proposed / disputed，由代码按争议图递归计算；意图 open / claimed / closed，带 `attempts` 上限与 `retry_of`；`post_intent(claim=true)` 原子认领。
- Goal、domain_context、验收条件都是文字；**验收由 close 裁定**，调度器在"有事实带 `satisfies`"或"探索停下来且黑板有变化"时触发裁定；裁定与探索并行；met 项的支撑事实被争议时自动回到 unmet。不设 setup 脚本。
- 预算按金额（`max_cost`、`close_reserve_ratio`），并发上限只在任务 `budget` 中；`context_threshold` 128K。
- 调度器自建 asyncio 循环，**不用 MAF Workflow**（超步屏障与连续调度冲突）；MAF 只用于 Agent 本体。
- MAF 核对结论：`max_iterations` 默认 40 需调大；函数中间件"替换结果"而不是 terminate；增量注入用 `enqueue_messages` + `MessageInjectionMiddleware`；MCP 工具经过函数中间件，参数为 `static_headers`。
- 模型统一 DeepSeek `deepseek-flash`；embedding 用本地 bge-small-zh-v1.5；v1 不部署模型网关。
- 部署：本机 Docker；同一时间一个任务；每任务一个 ubuntu 执行环境（每个 Agent 一个 Linux 用户），证据"引用时持久化 + 结束时归档"到 MinIO。

---

## 8. 参考

| 项 | 值 |
|---|---|
| Codex 会话：M0 / M0-fix | `01a0d260-2e17-7af1-9db0-ea6caf29d7b7` |
| Codex 会话：M1a | `01a0d284-c534-7072-8e46-7b128f50e65f` |
| Codex 会话：M2-env | `01a0d284-d0f9-73c0-a5e9-cc064bf72fed` |
| Codex 会话：EVAL（被内容审核中断） | `01a0d284-dac0-74a0-9622-d18680cbd912` |
| 派发日志 | `/tmp/bbx-codex-{M0,M0-fix,M1a,M2env,EVAL}.log`（临时目录，重启后可能丢失） |
| companion 脚本 | `~/.claude/plugins/marketplaces/openai-codex-plugin-cc/plugins/codex/scripts/codex-companion.mjs` |
| DeepSeek 接口 | `https://api.deepseek.com`（OpenAI 兼容 Chat Completions）；模型 `deepseek-flash` |
| 本地端口 | postgres `127.0.0.1:55432`；minio API `127.0.0.1:59000`、控制台 `127.0.0.1:59001` |
