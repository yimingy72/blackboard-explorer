import { expect, it } from 'vitest';
import { reduceBoard } from './reducer';
import { taskEndExplanation } from './view';
import type { BoardEvent, BoardEventType } from './types';

function event(version: number, type: BoardEventType, payload: Record<string, unknown>): BoardEvent {
  return { version, type, payload, actor: 'agent-1', task_id: 'task', created_at: '2026-09-25T00:00:00Z' };
}
const created = event(1, 'task.created', { goal: 'Investigate', budget: { max_cost: 1 }, acceptance: [{ id: 'A1', desc: 'Proof' }] });

it('reconstructs notes, receipts, and tool calls only after their event version', () => {
  const events = [created, event(2, 'agent.spawned', { id: 'agent-1', task_type: 'explore' }), event(3, 'intent.posted', { id: 'I1', author: 'agent-1', claim: true }), event(4, 'tool_call.recorded', { id: 'c1', agent_id: 'agent-1', tool: 'get', args: { id: 'F1' }, result_uri: 'toolcalls/task/c1.txt' }), event(5, 'intent.released', { intent_id: 'I1', holder: 'agent-1', note: 'Future handoff', counted: false }), event(6, 'agent.finished', { agent_id: 'agent-1', end_reason: 'normal', receipt: { accepted: true, data: { note: 'Future receipt' } } })];
  const past = reduceBoard(events.filter((item) => item.version <= 3));
  expect(past.intents.I1.notes).toEqual([]);
  expect(past.agents['agent-1'].receipt).toBeNull();
  expect(past.toolCalls).toEqual({});
  const present = reduceBoard(events);
  expect(present.intents.I1.notes?.[0].text).toBe('Future handoff');
  expect(present.agents['agent-1'].receipt).toEqual({ accepted: true, data: { note: 'Future receipt' } });
  expect(present.toolCalls?.c1.resultUri).toBe('toolcalls/task/c1.txt');
});

it('preserves the closing cause after the task.finished event', () => {
  const state = reduceBoard([created, event(2, 'task.closing', { reason: 'accepted' }), event(3, 'task.finished', { reason: null })]);
  expect(taskEndExplanation(state)).toContain('验收条件已全部满足');
});
