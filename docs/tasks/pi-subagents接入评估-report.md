# pi-subagents 接入评估

> 2026-11-06 统一开发入口：[平台架构重构开发方案](平台架构重构开发方案-20261105.md)。三路第二轮审核通过进入 P0/P1，用户已授权开发和正本同步；本文以下保留前轮插件源码评估，早期“四工具 operations”、逐 Agent 文件硬隔离等表述不作为当前实施合同。子会话确认与后台收尾仍须实际通过 G3。

> 历史更新曾记录于“官方 MCP 与任务隔离更新报告”；当前已收敛为官方 SDK/execd 上的命令身份/低权限文件窄扩展，平台 SDK 管生命周期，同 Task 共同低权限文件身份，网络身份用于归属。当前合同统一见上述开发入口，不沿用旧“原版 MCP 完全直通”选择。

日期：2026-11-05。以下原评估范围为依赖与架构，当轮不安装插件、不恢复开发；后续用户已确认采用方案。此前 subagent 设计/图集更新为历史材料，本轮不复制或更新旧图集；本文保留原评估依据，当前实施状态以 P0/P1 记录为准。

## 1. 结论

建议采用 `pi-subagents` 复用子会话、委派、结果返回、状态查询和取消机制；平台自动继承父 Worker 的有效配置，Worker 只提出子任务、材料引用和预期产物。现有设计中“平台自建 subagent 工具并自行管理整套子会话”的表述应调整。

这项选择是有接入条件的推荐，不能将 `pi install npm:pi-subagents` 等同于已经满足 OpenSandbox、预算、权限、停止和恢复合同。两个开发前需要验证的接缝是：子会话检查点接入，以及后台 Runner 的完整停止与回收。

`pi-subagents` 是 Nico Bailon 维护的社区扩展，使用 Pi 官方扩展机制；不是 Pi 官方维护的包。[npm 元数据](https://registry.npmjs.org/pi-subagents/0.85.0)、[包定义](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/package.json)。

本次锁定检查 npm `0.85.0`，其 `gitHead` 为 `b6647222bd2e849021054292927ef39fea0793ed`。Pi SDK 对照上一轮选定的 `v1.5.0`；包声明的依赖范围不等于两者已实际联调通过。后续安装放在可信 Runner 镜像构建或部署配置中，固定版本，不交给 Worker 临时安装。

## 2. 插件实际能继承什么

| 项目 | 上游实际行为 | BBX 应采取的规则 |
| --- | --- | --- |
| 工作目录 | 未传子目录时使用调用上下文目录，也允许调用参数覆盖 | 默认绑定父 Worker 当前题目目录，禁止模型改为其他任务或 Runner 路径 |
| 模型 | 无其他覆盖时可回退到父模型；插件配置、角色定义、调用参数均可能改变选择 | 自动沿用父 Worker 已解析的模型与思考配置；模型策略由平台配置决定 |
| 工具 | 未指定 tools 时是 Pi 默认内置工具，不是父 Worker 的所有动态工具；指定时是显式白名单 | 从父 Worker 当前有效工具清单生成子权限快照，不能回落到本机四工具 |
| MCP | 支持直接 MCP 工具选择；需相应配置或扩展注册，不复制父进程的任意连接和回调 | 继承可用服务器、工具合同与访问范围；为子 Agent 新建受限调用身份 |
| 扩展 | 前台子会话不加载父会话的 ambient extensions；后台可按配置重新加载 | 显式加载批准的 BBX 接入扩展，避免个人或题目目录中的扩展被重新发现 |
| 提示词与技能 | 有自己的 Agent 定义、prompt 模式、技能继承开关 | 平台提供统一 helper 角色和同版本的获准技能；不要求 Worker 拼接完整系统提示词 |
| 对话上下文 | 支持 fresh/fork，独立于环境和工具继承 | 默认独立上下文，自动注入当前题目说明、材料、黑板摘要；Worker 补充子目标 |

依据：[目录解析](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/src/shared/utils.ts#L125)、[模型解析规则](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/docs/models.md)、[工具与 MCP 选择](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/docs/agents.md#tool-and-extension-selection)、[上下文配置](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/docs/configuration.md#defaultsubagentcontext)。

不能把文件目录自动继承解释为整个运行对象自动克隆。前台子会话可继承已注册的 provider，但对应扩展的会话 hooks 不随 provider 一起继承。父 Worker 的 stream 包装、工具 operations 和预算关联都需要在子会话中有明确接入。[子会话构造源码](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/src/runs/shared/child-session.ts#L175)。

## 3. 推荐的自动继承合同

### 3.1 Worker 的输入保持简单

Worker 只需要表达“分析这个附件中的校验算法，返回可复现脚本和结论”，并给出本题材料引用与期望产物。角色固定为平台发布的 helper。模型、cwd、MCP 地址、权限、凭据、执行身份、剩余预算不由 Worker 填写。

即使底层插件工具仍提供这些可选参数，平台也必须在启动准入时约束，而不是仅在提示词中要求不填写。第一阶段只开放本题单层委派及对应状态、消息、取消能力，不开放任意工作流脚本、定时任务、跨项目放置、外部 CLI Runner 或动态安装/修改 Agent 定义。

### 3.2 平台绑定父 Worker 的有效快照

自动绑定同一 Task、Assignment、题目范围、配置版本、Sandbox、网络环境、模型策略和已授权专业工具。子 Agent 拥有独立身份、日志、调用编号和 MCP 会话；不复制父 Agent 的 bearer token 或 invocation。

权限取父 Worker 当前权限与 helper 角色上限的交集。helper 可以调用本题求解工具、读黑板、记录带来源的进展与产物；主题 owner、最终候选 Flag 归并和结果交付由父 Worker 保持。Lead 的环境管理、平台提交与验收权限不下传；默认不允许递归创建子 Agent。

配置快照用于保证一致性；平台实时暂停、撤权和停止仍优先于旧快照。

### 3.3 工作目录与文件协作

默认沿用父 Worker 当前题目的可见工作目录，确保相对路径和附件位置一致。平台另分配 `.subagents/<child_id>/` 存放子任务临时文件与产物，默认在该目录写入；父 Worker 汇总。确需修改共享文件时，沿用平台的文件权限、I/O lease 和版本校验，避免两个 Agent 同时覆盖。

这是同一题目目录的受控视图，不授予整个任务或其他题目的目录。实际 OpenSandbox 执行会话、UID、文件挂载和远端 cwd 由服务端绑定。插件的 `ctx.cwd` 属于可信 Runner 资源发现上下文，不能直接当作远端目录已经存在；需要显式映射逻辑工作目录，并由远端四工具执行。会话日志、插件配置、运行凭据保存在 Worker 不可写的可信路径。

### 3.4 资源与成本

沿用已有席位规则：helper 借用空闲 Worker 席位，父子共同计入总上限；例如上限 20 时，18 个求解 Worker 加 2 个 helper 已满。没有空闲席位直接拒绝本次增援，父 Worker 继续求解，避免所有父 Worker 同步等待额外席位。

子调用进入同一任务模型准入与费用账本，继承任务剩余时间，不能因插件自行统计费用而另建一套硬预算。插件原生 usage/cost 用于展示和核对。先覆盖可独立求解的主任务，再用余量增援。

## 4. 复用边界与最小接入

推荐复用插件现有公开接缝：

- `registerRequiredChildExtensions`：强制为子会话加载同一份 BBX 接入扩展；使用 `requireForAllRunners: true`，不能绕到不加载平台接入的外部 Runner。加载或启动失败时，不允许回落到原生本机执行。
- `registerSubagentCapabilityCeiling`：将工具与允许的 helper 角色上限绑定父会话。平台保留执行端鉴权；该 API 是进程内约束，不是操作系统隔离。需要 BBX 扩展时不能同时设 `denyExtensions: true`，否则上游会拒绝启动。
- 插件 preflight：检查解析后的工具、扩展、目录与模型；用于核对，不取代平台原子容量占位和最终执行准入。
- 状态、结果、取消和通知继续由插件提供，平台映射至现有 Agent/子任务视图，不引入另一套子 Agent 管理页面。

BBX 接入扩展供 Worker 和 helper 共用：从可信服务取得各自绑定，注册官方四工具的远端 operations、获准 MCP 工具与黑板工具。共同配置自动应用，身份与作用域按会话重新签发。前台子会话可显式加载该扩展，无需为动态 BBX 工具复制父进程函数闭包；插件的原生 ambient MCP 路径与这种显式工具注册路径需区分。

依据：[强制子扩展接口](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/src/shared/required-child-extensions.ts#L73)、[权限上限](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/src/runs/shared/capability-ceiling.ts)、[公开接入 API](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/docs/extension-api.md)。

### 4.1 子会话检查点是尚未验证的接缝

最新版已在内部调用 `createAgentSession`：前台位于父 Pi 进程，后台位于独立 Runner；不需要 BBX 再造一个 Agent 循环。

但插件公开 exports 没有暴露通用子会话创建工厂或原始 AgentSession 回调。源码存在可注入的内部 `ChildSessionFactory`，主要供内部组合与测试，不能当作已稳定承诺的公共接口。Pi v1.5.0 的普通 ExtensionContext 也不直接公开原始 `session.agent`。

上一轮架构要求通过原始会话接入 awaited 事件/stream 包装，保证发出模型或副作用请求前检查点可用。强制加载一个扩展并不自动证明这项要求已满足。开发前应验证公开 hooks 能否提供相同的日志顺序和失败拒绝语义；不能满足时，向上游补一个小的宿主生命周期接缝，不长期依赖私有模块或 fork 整套插件。此项完成前，不能宣称恢复能力已等价。

依据：[子会话接口和构造](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/src/runs/shared/child-session.ts#L57)、[公开 exports](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/package.json)、[Pi ExtensionContext](https://github.com/earendil-works/pi/blob/f9a26365568127461c47a07a37dcb42c140a3ac5/packages/coding-agent/src/core/extensions/types.ts#L345)。

### 4.2 异步生命周期必须归入任务 supervisor

为了让 Worker 可以继续解题，目标支持插件异步子会话。其后台 Runner 必须成为 Task supervisor 可枚举、可取消、可回收的受控子进程；不会增加常驻管理 LLM。

上游明确说明：父会话结束后，detached children 仍可继续运行，且通知可能无人接收。因此不能用父会话关闭证明子任务已停止。平台 STOPPING 先关闭所有父子的新模型/工具准入，再取消子会话、远端命令与文件 I/O，核对停止证据后释放席位。已启动的 OpenSandbox 长命令必须由独立清理路径收尾。

插件完成通知可能自动触发父模型新回合，必须经过同一任务准入；父任务已停止时保留结果，不自动重启。异步结果按 run ID 去重，不能把一次插件完成通知认定为整题通过。无 UI 的 SDK 场景不能只监听 UI 专用 `subagent:jobs-changed`，应接已保存状态、结果和明确控制事件。

依据：[宿主生命周期与通知](https://github.com/nicobailon/pi-subagents/blob/b6647222bd2e849021054292927ef39fea0793ed/docs/extension-api.md#host-session-lifetime-and-completion-wakes)。

## 5. 对原设计的建议修订

| 原设计位置 | 建议修改 |
| --- | --- |
| 主方案 §4.5 插件与 subagent | 改为锁定版本的 pi-subagents + 共用 BBX 接入扩展，删除“整套自建 subagent 会话机制”的预设 |
| 主方案 §5.3 | 增加自动继承合同，Worker 不指定环境与权限；明确同 cwd、独立产物目录及实时撤权 |
| 主方案 §7 工具权限 | 补充插件侧能力上限与执行端鉴权；子身份不复制父凭据 |
| 主方案 §10 停止恢复 | 增加 detached Runner、通知触发新回合、子日志检查点和远端副作用收尾 |
| 架构图 02/03/05/08 | 展示插件与宿主边界、继承关系及后台停止路径；其他图页仅随实际接入变动 |

上述推荐已在后续设计修订中同步进主设计稿、CTF 专项稿和 12 页图集；集成验证仍未执行，统一编号为 U-S1–U-S4。

## 6. 验证结果与下一步

实际完成：读取 npm 元数据、固定提交的插件文档和关键源码、Pi v1.5.0 扩展类型，以及本仓库当前设计。GitHub 连接器请求失败后改用公开 HTTPS 只读读取。没有安装依赖、调用真实模型、启动容器、读取 `.env` 或运行应用测试。

后续只需一个最小接入样例，配合假模型集中验证四件事：

1. 子 Agent 自动取得正确模型、逻辑目录、工具清单和独立身份，材料可读；所有执行只落到绑定的 OpenSandbox，默认本机工具无法回落。
2. 去掉 BBX 接入扩展、越权指定模型/目录/工具、额度不足和满席位都在调用前拒绝。
3. 停止 Worker/Task 后，前台与异步子 Agent、远端长命令、迟到结果和自动唤醒都按同一规则收尾。
4. 子会话在工具发出前后中断、回执丢失与恢复时，原检查点和 invocation 对账合同仍成立。

仅当这四项成立，才能将“自动继承并受平台治理”作为已接通能力；不以插件单独能运行作为验收。当前没有功能提交。未来实现应按“共用接入扩展与绑定 → 插件接入 → 停止/恢复与 UI 投影”分小步提交，不提前开展大范围迁移。
