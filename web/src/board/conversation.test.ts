import { describe, expect, it } from 'vitest';
import { agentLabel, agentNumbers, taskDuration } from './agents';
import { conversationEntries } from './conversation';
import { reduce } from './reducer';
import type { BoardEvent, BoardEventType } from './types';

function event(taskId: string, version: number, type: string, payload: Record<string, unknown>): BoardEvent {
  return {
    task_id: taskId, version, type: type as BoardEventType, payload,
    actor: 'runtime', created_at: `2026-01-01T00:00:${String(version).padStart(2, '0')}Z`,
  };
}

describe('Agent display and conversation history', () => {
  it('numbers agents by registration version within each task and cutoff', () => {
    const first = [
      event('one', 5, 'agent.spawned', { id: 'agent-9', task_type: 'explore' }),
      event('one', 3, 'agent.spawned', { id: 'agent-2', task_type: 'explore' }),
      event('one', 8, 'agent.spawned', { id: 'agent-10', task_type: 'derive' }),
    ];
    expect(agentNumbers(first)).toEqual({ 'agent-2': 1, 'agent-9': 2, 'agent-10': 3 });
    expect(agentLabel('agent-9', agentNumbers(first))).toBe('Agent 2');
    expect(agentNumbers(first.filter((item) => item.version <= 5))).toEqual({ 'agent-2': 1, 'agent-9': 2 });
    expect(agentNumbers([event('two', 2, 'agent.spawned', { id: 'agent-9' })])).toEqual({ 'agent-9': 1 });
  });

  it('merges recorded traces and tools by version without exposing later records', () => {
    const events = [
      event('one', 1, 'task.created', { goal: 'Test', acceptance: [], budget: {}, usage: {} }),
      event('one', 2, 'agent.spawned', { id: 'agent-1', task_type: 'explore', is_seed: true }),
      event('one', 3, 'agent.trace.recorded', { agent_id: 'agent-1', kind: 'initial_context', step: 1, uri: 'trace/1', summary: 'Input' }),
      event('one', 4, 'tool_call.recorded', { id: 'call-1', agent_id: 'agent-1', tool: 'search', args: {}, result_head: 'OK', result_uri: null }),
      event('one', 5, 'agent.trace.recorded', { agent_id: 'agent-1', kind: 'model_output', step: 1, uri: 'trace/2', summary: 'Output' }),
      event('one', 6, 'agent.trace.recorded', { agent_id: 'agent-2', kind: 'model_output', step: 1, uri: 'trace/3', summary: 'Other' }),
    ];
    const cutoff = events.filter((item) => item.version <= 4);
    expect(conversationEntries('agent-1', cutoff, reduce(cutoff)).map((item) => item.version)).toEqual([3, 4]);
    expect(conversationEntries('agent-1', [...events].reverse(), reduce(events)).map((item) => item.version)).toEqual([3, 4, 5]);
    expect(conversationEntries('agent-2', cutoff, reduce(cutoff))).toEqual([]);
    expect(conversationEntries('agent-1', events.slice(0, 2), reduce(events.slice(0, 2)))).toEqual([]);
  });

  it('uses live, frozen, and historical end times', () => {
    const start = '2026-01-01T00:00:00Z';
    expect(taskDuration(null, null, null, Date.parse(start))).toBe('待启动');
    expect(taskDuration(start, null, null, Date.parse('2026-01-01T00:01:03Z'))).toBe('1 分 03 秒');
    expect(taskDuration(start, '2026-01-01T00:02:04Z', null, Date.parse('2026-01-01T00:10:00Z'))).toBe('2 分 04 秒');
    expect(taskDuration(start, '2026-01-01T00:02:04Z', '2026-01-01T00:00:12Z', Date.parse('2026-01-01T00:10:00Z'))).toBe('0 分 12 秒');
    expect(taskDuration(start, '2026-01-01T00:02:04Z', '2026-01-01T00:05:00Z', Date.parse('2026-01-01T00:10:00Z'))).toBe('2 分 04 秒');
  });
});
