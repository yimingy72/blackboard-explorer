# Pi 迁移源码交付收尾

日期：2026-11-02。继续会话“完成 M2 环境修复任务 (3)”的未完成交付；工作树 `/Users/yym/bbx-wt/platform-performance-audit`，分支 `feature/pi-runtime-migration`。

## 完成内容与提交划分

原会话已完成 Pi 迁移和五项复审修复，但前序 CTF 性能、参数反馈与迁移实现尚未提交。本轮保留当前生产源码的全部字节，使用先前发布时保存的源码快照和初版 Pi 候选镜像，恢复各阶段的独立提交边界；没有回滚工作树或重写已有提交。

| 提交 | 内容 |
| --- | --- |
| `b69669c` — `feat: improve CTF sharing and asynchronous command coordination` | 精确记录共享、角色参数、通知退休、异步命令及配套 API、合同、界面和回归，49 文件。 |
| `1b5b69d` — `fix: return pre-dispatch command refusals as correctable feedback` | 执行前参数拒绝与安全子异常诊断，12 文件。 |
| `260673f` — `fix: make CTF snapshot ordering deterministic` | 快照内成员、turn、消息和题目采用稳定排序，1 文件。 |
| `8a97c71` — `docs: clarify the Node requirement for Pi proxy support` | 将原有未提交的 Node 版本说明单独提交，平台最低要求为 22.21。 |
| `5a0a7b9` — `refactor: replace MAF execution with native Pi runtime` | Pi 原生循环、模型适配、会话、工具和追踪接入，以及镜像、锁文件和离线/集成验证，134 文件。 |
| `a178b47` — `fix: enforce pinned MCP tools and preserve MCP resources` | 固定目标与白名单、完整分页、资源保留及回归。 |
| `7c319e9` — `fix: honor telemetry content capture permissions` | 追踪正文权限及允许属性筛选。 |
| `640ec64` — `fix: retain Google provider HTTP diagnostics` | Gemini/Vertex 的状态码、请求标识、退避元数据与并发归属。 |
| `docs: record Pi migration source delivery` | 本报告、前序五份报告及 HANDOFF 的状态同步。 |

本轮共 9 个提交。此前的 CTF 设计 `9bdc6e6` 和 Pi 设计 `c421120` 保留；相对当前 `main` 的完整待合并范围因此为 11 个提交。运行时和打包在一个提交内交付，避免出现依赖已移除而 Node/Pi 尚未安装的中间版本；三组复审修复仍保持独立。

## 实际验证

| 检查 | 本轮结果 |
| --- | --- |
| 当前完整工作树 `make check` | **2605 passed、438 deselected，173.28 秒**；Ruff、Pyright 和 5 项 Pi 原生检查通过。 |
| 独立导出的 CTF 性能提交 | `make check`：**2589 passed、429 deselected，87.34 秒**；静态检查通过。 |
| 独立导出的参数反馈提交 | `make check`：**2601 passed、432 deselected，86.41 秒**；静态检查通过。 |
| 独立导出的 Pi 基础提交 | `make check`：**2584 passed、434 deselected，176.56 秒**；静态检查与 Pi 原生检查通过。 |
| 在逐层更新的导出源码上验证 MCP 修复 | **17 passed，3.19 秒**。 |
| 同一导出源码上的追踪修复 | **18 passed，1.83 秒**。 |
| 同一导出源码上的 Google/Vertex 修复 | **11 passed，5.55 秒**。 |
| 同一导出源码上的真实 HTTP 代理/直连 | **6 passed，3.44 秒**；使用本机模拟端点。 |
| CTF 并发快照与归档/重放 | **2 passed，6.60 秒**；使用隔离 PostgreSQL，验证归档前后及重放后的状态保持。 |
| 前端 `pnpm --dir web check` | **155 passed**；ESLint、TypeScript 通过。 |
| 前端 `pnpm --dir web build` | 成功；保留原有大 chunk 提示。 |
| 候选 `bbx-pi-runtime:20261031` | 核对 **72 个**相关源码、依赖清单和构建文件，SHA256 全部与当前工作树一致。 |

三个独立导出树在提交前以 Git tree 摘要逐一核对，确保检查对应实际提交内容；修复按顺序叠加到 Pi 导出源码后再运行定向检查。独立导出使用各自的虚拟环境，不依赖当前工作树中已经修复的 Python 模块。Pi 的锁定 Node 依赖相同，因此复用已安装的同版依赖目录。

普通检查不调用真实模型。HTTP 使用模拟响应，数据库使用随机端口的隔离容器；未读取 `.env`、连接业务数据库或调用外部题目。日志、导出树摘要、源码核验记录位于忽略目录 `.data/pi-migration/submission/`。

复现当前交付的主要命令：

```sh
make check
pnpm --dir web check
pnpm --dir web build
.venv/bin/pytest -q services/agent-runtime/tests/test_pi_proxy_integration.py -m 'integration and not live'
.venv/bin/pytest -q services/blackboard/tests/test_ctf_snapshot_integration.py services/blackboard/tests/test_ctf_integration.py::test_terminal_archive_resume_and_scoped_purge -m 'integration and not live'
git log --oneline main..feature/pi-runtime-migration
git diff --check
```

## 偏差与待决

- 本轮没有新增生产逻辑、依赖、数据库迁移或配置项；当前源码与接手时已验证的最终版本保持一致。所有历史报告保留当时的检查与发布记录，最新交付状态以本报告及 HANDOFF 为准。
- 共享文件中，合同及生成 schema/OpenAPI/前端类型归入 CTF 性能或参数反馈提交；`Makefile`、runtime Dockerfile/依赖和 `uv.lock` 归入完整 Pi 迁移提交。没有混入 `.env`、私有发布材料或缓存文件。
- 初版的 430 项全量集成、上轮的 15 项相关集成与容器烟测仍是历史验证，本轮没有重复宣称运行。此次新增的实际集成为上表 6 项 HTTP 和 2 项数据库检查。
- 真实模型、云账户鉴权、实际解题速度及高并发容量没有实测。
- 当前 `main` 为 `55044a9`，主工作树干净，且为本任务分支的祖先，可快进合并。本轮未合并、推送、打标签或部署。
- 本地仍运行 `ctf-input-feedback-20261028`，候选镜像未推广；公网未操作。没有修改业务任务、预算、配置或存档，也没有续跑历史任务。

## 下一步

源码已具备合并检查点。仓库 `AGENTS.md` 第 7 节要求合并到 `main` 前取得用户明确确认；确认后可快进合并完整的 11 个提交。发布还需单独准备匹配的 blackboard 镜像，在空闲窗口保留数据和回退镜像后切换，不从源码提交完成推断已经部署。
