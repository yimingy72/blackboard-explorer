# CTF-run3-hardening · Run3 独立审查与中断恢复

用户要求审查 `/Users/yym/bbx-wt/bbx-cybench-smoke/deploy/batch/CYBENCH-RUN3-AUDIT.md`，另外独立分析本次测试并继续优化缓存和其他问题。工作树沿用 `ctf-cooperation`，保留所有前序未提交改动；子代理统一 GPT-6.1 Sol high。复用当前 MAF，不新增依赖或通用重试/记忆服务。

## 已核实

任务 `cda295d2-82ce-4cdf-bcad-dce5eb191433`，703 次成功 model_output，用量与 task/execution turns/members 完全一致，命中率 96.06%；10 次前缀更换发生零命中，其他 693 次为97.61%。单次异常为 ChatClientException→RemoteProtocolError，流读取阶段，transient=true。attempt_limit=5 是配置而非实际已发送五次证明。

六个封锁记录含 null message_id，SQL NOT IN(...,NULL) 阻挡新指令；member-11 active/idle 有三条 queued instruction，member-10 已被 Lead 移除且恢复消息合法 cancelled。旧未确认输入需要保留为 deferred，新恢复不能再次租赁同一封锁输入。

外部 grade 的16/31是记录与报告文本的答案字符串匹配，不是平台提交 accepted；13个completed是工作状态。三条 sent未知预留无可信用量，不能因 interrupted 或终态清零。原报告、答案、题目攻略、旧任务状态和账本不改写。

## 范围

1. 仅明确分类的瞬时模型错误保存原 Session，以 provider_transient 结算当前回合；单成员每run最多三次退避恢复，2/4/8秒，保留其他在跑成员。计数/退避持久，旧 unknown 不释放，预算/phase/generation/drain门禁保留。永久协议、内容审核及内部逻辑错误不绕过。
2. 兼容旧 null ID，今后只存有效ID，恢复隔离旧未确认输入并返回可观测投递状态。取消、移除、deferred消息不自动复活。
3. 模型发送前遇新增批次跨阈值时，仅裁已确认旧历史的完整原子组到低水位，保护新用户/派工和未完成工具组。复用 MAF公开分组、当前checkpoint和稳定预览；匹配失败保守阻挡，切点/epoch持久，原历史不删除，不调用摘要模型。
4. 扩展现有record_candidate可选work_status，owner/Lead显式原子交付候选、证据和工作状态；省略兼容，保留可信平台/user验证，不把字符串自动变成accepted/completed。
5. 完整普通/隔离集成/必要前端类型回归，源码镜像和新任务模板部署；原B前端、模型配置、任务/会话/账本/归档保持，部署前核对无活动任务，不自动重跑或续跑。

已读AGENTS、HANDOFF、CTF-team-mode提案、前序context-recovery与batch-hardening任务及报告。不改docs/design、.env、全局设置、依赖或Git；真实模型与靶场不属于本次检查。中文报告记录独立事实、更正、实际检查、偏差与建议提交划分。
