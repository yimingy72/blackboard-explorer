# Completion-review · 必要推导复核与明确完成例外

用户要求derive原则上必经，用于防止探索过早放弃；明确补充：目标非常明确地达成时可以跳过。按通用证据/范围判断，不做关键词判题，不塞入单一场景清单。

## 合同

- VerdictItem与AcceptanceItemState新增completion_basis: Literal['explicit','inferred'] 默认inferred，completion_reason:str|null。explicit仅允许met，且必须有非空completion_reason及evidence_facts；含义为目标边界明确、直接完成证据充分、无影响完成结论的未验证事项。代码不能替代语义判断，但必须校验字段/有效支撑事实并保留理由。
- shared bbx_contracts.completion提供 all_met(task)、explicit_completion(task)（全部met/explicit/理由/支撑事实且last_judgment_version>=last_change_version）、current_review(task,agents)（返回最新有效完成复核Agent）。root负责此文件与contracts字段。
- agent_runs新增derive_review bool默认false、finished_version bigint|null；所有agent.finished投影保存事件version。复核derive的derive_from_version记录启动时last_change_version；普通并行derive仍记录最新Fact版本。
- SpawnDerive新增review:bool=False；登记API/BlackboardClient/register_agent新增derive_review:bool=False；review与parallel不能同true。
- 完成复核仅在quiescent：无活动explore/derive、无open/claimed Intent、无Close、最新黑板已经裁定时启动，不受derive_enabled=false禁用（该开关只控制主动并行推导及额外空复核）。派发时在任务锁内复核阶段/预算/版本条件，竞态返回stale_derive。非常明确完成时不再派复核。
- 若全部met但非explicit，不得直接closing。继续派发已有Intent/允许原Explore工作；静止后必经derive_review。复核提出Intent则回探索；空产必须是有效DeriveReceipt，excluded至少有一条非空理由，且实际没有该Agent创建的Intent。实际posted以黑板作者记录为准，不能只信回执声称空。
- 复核结束后必须再由Close judge裁定。用last_judgment_version>=该review.finished_version判定裁定已覆盖复核，opening Close应注入最新复核摘要。只有quiescent+fresh review+覆盖复核的裁定全部met才可以accepted收尾；或explicit完成例外可直接收尾（即使存在已无关的活动方向，由Close说明）。
- current_review只接受derive_review、status=finished、end_reason=normal、accepted=true、无raw_text、data.posted=[]、excluded有非空理由、derive_from_version==task.last_change_version、finished_version>0。新Fact/Intent/争议/方向状态变化会使旧复核失效。失败/拒绝/无效空回执不能当通过；在running状态归一为runtime_error计入现有连续失败上限，runtime_restart不计。
- 未全部met时的quiescent推导也记为derive_review。空结果保持derive_empty_streak；陈旧复核不得增加空计数。最新复核及再次裁定后仍unmet：derive_enabled=true按原derive_empty_limit（默认2），false至少一次复核后可terminated。这样single仍只有一个Explore，但新版本增加必要的derive复核，不能沿用旧single“完全无derive”的口径。
- 硬时限/探索预算耗尽、人工停止、无法起步/连续错误仍可结束，不为了必经复核突破这些约束；异常退出不是成功证明。关闭API中reason='accepted'必须在锁内验证explicit_completion或quiescent+fresh review+covered judgment+all_met，防止调度旧快照越过门控。
- 新模式信息作为运行任务上下文注入；prompts/default与single同步通用规则。保留derive_enabled字段兼容，UI文案改主动推导并注明必要复核保留。模板热更新仍有效。

## 分工

root：contracts/helpers、提示词与上下文、文档、前端可辨认标签、整体验证部署。
backend代理：blackboard schema迁移0007、API/domain/projection/service、对应测试；不改contracts/runtime。
scheduler代理：runtime scheduler/actions/executor/clients登记、调度测试与场景（含single新口径）；不改blackboard/contracts/runner/opening/prompts。

make check不得调用模型，完成标准包括脚本场景：明确完成跳过、模糊met复核空后再判完成、复核发现方向继续探索、过期复核重做、无效回执不通过、预算人工停止可结束。最终报告中文。
