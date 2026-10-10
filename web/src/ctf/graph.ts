import { MarkerType, type Edge, type Node } from '@xyflow/react';
import { verificationLabel } from './view';
import type { CtfChallenge, CtfMember, CtfRecord } from './types';

export type CtfGraphNodeData = {
  entityId: string;
  label: string;
  role?: CtfMember['role'];
  status: string;
  detail: string;
  candidate?: string | null;
  verification: string;
  verificationText?: string;
};

export type CtfGraphNode = Node<CtfGraphNodeData, 'ctf-agent' | 'ctf-task'>;
export type CtfGraphEdge = Edge<{ relation: 'claim' | 'collaborate' }>;

export function candidateRecords(records: CtfRecord[], challengeId: string): CtfRecord[] {
  return records.filter((record) => record.challenge_id === challengeId && record.verification?.status === 'candidate');
}

export function candidateText(record: CtfRecord): string {
  const verification = record.verification ?? {};
  for (const key of ['candidate', 'flag', 'answer', 'value']) {
    if (typeof verification[key] === 'string' && verification[key].trim()) return verification[key].trim();
  }
  return record.body.trim() || '候选答案已登记';
}

function latestCandidate(records: CtfRecord[], challengeId: string): string | null {
  const candidates = candidateRecords(records, challengeId);
  return candidates.length ? candidateText(candidates[candidates.length - 1]) : null;
}

function statusDetail(member: CtfMember): string {
  if (member.lifecycle === 'removed') return '历史成员 · 会话保留';
  if (member.lifecycle === 'stopped') return '已停止 · 等待明确恢复';
  return member.run_state === 'running' ? '工作中 · 实时活动' : '等待任务';
}

export function deriveCtfGraph(members: CtfMember[], challenges: CtfChallenge[], records: CtfRecord[] = []): { nodes: CtfGraphNode[]; edges: CtfGraphEdge[] } {
  const nodes: CtfGraphNode[] = [];
  const edges: CtfGraphEdge[] = [];
  members.forEach((member, index) => nodes.push({
    id: `agent:${member.id}`,
    type: 'ctf-agent',
    position: { x: 48, y: 48 + index * 150 },
    data: { entityId: member.id, label: member.display_name, role: member.role, status: member.lifecycle === 'active' ? member.run_state : member.lifecycle, detail: statusDetail(member), verification: member.role === 'lead' ? '协调中' : '协作成员' },
  }));
  challenges.forEach((challenge, index) => {
    const candidate = latestCandidate(records, challenge.id);
    nodes.push({
      id: `challenge:${challenge.id}`,
      type: 'ctf-task',
      position: { x: 430, y: 36 + index * 176 },
      data: { entityId: challenge.id, label: challenge.title, status: challenge.tombstone ? '已归档' : challenge.work_status, detail: challenge.description || challenge.requirements || '暂无题目说明', candidate, verification: challenge.verification?.status === 'accepted' && challenge.verification.test_only ? 'simulated' : String(challenge.verification?.status ?? 'unknown'), verificationText: verificationLabel(challenge.verification ?? {}) },
    });
    if (challenge.owner_id && members.some((member) => member.id === challenge.owner_id)) {
      edges.push({ id: `claim:${challenge.owner_id}:${challenge.id}`, source: `agent:${challenge.owner_id}`, target: `challenge:${challenge.id}`, type: 'smoothstep', data: { relation: 'claim' }, animated: challenge.work_status === 'in_progress', style: { stroke: 'var(--color-primary)', strokeWidth: 1.7, strokeDasharray: '5 4' }, markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--color-primary)' } });
    }
    for (const collaboratorId of challenge.collaborator_ids) {
      if (!members.some((member) => member.id === collaboratorId)) continue;
      edges.push({ id: `collaborate:${collaboratorId}:${challenge.id}`, source: `agent:${collaboratorId}`, target: `challenge:${challenge.id}`, type: 'smoothstep', data: { relation: 'collaborate' }, style: { stroke: 'var(--color-info)', strokeWidth: 1.3, strokeDasharray: '2 5' }, markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--color-info)' } });
    }
  });
  return { nodes, edges };
}
