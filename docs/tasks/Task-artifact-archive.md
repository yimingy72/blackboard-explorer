# Task-artifact-archive · 任务必要产物归档与容器销毁

用户已确认：只保留通过 Fact 附件明确登记的证据、复现脚本和必要依赖；任务结束后自动销毁该任务 Ubuntu，删除任务时清理该任务全部数据存档。设计依据：实现架构第2节、8.4与9.1；概念设计“必要产物归档修订”。先读 AGENTS.md、HANDOFF 和 Control-connection-resilience / Runtime-continuation 报告。

## 合同与范围

- 每任务独立容器，同任务多个Agent共用。保留现有 `post_fact.evidence` 登记机制，不加猜测路径、目录递归收集或领域专属规则；文件提交时已上传MinIO。更新Worker说明，明确复现脚本与必要输入/依赖须逐个登记，未登记临时文件不会保留。
- 新增仅service可访问的 `GET /api/tasks/{task_id}/archive-data`。任务须终态且无活动Agent，在任务事务锁下导出一致快照：`format: bbx.task-archive.v1`、`task_id`、`run_number`、`state`（任务/事实/意图/Agent）、`events`、`sessions`（agent_id、session、opening_instructions、origin、revision等必要数据）、`messages`（用户可见消息列，不含claim_token）、`task_runs`。不读取/导出平台凭据注册表；访问隔离与非终态拒绝须有测试。
- runtime 新增 `BlackboardClient.archive_data(task_id)` 和 `ExecEnvManager.archive_task(task_id, archive_data) -> ArchiveResult`。后者只根据快照的Fact evidence声明，从同任务MinIO对象生成必要文件 tar.zst，包含 `.bbx/task.json` 与 `.bbx/manifest.json`；普通工具记录/trace/图片/报告继续保存在原对象命名空间并由清单引用。现有envd全量 `/archive` 留作兼容/诊断，不用于正常cleanup。
- workspace归档key延用现有规则，fallback=`none`；manifest明确policy=`declared-evidence`。验证所有必要对象归属/可读、保留URI及原path映射，安全规范化路径，拒绝穿越、重复/前缀冲突及元数据保留目录冲突；只写普通文件/目录。相同原路径多版本保留全部引用，恢复选择最高Fact.version（并确定性处理同版本）。空附件任务也生成可用归档，保留shared目录。
- 文件先按块读取到临时文件再压缩/上传；不把整个工作区或大归档读入内存。不引入新压缩格式；可给runtime显式声明仓库已用的zstandard依赖。缺失对象/上传失败不能销毁容器或标记cleanup_ready。
- Supervisor在finished/failed/stopped后先drain，导出快照，生成并登记必要归档，销毁该任务容器，再record_cleanup；即使源容器已丢失，也能从已存证据生成归档。保留旧轮次归档；显式续跑只恢复所选文件，既有黑板/会话按原协议继续。
- 删除任务复用现有purge：清理本task所有 evidence/toolcalls/traces/reports/workspace key及数据库数据，不影响另一task。新增归档内容置于既有key/前缀内，不另建全局数据区。
- 工作台/部署文档的归档和续跑说明应如实写“已登记文件”，不得承诺完整工作区恢复。保留现有UI结构，不做无关重排。

## 验证与交付

自动化不调用真实模型。覆盖附件脚本/依赖字节与路径、旧版本引用、空任务、临时文件不入档、路径/任务隔离、对象缺失/上传失败保留容器、成功归档后销毁、续跑新容器恢复必要文件、删除所有轮次对象但保留另一任务。make check与make test-integration全绿；若涉及前端文案运行web check/build。

部署后处理已有413阻塞任务：先独立核对新归档附件清单/脚本字节/数据快照及旧归档保留，确认成功登记再让cleanup销毁Ubuntu，不重跑目标或删除任务存档。报告 `docs/tasks/Task-artifact-archive-report.md` 中文，列真实结果、偏差与待决、提交划分。
