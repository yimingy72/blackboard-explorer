# CTF-team-mode · 分阶段开发计划

> 当前任务：T0设计修订与独立复核已完成，T1–T6及正式前端已实施并验收。用户于2026-10-10明确授权提交、合并main及推送；旧“不提交/不推送”仅为开发阶段历史约束。T0章节保留方案阶段要求，实际检查与边界见报告。
> 工作树 `/Users/yym/bbx-wt/ctf-team-mode`，分支 `ctf-team-mode`，基线 `affc6cd48fe31bb8b29737e75a7e62b3189b7f3b`。
> 方案：[CTF 团队模式设计提案](../proposals/CTF-team-mode-design.md)。本轮报告：[CTF-team-mode-report](CTF-team-mode-report.md)。
> 用户新增顺序要求：完整方案后由主会话安排 **GPT6 astra** 讨论评审；技术细节确认后，用户已授权进入此工作树开发。影响产品行为的歧义先明确，不自行扩大范围。

## 1. 开发约定与范围

遵守根 AGENTS.md：Python 3.12 语法、uv workspace、ruff format/lint、pyright standard、pytest/pytest-asyncio；代码标识符/注释/docstring/提交为英文，用户文档为中文。跨服务契约只在 packages/contracts 定义。现有黑板模式、default/single 提示词与其他工作树保持原行为；不修改 docs/design 三份正本或 AGENTS.md。本提案确认后的 CTF 新设计作为该模式的任务依据；确需修改既有正本时另列具体改动、取得用户同意并先独立提交。

首版在现有 runtime/API/UI 中按模式增补，不引入新执行引擎、GroupChat/Magentic、消息 broker、共享文件服务或新的基础依赖。所有真实模型、真实评测、真实靶机操作、凭据读取、部署/发布/推送均不在本轮授权内。开发检查只用假模型和假平台；检查不得读取 .env。Docker操作和Git写操作按沙箱审批执行；main合并/打标签/删除工作树仍需用户明确确认。

阶段以可演示检查点推进，不承诺人日。一次只实施已经通过评审的当前阶段；每阶段追加中文报告，列实际检查、共享文件改动和未测边界。本轮不因 AGENTS 的提交建议而提交文档，用户本轮“不提交”优先。

## 2. 阶段总览

| 阶段 | 结果 | 依赖/检查点 |
|---|---|---|
| T0 方案与 GPT6 astra 评审 | 当前方案、状态机、存储/接口和评审问题闭合 | 首轮“修订后可开发”；修订复核见报告 |
| T1 模式与存储契约 | 新任务能选择CTF；旧请求仍走黑板；CTF表/权限/快照落库 | T0技术结论及名额建议评审 |
| T2 同Session执行与定向消息 | 假模型Lead+两队友并行、generation保护、注入恢复、可靠结果通知及最小收尾 | T1；此阶段外部命令用FakeEnvd |
| T3 队友生命周期与执行停止 | 创建限额、停止/恢复/移除、boot/generation、幂等命令与真实envd drain | T2；真实本地执行前完成停止/重发边界 |
| T4 题目任务板与增援记录 | owner/CAS、题目记录、必要脚本、帮手互聊 | T3；普通题与困难题两条流程可演示 |
| T5 靶机/验证/CTF前端 | Lead管理工具白名单、验证证据、完整工作台 | T4；沿用任务工具配置，fake平台验收，不要求真实接入 |
| T6 收尾、归档、删除与恢复验收 | 全链路停机/重启/显式续跑/旧模式兼容 | T1–T5；全套假模型检查与隔离集成 |

可并行：T1契约确认后，后端事务和CTF UI类型适配可分别推进；T2的MAF注入实验与T3 envd幂等/drain可同时开发；T4完成后，T5 UI与fake平台适配、T6归档/恢复测试可并行。共享文件（contracts/schema/repository/supervisor）由单一负责人顺序合并，子任务按文件归属开发，不共改共享文件。

## 3. T0：当前交付与指定模型评审

### T0.1 当前方案交付

- 核对现有CTF工作树的HEAD/status/分支与指定基线；此前工作树已创建，本轮不重建、不修改其他工作树。
- 读取AGENTS、三份设计的相关章节、HANDOFF与近期Session/归档任务合同，以当前代码为准。
- 追踪创建→配置→runtime→MAF→Session→消息→工具→归档→UI，标出旧模式强绑定及可共享原语。
- 核验DSH固定提交 `5badb15009ae1756c3afe0ae0cef1faafc290ccc` 与MAF实际锁1.19.0/固定提交 `703fbce285ee0f026e5effcadfb9e65aab7f5d84`。
- 修订已有设计、开发计划和报告；本轮仅修改这三个未跟踪文件。

### T0.2 GPT6 astra 技术讨论

主会话向指定模型提供上述三文件、用户方向和当前基线。必须讨论并在本方案中记录结论：

1. CTF创建/View/Profile分支如何保持旧payload、旧事件、旧配置不变；公共API的分派点是否完整。
2. MAF mailbox读取→enqueue→injection→逐模型调用history/checkpoint的顺序及消息ID保存；delivery与Session同事务、响应丢失对账。
3. 每成员单turn、generation/fencing、停止/退役和恢复状态机；旧ChatWorker不能接CTF idle。
4. MAF MCP断连重发、稳定tool call_id传播、envd去重和远端drain证明；哪些未知副作用必须由Lead查询后决定。
5. CTF projector/replay、记录artifact、archive/purge、终态只读复盘、显式续跑和旧模式回归范围。
6. 模型上下文上限、保留完整Session与有界输入视图、step上限后idle通知，不靠删队友丢历史。
7. 是否能删去任何非必需抽象/接口而保留用户明确要求。

产品建议仅保留R1/R2：界面“最大队友数，不含Lead”（初始建议4）及存续/退役名额；Lead按用户整体目标显式结束，与成员声明/平台验证分开。它们作为本轮可采用的合理默认，4不是用户明确指定数值。工具/MCP优先沿用当前任务配置，缺外部能力用模拟工具验证，不把指定真实平台或凭证当开发前置；任务流程不限TSec Benchmark。确有改变用户可见行为的关键歧义再交主会话，不把技术评审当用户已做产品决定。

通过标准：无未解决的P0以及阻碍正确实现的P1；R1/R2默认建议和技术讨论结论有记录；方案明确未实现项与真实平台未核验边界。通过之后才开始T1，不提前写功能。本轮已有首轮独立评审八项意见，修订后再只读复核并记录结论；不以模型自述验证内部模型标识。本轮交回主会话，不自动进入T1。

## 4. T1：模式、契约和数据库

### T1.1 新增跨服务契约

新增 `packages/contracts/src/bbx_contracts/ctf.py`：CtfTaskCreate/Spec/View、CtfAgentProfile/Options、Member/Turn/Message/Challenge/Record、CTF事件/API body。原TaskSpec/AgentRun/黑板Profile保留；旧请求不传mode仍通过现有校验，CTF需显式mode=ctf。CTF金额/分钟沿用纯类型；max_teammates独立含义。schema导出需将新ctf模块纳入现有 `schemas.py` 枚举，OpenAPI+schema.d.ts同步生成。

### T1.2 追加迁移与投影

在最新迁移后追加mode/ctf_options、CTF control/conclusion及五张CTF表/索引，保持旧行默认blackboard。Repository.apply显式支持ctf.*，task.created旧事件缺mode默认blackboard；replay仅重建事件覆盖的展示投影；禁止将mailbox、Session、运行控制字段/表追加到truncate名单，按设计10.1逐表执行。CTF共享Task元数据但不写黑板知识变更计数，不触发旧裁定。所有CTF业务写走任务锁、身份与revision/generation校验、追加事件、提交后通知。

### T1.3 配置和公共入口分派

按设计11.3完整正文新增profiles/ctf的Lead/teammate模板与快照；沿用模型目录/MCP版本/exec资源存储底层，配置校验按模式。create/detail/list/start/stop/resume/state及task supervisor profile加载点明确分支。旧Worker设置保留，新CTF设置独立命名空间；首版固定CTF提示词快照，后续聊天不热改系统指令。Runtime此阶段不执行真实模型/命令，可用假的coordinator证明分派。

允许目录：packages/contracts、services/blackboard/src及migrations/tests、profiles/ctf、runtime supervisor/client和针对分派的tests、生成的OpenAPI及web API类型。共享文件只追加必要内容，根依赖/compose预计不改。

验收：旧创建请求和旧task.created回放逐字段保持旧行为；CTF请求无旧Acceptance强制；不能切换已创建mode；跨模式写入409；CTF和黑板Profile相互拒绝；不同模式start/recover都进入正确控制器；CTF事件不改变黑板 last_change_version。迁移与并发数据库测试用testcontainers，普通验证用纯规则/fake。

## 5. T2：MAF原生Session与定向消息

### T2.1 薄运行器

新增CTF coordinator/runner/middleware/tools。复用make_client、原生MAF Agent/AgentSession/ResponseStream、CheckpointHistoryProvider、SessionCheckpoint、ObjectStore和纯费用/诊断函数。每成员一个持久Session、独立Agent；每次唤醒一个turn，每成员单活跃turn，不用旧BoardSync/Receipt/finish_agent。

CTF参与者查询扩展Session服务校验，无需新增Session表或伪造Explore。stage_ctf_delivery→BlackboardClient→SessionBody→Conversations.put_session贯通CTF delivery和generation，不混入旧agent_messages。工具日志/trace/heartbeat按mode+generation分派；initial_context只记首份，后续turn记录触发消息和配置/历史视图引用。费用按完整模型响应、turn累计、任务总量原子增量；重复结算request_id不能双计费。CTF coordinator自己做跨turn累计金额/活动时长watchdog，全员idle也校验；复用纯计算而不调用旧黑板decision。从第一阶段运行即执行硬限制，不能等T6才补。

### T2.2 mailbox与唤醒

消息先落库，真实sender不可伪造；所有队员可Lead↔teammate、teammate↔teammate定向发送。idle启动同Session新run；busy下一模型边界注入，不开并发Session、不interrupt当前工具。启用逐模型调用history，checkpoint同事务确认已进入历史的messageId；coordinator tick对当前turn的消息claim续租，长模型响应跨租约期不能再次注入。dispatcher通知丢失由pending扫描补偿。run结束与入队在tasks锁下复核，保证消息不滞留。

独立CTF mailbox不接旧review pending；最早恢复逻辑支持runtime重启从Session和队列恢复，撤销旧generation。终态消息先明确只读/需resume，不触发执行；具体只读工具装配在T6完整验收。上下文有界视图按设计补充，完整历史继续保存。

允许目录：runtime ctf、session/models/billing/trace/clients（必要适配）及tests；blackboard ctf/conversations/service/api及tests；contracts必要补充。不改黑板decision/prompts。

验收剧本（全部假模型/FakeEnvd）：

- Lead创建两名具名成员；两人并行模型调用，成员A连续两次接活沿用相同Session ID，第一轮历史仍在。
- A忙时给A两条消息、A向B发消息、用户向Lead插话，真实sender和messageId进入正确Session；其他成员看不到不属于自己的私聊。
- 模型调用一次工具后再调用模型，注入正文两次可见且持久化；仅原生enqueue、没有dispatcher的idle不会被误称唤醒。
- queued/delivered与工作完成分开，未回复不显示“已处理”。
- 重复消息、通知丢失、checkpoint已提交但响应丢失、最终响应时消息到达、两个唤醒争抢、旧generation写Session均通过断言。
- 同一fake用量重复结算不双计费；金额或时长触限停止新模型请求，模型预算控制不依赖Lead自觉。

### T2.3 必须进入首个闭环的可靠性与收尾

- 实现设计6.2.1的DB mailbox+confirmed checkpoint恢复协议。稳定messageId对history/pending去重；同turn失败重新对账即使claim仍续租也要重投缺失输入；新generation撤销旧claim后再重认领。所有业务写、Session、用量/trace与工具准入验证可信generation。
- 每个带派工关联队友turn结算唯一assignment_result，含原因/最终回答或引用；显式回复关联/合并，纯通知不产生自动回执链，Lead不通知自己。
- finish工具原子存CTF conclusion/control并立即202；coordinator封闭派工、最后checkpoint和drain后终结；closing重启续收尾，用户插话持久deferred且不执行。预算/停止/故障不依赖模型总结。
- 验收：注入已drain后模型流失败；pending保存后重启；最终checkpoint失败/响应丢失；续租跨时限仍重投缺失输入；旧代次迟到写拒绝。恢复后消息历史一次且未丢失，不把pending当delivered。
- 验收：正常/限步/失败/停止/中断均有唯一派工结果；显式回复不双发；通知触发turn不自动回执；结算前后崩溃重试不重复通知。
- 验收：finish不等待自身turn、closing不新派工、插话deferred、重启phase不回退；门控分别检查lead_claim/required验证。角色模板严格渲染、实际工具清单与服务端授权、首任务和插话来源测试一并完成。

T2仅使用FakeEnvd验证协议，不能据此声称远端停止已实现；T3接入真实envd证明。不得把generation、可靠注入或最小收尾推迟到T6。

## 6. T3：成员限额、停止和远端执行确认

### T3.1 名册与逻辑生命周期

落实评审后的max_teammates口径；task锁内占名额与创建幂等；具名身份/display_name与agent-N分离；provisioning失败保留历史。stop保留名额/owner/inbox，resume启动同Session；remove先停再逻辑退役、释放名额，未完成题目blocked/unowned、记录原owner，取消未交付消息，禁止复用身份/昵称。Lead不可被队友管理，不能自移除留下孤儿团队。

### T3.2 envd最小增补

在现有execute_command内部增加可信generation/command_id、进程组registry、同键重试去重和内部stop/status/drain；generation在CTF命令执行与所有平台写边界检查。正常黑板无这些可选参数继续现有行为。已静态核验MAF FunctionInvocationContext.metadata的function_call_occurrence_id/provider call_id及可信kwargs['_meta']→FastMCP ctx.request_context.meta路径：调用前持久化occurrence ID或host UUID及参数摘要、checkpoint成功再送；不用provider ID或旧ToolLog事后随机ID充当唯一键。容器固定mode=ctf时缺_meta/未登记/旧boot/旧generation均拒绝，不以registry有无判断模式；可信service完成旧进程处置后才重登记。T0/T3必须用离线+MCP集成证明该固定版本契约，不因源码可行便称已实现。

stop/remove意图和request_id持久化，重启先续停止操作。stopping拒绝新模型/副作用，但允许可信service最后checkpoint/已得用量；保存+drain确认后再终结turn和撤销资格。确认失败维持stopping且占名额；重复操作恢复同request_id进度。每turn Agent token从auth.py签发CTF generation/turn claims，后端不信body自填新代次；旧token形状和黑板路径保持。registry跨runtime存活；envd每次启动新boot_id，重启丢registry保留DB未知命令/停止意图为unknown/stopping，空registry不能返回drained；旧组无法证明结束时登记必要产物并销毁旧容器确认后才准许新boot登记；后台脱离进程组及外部目标由任务资源清理/Lead管理，不能作完整进程隔离承诺。

允许目录：services/envd core/app/tests；runtime ctf/clients/envd、相关middleware/tests；contracts/blackboard ctf必要状态与权限。共享Makefile若为隔离镜像名称增加覆盖变量，单列报告；不改全局Docker配置。

验收：并发创建只成功N位队友（Lead另计）；N=0仅Lead；重复创建不多占名额；普通stop不释放；removed历史/Session/记录可查且新名额可用。阻塞命令停止后进程组确实不存在、后续旧代次命令/DB写被拒绝；MCP丢响应重连重发不执行第二次；停止确认超时不释放；remove时消息入队竞态无幽灵run。真实envd取消链与网络断连使用隔离Docker测试，不能只在单测直接cancel核心协程后宣称远端停止可用。

## 7. T4：任务板、共享记录与增援

### T4.1 CAS任务板

任何存续成员可创建；owner自领或Lead分配，协作者独立。每次更新expected_revision；两个成员认领同题只成功一个。分配/认领不自动唤醒，Lead显式发送指令；owner本人或Lead更新工作状态，帮手不抢owner。completed保留历史owner，reopen/release规则明确。Agent运行结束不自动完成题目。

### T4.2 题目记录与必要产物

只追加记录，作者/时间/version保存；普通记录自由body，增援五项明确。沿用agent目录和shared；必要脚本、输出和依赖说明通过envd安全读取→MinIO→record.artifact_refs登记，不建立新共享文件系统。request_help原子写记录+blocked+Lead消息，上传/登记失败有有界回收，不制造空增援消息。

Lead优先调配已有成员，给协作者发送题目/记录引用；帮手读记录后和owner定向沟通，复制脚本修改、补记录，owner决定提交。代码只提供权限/状态，不模拟策略判断或自动把记录转Fact/Intent。

允许目录：contracts ctf、blackboard ctf/objects相关校验、runtime ctf tools/evidence安全上传适配、execenv archive声明入口、各层tests；UI记录组件可并行。

验收：普通题一人独立完成；困难题记录包含路线/依据/脚本路径+持久URI/失败条件/难点，Lead调已有队友，帮手首步读记录并向owner发消息；owner保持同一人，协作者补充不覆盖。CAS冲突明确，跨任务artifact拒绝，删除队友留历史且未完成题转blocked。必要脚本在archive-builder声明列表中；仅路径的非声明文件明确不保证恢复。

## 8. T5：靶机、验证与完整CTF界面

### T5.1 平台适配

先用fake target MCP实现启动/连接/关闭/flag提交/查询状态。复用平台工具注册表、固定版本和白名单；只有Lead持管理工具；队友只拿题目连接/要求与选定提交权限。target摘要和verification历史进记录/题目摘要；可信适配保存调用/结果引用并生成source=platform，人工入口从用户身份生成source=user。Agent只写candidate/unknown，无适配结果待核实；拒绝Agent伪造source或accepted，不引入复杂冲突治理。timeout/断连写unknown并查询；选定无provider幂等键的管理/提交工具用标准MAF FunctionTool薄适配调用官方ClientSession一次，绕开原MCPTool自动重发层，不能只靠Lead事后查询阻止SDK已重发。只读/幂等工具沿用原装配，不写模型/工具loop，不承诺exactly-once。

优先从任务当前工具配置生成CTF快照和Lead/队友允许清单，不强制接新平台或询问凭证；权限用途不明的工具保守只给Lead或不开放，以fake继续。以后选定真实平台时才按实际MCP结果做最小适配，接口/凭证未核验列为边界，不阻塞开发与fake验收，不假称现成。用户自定流程不需要靶机或平台提交时也能交付分析/复现/报告。用户原运行目标默认不可由本任务随意关闭，外部资源失败留可见外部ID/待处理提醒。

### T5.2 UI

创建表单mode与“最大队友数N，不含Lead”说明；任务列表标模式，同一路由按TaskView.mode挂独立CTF工作台。成员名册/生命周期/运行状态、任务板owner/collaborators/work状态、平台verification状态、题目共享记录/附件、Lead和队友会话分别展示。安全Markdown、控件、费用/时长、SSE、报告/归档共用；CTF reducer独立，不改黑板图谱规则。消息queued/delivered显示真实来源，不宣称已处理；removed历史可查，stopped/终态不会聊天自动执行。

首版CTF设置能编辑两角色模板/工具列表（新任务快照），不增加逐队友模型或运行时模板热更新。运行中只展示登记附件，任意共享实时文件树暂缓。窄屏、长正文、键盘焦点和非颜色状态按现有规范验收。

允许目录：web/src/ctf、公共创建/路由/列表/类型和必要会话适配、web测试/e2e；backend CTF MCP工具配置/验证/target摘要、contracts、runtime CTF tools。不新增UI依赖或靶场专用基础服务。

验收：mocked API/ScriptedChatClient覆盖平台验证流程“创建→派题→增援→拒绝→补记录→accepted”，以及无需平台提交的用户自定分析/复现/报告流程。Lead显式结束，成员声明/accepted不自动结束；required=false不强制flag，required=true的未接受阻止“目标达成”声明。同名任务两模式路由正确，旧黑板页面/创建/会话不变。队友绕过工具列表请求管理动作仍被服务/适配拒绝；candidate、completed、accepted、整体结束不同。真实平台/网络未接通不阻塞fake路径，不把fake结果当真实验证。

## 9. T6：停止、归档、删除与续跑

### T6.1 全链路恢复

在T2最小收尾和T3真实停止已通过基础上，补齐runtime重启、控制链断连、模型流不完整、Session CAS冲突、停机时消息/操作竞态。重启撤销旧generation/claim，保留单Session；未知工具不重放；先告诉Lead再继续明确待办。任务硬时限包含running期间idle等待，暂停间隔按现有活动时长规则不计；未拿到完整用量不编造费用。stopped/removed不自动唤醒；终态只读复盘不接执行工具，显式resume才重开。

### T6.2 必要产物归档与删除

archive-data和archive-builder加入CTF状态/Session/mailbox/记录及声明文件，原路径/链接/大小/initial inputs规则复用；确认保存/登记后才destroy Ubuntu。容量或上传失败保留任务容器供重试。生成CTF总结，不运行旧Close；外部目标关闭结果与本机cleanup分别展示。

resume保留mode/成员/历史/文件，追加预算幂等；先Lead核查连接再分配，removed/stopped不被恢复为active。旧轮次delivered不重复注入，queued待Lead确认，cancelled不复活。purge所有CTF数据和对象但不影响其他任务；移除队友不调用任务purge。

验收：fake端到端正向和失败剧本，archive仅登记文件恢复；CTF消息与Session导出完整；移除成员历史跨续跑保留；删除整个任务后全部前缀/表无残留。旧黑板完成复核、并发derive、停止、运行中用户消息、终态复盘、传输恢复、初始附件、归档/resume/replay全套回归通过。集成容器/网络带任务前缀、随机端口并结束清理；真实模型/评测仍不执行。

### T6.3 访问与竞态验收矩阵

按设计11.1.1对state、历史events/replay、SSE初订阅/补发、Session/transcript、tool log/trace、附件原文/预览/导出/归档逐入口测试：授权用户可查removed历史；Agent只见共享业务和本人私有数据，Lead不得旁读队友私聊；跨任务、旧token、review写入和URI绕过拒绝。

closing插话返回deferred并持久，不能新run；running任务stopped成员新消息queued且不自动执行；removed新执行消息拒绝而历史保留；两个resume以及review与resume争抢仅一个获写资格，预算追加request_id不重复。replay不清控制数据，权威Session/mailbox恢复使用真实存储；归档同路径恢复最新登记版本，历史引用不失效。T3另需重启旧boot请求/缺meta/重登记前调用/空registry未知进程真实MCP集成剧本。

## 10. 检查命令与判定

以下为功能开发阶段的可执行检查，不代表本轮已经运行。先在CTF工作树检查状态，开发环境不依赖主工作树.env：

```sh
cd /Users/yym/bbx-wt/ctf-team-mode
git status --short --branch
env UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  http_proxy=http://127.0.0.1:7897 https_proxy=http://127.0.0.1:7897 \
  all_proxy=socks5://127.0.0.1:7897 \
  no_proxy=localhost,127.0.0.1,::1,host.docker.internal \
  uv sync --frozen
make check
make web-check
make web-build
```

`make check`实际只包含lint/typecheck/非integration且非live测试，当前Makefile与较早设计文字不一致时以Makefile和AGENTS为准。任何提交前必须全绿；CTF前端变化同时检查web。新契约后运行 `make schemas`、`make openapi`、`make web-types` 并将必要生成文件列入当期改动，不在本輪执行生成。

Docker阶段必须先取得沙箱外执行批准，确认已有基础镜像可拉取，不改全局配置。当前 `make test-integration` 会构建仓库默认镜像；在并行开发下需先把构建/test镜像引用的必要覆盖点核实并配置为 `bbx-ctf-*`，或给Makefile追加最少可覆盖变量，避免覆盖其他工作树正在用的全局tag。之后执行同等测试入口，使用testcontainers随机主机端口与自动清理，不执行make up。集成断连/stop验收必须覆盖真实MCP链，不仅是FakeEnvd。

前端浏览器验收用mock API/项目Playwright夹具；可在已有依赖/隔离浏览器安装就绪后执行 `make web-e2e`。下载安装和镜像构建未获批准时报告具体未执行项及用户可用命令，不绕过沙箱。不得执行 `make test-live`、`make e2e`、`make eval-run` 或其他真实模型/跑分命令。

T0曾只进行文档核验；T1–T4实际功能检查、隔离集成及未执行项目见最新报告，不能将未来阶段检查标绿。

## 11. 建议提交划分与合并边界

本轮不提交。后续T0评审结论和各阶段检查通过、获得Git写操作批准后建议按以下顺序提交，勿改写已有提交、勿推送：

| 提交 | 包含文件范围 | 英文提交信息 |
|---|---|---|
| D0 | 本提案、任务计划、报告和已确认评审结论 | `Document CTF team mode design and staged delivery plan` |
| D1 | contracts、追加迁移/CTF后端状态、mode/config分派、生成类型与对应测试 | `Add CTF mode contracts and persistent team state` |
| D2 | CTF runner/coordinator/mailbox、Session/日志/计费适配及假模型场景 | `Run persistent CTF teammates with directed messaging` |
| D3 | envd命令ID/generation/stop/drain、队友stop/remove限额及集成测试 | `Confirm CTF teammate shutdown and deduplicate commands` |
| D4 | 任务板、共享记录、增援和必要artifact与测试 | `Add CTF challenge ownership and assistance records` |
| D5 | CTF工具白名单/验证、完整UI/设置和前端测试 | `Add CTF target coordination and team workbench` |
| D6 | 归档/purge/resume/复盘/故障恢复、兼容场景及阶段报告 | `Complete CTF recovery and preserve blackboard compatibility` |

每个提交正文末尾按仓库规则附 `Implemented by Codex (gpt-6-sol) for task CTF-team-mode.`，不因本轮代理实际模型不同而漏掉用户要求的固定尾注。实际diff较大可拆分，但每个提交必须可检查、只包含任务文件，.env绝不暂存。main合并/标签/删分支或工作树需要用户明确确认，不能把“授权开发”当合并授权。

## 12. 必需项、暂缓项与阶段退出条件

必需项不能因最小方案省掉：跨模式隔离、真实身份和Lead权限、名额与历史保留、每成员单Session写者、消息先持久化/闲时唤醒/忙时注入/恢复、题目CAS/记录增援、必要脚本恢复、工作/运行/验证状态区分、硬限制、远端停止确认、命令重发处理、终态与resume分离、旧模式兼容。

暂缓项：DAG任务依赖、多级团队、全面文件冲突调度、靶场锁、自动route评分、全部transcript广播、运行中任意目录树、额外模型摘要服务、逐队友模型设置、热改CTF提示词、分布式运行、新部署服务。只有实际验收暴露首版无法完成用户任务才重新讨论这些项，不为未来扩展预留接口框架。

T0评审解决技术细节是开发开始条件；真实平台未指定不阻塞fake工具全部功能与测试，只保留真实接入验证边界。每阶段完成后报告完成编号、用户复现命令、实际结果、偏差待决和下一步；最终交付需三维状态可见、支持不同用户流程的完整假模型闭环、恢复/归档/删除可复现，且现有黑板回归全绿。
