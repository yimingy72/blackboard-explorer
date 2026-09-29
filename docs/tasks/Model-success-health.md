# Model-success-health · 成功模型调用打断连续故障

依据：用户持续授权修复；概念设计“会话复用与显式续跑”的连续故障修订。先读AGENTS及Provider-failure-coalescing、Control-connection-resilience和Runtime-streaming报告。

事实：任务4891d8c7第3轮的终止由一条unknown与两个connection窗口叠加触发，两个connection窗口之间仍有80次model_output。现有日志不能定位历史连接故障来源；不得宣称已证明网络、网关或模型全面不可用。

- Blackboard新生成的heartbeat进度事件，在合法活动Agent、非终态任务、steps>0且当前故障为模型调用家族时记录显式清零标记，投影事务清空failure_streak及故障窗口。瞬时模型故障用model_transient，其他带模型错误元数据的故障用model_error；无效回执/普通本地运行错误不因模型回复成功而清零，重复无效复核仍应失败。零步心跳/扣宽限额度不清，旧derive轮次仍拒绝，终态冻结；无标记旧事件重放保持既有结果。正常Agent完成清零及无成功调用时的120秒合并窗口/失败阈值保留。
- 错误终止理由改为来源中性的“连续运行失败达到保护阈值”，不把混合错误计数统称模型服务不可用。
- 混有本地/回执错误的计数用mixed保守标记，不被模型心跳清除；保留正常Agent结束清零与有锚点的120秒并发合并，避免恢复健康规则掩盖无效复核。
- Runtime诊断增加有界、非正文的异常类型/传输类与固定失败阶段信息，区分连接建立、响应读取、流缺完成事件、已返回HTTP错误及未知本地异常；保留SSE终态类型与安全provider_code，沿用现有校验。禁止日志记录异常原文、请求/响应正文、headers、密钥。attempt_limit明确是上限，日志不能宣称所有错误都已耗尽重试。不得新增应用层自动重放、降低完成验证或绕过内容审核。
- 测试覆盖失败→模型成功→新失败，单个长Agent成功步骤能清零；真正连续失败仍到阈值，零步/终态/旧事件不清零，PG实时投影与replay一致；网络/流/unknown诊断和脱敏回归。make check与make test-integration不调用真实模型。
- 报告中文 `docs/tasks/Model-success-health-report.md`，明确实际验证与仍未知的连接故障原因。原任务已结束且run3归档完成，不自动续跑或修改原结束事件。
