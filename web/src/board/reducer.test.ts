import { describe, expect, it } from 'vitest';

import { orderedEvents, reduce } from './reducer';
import type { BoardEvent, BoardEventType } from './types';

const tid = '11111111-1111-4111-8111-111111111111';

function event(version: number, type: BoardEventType, payload: Record<string, unknown>): BoardEvent {
  return {
    version,
    task_id: tid,
    type,
    actor: 'scheduler',
    created_at: '2026-01-01T00:00:00Z',
    payload,
  };
}

const created = event(1, 'task.created', {
  goal: 'Find cause',
  domain_context: null,
  acceptance: [{ id: 'A1', desc: 'Show cause' }],
  acceptance_state: { A1: { status: 'unmet', reason: null, missing: null, evidence_facts: [] } },
  budget: { max_cost: 10 },
  usage: {},
});

describe('reduce', () => {
  it('orders and deduplicates versions without changing inputs', () => {
    const facts = event(4, 'fact.posted', {
      id: 'F1', kind: 'observation', statement: 'Gateway returned 502', author: 'agent-1',
      provenance: 'self_reported', derived_from: [], disputes: [], satisfies: [], evidence: [],
    });
    const input = [facts, created, facts, event(2, 'task.provisioning', { status: 'provisioning' })];
    const copy = [...input];
    expect(orderedEvents(input).map((item) => item.version)).toEqual([1, 2, 4]);
    const board = reduce(input);
    expect(input).toEqual(copy);
    expect(board.task?.status).toBe('provisioning');
    expect(board.task?.version).toBe(4);
    expect(Object.keys(board.facts)).toEqual(['F1']);
  });

  it('follows backend task, intent, dispute, acceptance, and agent events', () => {
    const events: BoardEvent[] = [
      created,
      event(2, 'task.running', { status: 'running' }),
      event(3, 'agent.spawned', { id: 'agent-1', task_type: 'explore', is_seed: true }),
      event(4, 'agent.spawned', { id: 'agent-2', task_type: 'explore' }),
      event(5, 'fact.posted', {
        id: 'F1', kind: 'observation', statement: 'Gateway returned 502', author: 'agent-1',
        provenance: 'self_reported', derived_from: [], disputes: [], satisfies: [], evidence: [],
      }),
      event(6, 'intent.posted', {
        id: 'I1', statement: 'Trace upstream', based_on: ['F1'], expected: 'cause',
        method: 'logs', relates_to: ['A1'], author: 'agent-1', claim: false,
      }),
      event(7, 'intent.claimed', { intent_id: 'I1', holder: 'agent-2' }),
      event(8, 'agent.progress', { agent_id: 'agent-2', steps: 2, context_tokens: 500, usage: { output_tokens: 10 } }),
      event(9, 'fact.posted', {
        id: 'F2', kind: 'inference', statement: 'Pool exhausted', author: 'agent-2',
        provenance: 'tool_backed', derived_from: ['F1'], disputes: [], resolves: 'I1',
        result: 'confirmed', satisfies: ['A1'], evidence: [],
      }),
      event(10, 'intent.closed', { intent_id: 'I1', result: 'confirmed', by: 'F2' }),
      event(11, 'acceptance.judged', { verdicts: [{ id: 'A1', verdict: 'met', reason: 'trace', evidence_facts: ['F2'] }], judge_from_version: 10 }),
      event(12, 'fact.posted', {
        id: 'F3', kind: 'observation', statement: 'Trace stale', author: 'agent-1',
        provenance: 'self_reported', derived_from: [], disputes: ['F2'], evidence: [],
      }),
      event(13, 'fact.disputed', { fact_id: 'F2', by: 'F3' }),
      event(14, 'acceptance.reverted', { id: 'A1', fact_id: 'F2' }),
      event(15, 'fact.posted', {
        id: 'F4', kind: 'observation', statement: 'Fresh trace', author: 'agent-2',
        provenance: 'self_reported', derived_from: [], disputes: ['F3'], evidence: [],
      }),
      event(16, 'fact.disputed', { fact_id: 'F3', by: 'F4' }),
      event(17, 'fact.undisputed', { fact_id: 'F2', by: 'F4' }),
      event(18, 'agent.finished', { agent_id: 'agent-2', end_reason: 'normal' }),
      event(19, 'task.closing', { status: 'closing' }),
      event(20, 'task.report', { uri: 'reports/task.md' }),
      event(21, 'task.finished', { status: 'finished' }),
    ];
    const board = reduce([...events].reverse());
    expect(board.task).toMatchObject({ status: 'finished', report_uri: 'reports/task.md', version: 21 });
    expect(board.facts.F1.reliedBy).toBe(1);
    expect(board.facts.F2.status).toBe('proposed');
    expect(board.facts.F3.status).toBe('disputed');
    expect(board.intents.I1).toMatchObject({ status: 'closed', holder: null, result: 'confirmed', resultFacts: ['F2'] });
    expect(board.agents['agent-2']).toMatchObject({ status: 'finished', steps: 2, contextTokens: 500, intentId: null });
    expect(board.acceptance.A1).toMatchObject({ status: 'unmet', evidence_facts: [], missing: '支撑事实 F2 被争议' });
  });

  it('counts releases and applies a later judgment without recomputing disputes', () => {
    const board = reduce([
      created,
      event(2, 'agent.spawned', { id: 'agent-1', task_type: 'explore' }),
      event(3, 'intent.posted', { id: 'I1', statement: 'Try logs', based_on: [], author: 'agent-1', claim: true }),
      event(4, 'intent.released', { intent_id: 'I1', holder: 'agent-1', counted: true }),
      event(5, 'agent.conclude_requested', { agent_id: 'agent-1', reason: 'limit' }),
      event(6, 'agent.progress', { agent_id: 'agent-1', grace_left: 2 }),
      event(7, 'acceptance.judged', { verdicts: [{ id: 'A1', verdict: 'met', reason: 'confirmed', evidence_facts: ['F1'] }], judge_from_version: 6 }),
    ]);
    expect(board.intents.I1).toMatchObject({ status: 'open', attempts: 1, holder: null });
    expect(board.agents['agent-1']).toMatchObject({ status: 'concluding', graceCallsLeft: 2, intentId: null });
    expect(board.acceptance.A1).toMatchObject({ status: 'met', evidence_facts: ['F1'], judged_version: 6 });
  });

  it('keeps the terminal status and exposes an archived workspace once', () => {
    const archived = event(3, 'task.archived', {
      uri: `workspace/${tid}.tar.zst`, size: 1234, fallback: 'agents-only',
    });
    const board = reduce([archived, event(2, 'task.finished', { status: 'finished' }), created, archived]);
    expect(board.task).toMatchObject({
      status: 'finished', workspace_uri: `workspace/${tid}.tar.zst`, version: 3,
    });
    expect(orderedEvents([archived, archived]).length).toBe(1);
  });
});
