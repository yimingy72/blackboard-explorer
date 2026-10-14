import { describe, expect, it } from 'vitest';
import { candidateRecords, candidateText, ctfNodeSize, deriveCtfGraph, preserveCtfPositions, type CtfGraphNode } from './graph';
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
    expect(graph.edges.map((edge) => edge.data?.relation)).toEqual(['team', 'claim', 'collaborate']);
    expect(graph.nodes.find((node) => node.id === 'challenge:C1')?.data.candidate).toBe('candidate-two');
    const reassigned = deriveCtfGraph(members, [{ ...challenge, owner_id: 'member-1' }], records);
    expect(reassigned.edges.find((edge) => edge.data?.relation === 'claim')?.source).toBe('agent:member-1');
  });

  it('keeps one shared challenge with its owner and every collaborator', () => {
    const extra = { ...members[1], id: 'member-2', display_name: '第二位队友' };
    const graph = deriveCtfGraph([...members, extra], [{ ...challenge, owner_id: 'member-1', collaborator_ids: ['lead', 'member-2'] }]);
    expect(graph.nodes.filter((node) => node.type === 'ctf-task')).toHaveLength(1);
    expect(graph.nodes.find((node) => node.id === 'challenge:C1')?.data.participantCount).toBe(3);
    expect(graph.edges.filter((edge) => edge.target === 'challenge:C1').map((edge) => [edge.source, edge.data?.relation])).toEqual([
      ['agent:member-1', 'claim'], ['agent:lead', 'collaborate'], ['agent:member-2', 'collaborate'],
    ]);
  });

  it('reports a missing owner without inventing an agent or assignment edge', () => {
    const graph = deriveCtfGraph(members, [{ ...challenge, owner_id: 'unknown-owner', collaborator_ids: [] }]);
    expect(graph.nodes.some((node) => node.id === 'agent:unknown-owner')).toBe(false);
    expect(graph.nodes.find((node) => node.id === 'challenge:C1')?.data.ownerMissing).toBe(true);
    expect(graph.edges.some((edge) => edge.data?.relation === 'claim')).toBe(false);
    expect(graph.edges.every((edge) => graph.nodes.some((node) => node.id === edge.source) && graph.nodes.some((node) => node.id === edge.target))).toBe(true);
  });

  it('retains removed members and their historical challenge relationship', () => {
    const graph = deriveCtfGraph([members[0], { ...members[1], lifecycle: 'removed' }], [{ ...challenge, owner_id: 'member-1', collaborator_ids: [] }]);
    expect(graph.nodes.find((node) => node.id === 'agent:member-1')?.data).toMatchObject({ status: 'removed', detail: '历史成员 · 会话保留' });
    expect(graph.nodes.find((node) => node.id === 'challenge:C1')?.data.ownerMissing).toBe(false);
    expect(graph.edges.find((edge) => edge.data?.relation === 'claim')?.source).toBe('agent:member-1');
  });

  it('deduplicates entities and does not also label the owner as a collaborator', () => {
    const shared = { ...challenge, collaborator_ids: ['lead', 'member-1', 'member-1'] };
    const graph = deriveCtfGraph([...members, members[1]], [shared, shared]);
    expect(graph.nodes).toHaveLength(3);
    expect(new Set(graph.nodes.map((node) => node.id)).size).toBe(graph.nodes.length);
    expect(graph.edges).toHaveLength(3);
    expect(new Set(graph.edges.map((edge) => edge.id)).size).toBe(graph.edges.length);
    expect(graph.edges.filter((edge) => edge.source === 'agent:lead' && edge.target === 'challenge:C1').map((edge) => edge.data?.relation)).toEqual(['claim']);
    expect(graph.nodes.find((node) => node.id === 'challenge:C1')?.data.participantCount).toBe(2);
  });

  it('keeps work completion separate from answer acceptance', () => {
    const completed = deriveCtfGraph(members, [{ ...challenge, work_status: 'completed', verification: { status: 'candidate', source: 'agent' } }]);
    expect(completed.nodes.find((node) => node.id === 'challenge:C1')?.data).toMatchObject({ status: 'completed', verification: 'candidate' });
    const verified = deriveCtfGraph(members, [{ ...challenge, work_status: 'in_progress', verification: { status: 'accepted', source: 'user' } }]);
    expect(verified.nodes.find((node) => node.id === 'challenge:C1')?.data).toMatchObject({ status: 'in_progress', verification: 'accepted' });
  });

  it.each([
    { source: undefined, testOnly: false, expected: 'unknown' },
    { source: 'agent', testOnly: false, expected: 'unknown' },
    { source: 'unknown', testOnly: true, expected: 'unknown' },
    { source: 'platform', testOnly: false, expected: 'accepted' },
    { source: 'user', testOnly: false, expected: 'accepted' },
    { source: 'platform', testOnly: true, expected: 'simulated' },
  ])('projects accepted source $source and testOnly $testOnly as $expected', ({ source, testOnly, expected }) => {
    const graph = deriveCtfGraph(members, [{ ...challenge, verification: { status: 'accepted', source, test_only: testOnly } }]);
    expect(graph.nodes.find((node) => node.id === 'challenge:C1')?.data.verification).toBe(expected);
    if (expected === 'unknown') expect(graph.nodes.find((node) => node.id === 'challenge:C1')?.data.verificationText).toBe('验证来源待核实');
  });

  it('uses the newest ordinary record as progress and sorts candidates by version', () => {
    const latest: CtfRecord = { ...records[0], id: 'R3', kind: 'note', body: '## 新进展\n继续分析响应', created_version: 8, verification: undefined };
    const unrelated: CtfRecord = { ...latest, id: 'R4', challenge_id: 'other', created_version: 20 };
    const graph = deriveCtfGraph(members, [challenge], [latest, records[1], unrelated, records[0]]);
    expect(graph.nodes.find((node) => node.id === 'challenge:C1')?.data).toMatchObject({ progress: '新进展 继续分析响应', recordCount: 3, candidate: 'candidate-two' });
    const empty = deriveCtfGraph(members, [challenge]);
    expect(empty.nodes.find((node) => node.id === 'challenge:C1')?.data).toMatchObject({ progress: '', recordCount: 0, candidate: null });
  });

  it('places 31 challenges and 9 members without overlapping node rectangles', () => {
    const team = [members[0], ...Array.from({ length: 8 }, (_, index) => ({ ...members[1], id: `worker-${index}` }))];
    const challenges = Array.from({ length: 31 }, (_, index) => ({
      ...challenge, id: `challenge-${index}`, owner_id: index % 5 === 0 ? null : team[index % team.length].id,
      collaborator_ids: index % 3 === 0 ? [team[(index + 1) % team.length].id] : [],
    }));
    const graph = deriveCtfGraph(team, challenges);
    expect(graph.nodes).toHaveLength(40);
    assertNoOverlap(graph.nodes);
  });
});

function assertNoOverlap(nodes: CtfGraphNode[]): void {
  for (let first = 0; first < nodes.length; first++) {
    for (let second = first + 1; second < nodes.length; second++) {
      const a = nodes[first], b = nodes[second], aSize = ctfNodeSize(a), bSize = ctfNodeSize(b);
      const intersectionWidth = Math.min(a.position.x + aSize.width, b.position.x + bSize.width) - Math.max(a.position.x, b.position.x);
      const intersectionHeight = Math.min(a.position.y + aSize.height, b.position.y + bSize.height) - Math.max(a.position.y, b.position.y);
      expect(intersectionWidth <= 0 || intersectionHeight <= 0, `${a.id} overlaps ${b.id}`).toBe(true);
    }
  }
}

describe('preserveCtfPositions', () => {
  const dragged = deriveCtfGraph(members, [challenge], records).nodes.map((node) => node.id === 'challenge:C1' ? { ...node, position: { x: 1000, y: 90 } } : node);

  it.each(['record', 'reassignment', 'challenge', 'member'] as const)('keeps dragged coordinates after a new %s and finds space for new nodes', (change) => {
    const nextMembers = change === 'member' ? [...members, { ...members[1], id: 'member-2' }] : members;
    const nextChallenges = change === 'challenge' ? [challenge, { ...challenge, id: 'C2' }] : change === 'reassignment' ? [{ ...challenge, owner_id: 'member-1' }] : [challenge];
    const nextRecords = change === 'record' ? [...records, { ...records[0], id: 'R3', created_version: 9, body: '新的进展' }] : records;
    const fresh = deriveCtfGraph(nextMembers, nextChallenges, nextRecords).nodes;
    const updated = preserveCtfPositions(fresh, dragged);
    for (const previous of dragged) expect(updated.find((node) => node.id === previous.id)?.position).toEqual(previous.position);
    expect(updated.map((node) => node.id)).toEqual(fresh.map((node) => node.id));
    if (change === 'record') expect(updated.find((node) => node.id === 'challenge:C1')?.data.progress).toBe('新的进展');
    if (change === 'challenge' || change === 'member') {
      expect(updated).toHaveLength(dragged.length + 1);
      assertNoOverlap(updated);
    }
    if (change === 'member') expect(updated.find((node) => node.id === 'agent:member-2')?.position).not.toEqual({ x: 1000, y: 90 });
    expect(dragged.find((node) => node.id === 'challenge:C1')?.position).toEqual({ x: 1000, y: 90 });
  });
});
