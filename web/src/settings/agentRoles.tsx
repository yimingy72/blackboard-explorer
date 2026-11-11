import { BranchesOutlined, CompassOutlined, FileDoneOutlined, FlagOutlined, ApartmentOutlined } from '@ant-design/icons';
import type { SettingsRole } from '../api/client';

export const roles = {
  explore: { title: 'Explore', subtitle: '探索', icon: <CompassOutlined />, caption: '调查、取证与登记事实',
    description: '调查当前意图，采集可回读的证据，并把可靠观察写入任务黑板。',
    work: [['调查当前意图', '以任务目标、当前意图和已有事实为起点，先核对已知信息。'], ['登记证据与事实', '区分直接观察与推断，记录可回读的证据引用。'], ['提出后续方向', '存在明确缺口时登记意图；结束本轮时释放认领。']],
    limits: [['执行范围', '仅使用当前任务分配的执行环境。'], ['结果边界', '登记事实和意图；最终验收由 Close 裁定。']] },
  derive: { title: 'Derive', subtitle: '推导', icon: <BranchesOutlined />, caption: '关联事实与补足方向',
    description: '复核已有事实与验收缺口，提出尚未覆盖且能够验证的方向。',
    work: [['核对已有事实', '关联彼此支持或冲突的事实。'], ['寻找验收缺口', '定位仍缺证据的验收条件。'], ['登记新的意图', '去除重复、已排除及没有依据的方向。']],
    limits: [['执行范围', '仅使用任务黑板资料。'], ['结果边界', '只登记必要的新意图，已有事实原样保留。']] },
  close: { title: 'Close', subtitle: '裁定与终结', icon: <FileDoneOutlined />, caption: '核对验收与生成报告',
    description: '逐项核对验收条件和证据，提交裁定或最终报告。',
    work: [['逐项核对验收', '将验收条件与可回读的事实、证据相对应。'], ['说明不足和冲突', '缺少证据时保留未达成状态。'], ['提交终结结果', '整理达成情况、证据引用和未完成项。']],
    limits: [['执行范围', '只回读任务资料，无执行环境工具。'], ['结果边界', '只提交裁定或报告，不改写证据。']] },
  lead: { title: 'Lead', subtitle: '任务统筹', icon: <FlagOutlined />, caption: '协调任务与共享网络',
    description: '统筹原生 CTF 的分工、记录与收尾，并按任务需要选择网络。',
    work: [['协调任务分工', '核对任务说明、题目状态与工作范围。'], ['复核记录', '区分已核对的结果与未完成项。'], ['按需选择网络', '部署开放时，按任务说明选择默认网络或共享 VPN。']],
    limits: [['配置边界', '角色和工具由部署发布，当前只读。'], ['任务隔离', '既有任务继续使用创建时的配置快照。']] },
  teammate: { title: 'Worker', subtitle: '任务执行', icon: <CompassOutlined />, caption: '处理分配工作并登记记录',
    description: '处理分配给自己的工作，在题目范围内读取任务材料并登记进展。',
    work: [['理解分配范围', '以获准题目及任务公告为准。'], ['登记工作进展', '在分配的题目内记录可复查的观察与候选。'], ['报告阻塞', '缺少授权、材料或共享网络时说明原因。']],
    limits: [['写入边界', '限获分配题目，平台提交由 Lead 负责。'], ['网络边界', '继承任务网络，共享 VPN 由 Lead 管理。']] },
  progress: { title: '成果整理 Agent', subtitle: '', icon: <ApartmentOutlined />, caption: '读任务资料，写成果树',
    description: '把分散的任务资料归并为可回读的成果树，保留关键节点与证据引用。',
    work: [['读取增量资料', '读取任务中新增的记录、对象和登记证据。'], ['归并相同成果', '保留已有节点、可回读引用和不同来源。'], ['维护候选成果树', '只更新有变化的节点，由平台在整理回合成功后发布。']],
    limits: [['读取范围', '只读当前任务资料与成果树。'], ['写入范围', '只维护候选成果树，无命令、派工或黑板通用写入权限。']] },
} satisfies Record<SettingsRole, { title: string; subtitle: string; icon: React.ReactNode; caption: string; description: string; work: string[][]; limits: string[][] }>;

const shared = { goal: '任务目标', domain_context: '任务背景与约束', acceptance_status: '当前验收状态' };
const ctf = { task_id: '任务标识', member_id: '成员标识', member_name: '角色显示名', agent_workspace: '角色工作区', shared_workspace: '任务共享工作区', allowed_tool_names: '已发布且获准的工具名称' };
export const promptVariables: Partial<Record<SettingsRole, Record<string, string>>> = {
  explore: { ...shared, agent_id: '当前角色实例', current_intent: '当前意图', yaml_snapshot: '任务黑板快照', seed_max_steps: '种子探索最大步数', conclude_grace_calls: '结束后工具宽限次数' },
  derive: { ...shared, facts_text: '已知事实', intents_text: '已有意图', previous_excluded: '已排除的方向' },
  close: { ...shared, mode: '裁定或终结模式', satisfies_facts: '与验收相关的事实', judgment_history: '历史裁定', yaml_snapshot: '任务黑板快照', budget_left: '可用预算' },
  lead: ctf, teammate: ctf,
};
