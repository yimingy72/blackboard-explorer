# 任务 M0-fix · M0 审查后的修正

Claude 已审查 M0，成果已提交在分支 `m0`。先重新阅读 `AGENTS.md`（第 6、7 节有更新），然后完成以下修正。范围仅限本文件，不要做其他改动。

## 1. 对象存储镜像

`docker-compose.yml` 中 `minio` 与 `minio-init` 均改用 `pgsty/minio:RELEASE.2026-04-17T00-00-00Z`（固定版本；该镜像自带 `mc`）。`minio-init` 用同一镜像、以 `mc` 创建 bucket，保持幂等。

## 2. 参数调整（对应设计文档的最新修订）

设计文档已更新（见 `docs/design/黑板式探索架构设计.md` 第 5.1、5.6、9 节）：

- `close_reserve_cost` 改名为 `close_reserve_ratio`，含义是占 `max_cost` 的比例，范围 [0, 1)，默认 0.05。更新 `Params`、`profiles/default/params.yaml`、测试和导出的 JSON Schema。
- `max_concurrent_agents` 只属于任务的 `Budget`，从 `Params` 与 `params.yaml` 中删除；`TaskSpec.params` 的覆盖校验随之生效（覆盖该键应被拒绝）。补一条测试。

## 3. 验证（实际运行并写入报告）

1. `make check` 全绿；`make schemas` 重新导出。
2. `make up`：postgres、minio 为 healthy，`minio-init` 成功退出，bucket `blackboard` 存在（例如 `docker compose exec minio mc ls local/`，需要先 `mc alias set`，或查看 `minio-init` 的日志）。
3. 再次 `make up` 确认建桶幂等；然后 `make down`。

## 4. 报告

在 `docs/tasks/M0-report.md` 末尾追加"## M0-fix 补充"一节：改了什么、验证结果、建议的提交划分。不要改动报告原有内容。
