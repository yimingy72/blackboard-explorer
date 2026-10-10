import { describe, expect, it } from 'vitest';
import { candidateRecords, candidateText, deriveCtfGraph } from './graph';
import type { CtfChallenge, CtfMember, CtfRecord } from './types';

const members: CtfMember[] = [
  { id: 'lead', display_name: 'Lead', role: 'lead', lifecycle: 'active', run_state: 'idle', generation: 1 },
  { id: 'member-1', display_name: '队友', role: 'teammate', lifecycle: 'active', run_state: 'running', generation: 1 },
];
const challenge: CtfChallenge = {
  id: 'C1', title: '模拟题', description: '合成题目', owner_id: 'lead', collaborator_ids: ['member-1'], work_status: 'in_progress', revision: 1, verification: { status: 'candidate' }, target: {},
};
const records: CtfRecord[] = [
  { id: 'R1', challenge_id: 'C1', author_id: 'lead', kind: 'verification', body: '候选一', created_version: 1, artifact_refs: [], verification: { status: 'candidate', candidate: 'candidate-one' } },
  { id: 'R2', challenge_id: 'C1', author_id: 'member-1', kind: 'verification', body: '候选二', created_version: 2, artifact_refs: [], verification: { status: 'candidate', candidate: 'candidate-two' } },
];

describe('CTF graph projection', () => {
  it('keeps all candidate records and projects claim/collaboration edges', () => {
    expect(candidateRecords(records, 'C1')).toHaveLength(2);
    expect(candidateText(records[1])).toBe('candidate-two');
    const graph = deriveCtfGraph(members, [challenge], records);
    expect(graph.nodes.map((node) => node.id)).toEqual(['agent:lead', 'agent:member-1', 'challenge:C1']);
    expect(graph.edges.map((edge) => edge.data?.relation)).toEqual(['claim', 'collaborate']);
    expect(graph.nodes.find((node) => node.id === 'challenge:C1')?.data.candidate).toBe('candidate-two');
    const reassigned = deriveCtfGraph(members, [{ ...challenge, owner_id: 'member-1' }], records);
    expect(reassigned.edges.find((edge) => edge.data?.relation === 'claim')?.source).toBe('agent:member-1');
  });
});
