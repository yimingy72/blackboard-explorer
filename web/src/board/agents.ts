import type { BoardAgent, BoardEvent, BoardState } from './types';

export function agentNumbers(events: readonly BoardEvent[]): Record<string, number> {
  const numbers: Record<string, number> = {};
  for (const event of [...events].sort((a, b) => a.version - b.version)) {
    if (event.type !== 'agent.spawned') continue;
    const id = event.payload?.id;
    if (typeof id === 'string' && id && numbers[id] === undefined) {
      numbers[id] = Object.keys(numbers).length + 1;
    }
  }
  return numbers;
}

export function agentLabel(id: string, numbers: Record<string, number>): string {
  const number = numbers[id];
  return number === undefined ? id : `Agent ${number}`;
}

export function agentRole(agent: BoardAgent): string {
  if (agent.isSeed) return '种子探索';
  if (agent.taskType === 'explore') return '探索';
  if (agent.taskType === 'derive') return '推导';
  return agent.closeMode === 'final' ? '终结' : '裁定';
}

export const agentStatusLabel: Record<BoardAgent['status'], string> = {
  running: '运行中', concluding: '收尾中', finished: '已结束', failed: '失败',
};

export const agentEndReasonLabel: Record<string, string> = {
  normal: '正常结束', refused: '拒绝开始', limit: '达到运行上限',
  grace_timeout: '交接超时', heartbeat: '心跳超时', runtime_error: '运行错误',
  runtime_restart: '运行器重启', closing: '任务进入收尾', failed: '任务失败',
};

export function orderedAgents(state: BoardState, numbers: Record<string, number>): BoardAgent[] {
  return Object.values(state.agents).sort((a, b) =>
    (numbers[a.id] ?? Number.MAX_SAFE_INTEGER) - (numbers[b.id] ?? Number.MAX_SAFE_INTEGER));
}

export function taskDuration(startedAt: string | null | undefined, finishedAt: string | null | undefined, cutoffAt: string | null | undefined, now: number): string {
  if (!startedAt) return '待启动';
  const start = Date.parse(startedAt);
  const end = Math.min(
    cutoffAt ? Date.parse(cutoffAt) : Number.POSITIVE_INFINITY,
    finishedAt ? Date.parse(finishedAt) : Number.POSITIVE_INFINITY,
    now,
  );
  if (!Number.isFinite(start) || !Number.isFinite(end)) return '—';
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours > 0 ? `${hours} 小时 ` : ''}${minutes} 分 ${String(seconds % 60).padStart(2, '0')} 秒`;
}
