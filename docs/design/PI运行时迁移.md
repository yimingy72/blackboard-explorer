# PI 运行时迁移

[P7 · 单站点与共同 Pi 沙箱执行](../tasks/P7-单站点与共同Pi沙箱执行-20261111.md) 将黑板的授权协作会话迁至共同 Pi 1.5 与 OpenSandbox，沿用 Task、AgentRun、journal、可信 Pi 发送与单次费用回执。黑板保留 Fact/Intent、Derive 与 Close，不生成 CTF 身份。普通 Explore 的命令和文件走已有 SandboxMCP 授权，保留 BoardSync 增量、用户输入确认和 Derive 轮次。默认与平台强制 VPN/SOCKS5 的配置不回退；以本机受控网络验证切换，旧 Docker/envd 求解链在替代证实后移除。历史工作区及记录保留原代次；用户可继续使用只读复盘与模型测试。下文 P6 普通 Pi 1.4 说明对应现行发布及独立窄用途。

[P6](../tasks/P6-Pi原生模型与MCP迁移-20261110.md) 是当前模型与 MCP 合同，承接用户以 Pi 替换 MAF 并移除 AgentGateway 的决定。普通路径继续 Pi 1.4，Native Runner 继续 Pi 1.5；本轮不为迁移强行升级两套 SDK。原 P3c–P5e 网关接线只按冻结旧版本短期保留；旧活动/待收尾不能运行中换核，终态以后复开用经核准的新运行。 P6c 切换完成后，旧 AgentGateway 只保留本地历史证据读取，不再是启动、发送、恢复或管理后备；无法证明迁移的旧执行保留原记录且标为阻塞。主站指定操作者可用平台账号执行有界单轮模型演练，通过同一可信 Runtime 和 Pi 账本，但 `purpose=playground` 无 Task/Agent/额度，按账号和原 request ID 单次发送，原回复和计量同一终态持久化。它不混入任务或用户复盘；缺价和未确认使用未知，不自动重发。

## 组件选择

| 能力 | 实现 |
|---|---|
| 会话与 Agent 循环 | Native `pi-coding-agent` 1.5.0 的 AgentSession/SessionManager；普通已用 `pi-agent-core` 1.4.0 的宿主回调保持 |
| 多提供方模型、多模态、思考、流式和用量 | 上游供应商协议、流解析、用量和费用计算复用现有可信 Runtime 的 Pi AI；平台负责凭据、许可与站点账本 |
| 子 Agent | 社区 `pi-subagents` 0.85.0 / b6647222bd2e849021054292927ef39fea0793ed；复用公开子会话、消息、结果与取消，平台补身份/席位/确认/收尾 |
| 业务工具 | P6a 旧 MCP 维持；P6b 用官方低层 Pi MCP＋最小平台桥直达业务/Sandbox，保留原领域许可，不重复客户端/聚合 |
| 命令与文件 | 本 Task Sandbox MCP 窄扩展→官方 OpenSandbox SDK/execd；稳定命令身份、低权限文件、审计和流量归属从首命令接入 |
| 任务与运行治理 | PostgreSQL 保存 Task/Assignment、租约/epoch、调用关联、站点/call_id 费用去重、待核与阈值停止、输入/业务回执及会话确认引用；不建立第二会话历史或并行模型预算执行器 |
| 本机工具与资源 | 默认关闭 Pi bash/read/write/edit、cache warmer 及未登记后台模型路径；显式平台 ResourceLoader 只加载已发布配置、Skills 和可信扩展 |

不采用 pi-durable、Chord 应用框架、管理 LLM 或第二套工作流循环。所选 SDK/插件的持久确认与前后台宿主接口须先通过 G3，包名或源码存在不能证明可用。评估版本与部署锁分开；P6a 先验证真实 Pi 请求、账本及版本间流消息契约。AGW/OpenSandbox 既有固定基线继续作为兼容证据。

仅从平台冻结配置注入 MCP，禁止默认项目/全局 mcp.json、stdio/codemode 和本机执行。P6b 私网业务与 Sandbox sidecar 两目标各有独立新 issuer/audience，direct 切换前拒旧共享 audience；保持真实 403/409/422、许可、结果、命令审计和清理边界。普通 Explore 与原生 CTF 的 Lead/Worker 可使用操作者明确发布到该角色的外部工具；原生新 Task 分角色选择并冻结版本和工具，helper 不继承。原生 Pi 会话仍经现有 Business-v2 连接，通过有命名空间的工具进入 Blackboard 受信 host，再用官方 MCP SDK 向批准的 HTTPS Server 发送；不新增通用网关，不把外部凭据交给 Runner 或把内部能力交给远端。

高层 extension 忽略稳定 toolCallId/meta、404 effect 重发等会要求更多桥，故暂推荐官方低层客户端＋窄桥，保可等待确认/absolute permit/结果/关闭 gate；不是保留自研客户端或自研 MCP 整层。模型目录/路由单源可编辑，SDK 快照派生；DTO/小元数据去重不造 registry。Runner poll 后续只复用现 PG 通知（先订阅后读命令表、提交生效、listener/租约回退），不增总线。K8s 原生 workload/watch/Lease/卷约束不替 Task DB epoch、实际进程清理和 journal 单写证据，probe 不等产品控制器。

## 运行边界

1. Python Runtime 保留 Task 租约、普通 Explore/Derive/Close 调度、CTF Assignment/席位、成果整理租约、准入与独立 cleaner；Node/Pi 负责原生会话循环。Task Runner 是独立 Pod，可能跨控制服务重启存活，不能以 Python 协程死亡直接 finish、重派或释放席位。
2. 受控 Node.js >=22.21 进程只读取平台明确提供的资源；不读取用户 `~/.pi`、`.env`、全局提示词/扩展/凭据。Runner 不挂求解工作卷，不执行题目脚本；默认工具/Skills/子插件均不能恢复本机执行。命令、文件、业务和生命周期按实现正本四条路径分开。
3. 模型前完成权限/预算、提示词热更、输入与黑板/公告增量及 journal/业务回执确认。已发布提示词仍在下一次模型调用前替换单份系统指令，并记录版本；不改模型/工具快照、不扩权。完整模型响应确认后才允许其工具，不完整流/取消不派发半截工具。
4. 普通 subscribe 回调不可等待，不能承担硬屏障。主会话通过公开 session.agent.streamFunction 包装先确认再调用原函数；确认失败返回 SDK 可消费的 error/aborted 终态流、上游 0 请求，不直接 throw/reject。包装后不继承 ModelRuntime 私有 auth 标记，不 deep import/复制私有标记；主回合、压缩、分支总结、hosted helper 均通过同一可信 Pi 发送边界。仅在 Runtime 实际发送前重核 live fence、冻结模型与阈值，绑定请求摘要和 call/attempt 并许可一次；可信 worker 持久结算后才交最终消息。Runner 只持短期能力，不得直接持长期凭据或备用出站。
5. 工具参数使用 Pi schema，REST/MCP 共用领域处理器，实际写事务同连接重核 Task/Agent/role/Assignment/generation/epoch/runtime fence。Worker MCP 列题在既有 scope 上再限当前 binding.challenge_id，公告 Task 共享；metadata 不充当权限。可纠正参数错误可作工具结果，旧代次/未确认/未知副作用冻结相关准入。
6. 每个工具调用使用稳定 invocation/command_id，可信 host 先保存准入/审计，再实际执行；原结果由 host 持久后才交 Pi，Runner 不能上传任意结果造完成。ACK 丢失只按原 ID/SDK/参数读已保存 completed；当前合法 owner 恢复同 binding/SDK 可读原结果，历史读取不授新执行。已发 unknown 不重派，后台执行保原 ID/status/log cursor 和独立 drain。
7. P6a 由 Task 所属站 PG 以受信 site/call_id 一次登记实际发送及费用。累计达到阈值后拒新，保留多席位和已在途越线；未知保持保护、不重发或记零。用户只读复盘分账且不设金额上限，跨站按原用户/站点调用去重。
8. helper 从父有效快照继承模型/强度、获准 Skills/工具/环境，权限取交集、独立身份和 journal，主/子共用原子席位。父普通回合结束不取消子运行；换题、分配取消、整题完成或 Task 停止级联收尾。迟到产物归原 ChildRun，不投下一题。

## 会话与兼容

- Pi SessionManager 是新会话内容权威；journal 放 Runner 专属持久卷，一会话单有效写入者。平台保存 active leaf、confirmed_seq/hash、归属、输入和业务回执，保存确认备份/归档，不逐 token 复制完整历史。同步 append 不自动等于 fsync，实际存储与持久屏障须验证。
- 人工输入先持久入箱，确认进入 journal 后才显示已应用；普通 steer 在安全边界注入，停止并改计划先收尾旧执行。终态复盘另开只读用途会话，不恢复求解工具权限或自动重跑任务。
- 已有任务、预算、模型配置、黑板对象、MinIO 档案及原始 MAF/core 历史不重写、不删除。旧 Fact/Intent 保持空题目范围，普通 holder/resolves 与完成复核不被 CTF Assignment 替换。
- 历史读取适配只转换消息结构；保留工具调用 ID、工具结果、图片引用、来源 ID、思考/签名及 provider 元信息。旧工具配对修复依然不重执行原工具。
- 服务端统一 ConversationEntry/TaskEvent 投影供浏览器读取；浏览器不解析原始旧 Session 或 SDK JSONL。旧格式转换可单独测试，无须安装 MAF 才能读取历史；实际未保存的历史明确缺失，不补造。
- 停止先持久撤权/冻结，再取消 SDK、helper 宿主与远端命令，独立核对费用、执行和归档。新 epoch 接管先 frozen，旧 Runner/副作用、卷单写和环境确认后才恢复；Kubernetes 404 不证明分区旧进程已结束。
- 模型默认值、上下文与最大输出、角色覆盖、价格/币种和 Responses 兼容模式不能因框架切换被静默重置。不支持的旧连接明确报兼容性错误，不擅自换模型。
- 保留旧 USD/CNY、旧来源、冻结价目/汇率及已确认和未决记录，不回算。旧暂停任务迁入必须核准冻结模型/旧账/journal 和未知保护后换代；否则保全并明确阻塞。

## 验收与发布

先用脚本假模型和本机 HTTP/MCP 服务验证 SDK/子插件持久接缝，再覆盖既有角色用途、图片/流式和用量。G3 继续证明前后台确认、失败终态流且上游 0 请求、结果持久后不重放、继承及主/自动/手动压缩身份；G5 以所属站事务、跨阈与在途、每次真实 provider POST/许可/费用一一对应、未知、历史币种和换代证据验证。一次 streamFunction 不等于一次发送，保留可信发送摘要和回执；前台或一个阈值例子不代表全闸门通过。只补必要接缝，不调用真实模型/e2e/eval-run 或真实靶场。

最终标准是每个替代闭环同时移除对应旧通用代码及失效配置/依赖/测试，不全部拖 P6；新 Task 实际进锁定 SDK/可信发送/OpenSandbox，对应旧创建停止，明确旧活动/收尾绑定迁完或转只读。历史数据/已发生调用结算保最小读取，普通/成果整理/chatworker 等真实调用有迁移出口后删旧循环，不能用未来可续跑永久保引擎；也不能未接通就删或包完整旧层冒替代。

P6a 源码开发不自动切换本地/公网服务。发布另核 G1–G7、活跃资源、未知调用/费用和归档，保留数据库/对象存储/配置与可回退镜像。回退先停新准入，仅未启动新 Task 可选旧路径；已发生新副作用的任务保留现场核对，不倒回旧执行器重跑。
