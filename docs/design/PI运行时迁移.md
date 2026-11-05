# PI 运行时迁移

2026-11-06 当前合同，承接 2026-10-30 用户以 [Pi](https://github.com/earendil-works/pi) 替换 MAF 的决定及后续 SDK 选型。用户已授权审核通过后开发和 [开发方案第 16 节](../tasks/平台架构重构开发方案-20261105.md)的正本同步；三路第二轮审核通过进入 P0/P1，不代表 G1–G7 已通过。

本合同替换旧的“直用 pi-agent-core＋逐模型/工具 Python 回调＋数据库复制完整会话”方案。普通黑板、CTF 团队、只读复盘及成果整理的领域规则保持；存量任务按创建时冻结的执行版本继续，不能在运行中换核。

## 组件选择

| 能力 | 实现 |
|---|---|
| 会话与 Agent 循环 | `@earendil-works/pi-coding-agent` SDK 1.5.0 的 AgentSession/SessionManager；core/ai 是内部依赖，不并列维护直接 core 执行器 |
| 多提供方模型、多模态、思考、流式和用量 | 复用 SDK/pi-ai 与模型准入→agentgateway 路由；平台保留价格/币种、预算和未知费用账本 |
| 子 Agent | 社区 `pi-subagents` 0.85.0 / b6647222bd2e849021054292927ef39fea0793ed；复用公开子会话、消息、结果与取消，平台补身份/席位/确认/收尾 |
| 业务工具 | pi-mcp→agentgateway→共享 BBX 业务 MCP，REST/MCP 共用原 Python 处理器；不重写领域规则 |
| 命令与文件 | 本 Task Sandbox MCP 窄扩展→官方 OpenSandbox SDK/execd；稳定命令身份、低权限文件、审计和流量归属从首命令接入 |
| 任务与运行治理 | PostgreSQL 保存 Task/Assignment、租约/epoch、调用账本、预算、输入/业务回执及会话确认引用；不建立第二份完整会话真相 |
| 本机工具与资源 | 默认关闭 Pi bash/read/write/edit、cache warmer 及未登记后台模型路径；显式平台 ResourceLoader 只加载已发布配置、Skills 和可信扩展 |

不采用 pi-durable、Chord 应用框架、管理 LLM 或第二套工作流循环。所选 SDK/插件的持久确认与前后台宿主接口须先通过 G3，包名或源码存在不能证明可用。评估版本与部署锁分开记录：当前旧锁仍为 core/ai/telemetry 1.4.0；P0 不以改文档宣称依赖已升级。网关/OpenSandbox 固定基线和镜像锁计划见开发入口第 0.2、15–17 节。

## 运行边界

1. Python Runtime 保留 Task 租约、普通 Explore/Derive/Close 调度、CTF Assignment/席位、成果整理租约、准入与独立 cleaner；Node/Pi 负责原生会话循环。Task Runner 是独立 Pod，可能跨控制服务重启存活，不能以 Python 协程死亡直接 finish、重派或释放席位。
2. 受控 Node.js >=22.21 进程只读取平台明确提供的资源；不读取用户 `~/.pi`、`.env`、全局提示词/扩展/凭据。Runner 不挂求解工作卷，不执行题目脚本；默认工具/Skills/子插件均不能恢复本机执行。命令、文件、业务和生命周期按实现正本四条路径分开。
3. 模型前完成权限/预算、提示词热更、输入与黑板/公告增量及 journal/业务回执确认。已发布提示词仍在下一次模型调用前替换单份系统指令，并记录版本；不改模型/工具快照、不扩权。完整模型响应确认后才允许其工具，不完整流/取消不派发半截工具。
4. 普通 subscribe 回调不可等待，不能承担硬屏障。主会话通过公开 session.agent.streamFunction 包装先确认再调用原函数；确认失败返回 SDK 可消费的 error/aborted 终态流、上游 0 请求，不直接 throw/reject。包装后不继承 ModelRuntime 私有 auth 标记，不 deep import/复制私有标记；主回合、自动/手动压缩均通过公开接缝使用受限网关身份。最终模型 HTTP 准入再次拒绝未确认调用，包装与入口共用 call/attempt、不重复预留；子会话独立证明同等语义，未登记后台调用保持关闭。
5. 工具参数使用 Pi schema，并由共享领域处理器核验可信 Task/Agent/role/Assignment/generation/epoch。可纠正参数错误可作工具结果；未知写入、控制面失败、旧代次或 checkpoint 未确认必须停止当前准入，不能由 SDK 吞错后继续执行。
6. 每个工具调用使用稳定 invocation/command_id，先保存准入/审计，再调用官方 SDK；结果先持久化，再写 Pi journal 并确认。日志已有结果但 ACK 丢失按原 ID/游标核对；已发送工具结果未知、取消或进程丢失均不能重派。同代次后台执行保留原 ID/status/log cursor 和独立 drain。
7. 主回合、压缩、摘要、helper、复盘与成果整理都经过同一模型入口并保持各用途原额度。按最终请求可证明的费用上界预留；输入估算不是严格金额上界。一次 streamFunction 不等于一次真实发送，429/503/断流的每次真实发送都有独立 attempt/准入/回执；断流 unknown，不叠加 Pi/provider/gateway 重试，不自动跨模型 fallback。
8. helper 从父有效快照继承模型/强度、获准 Skills/工具/环境，权限取交集、独立身份和 journal，主/子共用原子席位。父普通回合结束不取消子运行；换题、分配取消、整题完成或 Task 停止级联收尾。迟到产物归原 ChildRun，不投下一题。

## 会话与兼容

- Pi SessionManager 是新会话内容权威；journal 放 Runner 专属持久卷，一会话单有效写入者。平台保存 active leaf、confirmed_seq/hash、归属、输入和业务回执，保存确认备份/归档，不逐 token 复制完整历史。同步 append 不自动等于 fsync，实际存储与持久屏障须验证。
- 人工输入先持久入箱，确认进入 journal 后才显示已应用；普通 steer 在安全边界注入，停止并改计划先收尾旧执行。终态复盘另开只读用途会话，不恢复求解工具权限或自动重跑任务。
- 已有任务、预算、模型配置、黑板对象、MinIO 档案及原始 MAF/core 历史不重写、不删除。旧 Fact/Intent 保持空题目范围，普通 holder/resolves 与完成复核不被 CTF Assignment 替换。
- 历史读取适配只转换消息结构；保留工具调用 ID、工具结果、图片引用、来源 ID、思考/签名及 provider 元信息。旧工具配对修复依然不重执行原工具。
- 服务端统一 ConversationEntry/TaskEvent 投影供浏览器读取；浏览器不解析原始旧 Session 或 SDK JSONL。旧格式转换可单独测试，无须安装 MAF 才能读取历史；实际未保存的历史明确缺失，不补造。
- 停止先持久撤权/冻结，再取消 SDK、helper 宿主与远端命令，独立核对费用、执行和归档。新 epoch 接管先 frozen，旧 Runner/副作用、卷单写和环境确认后才恢复；Kubernetes 404 不证明分区旧进程已结束。
- 模型默认值、上下文与最大输出、角色覆盖、价格/币种和 Responses 兼容模式不能因框架切换被静默重置。不支持的旧连接明确报兼容性错误，不擅自换模型。

## 验收与发布

先用脚本假模型和本机 HTTP/MCP 服务验证 SDK/子插件持久接缝，再覆盖普通探索/推导/裁定、CTF 派工/输入/收尾、成果整理/复盘、图片/流式和用量。G3 分别证明主/前台/后台日志确认、确认失败终态流且上游 0 请求、结果保存后崩溃不重放、强制身份/工具继承及主/自动压缩/手动压缩的受限网关身份；G5 现有模型场景覆盖 429/503/断流的逐 attempt 账本，不另建重复测试矩阵。前台通过不代表后台通过，后台未过则关闭异步能力。只补必要接缝测试，不调用真实模型、不运行 e2e/eval-run 或真实靶场。

最终完成标准：新 Task 实际进入锁定 SDK、受控网关和 OpenSandbox，旧 core/MAF 入口停止接新 Task；现有业务与必要集成门禁通过。存量旧任务仍保留所需运行/停止/恢复/结算写入；确认没有旧运行或续跑绑定、历史读取/复盘通过后才删除旧执行器及依赖。不能仅改导入名称、保留第二循环或删历史来宣称迁移完成。

P0/P1 源码开发不自动切换本地/公网服务。发布另核 G1–G7、活跃资源、未知调用/费用和归档，保留数据库/对象存储/配置与可回退镜像。回退先停新准入，仅未启动新 Task 可选旧路径；已发生新副作用的任务保留现场核对，不倒回旧执行器重跑。
