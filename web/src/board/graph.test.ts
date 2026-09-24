import { MarkerType } from '@xyflow/react';
import { describe, expect, it } from 'vitest';

import { deriveGraph } from './graph';
import type { BoardState } from './types';

const state: BoardState = {
  task: {
    goal: 'Find cause', status: 'running', domain_context: null,
    acceptance: [{ id: 'A1', desc: 'Show cause' }], budget: {}, usage: {},
    report_uri: null, fail_reason: null, version: 11,
  },
  acceptance: {
    A1: { id: 'A1', desc: 'Show cause', status: 'unmet', reason: null,
      missing: null, evidence_facts: [], judged_version: null },
  },
  facts: {
    F1: { id: 'F1', version: 2, kind: 'observation', statement: 'Gateway failed', author: 'agent-1',
      provenance: 'self_reported', status: 'proposed', reliedBy: 1, derivedFrom: [],
      disputes: [], resolves: null, result: null, satisfies: [], evidence: [] },
    F2: { id: 'F2', version: 7, kind: 'inference', statement: 'Pool exhausted', author: 'agent-2',
      provenance: 'tool_backed', status: 'disputed', reliedBy: 0, derivedFrom: ['F1'],
      disputes: [], resolves: 'I1', result: 'confirmed', satisfies: ['A1'], evidence: [] },
    F3: { id: 'F3', version: 8, kind: 'observation', statement: 'Trace stale', author: 'agent-1',
      provenance: 'self_reported', status: 'proposed', reliedBy: 0, derivedFrom: [],
      disputes: ['F2'], resolves: null, result: null, satisfies: [], evidence: [] },
  },
  intents: {
    I1: { id: 'I1', version: 4, statement: 'Trace upstream', basedOn: ['F2'], expected: 'cause',
      method: 'logs', relatesTo: ['A1'], retryOf: null, author: 'agent-1', status: 'claimed',
      holder: 'agent-1', result: null, closedBy: null, resultFacts: [], attempts: 1 },
    I2: { id: 'I2', version: 10, statement: 'Retry trace', basedOn: ['F1'], expected: 'cause',
      method: 'metrics', relatesTo: ['A1'], retryOf: 'I1', author: 'agent-2', status: 'open',
      holder: null, result: null, closedBy: null, resultFacts: [], attempts: 0 },
  },
  agents: {
    'agent-1': { id: 'agent-1', taskType: 'explore', isSeed: true, closeMode: null,
      status: 'running', steps: 2, contextTokens: 300, intentId: 'I1', usage: {},
      lastSeenVersion: 9, graceCallsLeft: null, endReason: null },
    'agent-2': { id: 'agent-2', taskType: 'explore', isSeed: false, closeMode: null,
      status: 'finished', steps: 3, contextTokens: 400, intentId: null, usage: {},
      lastSeenVersion: 9, graceCallsLeft: null, endReason: 'normal' },
  },
};

describe('deriveGraph', () => {
  it('emits semantic nodes and all six directed relationship edges', () => {
    const { nodes, edges } = deriveGraph(state);
    expect(nodes.map((node) => [node.id, node.type])).toEqual([
      ['goal', 'goal'], ['F1', 'fact'], ['F2', 'fact'], ['F3', 'fact'],
      ['I1', 'intent'], ['I2', 'intent'], ['agent-1', 'agent'],
    ]);
    expect(nodes.find((node) => node.id === 'goal')?.position.x).toBe(0);
    expect(nodes.find((node) => node.id === 'goal')?.data.acceptance).toEqual([{ id: 'A1', status: 'unmet' }]);
    expect(nodes.find((node) => node.id === 'F2')?.data).toMatchObject({
      status: 'disputed', kind: 'inference', provenance: 'tool_backed', version: 7,
    });
    expect(nodes.find((node) => node.id === 'I1')?.data).toMatchObject({
      status: 'claimed', holder: 'agent-1', attempts: 1, version: 4,
    });
    expect(nodes.find((node) => node.id === 'agent-1')?.data).toMatchObject({
      status: 'running', taskType: 'explore', isSeed: true, intentId: 'I1',
    });
    expect(new Set(edges.map((edge) => edge.data?.relation))).toEqual(new Set([
      'derived_from', 'based_on', 'resolves', 'disputes', 'retry_of', 'claim',
    ]));
    expect(edges.find((edge) => edge.data?.relation === 'derived_from')).toMatchObject({ source: 'F1', target: 'F2' });
    expect(edges.find((edge) => edge.data?.relation === 'resolves')).toMatchObject({ source: 'I1', target: 'F2' });
    expect(edges.find((edge) => edge.data?.relation === 'disputes')).toMatchObject({ source: 'F3', target: 'F2' });
    expect(edges.every((edge) =>
      typeof edge.markerEnd === 'object' && edge.markerEnd.type === MarkerType.ArrowClosed,
    )).toBe(true);
    expect(edges.every((edge) => String(edge.style?.stroke).startsWith('var(--color-'))).toBe(true);
  });
});
