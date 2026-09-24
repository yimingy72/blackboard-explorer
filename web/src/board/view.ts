import { deriveGraph } from './graph';
import type { BoardState } from './types';

export type BoardFilters = { kind: string; factStatus: string; intentStatus: string; agent: string; acceptance: string; collapseClosed: boolean };

export function filterBoard(state: BoardState, filters: BoardFilters): { state: BoardState; hidden: number } {
  const hidden = new Set<string>();
  if (filters.collapseClosed) {
    const graph = deriveGraph(state);
    const downstream = new Map<string, string[]>();
    for (const edge of graph.edges) {
      if (edge.data?.relation === 'disputes' || edge.data?.relation === 'claim') continue;
      downstream.set(edge.source, [...(downstream.get(edge.source) ?? []), edge.target]);
    }
    const queue = Object.values(state.intents).filter((intent) => intent.status === 'closed').map((intent) => intent.id);
    const visited = new Set<string>();
    for (let index = 0; index < queue.length; index += 1) {
      const id = queue[index];
      if (visited.has(id)) continue;
      visited.add(id);
      for (const next of downstream.get(id) ?? []) {
        if ((state.intents[next] && state.intents[next].status !== 'closed') || state.facts[next]?.status === 'disputed') continue;
        hidden.add(next);
        queue.push(next);
      }
    }
  }
  const matchingIntents = Object.values(state.intents).filter((intent) => !hidden.has(intent.id)
    && (!filters.intentStatus || intent.status === filters.intentStatus)
    && (!filters.agent || intent.author === filters.agent || intent.holder === filters.agent
      || intent.notes?.some((note) => note.by === filters.agent)
      || intent.resultFacts.some((id) => state.facts[id]?.author === filters.agent))
    && (!filters.acceptance || intent.relatesTo.includes(filters.acceptance)));
  const relevantFacts = new Set(matchingIntents.flatMap((intent) => intent.basedOn));
  for (const id of state.acceptance[filters.acceptance]?.evidence_facts ?? []) relevantFacts.add(id);
  for (const fact of Object.values(state.facts)) if (fact.satisfies.includes(filters.acceptance)) relevantFacts.add(fact.id);
  const pending = [...relevantFacts];
  for (let index = 0; index < pending.length; index += 1) {
    for (const id of state.facts[pending[index]]?.derivedFrom ?? []) if (!relevantFacts.has(id)) { relevantFacts.add(id); pending.push(id); }
  }
  const facts = Object.fromEntries(Object.values(state.facts).filter((fact) => !hidden.has(fact.id)
    && (!filters.kind || fact.kind === filters.kind)
    && (!filters.factStatus || fact.status === filters.factStatus)
    && (!filters.agent || fact.author === filters.agent)
    && (!filters.acceptance || relevantFacts.has(fact.id))).map((fact) => [fact.id, fact]));
  const intents = Object.fromEntries(matchingIntents.map((intent) => [intent.id, intent]));
  const agents = Object.fromEntries(Object.values(state.agents).filter((agent) => !filters.agent || agent.id === filters.agent).map((agent) => [agent.id, agent]));
  return { state: { ...state, facts, intents, agents }, hidden: Object.keys(state.facts).length + Object.keys(state.intents).length - Object.keys(facts).length - Object.keys(intents).length };
}

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
  'acceptance.reverted': '验收回退', 'budget.updated': '用量更新', 'tool_call.recorded': '工具调用',
};
