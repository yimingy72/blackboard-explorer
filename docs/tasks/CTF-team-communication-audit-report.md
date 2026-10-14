# CTF Lead 团队交流与性能审查

日期：2026-10-14（北京时间）。工作树 `/Users/yym/bbx-wt/ctf-cooperation`，分支 `feature/ctf-cooperation`，HEAD `c1bd222`，**包含当前尚未提交的 transport/control 修复**。本次是审查与优化建议，不修改生产代码、提示词或配置，不部署、提交或重跑任务。

> 后续实施已获用户于 2026-10-14 认可：先处理观测去重、旧恢复输入和 checkpoint 开销，再完善派工及反馈；最新事故合并见第 10 节。前九节保留审查时的证据与建议，实施结果以后续补充为准。

> **最新状态：13 项均已完成对应优化并本地部署。** 首批结果见第 11–12 节，剩余五项及最终检查见第 13 节；最新普通检查 1,741 项、完整隔离集成 307 项全部通过。历史上游错误码不可恢复，未将组件微测外推成真实解题提速承诺。

## 1. 项目作用、负载与结论

平台通过独立的 CTF 团队模式组织 Lead 与具名队友合作：Lead 决策和分配，队友并行执行、直接交流，任务板保留进度和证据。MAF 负责 Agent、Session、模型与工具循环；平台负责持久邮箱、唤醒、授权、预算、代次和执行环境。该模式不使用普通黑板模式的 Fact/Intent/Derive/Close 调度。

假定当前本机单 runtime、同一时间一个执行任务，按最新样本的 **31 题、1 Lead + 8 队友、上千次模型调用**审查。没有推断多主机多写者部署正确。

**优先优化观测去重、恢复输入生命周期和完整 Session 读写，然后合并派工、补齐调度通知与按需读取。** 继续增加 Agent 数量会增加控制面竞争；当前数据没有证明它是最有效的第一步。Lead 的分工与解题策略继续由模型判断，优化平台执行其决定的路径。

## 2. 当前实际交流流程

1. 创建任务后，把整体目标及初始材料作为持久消息送给 Lead。
2. Lead 建题目台账、创建/复用具名队友；修改 owner/collaborators 后，另用 `send_message(instruction=true)` 发出派工。仅修改任务板不唤醒队友。
3. Coordinator 默认每次完成 tick 后等待 0.2 秒，读取轻量 runtime-state；对有待投递输入且无本地保存/结算屏障的成员领取 turn，启动独立 MAF Agent/Session。多个成员可以同时执行。
4. 每次模型调用前检查预算和代次，领取该成员邮箱，通过 MAF 原生 MessageInjectionMiddleware 注入。输入进入确认保存的 Session 时，邮箱才标 delivered；delivered 不代表已经执行完成。
5. 队友可以直接向 Lead 或其他队友发送消息，不要求经过 Lead 转发。共享记录由 Lead、owner 或已登记协作者追加；帮手不能替 owner 宣布完成。owner 的 request_help 会在同事务登记阻塞并通知 Lead。
6. 忙碌 Agent 在下一次模型调用边界接收消息；当前长模型响应或长工具批次不会被插话直接抢占。空闲 Agent 由邮箱唤醒；各成员仍只有一个 Session 写者。
7. 派工 turn 结束时产生唯一 assignment_result 通知；已显式回复时避免再复制最终正文。Lead 根据题目记录和完成要求调用 finish_task，平台再停止、结算和归档。候选、工作完成和可信验收是独立状态。

主要入口：[coordinator.py:329](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/coordinator.py:329)、[middleware.py:128](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/middleware.py:128)、[ctf.py:1553](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf.py:1553)、[ctf.py:1905](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf.py:1905)。

## 3. 最新样本的只读证据

样本为当前数据库创建时间最新的 CTF 任务 `a5d138ce-65e7-4e6b-b911-efbc1783f957`，2026-10-14 07:27:15 开始、09:11:38 结束，状态 failed。本次不重新归因该任务的失败，也不把“回合结束次数”当作“解出题数”。

| 项目 | 实测 |
| --- | --- |
| 规模 | 31 题、9 成员、34 个 execution turn、135 封消息 |
| 完整模型输出观测 | 1,074 次；Lead 185 次，队友 889 次；另有 1 条队友 model_error 观测 |
| 已记账输入缓存命中 | Lead 95.30%，队友 95.81%，全队按输入 token 加权 **95.71%** |
| 首名队友开始执行 | 整体开始后 **159.26 秒**；8 名队友首轮在约 1.07 秒内陆续启动，支持真实并发 |
| Lead 工具结果计数 | execute_command 150、send_message 61、update_challenge 45、create_challenge 31、list_challenges 21、list_records 20 |
| Lead Session | JSON 文本 6,464,161 字节、419 条历史消息、revision 1,052；恢复视图 epoch 7、protected_inputs 29 条 |
| 全队 Session | 当前 JSON 文本合计 39,022,852 字节，累计 revision 合计 5,682 |
| 观测规模 | 4,339 条 agent.trace.recorded；整个 events 表 34,181 行 |
| 回合通知 | 28 条 turn_finished，13 条标记 truncated，9 条因显式回复省略正文，6 条仍 queued；这些计数可能重叠 |
| 送达证据 | 128 条 delivered 消息全部有正 session_revision |

缓存比率为账本内 `sum(cache_hit_tokens) / sum(cache_hit_tokens + cache_miss_tokens)`，不是逐请求命中率的算术平均，也不包含没有可靠用量的未知请求。**该样本不支持“当前主要因为只有 20% 缓存而慢”的判断。** 缓存命中高仍会有模型输出、工具、持久化和协调等待。

Lead 的 150 次命令只能证明调用数量，未读取命令内容，不能据此断言都不应由 Lead 执行。初次派工延迟也不能全部归因于单一组件。

运行容器内 middleware.py、coordinator.py、ctf.py、ctf_api.py 的 SHA256 与当前工作树分别一致。上述核心发现适用于当前运行代码；没有据四个文件的核对声称整个镜像和全部配置完全一致。

## 4. 必须优先处理

### 1）观测去重在任务全局锁内扫描事件历史

- **作用**：每次工具调用/结果和模型输出都登记可追溯观测；上传对象前后检查重复及旧代次。
- **问题与证据**：[ctf.py:1456](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf.py:1456) 按 `payload.observation_id` 查询。上传前后各查一次，均先取得任务锁；[schema.py:131](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/store/schema.py:131) 没有对应索引。对新观测 ID 的等价只读 EXPLAIN 三次均选 Seq Scan，过滤 **34,181 行**，耗时 **111.220 / 38.339 / 38.745 ms**，每次命中 2,989 个缓冲块。不是只扫本题或本成员。
- **最小方案**：普通观测改用已经写入的 `events.object_id=observation_id`，增加按 task_id/object_id、限定 agent.trace.recorded 的部分索引。initial_context 的一次性特殊查询单独保留处理。保持上传前后检查、原事务和代次校验。
- **不处理的后果**：一对工具 call/result 有四次去重查询；长任务重复扫描增长，并在任务锁下阻塞其他成员的派工、预算和消息确认。不能用本次三次查询直接推算全程提速百分比。

### 2）恢复前缀中的旧输入跨回合没有退役路径

- **作用**：上下文裁剪时保留当前重要输入，防止刚收到的用户指令或派工被静默截掉。
- **问题与证据**：[middleware.py:479](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/middleware.py:479) 原样返回旧 protected_inputs；[middleware.py:595](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/middleware.py:595) 新回合继续载入，后续裁剪继续累计。即使当前保护 ID 只有新派工，也保留已结束旧回合的派工。纯内存复现：输入门限 800，旧前缀 641 token，新输入与 seed 单独仅 341，合并为 959，发生 context_blocked。完整复现见附录。
- **最小方案**：新回合加载时，凭旧回合结算与输入交付证明，退役不再需要保护的旧输入；保留原始目标、当前派工、未确认输入以及仍有效约束。通过正常 checkpoint 持久化这次变化，完整历史保留。
- **不处理的后果**：长期 Session 不可裁前缀越来越大，压缩模型可用工作空间，触发本可避免的封锁和恢复。样本的 29 条 pinned 说明有实际保留规模，**不证明该任务已因此失败或 context_blocked**。不能以清空全部旧输入代替生命周期修复。

## 5. 应当处理的协作与性能问题

### 3）分工登记与真正派工拆成两次工具操作

- **作用**：Lead 先更新 owner/collaborators，再发 instruction 唤醒成员。
- **问题与证据**：[board_tools.py:68](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/board_tools.py:68) 明确不唤醒；[ctf_board.py:255](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf_board.py:255) 只有任务板更新。跨模型调用执行会增加一次依赖往返并留下半完成窗口；放在同一响应里又不能假定按列出的顺序提交，因为 MAF 默认并发工具，帮手可能先收到任务、后获得记录权限。
- **最小方案**：给现有 update_challenge 增加可选派工正文/收件人，在同一 CAS、幂等事务里完成分工和邮箱写入。owner、协作者、正文都由 Lead 明确提供；未传正文保持原行为。
- **不处理的后果**：成员已被分配但仍 idle，或因权限尚未提交而失败，需要额外轮次修正。并非“每工具一定消耗一次模型调用”，也不要求平台自动决定分配。

### 4）每次 checkpoint 读写并回传完整 Session，正常应答只需要小回执

- **作用**：保留模型与工具历史、未知命令状态、用量和可靠消息确认。
- **问题与证据**：[session.py:148](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/session.py:148) 完整序列化；[conversations.py:207](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/conversations.py:207) 为读 revision 先取整条旧 Session；[conversations.py:236](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/conversations.py:236) 返回完整新 Session，CTF checkpoint 原样回传。正常调用者最后只使用 revision；命令执行前后及工具结果仍各有必要保存点。样本单份 Session 已为 3.13–6.46 MB。
- **最小方案**：首先把 CTF checkpoint 成功应答缩成 revision、checkpoint 标识及必要确认字段；正常 CAS 查询只投影 revision。应答丢失时继续走现有完整 GET 与内容核对。下一阶段再依据计时考虑已确认消息的增量存储，不先重构整个会话系统。
- **不处理的后果**：每个短工具动作都伴随大历史序列化、数据库读取、HTTP 回传和解析，放大并发开销。不能删掉副作用前后保存点，也不能把最终 JSON 大小乘以全部 revisions 冒充实测总流量。

### 5）工具路由读取无关调度数据，读板还先执行一次必然失败的授权

- **作用**：校验角色、turn 和任务固定配置，再执行工具。
- **问题与证据**：[ctf_api.py:187](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf_api.py:187) 执行态读板先尝试终态 review 授权，失败后再 execution 授权；[ctf_api.py:212](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf_api.py:212) 每个工具又读 runtime_state 的任务、成员、running turns 和 ready mailbox，通常只需要 profile 标识。
- **最小方案**：一次权威授权识别 purpose，返回窄 task/profile 元数据。只有 list_team 读取名册；其它工具直接执行需要的查询。
- **不处理的后果**：每次执行态读板多一次失败授权事务、两个 SQL；多数工具还多做三至四个调度查询。它们在同一工具 HTTP handler 内，不应误算成额外 HTTP 请求。

### 6）任务重新变为可分配时，Lead 可能没有被唤醒

- **作用**：释放题目让后续成员重新处理；idle_watchdog 防止全队静止。
- **问题与证据**：[ctf_board.py:284](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf_board.py:284) release 后只写任务板事件。假设成员释放 A 后继续长任务 B，而 Lead 和其他队友 idle；[coordinator.py:474](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/coordinator.py:474) 只认 mailbox ready，[coordinator.py:494](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/coordinator.py:494) 有任何 active future 时不调用全队 idle_watchdog。A 可能一直闲置到下一条独立消息到达。
- **最小方案**：在“有负责人变成可重新调度”等明确边界，合并、去重发送短 advisory 给 Lead，带 challenge ID/revision/原因；Lead 自主决定是否重派。request_help 已有通知，不再重复通知它。
- **不处理的后果**：一部分成员长期空闲、一部分继续长工作，整体表现像接力。不能据此自动抢题、周期让模型轮询全表或把每条记录都广播全队。

### 7）回合结果通知没有完整说明自动恢复的决定

- **作用**：向 Lead 交付回合结果，必要时自动有界续接。
- **问题与证据**：[ctf.py:1987](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf.py:1987) 先生成结果通知，[ctf.py:2060](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf.py:2060) 才决定恢复。recovery_requested/disabled 和 ready_at 主要在控制状态/事件；exhausted 的 provider/context 分支另有通知，但不统一。Lead 的提示词要求系统已安排恢复时不要重复派发，却不能从当前结果通知判断所有分支。
- **最小方案**：原恢复函数返回结构化决定，在同事务形成最终唯一 assignment_result 时附 recovery status、原因、attempt/limit/ready_at。保留 Lead 接管权与必要故障信息。
- **不处理的后果**：额外查询、重复派工或过早创建替代成员的风险增加；本次没有测出这些行为实际发生的次数。

### 8）发出结果引用，但 Agent 按需读取的链路不完整

- **作用**：对话保持简洁，大正文仍保存在原 Session 或结果对象。
- **问题与证据**：回合通知 [ctf.py:1992](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf.py:1992) 提供 result_ref、正文最多 2,000 字符；现成 [turn_result:2189](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf.py:2189) 只向 API 暴露，模型工具没有包装。样本 28 条通知中 13 条标记截断。另有 [context.py:180](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/context.py:180) 的长工具预览给 message/call ID，但执行工具没有按这些 ID 重读中段的入口。命令已有 full_output_path、记录可分页，不能把它们都算成不可读取。
- **最小方案**：包装已有 turn_result，按原权限分页供 Lead 读取已结算派工回执；对无其它原文路径的工具结果，增加只读、定长分页的本成员 saved-tool-result 入口。复用既有 Session/对象，不开放队友私聊或私有 Session。
- **不处理的后果**：Lead 需要再问队友，或成员重发原 MCP 查询来找中段信息；不能假定所有重复查询都由此造成。

### 9）模型历史裁剪仍有平方开销，且重复处理已排除的完整历史

- **作用**：选择不拆工具/推理配对的安全边界，并保持稳定缓存前缀。
- **问题与证据**：[middleware.py:418](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/middleware.py:418) 对每个候选边界重分组、扫前缀并求后缀和；[middleware.py:661](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/middleware.py:661) 加载裁剪也有同类循环。长度 N、候选边界 K 时 O(KN)，K 接近 N 时为 O(N²)，同步占用共享事件循环。旧协作报告的线性长度优化没有覆盖这两条新路径。
- **最小方案**：每次裁剪一次计算原生安全边界、长度前缀和及 protected ID 集合，增量迁移保护输入；对已确认不可变消息缓存签名/长度，复用 MAF 增量分组注解。保持选界、低水位、未确认输入和原 provider 计数基准。
- **不处理的后果**：长 Session 越来越耗 CPU，并拖慢其它成员唤醒/控制请求。源码可确认复杂度，但本次没有端到端 CPU profile，不声称这是主要耗时来源。

### 10）续跑/恢复给出一页 deferred ID，却没有按需翻页入口

- **作用**：Lead 显式确认旧输入是否仍适用，防止旧工作自动重放。
- **问题与证据**：[ctf_lifecycle.py:288](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf_lifecycle.py:288) 最多给 100 条旧消息元数据；其它恢复诊断最多 50 条。工具只有 confirm_messages，返回仅已确认 ID，没有 deferred 清单查询/分页工具。超过上限后，Lead 只能依赖后续恢复/全队静止时重新通知；还有长任务运行会延迟通知。
- **最小方案**：提供 Lead 专用的分页只读 deferred 元数据，含收件人、来源、kind、时间及已有题目/记录引用；继续显式确认具体 ID，不泄露队友私聊，不绕过 context block。
- **不处理的后果**：历史输入可能长时间得不到处理。属于自主查询链不完整，不是数据被删除或永远不可恢复；当前最新样本未证明达到 100 条上限。

## 6. 次级优化：先量化，再决定范围

11. **减少无变化时的控制扫描**：[coordinator.py:330](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/coordinator.py:330) 每 tick 读 runtime-state、再读 envd 状态；间隔 0.2 秒，在极低延迟下接近每秒 5 tick，实际受请求耗时限制。可复用普通 [SchedulerLoop:91](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/scheduler/loop.py:91) 已有 SSE 唤醒方式，保留丢事件补偿扫描；环境健康检查单独节流，执行和代次安全检查不缓存放行。只订阅相关控制事件并合并唤醒，避免每条 trace 仍触发整次扫描。先测 HTTP/SQL 请求数和停止响应时间，不能只调大轮询周期。

12. **复用固定 MCP catalog/连接建立成本**：[platform.py:56](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/platform.py:56) 每回合重新取服务器与 list_tools，[platform.py:22](/Users/yym/bbx-wt/ctf-cooperation/services/agent-runtime/src/bbx_runtime/ctf/platform.py:22) 每工具重建 HTTP/MCP session。先按固定 server/version 缓存 catalog，继续检查启用、权限和版本；连接复用须保留故障确认语义。Lead 长管理工具会阻塞下一次收消息；若外部接口已有快速返回 operation ID/status，优先暴露该现成功能，不另造执行引擎。实际收益取决于握手/管理耗时，当前未测。

13. **补齐邮箱与记录查询索引**：[ctf.py:1083](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf.py:1083) 用 MAX(sequence) 分配邮箱序号，缺少 task/recipient/sequence 索引；样本 Lead 扫 56 行、0.130 ms，当前优先级低于观测全表扫描。[ctf_board.py:313](/Users/yym/bbx-wt/ctf-cooperation/services/blackboard/src/bbx_blackboard/ctf_board.py:313) 按任务、JSON challenge_id、version 读取，缺少匹配表达式索引；在记录继续增长后补索引，比另建消息/搜索服务小。保留现有锁与分页契约，不单凭缺索引推断当前已经明显慢。

## 7. MAF 复用与提示词方向

核实对象是本机已安装 **agent-framework-core 1.19.0**，不是对未来版本的推测。

- 已在用 Agent、AgentSession、逐模型调用历史保存、MessageInjectionMiddleware、ResponseStream、Function/Chat Middleware、原生消息分组；没有每步重新注入全黑板。
- [原生工具执行:2506](/Users/yym/bbx-wt/ctf-cooperation/.venv/lib/python3.12/site-packages/agent_framework/_tools.py:2506) 默认允许同批工具并发；开启一个并发参数不能解决派工依赖、全局锁与 Lead 单会话等待。
- [annotate_message_groups:670](/Users/yym/bbx-wt/ctf-cooperation/.venv/lib/python3.12/site-packages/agent_framework/_compaction.py:670) 已支持保留旧分组、增量 token 注解，可复用。直接接默认裁剪策略没有本平台的交付证明、保护输入和未知副作用语义，不能替换这些业务约束。
- 本机 orchestrations 入口只是可选包的惰性导出，未安装 agent_framework_orchestrations。即使引入 GroupChat/Magentic，也没有证据它能直接保留当前持久邮箱、代次、预算和排空协议；目前不建议为这些问题更换团队框架。

提示词已有按需读记录、直接队友交流、早发布、避免轮询及恢复时不重复派工等规则。后续应在工具补齐后让指令引用新入口：原子派工、带恢复状态的结果、按 ID 读取完整回执/工具结果。保持初始全面盘点范围，避免重现漏题；可以优化台账批量写入与派工依赖，不能为缩短启动时间而省略目标。

维持稳定系统提示与工具 schema，关键变化以短通知和引用进入追加历史，详细材料按需补读。把完整快照反复塞进 Lead 前缀会增加输入与缓存抖动；当前 95.71% 的账本缓存样本不支持盲目重写全部提示词或引入收费摘要。

## 8. 建议实施与验证顺序

| 阶段 | 内容 | 最小验证 |
| --- | --- | --- |
| A | #1 观测索引/查询；#2 旧输入退役；#4 checkpoint 小回执/窄 revision 查询 | 固定规模事件的查询计划；新/旧回合保护输入；CAS 与应答丢失核对；成功响应字节数 |
| B | #3 原子派工；#6 可调度变化通知；#7 恢复决定入结果 | 两成员假模型并发；单次分工同时有权限与派工；中途断连幂等；部分成员忙时 Lead 能收到一次相关通知 |
| C | #5 精简工具路由；#8 按引用补读；#9 线性裁剪；#10 deferred 分页 | 真实工具路由 SQL 计数；引用权限/分页；保持完全相同裁剪边界；超过一页旧输入的可达性 |
| D | #11–13 根据测量选择 | 空闲/繁忙控制请求数、停止延迟；MCP 握手时间；邮箱/记录查询计划 |

先使用已有假模型、原生 MAF 和隔离数据库验证业务边界；没有必要为审查启动新的收费任务。后续统一记录派工提交至成员首调用、消息排队至模型可见、模型等待/工具等待/持久化时间、每次 checkpoint 字节数以及重复管理调用次数，再用同模型同参数的真实样本比较。不能把 SQL 或纯内存微测直接换算成解题提速倍数。

## 9. 已执行、未检查和偏差

已执行：源码调用链审查；三名 GPT-6.1 Sol high 子代理独立检查通信、上下文与调度；只读 SQL 聚合、只读 EXPLAIN ANALYZE；四个运行源码 SHA256 核对；子代理纯内存恢复前缀复现；本地 MAF 1.19.0 源码核实。没有修改原有未提交修复。

未执行：make check、完整集成、收费模型、靶场命令、真实任务重跑、供应商缓存 A/B、端到端 CPU/网络 profile。未读真实 Session 正文、工具命令正文、flag、标准答案、grade、密钥或 .env。未重新判分、账单校正、修改已有任务数据、配置、镜像或容器。

偏差与待决：没有代码实施，尚无生产收益承诺。#3/#6/#7/#8/#10 涉及 CTF 工具/交流合同扩展，实施时应先补相应提案/任务合同并记录兼容行为；#1/#4/#5/#9 主要是既有协议内的实现优化。未修改 docs/design。

建议后续提交划分（本次不提交）：`perf(ctf): index observation lookup and trim checkpoint responses`；`fix(ctf): retire settled recovery inputs safely`；`feat(ctf): make assignment and coordination notifications complete`；`perf(ctf): reuse confirmed context metadata and streamline tool reads`。仅本审查报告可单独以 `docs(ctf): audit team communication and performance` 提交。

结论：保留 Lead 自主决策、队友直接交流、持久 Session 与可靠交付架构；首先解决锁内扫描、陈旧恢复输入和大快照往返，再减少协调中的分次操作与信息缺口。未逐行复审独立前端、全部第三方依赖、外部 MCP 服务、真实解题策略或所有安全场景。

## 10. 最新任务意外失败与已批准的实施范围

### 10.1 事故证据

最新任务仍为 a5d138ce。2026-10-14 09:11:30（北京时间），member-5、generation 5、turn a0eeb1a1-d030-4d36-a18f-d31e6decbc8d 在消费 MAF/OpenAI 模型流时抛出 ChatClientException；09:11:31 登记 system_failure，09:11:38 关闭全队。关联 ID 为 6a5a2674-ba71-41a6-be20-b00608ccc631。

从 MinIO 精确读取该 model_error 的白名单诊断字段：category=unknown、cause_type=APIError、exception_type=ChatClientException、elapsed_ms=36343、http_status/provider_code/request_id/transport_type 均为空，transient=false。受控栈位于 runner 的流消费、MAF ResponseStream 和 agent_framework_openai._chat_client 的 _stream/_handle_request_error。未读取模型正文、工具命令、实际答案或 Session 正文。

源码及真实 SDK 离线 MockTransport 证明：HTTP 200 SSE 的 error 事件可抛普通 APIError；当前 model_error_metadata 只从 APIStatusError 提取 code，导致合成 content_filter/server_error/rate_limit_exceeded 均被记为 unknown。failure_diagnostic 又把多数模型边界异常显示成“内部错误”。**这是确认的分类和诊断问题；历史原始码已经丢失，不能证明本次是哪一种流内错误，更不能把未知错误直接当作瞬态重试。**

### 10.2 实施决定

1. 从已有 APIError/MAF 异常链提取有界结构化码，仅明确瞬态码复用当前成员级 provider_transient、持久 checkpoint、有限续接及未知费用保护；内容审核、鉴权、无效请求和不明错误不放宽。固定摘要区分模型边界异常与本地内部错误。
2. 优先完成观测查找索引、恢复输入生命周期、checkpoint 小回执及 revision 窄查询。成功应答缩小，失败读回继续比较完整快照；不删除副作用前后保存点。
3. 恢复输入退役只适用于具有权威交付、旧 execution turn 正常完成、来源和本地确认历史证明的旧一次性派工/结果通知；原始目标、用户输入、来源未知、同回合输入、当前派工、未确认保存及未知工具组保留。不能用正常 completed 以外的 turn 终态推断工作已经完成。
4. 后续原子分工与派工、可调度变化的去重通知、带恢复状态的结果、按引用补读。Lead 决策、单 Session 写者、CAS/代次、停止、预算和可信验证保持。
5. 复用本机 MAF 1.19 的 Agent、Session、注入、中间件、工具并发及安全分组，不添加编排框架、消息服务或收费摘要。

任务合同见 [CTF-team-communication.md](CTF-team-communication.md)。本节合并诊断后再开始代码实施；检查、部署及现有数据保持情况会追加在本报告。

## 11. 实施结果与验证（2026-10-14 后续）

### 11.1 已完成的修复

- **模型流错误分类**：普通 APIError 的有界结构化 code/request_id 现在能保留。明确 server_error/rate_limit 才复用成员级有限恢复；未知、审核、鉴权、无效请求及本地异常不放宽。模型边界异常不再被无依据显示成内部代码故障。原生 SDK/MAF 回归同时核对已完成工具一次、不完整工具零次、其它成员继续、未知费用保留；不修改历史任务失败结论。
- **观测查询**：改查已有 object_id，迁移 0015 加部分索引；SQL 中固定可信事件类型，让 generic prepared plan 也能匹配部分索引。保留两次 fence、上传与去重确认。
- **旧恢复输入**：新增 service-only 的只读 proof 入口，按本任务/成员/旧 delivery turn/generation/正常 completed 联结，最多 100 ID 一批，不返回正文。runtime 再核对完整已确认历史签名、已排除前缀、原生完成边界及未知工具；仅改变模型视图，不删除历史。用户/目标/当前输入及不确定来源保留，退役证明进入原 Session；epoch 变更在下次模型请求前保存。proof 瞬断复用既有三次有界读取，耗尽后保守保留。
- **checkpoint**：正常 revision 查询不再读整份旧 Session；CTF 成功应答缩为八个字段，普通模式完整应答保持。客户端校验本次 checkpoint ID、task/member/turn/generation/purpose、revision 和确认输入；残缺小回执不能只凭 revision 放行，必须完整读回证明。命令前后保存点、原 CAS 和投递同事务不削弱。
- **原子派工**：update_challenge.instructions 支持逐人正文，分工权限与所有派信同事务；排序幂等、CAS 只有赢家发信、任一发信失败全部回滚。未传 instructions 的旧请求哈希保持，单纯修改任务板依然不唤醒。
- **调度反馈**：释放/重新开放题目真正变成可调度时，向 Lead 发一次短 advisory；不自动抢题。唯一 assignment_result 在实际恢复决定之后生成，附 requested/disabled/exhausted、原因、次数和等待时间。旧 context_limit 兼容路径明确为 continuation_pending，不假称已经安排或没有恢复。
- **按引用读取**：新增 read_turn_result、read_saved_tool_result、list_deferred_messages；复用已结算回执和已有 Session，保持 Lead/本成员权限，不开放他人私聊。正文默认每页 2,000 字符，deferred 默认 5 条，可显式分页；省略内容可补读，不需重执行工具。默认大窗口预览回归覆盖五条完整元数据及各 240 汉字预览，未把该结论外推到任意小窗口。

MAF 的实际嵌套工具输入可能是 dict；原子派工薄适配已用共享 Pydantic 合同统一规范化，真实 native MAF 工具回归证明只需一次更新调用，无需额外 send_message。

### 11.2 配置与前端保持方式

本轮不自动发布或覆盖现有 CTF/任务配置。历史任务继续使用创建时的快照；原子派工属于已有 update_challenge 的 schema 扩展，可直接使用。三个新读取工具严格受原 Worker 白名单限制，在配置页新增可选项，由用户显式启用后用于之后创建的任务；没有暗中绕过旧 allowlist。仓库内 bundled 模板已写入新工具用法，现有数据库里的自定义提示词不自动替换。

前端发布源从当前已部署的 ui-rebuild 工作树只读复制，保留该工作树的未提交设计；本轮仅叠加 CtfSettings 中两行工具选项。临时构建目录与原源文件比较，只有 CtfSettings 不同；没有用本后端分支的旧画布覆盖当前环绕拓扑。TypeScript/Vite 构建通过，既有大 chunk 提示保持；未做无关布局重构。

### 11.3 已实际运行的检查

- 最终普通检查：**1,678 passed / 303 deselected**，32.31 秒，Ruff/Pyright 通过，三条既有 MCP 弃用警告。日志 `.data/ctf-team-communication/check-final.log`。
- 阶段真实 PostgreSQL 专项：存储/小回执/proof 两项通过；原子派工/回滚/CAS/部分忙碌唤醒及按引用读取十项通过。用例复用现有随机端口 fixture，不连接业务数据库。
- 查询计划专项在 12,000 条合成观测中确认使用新部分索引；单个 200 KB 历史与一条投递的成功回执小于 600 字节，完整 GET 与原历史相同。这不是生产总流量或真实解题提速比例。
- native MAF、真实 SDK MockTransport 和认证 ASGI 路由检查覆盖嵌套派工、按引用读取、服务身份 proof、Worker 白名单、越权和分页边界；初次联调的 nested dict 适配问题及旧合同预期已修正后再跑全检。
- 候选 runtime/blackboard 已以 `--network=none` 复用已验证依赖构建，69/140 项源/合同/迁移文件散列与工作树一致。完整隔离集成及部署核对结果见第 12 节。

未调用收费模型、重跑靶场或读取实际答案。#5 工具路由进一步精简、#9 线性裁剪、#11 事件唤醒、#12 MCP 连接复用和 #13 次级索引留待后续量化；本轮先完成用户指定前三优先项及派工反馈，不扩大为编排框架替换。

建议本轮提交划分：`fix(models): classify structured stream errors without unsafe retries`；`perf(ctf): index observations and acknowledge compact checkpoints`；`fix(ctf): retire completed recovery inputs with durable proofs`；`feat(ctf): dispatch assignments atomically and expose complete feedback`；`docs(ctf): record failure analysis and local verification`。尚未执行 Git 提交、合并或推送；上一轮 transport/control 的未提交改动保留。

## 12. 最终集成与本地发布

完整隔离集成命令：

```sh
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env \
  -o image-agent-runtime test-integration
```

首次完整执行结果 **298 passed / 1 failed / 1682 deselected**，465.49 秒；唯一失败为原有 runtime image 检查在 100×0.1 秒窗口内未见启动锁。相同源码、相同镜像、不修改等待阈值，单独复核 `services/agent-runtime/tests/test_runtime_image_integration.py` 后 **1 passed，20.75 秒**，互斥锁、SIGTERM 正常退出及锁释放均通过。299 个集成用例都有通过结果，**不是声称首次完整命令 exit 0**。日志为 `integration-final.log` 与 `runtime-image-rerun.log`，均在 `.data/ctf-team-communication/`。

已更新本地 <http://127.0.0.1:58000/login>：

- 更新前确认零活动任务、零 running turn，保存旧源文件与公共静态资源；只停止并更新原 blackboard/runtime 应用容器，未重建 PostgreSQL、MinIO、数据卷或靶场。启动 blackboard 完成迁移及前端 HTTP 核验后再启动 runtime。
- 原 **11 任务、15 task_runs、74 Session、76 成员、339 turn、634 消息、191 题目、997 记录、24 Profile、3 平台配置、1 app_settings、34,181 events** 的数量及逐表内容摘要全部一致；未自动发布新 Profile 或覆盖模型、提示词、工具白名单、MCP 配置，也未重跑或续跑任务。
- schema 从 0014 到 **0015**，仅新增观测部分索引。对相同 34,181 行事件表的未命中查找，发布后实际使用 Index Scan，3 个缓冲块、Execution Time **0.039 ms**；审查前等价 JSON 查找全表扫描为 38–111 ms。该测量只证明数据库查找改善，不代表真实解题提速倍数。
- 两个运行应用均 running、restartCount 0，启动后 Traceback/ERROR 计数 0；runtime 的精确 advisory lock 为 1。登录 HTTP 200、入口 JS/CSS HTTP 200，新读取工具选项已包含于发布 bundle。
- runtime **69 项**、blackboard **140 项**的运行源/合同/迁移散列均与工作树及候选镜像一致。前端保留 ui-rebuild 现有源，仅叠加两行工具选项；发布 index SHA256 为 `5dba247c33d4069cb4de68c4068753b7134e316ef5a5646b3a4bcca5f48e333e`。
- 持久镜像标签：`bbx-ctf-communication-runtime:20261014` / `bbx-agent-runtime:latest`，镜像 ID `ea7c683432aa919e8c205e6c9916ee1bf86c01e688bbbea4afc905dc96b9cb1d`；`bbx-ctf-communication-blackboard:20261014` / `bbx-ctf-blackboard:latest`，镜像 ID `15127b7eae1be681a82e926c64e4d31423584b5d1e37067ed72857fd090ed1e9`。原应用容器保留部署环境，安装与这些镜像相同的已验证 payload 后重启。

核对材料：`before.json`、`pre-stop.json`、`after.json`、`deployment-verification.json`、`image-source-check.json`、`running-source-check.json`、`source-manifest.json`、`frontend-preservation.json` 及构建日志，位于忽略目录 `.data/ctf-team-communication/`。所有原表摘要相同；没有重新判分、回冲未知费用或读取真实答案。现有配置未自动启用新工具，在 CTF Worker 配置勾选三个新读取项后用于新任务；旧任务快照保持。

用户无需执行构建命令，直接打开本地平台测试。历史这次 APIError 的原始码未保留，仍不能确证具体供应商原因；没有用新的真实收费任务验证解题速度。代码及报告未提交、合并或推送。

## 13. 剩余五项实施（2026-10-14 后续）

用户继续要求完成剩余 #5/#9/#11/#12/#13，并沿用保留现有任务与配置的本地更新流程。本节更新第 11.3 节当时的待办状态，不改写历史审查和首次发布结果。

### 13.1 已实施

- **#5 工具路由**：由一个权威 authorizer 按原 turn purpose 校验并返回窄 task/profile/member 信息，不再先失败 review 授权后重来，也不再取完整 runtime_state。名册工具才查询名册。原 task 锁、代次、预算、pending 操作、固定 Profile 白名单及 review 四个只读入口保留；写操作仍在自己的事务重新校验。真实 PostgreSQL/ASGI 计数确认执行态读板 7 条 SQL、名册 8 条，无邮箱/全体 running turn/Session 调度查询。
- **#9 上下文裁剪**：复用 MAF 原生分组，每次预计算安全边界、长度前缀和与保护 ID，候选遍历中增量累计迁移输入，最终才构建和序列化前缀；跨跨度的禁止边界用差分覆盖计算。旧选界 oracle、随机历史及原生推理/不连续工具组验证等价。64→128→256 组时，加载 Python 操作计数 37,895→75,351→150,263，leaf 69,655→138,887→277,351；原生分组调用分别固定 1/2 次。保留完整历史确认签名，未添加不可靠的常驻缓存。总体为原生分组成本 A_MAF 加本地 O(N+C+B+K)；MAF 自身异常重复 call ID 等路径不据此宣称严格线性。
- **#11 事件唤醒**：复用已有 SSE 与 PostgreSQL 通知，首次 tick 前取事件水位、从该水位订阅并立即补 tick；只按实际消费推进 cursor。控制、消息与 future 完成唤醒，trace 仅推进 cursor；固定 0.2 秒合并、最多 2 秒补偿扫描，不由持续事件延长 deadline，也不缩短控制传输退避。相同绑定的健康检查最多缓存两秒，代次/绑定变化立即复查；真实执行 fence 不缓存。CTF 消息与任务板通知在同一数据库事务中发送，提交后才可见。假时钟 60 秒空闲 32 次扫描（两次起步＋30 次补偿），1000 条 trace 不触发额外 tick；不是实际解题耗时测试。
- **#12 MCP 复用**：固定 catalog 按任务/角色/权限/服务器与有效认证摘要隔离，build 每次重查启用状态、固定身份与凭据；每个 run 用一个公开 MAF MCP 生命周期复用原生 session。直接调用公开 ClientSession.call_tool，保持输出 schema 校验，绕过 MAF 中会再次调用的封装，不用 SDK 私有缓存。相同脚本传输下，两 run 各三次工具的 initialize/list_tools 均从 8 降到 2，tools/call 始终 6 次；新 session 原生必要的元数据读取保留。断连/取消按原 unknown 和单次副作用证据处理，下一次新授权调用才能重连；清理不盖原错误或第一次取消。Task 结束/分离清 metadata cache，run finally 关闭连接。
- **#13 查询索引**：迁移 0016 增加 task/recipient/sequence 以及 task/固定 JSON challenge_id/version 两个索引。记录读取的可信 JSON key 固定为 SQL literal，支持 generic prepared plan 匹配表达式索引；不改变序号分配锁或分页契约。8,000 条合成邮箱、12,000 条合成记录的查询计划及 cursor/offset 等价检查通过。

### 13.2 审核与检查

所有子代理均为 GPT-6.1 Sol high，分别实现路由、裁剪、MCP/唤醒，主会话审核及集成。交叉审查修复了 MCP cleanup 的 CancelledError 覆盖原始错误；新增回归同时保证正常关闭的外部取消仍传播、已有首个取消不被第二个取消覆盖。未添加新的编排框架、消息服务、依赖或配置项。

最终普通检查 **1,741 passed / 311 deselected**，33.77 秒；Ruff、Pyright 通过。24 条警告为框架 MCP sampling 弃用提示，未启用模型采样。新路由/索引六项 PostgreSQL 专项通过，提交/回滚通知两项真实 PostgreSQL 专项通过；后者确认未提交时不通知、回滚不通知、通知水位不超过可见状态。

### 13.3 最终集成与本地部署

同一候选镜像的完整隔离集成 **307 passed / 1745 deselected**，401.13 秒，exit 0；本轮原样的镜像启动/互斥/正常退出检查也通过，未修改原十秒等待门槛。普通检查、完整集成的日志分别为 `.data/ctf-team-performance/check.log`、`integration.log`。命令沿用第 12 节，使用新打包的 runtime 及已验证的未改执行/代理/评测镜像，未下载或重建无关依赖。

本地 <http://127.0.0.1:58000/login> 已更新，HTTP 200。更新前确认零活动任务、零 running turn，保存应用原源文件后只停止原两个应用容器；blackboard 完成迁移并确认前端后再启动 runtime。PostgreSQL、MinIO、靶场及数据卷保持。schema 从 0015 升为 **0016**，仅加两个索引。

最终逐表内容摘要及数量完全一致：11 任务、15 task_runs、74 Session、76 成员、339 turn、634 消息、191 题目、997 记录、24 Profile、3 平台配置、1 app_settings、34,181 events。没有自动创建任务、续跑、发布 Profile、改写提示词/模型/工具配置或回冲未知费用。前端 index 与发布前完全相同：`5dba247c33d4069cb4de68c4068753b7134e316ef5a5646b3a4bcca5f48e333e`。

运行源码与已接受检查的镜像/工作树一致：runtime 69 项，blackboard 141 项，无散列不符；两个应用运行正常，启动后无 ERROR/Traceback，runtime 精确 advisory lock 为 1。

- runtime：`bbx-ctf-performance-runtime:20261014` / `bbx-agent-runtime:latest`，镜像 ID `742f8fbd9513063ec3efef8bad4a7747626ae288c94226c90c396a73630b68e4`。
- blackboard：`bbx-ctf-performance-blackboard:20261014` / `bbx-ctf-blackboard:latest`，镜像 ID `ca2f6b09dcbe0d633cda551affe89c6cdee5c28b462f95f25ec638d131920838`。

核对材料位于忽略目录 `.data/ctf-team-performance/`：`before.json`、`pre-stop.json`、`after-final.json`、`deployment-verification.json`、`image-source-check.json`、`running-source-check.json`、`frontend-before.json`、`health.json` 及构建日志。使用既有容器部署环境安装相同已验证 payload 后重启，持久镜像标签供重建使用。没有执行收费模型、真实靶场或读取实际答案；用户无需执行构建命令，可直接打开本地平台测试。

建议本轮新增划分：`perf(ctf): authorize tools once and index inbox record reads`；`perf(ctf): preserve compaction decisions with linear candidate traversal`；`perf(ctf): wake scheduling from committed control events`；`perf(ctf): reuse pinned MCP catalogs and per-run sessions safely`；`docs(ctf): record final performance verification and deployment`。代码尚未提交、合并或推送；已有未提交修复保留。没有宣称消除 MAF 本体所有异常历史开销或已确定历史 APIError 的具体上游原因。

## 附录：旧恢复输入的纯内存复现

在本工作树执行以下命令，只读本地代码和内置 profile，不访问真实 Session 或模型；禁止生成 pyc：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
import asyncio
from pathlib import Path
from types import SimpleNamespace
from agent_framework import AgentSession, Content, Message
from bbx_contracts.ctf import load_ctf_profile
from bbx_runtime.ctf.context import estimate_messages_tokens
from bbx_runtime.ctf.middleware import CtfHistoryProvider, TurnBoundary

def msg(identifier, body):
    return Message(role="user", message_id=identifier,
                   contents=[Content.from_text(body)])

async def main():
    seed = msg("seed", "Synthetic goal")
    old = msg("prior-assignment", "!" * 600)
    fresh = msg("new-assignment", "?" * 300)
    session = AgentSession()
    session.state.update({
        "in_memory": {"messages": []},
        "ctf_context_recovery_seed": seed.to_dict(),
        "ctf_context_recovery": {
            "turn_id": "old-turn", "message": seed.to_dict(),
            "protected_inputs": [old.to_dict()],
        },
    })
    checkpoint = SimpleNamespace(
        session=session, task_id="synthetic-task", agent_id="synthetic-member",
        confirmed_history=(), delivered_message_ids=set(), deliveries=[],
    )
    provider = CtfHistoryProvider(
        None, checkpoint,
        {"id": "new-turn", "assignment_message_ids": ["new-assignment"]},
        load_ctf_profile(Path("profiles/ctf")).model, 1000,
    )
    view = await provider.get_messages(
        session.session_id, state=session.state["in_memory"],
    )
    print({
        "active_protected": sorted(provider._protected_input_ids()),
        "visible_ids": [message.message_id for message in view],
        "old_prefix_tokens": estimate_messages_tokens(view),
        "new_instruction_alone_with_seed": estimate_messages_tokens([seed, fresh]),
        "input_limit": provider.input_limit,
    })
    try:
        provider.prepare_provider_messages([*view, fresh])
    except TurnBoundary as error:
        print({"blocked": str(error), "estimate":
               session.state["ctf_context_blocked"]["estimated_input_tokens"]})

asyncio.run(main())
PY
```

子代理实际输出的关键字段：active_protected 只有 new-assignment；visible_ids 仍包含 seed、prior-assignment；old_prefix_tokens=641，new_instruction_alone_with_seed=341，input_limit=800；最后 blocked=context_blocked，estimate=959。
