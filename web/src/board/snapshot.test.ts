import { describe, expect, it } from 'vitest';

import { boardFromSnapshot } from './snapshot';

describe('board snapshot', () => {
  it('hydrates the current board without replaying the full event log', () => {
    const { state, version } = boardFromSnapshot({
      task: {
        version: 42,
        goal: 'Find cause',
        status: 'running',
        budget: { max_cost: '10' },
        usage: { cost: '1.25' },
        acceptance: [{ id: 'A1', desc: 'Show cause' }],
        acceptance_state: { A1: { status: 'met', evidence_facts: ['F1'], judged_version: 40 } },
        run_number: 2,
        active_seconds: 30,
        closing_reason: 'accepted',
      },
      facts: {
        F1: {
          id: 'F1', version: 20, kind: 'observation', statement: 'Observed', author: 'agent-1',
          provenance: 'tool_backed', status: 'disputed', relied_by: 2, derived_from: [],
          disputes: [], satisfies: ['A1'], evidence: [{ uri: 'evidence/1' }],
        },
      },
      intents: {
        I1: {
          id: 'I1', version: 21, statement: 'Check', based_on: ['F1'], expected: 'Result',
          method: 'Run', relates_to: ['A1'], author: 'agent-1', status: 'closed', holder: null,
          result: 'confirmed', closed_by: 'F1', result_facts: ['F1'], attempts: 1, notes: [],
        },
      },
      agents: {
        'agent-1': {
          id: 'agent-1', task_type: 'explore', is_seed: true, status: 'finished', steps: 4,
          context_tokens: 500, usage: { cost: '1.25' }, intent_id: null, end_reason: 'normal',
        },
      },
      last_change_version: 21,
    });
    expect(version).toBe(42);
    expect(state.task).toMatchObject({ goal: 'Find cause', runNumber: 2, activeSeconds: 30, closingReason: 'accepted' });
    expect(state.acceptance.A1).toMatchObject({ status: 'met', evidence_facts: ['F1'] });
    expect(state.facts.F1).toMatchObject({ status: 'disputed', reliedBy: 2 });
    expect(state.intents.I1).toMatchObject({ status: 'closed', closedBy: 'F1', resultFacts: ['F1'] });
    expect(state.agents['agent-1']).toMatchObject({ status: 'finished', steps: 4, usage: { cost: 1.25 } });
    expect(state.toolCalls).toEqual({});
  });
});
