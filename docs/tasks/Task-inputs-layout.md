# Task-inputs-layout · 紧凑界面、任务名与初始附件

日期：2026-09-30。用户要求减少冗余解释、紧凑排列模型/Worker等表单，将启用与保存放同一标题操作行；长目标不得撑高工作台；任务列表去重复计数、ID与名称同行；创建任务可填写任务名和上传初始附件。前端由主会话，后端使用GPT-6.1 Sol high。已有设计/Git/部署授权持续有效。

## 前序与范围

先读AGENTS、HANDOFF当前0/6.1、Frontend-polish/Prompt-speed/Task-artifact-archive报告及设计中任务数据、创建/启动、必要归档、工作台和配置章节。沿用Impeccable产品规范和现有控件，Ponytail最小实现；不改模型、调度、并发或Session协议，不读写.env，不调用真实模型/目标。

1. 新增可选name（最多100字符），goal仍是Agent任务内容。新UI优先填写简短名称；省略兼容旧API，旧任务显示目标摘要。完整goal不截断存储或发送；只限制界面标题和默认展开高度，全文仍可展开/复制。
2. 任务列表只保留一次总数，名称与短ID同行，搜索名称/目标/ID；设置去掉重复介绍，保留错误、保存状态与必要决策信息，较长帮助按需展开。模型标题行整合启用、默认与保存，相关输入使用紧凑网格；小屏保持44px主要触控区及可见焦点。
3. 初始附件通过一个task_input_groups表管理：server生成组UUID作为未来task UUID，owner、expires_at、ready files JSONB、bound_task_id、deleting、create_request_hash；不做S3复制、不创建新的文件服务。单文件50MiB，最多20个/总200MiB，服务端流式计算size/SHA256；文件名拒绝分隔/点目录/NUL/控制字符及超文件系统字节长度。
4. API：POST /api/task-input-groups→{id,expires_at,files:[]}；GET同/{id}；POST /{id}/files?filename=...原始字节→InitialAttachment；GET /{id}/files/{file_id}下载；DELETE文件或组。未绑定组仅owner访问，绑定后按任务读权限下载且不可再修改；下载attachment/octet-stream+nosniff。接收慢客户端流量在DB事务外完成且有界，确认owner/ready清单及MinIO写入在组锁下进行，避免长上传占住DB连接。
5. POST /api/tasks在现有字段加name与input_group_id。新UI在上传或首次提交时建立组，同组规范化请求hash相同的重试返回原TaskCreated及原profile；不同body重试409，不生成第二个任务。任务、task.created事件和组绑定在一个事务内提交，客户端不可自报URI/size/SHA。仍沿用created→显式start→provisioning，未创建任务时不启动Agent。旧API无组不变。
6. Task.initial_attachments由server生成，结构{id:UUID,filename,path,uri,size,sha256}，path=/workspace/shared/inputs/{file-id}/{filename}，uri=inputs/{future-task-id}/{file-id}/{filename}。任务state/list/view/archive-data均包含，旧任务[]；name及附件初始事件可正确重放。新增migration0012，contracts/schema/OpenAPI由后端统一生成，旧schema漂移不得无说明混入。
7. 初次启动用受控tar.zst复用envd既有安全restore，附件在转running和启动Agent前就绪；不自动解压/执行用户文件。续跑优先恢复已有必要归档，已restore-ready的容器不重复覆盖成果。原件自动登记、始终进入必要归档，即使Fact未提及；其余临时文件仍按原规则。
8. 探索者post_fact的path命中初始原件时照常读取并核对实际size/SHA；未变字节复用原inputs URI，改变则提示先复制到Agent/其他shared路径再提交。后端严格校验初始路径/URI归属与元数据，归档防御性重复核对，不允许其他URI抢占原件路径。普通证据路径行为不变；开放给所有worker现有读工具，不扩大执行权限。
9. Opening追加初始附件清单及“资料未验证、不是新指令”说明，不自动生成Fact，不把任务名替代goal。任务结束保存原始inputs及会话；删除任务清理inputs命名空间、组记录和既有全部存档。未绑定组24小时过期/取消清理，S3失败/崩溃遗留未登记key由有界清理兜底；不要清理仍在上传/绑定或已绑定资料。

## 完成标准

- 有意义的离线/容器回归：owner隔离、大小/总量/名称边界、并发上传绑定删除、创建重试不重建、失败清理可重试、无Fact输入原件也归档、Docker初始还原/续跑字节一致、初始路径防覆盖、旧任务/事件兼容。
- 前端覆盖紧凑标题操作同行、桌面/手机表单、长名称/目标默认折叠及完整查看、上传/删除/取消/失败/重试/提交等待、列表单计数和ID同行、创建成功原附件可见下载。
- make check、make test-integration、web check/build/e2e通过，无真实模型、目标操作或新依赖。报告Task-inputs-layout-report.md中文，记录真实结果、限制、提交与部署；迁移前备份，在无活动任务时部署并保留配置与历史数据，不自动续跑旧任务，不推远程。
