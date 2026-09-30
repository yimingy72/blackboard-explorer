# Prompt-speed · 种子早发布、任务思考强度与短暂断连恢复

日期：2026-09-30。用户在速度审查后明确认可第1、6项；第2项要求创建任务时自由选择思考强度并补DeepSeek max；第3项只解释Derive上下文；第4项只解释Session，先不修改；第5项不实施。已有开发/设计/Git授权有效，子代理统一GPT-6.1 Sol high，前端由主会话实现。

## 依据与前序

先读AGENTS、HANDOFF第0节和6.1、Code-simplification-report、Runtime-continuation、Runtime-streaming、Provider-failure-coalescing、Model-success-health及Worker-settings报告；设计依据概念5.1/5.3/5.4/8.1及会话/故障补充，实现6.3/7/8/9.1及配置/恢复补充。

最新只读样本98a84ccc：首Fact在第20个完整响应后189.09秒发布，190.01秒收到种子limit，首Intent在第22个响应后207.90秒交接时发布；5个Explore确实并发；缓存总体97.3%、derive96.7%；13次connection最终错误。该样本不能证明新提示词提速，不新跑收费目标测试。

## 范围与合同

1. 种子明确“核实最小资源/范围并保存证据→立即发布Fact→据此提出可独立执行Intent/认领最有把握方向→继续长准备”的顺序。保持证据、判重、step/conclude约束，不凑方向或写入单个题目的解法；保留已有每5轮提醒，不为本样本另设计时/关键词策略。
2. TaskCreateBody新增可选reasoning_effort；null/省略沿用所选平台模型设置，显式none仍表示不传思考参数（不等于强制关闭思考）。显式值只进入该任务不可变Profile；三Worker沿用共同任务模型，不改平台默认或旧任务/续跑。历史自定义Profile路径同样支持覆盖，参数不写入TaskSpec/任务表。Provider目录提供reasoning_efforts；支持思考参数的兼容/Responses连接若模型ID含deepseek，提供low/high/max；其他Provider沿用原选择，不支持则拒绝显式覆盖。旧DeepSeek minimal/medium/xhigh/off默认与兼容API值保留，不静默转换或自动降低max。
3. 前端模型编辑与创建表单沿用现有控件。创建默认“沿用模型配置”，切换模型回到继承选项；按Provider能力显示可用强度，保留既有模型中的兼容值。max按现有客户端直传Chat.reasoning_effort或Responses.reasoning.effort。官方DeepSeek将medium/xhigh映射high、max是独立强度；网关实际行为需另行实测，不能仅凭xhigh标签认定实际最大推理。
4. 仅对真实模型调用的connection/timeout等经明确分类的瞬时传输失败增加有界应用层恢复；永久请求、认证、余额、内容审核和普通本地/控制HTTP/会话错误不恢复。保留SDK当前4次重试，不重写请求绕过错误。
5. 共享恢复gate按任务/模型作用：短暂故障期间阻止新的Explore/Derive/judge派发，已有请求可完成，清扫、预算、人工停止、Conclude/Fail/SystemClose继续。final Close可登记收尾，其模型请求仍经过gate。首版等待仍计任务活动时长，不改预算规则或并发。
6. 每个Agent本次run累计最多2次额外MAF恢复，成功不重置额度；恢复episode固定120秒，含等待与恢复模型请求，受原任务/run/交接截止约束；退避2、4秒，至多一个探针在途、每episode最多两次，使用等待Agent自己的下一模型请求，不另发收费ping。完整成功可提前解除暂停；早于该成功开始的迟到失败不能重新封锁健康模型。无成功时恢复耗尽按原finish/failure保护处理，gate冷却保留至固定deadline，到期仅允许下一窗尝试而非宣布健康；持续故障按原跨120秒窗口阈值终止，不能无限重建恢复episode。
7. 重试边界在一次MAF run外层：丢弃失败流聚合器，使用既有检查点保留同Agent ID/Session/Intent/derive轮次；保留尚未持久化用户消息的ID/claim token，黑板注入水位以已持久或成功进度为准。已完成工具结果复用现有记录，未知结果标记中断，绝不重执行旧工具。Session PUT/GET/CAS实现、返回及保存频率不改；不能在已经yield的同一响应流中重试并叠加碎片。必须区分模型请求失败与后续heartbeat/checkpoint失败，后者走原错误路径。

## 完成标准

- 原检查及有意义的新增离线用例全绿：任务强度快照/默认/旧任务隔离、兼容DeepSeek max载荷、流断片不拼接、不重执行工具、待确认用户消息与增量保留、单探针/累积上限/持续故障终止、停止/预算可达。
- make check、make test-integration、前端check/build、浏览器原回归与新增创建/模型max检查通过。无真实模型/目标调用、.env读写、新依赖或全局配置。
- 报告Prompt-speed-report.md用中文说明Derive首次完整清单/后续增量/阈值或提示更新重建，以及完整Session上传与原样回包的现状；明确本轮不优化Session回执、不增加并发、不改Derive工作规则，不把离线测试当实测提速。
- 按范围分组提交，合并后更新HANDOFF，部署前检查是否有活动任务；保留用户自定义Worker/模型配置，正式存档和旧任务不自动续跑，不推送远程。

参考：[DeepSeek思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)、[Chat API](https://api-docs.deepseek.com/api/create-chat-completion/)、[Responses API](https://api-docs.deepseek.com/api/create-response/)。
