# Task-artifact-archive · 必要产物归档实施报告

日期：2026-09-29。用户已确认只保留通过 Fact 附件明确登记的证据、复现脚本和必要依赖；任务结束后销毁本任务 Ubuntu，删除任务才删除对应存档。

## 完成内容

- 每任务独立 Ubuntu、同任务 Agent 共用的隔离方式保持。原 13 GB 来自任务 `4891d8c7-7720-45de-8aec-5562809c57a3` 两轮探索中的下载、镜像展开和扫描临时文件，并非不同任务共用容器。
- 新增仅 service 可访问的 `GET /api/tasks/{task_id}/archive-data`，在任务行锁下导出终态、无活动 Agent、未删除任务的黑板、事件、完整 Agent 会话、公开用户消息和历次运行数据。不导出平台凭据注册表或消息 claim token。
- runtime 直接从 MinIO 的已登记 Fact 附件生成必要文件 tar.zst，不扫描或打包 Ubuntu 工作区。`.bbx/task.json` 保存结束时数据快照，`.bbx/manifest.json` 保存全部附件引用和恢复路径、大小、SHA-256；同一原路径多版本全部保留 URI，恢复选择最高 Fact 版本。工具全文、trace、已保存图片及报告仍在本任务 MinIO 命名空间，清单引用它们。
- 新终态流程为：等待 Agent 与会话保存结束 → 导出数据 → 验证附件可读 → 生成并上传归档 → 登记归档 → 销毁本任务 Ubuntu → 标记 cleanup_ready。源容器丢失也能从已存附件生成归档；对象缺失、上传失败、容量超限不会伪造归档成功或销毁容器。
- 删除任务沿用 task UUID 范围清理全部轮次档案、证据、脚本、工具记录、trace、报告、会话和数据库记录，并关闭该任务所有轮次预览缓存；另一任务保持不变。删除失败保留 deleting 状态以重试。
- default/single 的 Explore、Derive、Close 提示词、使用文档和续跑/归档界面文案明确“附件登记才保留”。不按自然语言中的路径或扩展名猜测必要文件，不扩大 Worker 工具权限；续跑创建新容器，仅恢复登记文件，进程、系统包和未登记临时目录需要重建。

## 验证

- `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check`：713 项普通测试通过，ruff format/lint、pyright 通过。日志 `/private/tmp/bbx-task-artifact-check-final.log`。
- `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make test-integration`：66 项通过，717 项非集成测试排除，224.50 秒。日志 `/private/tmp/bbx-task-artifact-integration-final.log`。首次全套有两项共享数据库状态污染失败，新增导出测试正常结束消息认领后，存储模块 34 项与完整套件均通过，原删除隔离断言保留。
- `pnpm --dir web check`：33 项测试、ESLint 和 TypeScript 通过；`pnpm --dir web build` 成功，保留既有大 chunk 提示。
- 真实 MinIO + Docker direct/proxy 恢复验证：新容器中的登记脚本与共享依赖字节一致，未登记临时文件不入档。另覆盖空附件任务、路径穿越/冲突、跨任务对象、缺失对象、上传失败、全版本引用与删除、取消时写线程清理、tar 头和 padding 的真实展开上限。
- 本轮没有调用真实模型或重新执行用户靶场任务。

## 旧任务迁移前校验

主代理先对该任务 210 个已登记对象逐个读取并保存 SHA-256 基线，共 2,200,330 字节；其中有 125 个工作区路径、12 个 script 附件。然后用新镜像生成候选归档并独立解包比对：所有恢复文件的大小、散列及全部对象引用一致，数据快照也完全一致。

候选归档 3,082,665 字节（约3.08 MB），包含125个恢复文件、40条Fact、30条Intent、39个Agent及39份Session、4,624条事件。校验记录在主仓库忽略目录 `.data/checkpoints/task-artifact-archive/`。上传登记与容器销毁的最终记录见交付补充。

## 边界与偏差

- 必须由 Agent 明确登记附件；未登记临时工作文件不保证保留。已提交附件的历史版本仍保留在 MinIO；归档中的恢复路径只选一个确定版本，不覆盖原历史证据。
- 归档保留文件内容与路径，恢复为普通文件，不恢复原进程、系统安装状态或全部 POSIX 元数据。既有全量归档保持可下载与读取；新归档按已登记文件恢复。
- 保留既有2 GiB压缩、8 GiB展开和10万条目安全边界；实际tar头、padding也计入展开限制。超过必要产物容量时仍应处理容量，不能静默丢附件。
- 黑板和会话仍在 PostgreSQL 供日常查询；MinIO 保存结束时快照。结束后的只读续聊继续按原协议保存会话，不把旧快照误称为后续聊天的实时镜像。

## 共享文件与提交划分

设计先行提交 `ba7de48`。实现增加 runtime 对仓库既有 `zstandard>=0.25,<1` 的直接依赖，并更新 `uv.lock`；没有新增依赖版本、数据库迁移或全局配置。OpenAPI与前端生成类型同步新service接口。

建议实现与跨服务回归作为一个提交，提示词/用户说明和前端文案作为一个提交，最后记录部署与旧任务归档校验。提交均标注 `Implemented by Codex (gpt-6-sol) for task Task-artifact-archive.`。
