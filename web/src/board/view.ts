import type { BoardState } from './types';

export function taskEndExplanation(state: BoardState): string | null {
  const task = state.task;
  if (!task) return null;
  if (task.status === 'failed') return task.fail_reason ?? '任务因运行错误结束';
  if (task.status === 'stopped') return '任务在开始探索前被停止';
  if (task.closingReason === 'accepted') return '验收条件已全部满足，系统完成终结报告';
  if (task.closingReason === 'manual' || task.closingReason === 'user_stop') return '用户请求停止，系统完成交接与终结报告';
  if (task.closingReason === 'terminated') {
    const reserve = Number(task.params?.close_reserve_ratio ?? 0.05);
    if (Number(task.usage.cost ?? 0) >= Number(task.budget.max_cost) * (1 - reserve)) return '探索金额预算用尽，系统使用预留额度收尾';
    if (task.params?.derive_enabled === false) return '探索时限已到，或探索停止且继续推导已关闭，系统进入终结收尾';
    return '探索时限已到，或连续推导没有新方向，系统进入终结收尾';
  }
  return task.closingReason ? `结束原因：${task.closingReason}` : null;
}

export const eventLabels: Record<string, string> = {
  'task.created': '创建任务', 'task.provisioning': '准备环境', 'task.running': '开始探索',
  'task.closing': '进入收尾', 'task.finished': '任务完成', 'task.failed': '任务失败', 'task.stopped': '任务停止',
  'task.report': '发布报告', 'task.archived': '归档完成', 'fact.posted': '提交事实',
  'fact.disputed': '事实被争议', 'fact.undisputed': '争议已解除', 'intent.posted': '提出意图',
  'intent.claimed': '认领意图', 'intent.released': '交接意图', 'intent.closed': '关闭意图',
  'agent.spawned': 'Agent 开始', 'agent.progress': '运行进度', 'agent.finished': 'Agent 结束',
  'agent.conclude_requested': '请求交接', 'derive.result': '推导结果', 'acceptance.judged': '验收裁定',
  'agent.trace.recorded': 'Agent 对话记录',
  'agent.message.posted': '用户发送消息', 'agent.message.delivered': '消息已送达',
  'agent.message.replied': 'Agent 回复用户', 'agent.message.failed': '消息处理失败',
  'acceptance.reverted': '验收回退', 'budget.updated': '用量更新', 'tool_call.recorded': '工具调用',
};
