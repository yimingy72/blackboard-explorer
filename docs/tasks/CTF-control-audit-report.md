# 控制链代码扩查报告

日期：2026-10-13。起点 `c1bd222`，工作树 `/Users/yym/bbx-wt/ctf-cooperation`。本轮来自用户要求审核 [回合传输事故报告](CTF-turn-transport-failure-report.md)、修复并扩查同类与相关 bug。假定本机单实例 runtime、同一时间一个执行任务、多成员并发和终态复盘；不据此假定多主机多写者部署正确。

## 项目作用与审查范围

平台是通用问题求解引擎，提供黑板 Fact/Intent 模式及独立 CTF 团队模式。MAF 执行模型与工具循环；平台负责共享状态、账本、代次、执行环境和生命周期。跨服务写入的确认、未知结果及恢复权限属于业务协议，不能仅依靠模型 SDK 重试。

审查从异常产生追到真正的调用者和副作用：CTF runner/client/coordinator、Session/checkpoint、观测、MAF 调用边界、执行登记/排空、普通 scheduler/sweeper/supervisor、复盘、领取/结算接口、用量 outbox/账本、停止操作、服务鉴权与生成合同。分别追查每个改动的调用者，复用既有协议。发现项需有源码触发链及故障注入，未将单纯缺少 catch 或个人风格列作 bug。

## 必须修复的发现

| # | 触发条件与原错误结果 | 最小修复与证据 |
|---|---|---|
| 1 | `CtfRunner.settle` 读取黑板或写 `finish_turn` 时抖动，异常穿过 `run`，协调器把单个回合错误升级为整队失败。 | 幂等读取有界重试；原 answer/reason 保留为 pending settlement；未知 finish 只读回核对；单次故障不取消同伴。真实 PG/认证 API/ASGI/native MAF 两成员故障注入覆盖读、已提交写丢响应。 |
| 2 | 活动 future 被删除后才请求关闭；关闭写未提交又断连，下一 tick 丢失确定性失败来源，任务可能继续。启动读断连也被直接 `fail_start`。 | 关闭确认前保留失败 future；启动先有界对账，不重发未知 start；running/closing/closed 读回视为已推进。确定性错误仍失败，持久故障仍有上限。 |
| 3 | 记录工具异常时，观测写的传输错误盖住原 `CancelledError`；或者 SDK 关闭连接池失败盖住主异常、跳过已完成结果结算。 | 失败观测 best-effort，保留原取消/异常；普通 SDK cleanup 错误只记录安全 WARN，不触发模型重跑或改回执。成功工具观测与保存屏障没有降级成可随意忽略。 |
| 4 | envd 已 register，但黑板 `record_execution` 断连导致无绑定；原 drain 跳过该成员并错误返回排空，新代次注册被拒。 | 核对真实 boot/member/gen registration，完整保留待确认身份；仅同身份幂等控制登记可重试，空 DB 绑定不证明排空。旧真实代次先 drain，新于调用者或异主身份拒绝。使用实际 `CtfCommandRegistry` 及既有真实 envd 集成验证。 |
| 5 | stop 目标取旧已 drained binding；真实新代次注册后只补 boot、不补 target generation，新 drain proof 永久 409。已停止的本地结算及新派工又可能串回合。 | 仅真实 matching 注册时严格更新目标/boot。操作历史另记录实际结算 turn ID/gen，保留真实排空代次；权威 stopped 对账不覆盖新 writer。待结算/保存期间只阻该成员的新领取，避免旧 pending 与新模型串回合。 |
| 6 | review claim 提交但应答丢失，读回的 runtime turn 没有 token；再次领取还可能在读写间创建新孤儿。 | 原 handler 对同实例、同当前 review 重签原 token；ACK-loss 重取带 `expected_turn_id`，任务锁内检查，过期证明不能新建回合。执行领取同样增加条件重取，默认领取不改。 |
| 7 | 复盘 finish 未提交而恢复次数用尽，同实例 review 指针长期占用，恢复数据库后仍不能续跑/删除。旧跨实例保存屏障及恢复应答丢失也会卡住。 | 已退出本地写者的目标复盘严格、幂等收尾；同原 turn/purpose/member/gen/answer 接受已完成的 interrupted 恢复。只处理目标，保原回答、Session 和费用；未确认保存/用量/观测不放行。旧屏障转故障历史，未确认时低频保留，不能伪造成功。 |
| 8 | 持久旧 review usage outbox 在新 review 中 flush，把旧调用费用计到新回合。 | 历史 entry 由服务从已确认 Session 对账，校验原 turn/member/gen/step/request ID，原账本幂等结算；不接受客户端任意历史账单，不清未确认项。真实 PG 覆盖新旧账本隔离及跨回合请求别名。 |
| 9 | 普通任务 provision 已成功后，状态读/运行状态确认断连被当确定启动失败；已 running 但本地 loop 未建立又可能变成孤儿。普通 loop/sweeper 的 5xx 分类与控制读取不同。 | 保留成功 handle、退避并重读权威状态；已推进则接回现有 loop，不能重复创建/销毁。loop/sweeper 下一周期重新决策，不盲重发失败写。4xx/本地协议/参数/缺环境等确定错误仍传播。 |
| 10 | 普通 Agent 最终回执已产生，finish 未提交又断连；local future 消失后通用 heartbeat/runtime_restart 结果覆盖原业务回执。 | 在原生 Session 保存带原 task/run/agent-run/derive-round 的最终回执，精确读回或 fenced 补偿；sweeper、重启恢复、终态 drain 先用同一接口处理已有证据，不重跑模型。新的 task/agent run 不可被旧回执结算。 |

这些是已验证的触发链，不代表已证明原事故每个丢失片段或所有环境中的结果。各专项检查存在重叠，最终通过数统一记录在 [修复报告](CTF-turn-transport-fix-report.md)。

## 入口清晰度与可观测性

- `append_record` 和 `record_candidate` 同样要求 Lead/owner/collaborator。日志只能证明当次没有权限，不能定位具体未登记成员或确认其完整上下文。保留权限，工具说明及拒绝反馈明确申请协作者或向 owner 发消息，不以另一个同权限工具绕过。
- 首次 closing 事务明确记录“取消请求”的回合 ID、成员、代次、数量与原因；不是宣称所有回合已取消或所有工作已丢失。写入幂等，事件失败回滚状态。
- 失败诊断加入有界 task/member/turn/generation 元数据。日志不记录异常原文、请求体、密钥或模型内容；旧诊断仍兼容。
- 公共控制异常分类区分 HTTPX 黑板/envd 与模型 HTTPX2；排除本地协议/不支持协议等确定错误。模型的 SDK 重试与审核拒绝语义保持。
- 取消快照保存失败时显式保留 interrupted，重试不因 partial answer 非空就标完成。安全停止之后的本地旧 settlement 只按 exact 原 turn 及持久停止证据收敛；不清未确认 checkpoint 或未知费用。

## MAF 复用及未扩大部分

复用已安装 MAF core 1.19.0 的 Agent、AgentSession、原生 History/工具循环/ResponseStream、消息分组和既有中间件。最终回执使用 Session.state，未另造会话库或模型调度器；控制恢复用既有事务、CAS、generation、outbox、registry、readback，不添加依赖或摘要模型。

不改上下文阈值、模型/工具配置版本、单价、执行镜像/权限、CTF判分与三份设计正本；没有读取真实答案或重跑靶场。未确认账单或保存屏障不能当作免费或成功，本轮不擅自回冲事故中的未知预留。

## 审查边界

覆盖上述风险路径及相连的角色鉴权、停止/续跑、必要归档；未逐行重审独立前端工作树的布局动画、全部评测样例、供应商模型服务实现或第三方依赖内部。故障注入证明控制协议，不证明公网断连的具体根因，也不保证任意长期失联都能继续执行。连接被提前关闭究竟源于服务、代理、连接池或网络，仍缺少现场证据。
