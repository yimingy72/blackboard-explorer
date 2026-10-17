# 多任务并行运行报告

日期：2026-10-17。工作树：`/Users/yym/bbx-wt/multi-task-runtime`，分支：`feature/multi-task-runtime`，基线 main `6936707`。

## 完成内容

1. 默认任务容量由 1 改为 **2**，Settings、Compose 与 `.env.example` 保持一致；`MAX_RUNNING_TASKS` 仍支持其他正整数，超额按任务创建时间排队。任务内 Agent 数仍分别受各任务预算控制。
2. 复核已有 TaskSupervisor：普通黑板和 CTF 共用容量，启动、运行、收尾归档中的同一任务按集合去重；清理完成后补位。复用原调度器、每任务容器、持久会话及 MAF Agent，没有新增框架、数据库表、模型调用路径或前端设置。
3. 增加普通/普通、CTF/CTF、普通/CTF、CTF/普通四组容量与清理回归；停止第一个任务、等待其归档，再启动第三个任务时，第二个任务继续运行。扩充恢复检查：即使容量调低至 1，重启仍接管两个已运行任务，不强停已有任务。
4. 增加真实 Docker 并行容器检查：两个任务同时创建独立执行容器和随机 relay 端口；同名 Agent 和共享文件不串用；一个任务的 token 无法管理另一任务；销毁其中一个后，另一容器仍能执行。
5. 已将本地和服务器实际 runtime 配置更新为 2，只重建 agent-runtime；保留 PostgreSQL、MinIO、blackboard 与原任务执行容器。

## 实际验证

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync --frozen
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
.venv/bin/pytest services/agent-runtime/tests/test_supervisor.py services/agent-runtime/tests/test_ctf_supervisor.py services/agent-runtime/tests/test_runtime_settings.py -q
TESTCONTAINERS_RYUK_DISABLED=true .venv/bin/pytest services/agent-runtime/tests/test_execenv_integration.py -m integration -q
```

- `make check`：**2149 passed、352 deselected**，58.97 秒；ruff、Pyright 通过。
- 最终监管/配置回归：**52 passed**，3.48 秒；包含后补的双任务重启恢复断言。
- 最终 Docker 生命周期/并行隔离：**5 passed**，23.35 秒；含原 direct/proxy 归档恢复、自定义 hostname 与新并行隔离。
- Compose 配置静态验证通过；两端运行容器均实际读取 `MAX_RUNNING_TASKS=2`。
- 本地与服务器登录页均返回 HTTP 200。
- 未修改前端，没有重跑前端套件或全部 Docker 集成；自动化测试未调用真实模型。

检查日志与非秘密部署快照位于忽略的 `.data/multi-task-runtime/`。

## 部署与数据核验

本地执行：

```sh
MAX_RUNNING_TASKS=2 sh scripts/deploy_ctf_team_mode.sh up -d --no-deps --wait agent-runtime
```

本地旧环境显式设置了 1，本次用进程变量覆盖，没有读取或修改 `.env`。后续重新部署时仍需传入该变量，或由部署者更新持久部署配置；只重启现有容器会保留当前值。入口固定历史数据卷，不会把数据库重建到空卷。

服务器将 `/opt/blackboard-explorer/operations/compose.server.yml` 中的容量改为 2，并用已有 `compose.py up -d --no-deps --wait agent-runtime` 生效。覆盖文件已持久保存，旧文件备份为 `compose.server.yml.before-multi-task-20261017`；没有读取或打印私有凭据，没有更新其他服务或执行镜像。

核验结果：

- 本地：19 个任务、150 个会话；配置内容摘要、任务 ID/状态保持。
- 服务器：19 个任务全部 ID 保留；会话由 131 增至 133，配置内容摘要保持。原运行任务从持久化状态恢复；观察到一个普通任务处于 closing 时，第二个任务已经进入 running，之后原排队任务继续补位。
- 两端 PostgreSQL、MinIO、blackboard 的容器 ID 和挂载卷在更新前后完全一致。活动任务的状态、会话及事件随业务正常推进，没有要求它们静止或伪造相同摘要。
- 地址：本地 `http://127.0.0.1:58000/`；服务器 `http://101.201.116.63:58000/`。

服务器原排队的两个业务任务被接纳后均遇到模型接口 **HTTP 402 / payment_required**：普通任务 `b3e0db23…` 的两个种子调用，以及 CTF 任务 `f39b4a8c…` 的 Lead 调用，持久化错误元数据都能确认该状态。前者最终为“种子无法起步”，后者为失败终态。这些任务不是本次新增的测试任务；没有更改其模型或凭据，也没有将失败改成成功、自动续跑或为它们换用其他账户。需要用户检查对应模型服务的账户/额度后再决定是否续跑。

## 偏差与待决

- 原单任务限制来自部署容量，而非 MAF 或数据库不能并行；因此实现只调整已有配置，调度代码无需重写。
- 默认使用 2 个槽位，避免在现有机器上无限创建执行容器；每容器原 CPU/内存上限保持。容量可以通过已有变量提高。
- 沿用当前 runtime 镜像并更新环境配置即可生效，不需要重新构建刚完成的 Kali/Ubuntu 工具链。
- 已按用户本次需求同步实现架构与开发方案，设计合同先单独提交为 `eff1698`。实现、回归与本报告保留在任务分支，尚未合并 main 或推送。

## 共享文件与建议提交

- `docker-compose.yml`：仅将任务容量默认值改为 2。
- `.env.example`：仅同步默认值；未修改 `.env`。
- 无新增依赖、锁文件、共享契约或数据库迁移。
- 设计提交已完成：`docs(runtime): define concurrent task execution and isolation`。
- 建议实现提交：`feat(runtime): enable concurrent tasks by default`，包含 Settings、Compose、示例配置及三份测试。
- 建议说明提交：`docs(runtime): document concurrent task operation and rollout`，包含 README、运行器说明、使用与部署、HANDOFF 和本报告。
