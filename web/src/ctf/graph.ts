import { type Edge, type Node } from '@xyflow/react';
import { challengeProgress } from './progress';
import { verificationLabel } from './view';
import { challengeState, challengeSolution, type ChallengeState } from './challengeView';
import type { CtfChallenge, CtfMember, CtfRecord } from './types';

export type CtfGraphNodeData = {
  entityId: string; label: string; role?: CtfMember['role']; status: string; detail: string;
  candidate?: string | null; verification: string; verificationText?: string;
  externalId?: string; recordCount?: number; participantCount?: number; progress?: string;
  ownerMissing?: boolean; related?: boolean; pinned?: boolean; history?: boolean; activity?: string;
  challengeState?: ChallengeState; artifactCount?: number;
  participants?: Array<{ id: string; name: string }>;
};
export type CtfGraphNode = Node<CtfGraphNodeData, 'ctf-agent' | 'ctf-task'>;
export type CtfGraphEdge = Edge<{ relation: 'team' | 'claim' | 'collaborate' | 'activity'; from?: { x: number; y: number }; to?: { x: number; y: number }; emphasized?: boolean; dimmed?: boolean; signal?: { id: string; kind: string; reverse: boolean } }>;

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
export function ctfNodeSize(node: CtfGraphNode): { width: number; height: number } {
  return node.type === 'ctf-task' ? { width: 236, height: 120 } : node.data.role === 'lead' ? { width: 216, height: 78 } : { width: 176, height: 68 };
}
function overlaps(a: CtfGraphNode, b: CtfGraphNode): boolean {
  const first = ctfNodeSize(a), second = ctfNodeSize(b), gap = 28;
  return a.position.x < b.position.x + second.width + gap && a.position.x + first.width + gap > b.position.x && a.position.y < b.position.y + second.height + gap && a.position.y + first.height + gap > b.position.y;
}
export function preserveCtfPositions(nodes: CtfGraphNode[], previous: CtfGraphNode[] = [], movable = new Set<string>(), pinned = new Set<string>()): CtfGraphNode[] {
  const old = new Map(previous.map((node) => [node.id, node]));
  const fixed = (id: string) => old.has(id) && (!movable.has(id) || pinned.has(id));
  const placed = nodes.filter((node) => fixed(node.id)).map((node) => ({ ...node, position: old.get(node.id)!.position }));
  for (const node of nodes.filter((item) => !fixed(item.id))) {
    let candidate = node;
    // Unchanged and manually pinned nodes stay fixed; only affected nodes find space.
    for (let step = 0; placed.some((item) => overlaps(candidate, item)); step++) {
      const radius = 90 * Math.sqrt(step + 1), angle = step * 2.399963229728653;
      candidate = { ...node, position: { x: node.position.x + Math.cos(angle) * radius, y: node.position.y + Math.sin(angle) * radius } };
    }
    placed.push(candidate);
  }
  const byId = new Map(placed.map((node) => [node.id, node]));
  return nodes.map((node) => byId.get(node.id)!);
}

function orbitLayout(nodes: CtfGraphNode[], challenges: CtfChallenge[]): CtfGraphNode[] {
  const lead = nodes.find((node) => node.data.role === 'lead');
  const workers = nodes.filter((node) => node.type === 'ctf-agent' && node !== lead);
  const workerIds = new Set(workers.map((node) => node.data.entityId));
  const clusters = workers.map((node) => [node.data.entityId]);
  for (const challenge of challenges.filter((item) => !item.tombstone && !['completed', 'cancelled'].includes(item.work_status))) {
    const participants = new Set([challenge.owner_id, ...challenge.collaborator_ids].filter((id): id is string => Boolean(id && workerIds.has(id))));
    const matching = clusters.filter((group) => group.some((id) => participants.has(id)));
    if (matching.length > 1) {
      matching[0].push(...matching.slice(1).flat());
      for (const group of matching.slice(1)) clusters.splice(clusters.indexOf(group), 1);
    }
  }
  const order = clusters.flat();
  const radiusX = Math.max(250, workers.length * 205 / (2 * Math.PI)), radiusY = Math.max(175, radiusX * .72);
  const angles = new Map(order.map((id, index) => [id, -Math.PI + (index + .5) * 2 * Math.PI / Math.max(1, order.length)]));
  const placed: CtfGraphNode[] = [];
  const center = (node: CtfGraphNode, x: number, y: number) => { const size = ctfNodeSize(node); return { ...node, position: { x: x - size.width / 2, y: y - size.height / 2 } }; };
  if (lead) placed.push(center(lead, 0, 0));
  for (const worker of workers) {
    const angle = angles.get(worker.data.entityId)!;
    placed.push(center(worker, Math.cos(angle) * radiusX, Math.sin(angle) * radiusY));
  }
  const byChallenge = new Map(challenges.map((item) => [item.id, item]));
  const tasks = nodes.filter((node) => node.type === 'ctf-task');
  // Put related tasks near their participants. Crowded sectors expand outwards, never into a single long column.
  for (const [index, node] of [...tasks.filter((item) => !item.data.history), ...tasks.filter((item) => item.data.history)].entries()) {
    const challenge = byChallenge.get(node.data.entityId)!;
    const participants = [...new Set([challenge.owner_id, ...challenge.collaborator_ids])].filter((id): id is string => Boolean(id && angles.has(id)));
    const ownerAngle = challenge.owner_id ? angles.get(challenge.owner_id) : undefined;
    const x = participants.reduce((sum, id) => sum + Math.cos(angles.get(id)!), 0), y = participants.reduce((sum, id) => sum + Math.sin(angles.get(id)!), 0);
    const angle = participants.length && Math.hypot(x, y) > .1 ? Math.atan2(y, x) : ownerAngle ?? -Math.PI + (index + .5) * 2 * Math.PI / Math.max(1, tasks.length);
    let candidate = node;
    for (let attempt = 0; ; attempt++) {
      const ring = Math.floor(attempt / 13), slot = attempt % 13;
      const offset = slot === 0 ? 0 : Math.ceil(slot / 2) * .22 * (slot % 2 ? 1 : -1);
      const rx = radiusX + 300 + ring * 275, ry = radiusY + 215 + ring * 180;
      candidate = center(node, Math.cos(angle + offset) * rx, Math.sin(angle + offset) * ry);
      if (!placed.some((other) => overlaps(candidate, other))) break;
    }
    placed.push(candidate);
  }
  const positioned = new Map(placed.map((node) => [node.id, node]));
  return nodes.map((node) => positioned.get(node.id) ?? node);
}
export function deriveCtfGraph(members: CtfMember[], challenges: CtfChallenge[], records: CtfRecord[] = []): { nodes: CtfGraphNode[]; edges: CtfGraphEdge[] } {
  const uniqueMembers = [...new Map(members.map((member) => [member.id, member])).values()];
  const uniqueChallenges = [...new Map(challenges.map((challenge) => [challenge.id, challenge])).values()];
  const lead = uniqueMembers.find((member) => member.role === 'lead');
  const memberIds = new Set(uniqueMembers.map((member) => member.id));
  const recordsByChallenge = new Map<string, CtfRecord[]>();
  for (const record of records) {
    const related = recordsByChallenge.get(record.challenge_id) ?? [];
    related.push(record); recordsByChallenge.set(record.challenge_id, related);
  }
  const nodes: CtfGraphNode[] = uniqueMembers.map((member) => {
    const position = { x: 0, y: 0 };
    const status = member.lifecycle === 'active' ? member.run_state : member.lifecycle;
    return { id: `agent:${member.id}`, type: 'ctf-agent', position, data: { entityId: member.id, label: member.display_name, role: member.role, status, detail: member.lifecycle === 'removed' ? '历史成员 · 会话保留' : member.lifecycle === 'stopped' ? '已停止 · 等待明确恢复' : member.role === 'lead' ? '团队协调' : '协作成员', verification: '', participantCount: uniqueChallenges.filter((challenge) => challenge.owner_id === member.id || challenge.collaborator_ids.includes(member.id)).length } };
  });
  for (const challenge of uniqueChallenges) {
    const people = [...new Set([challenge.owner_id, ...challenge.collaborator_ids].filter((id): id is string => Boolean(id)))];
    const position = { x: 0, y: 0 };
    const related = recordsByChallenge.get(challenge.id) ?? [];
    const progress = challengeProgress(related, challenge.id), candidates = candidateRecords(related, challenge.id).sort((a, b) => a.created_version - b.created_version);
    const verification = challenge.verification ?? {};
    const accepted = verification.status === 'accepted' && ['platform', 'user'].includes(String(verification.source));
    const solution = challengeSolution(challenge, related).current;
    nodes.push({ id: `challenge:${challenge.id}`, type: 'ctf-task', position, data: { entityId: challenge.id, label: challenge.title, externalId: challenge.external_id ?? challenge.id.slice(0, 8), status: challenge.tombstone ? 'archived' : challenge.work_status, challengeState: challengeState(challenge), participants: people.map(id => ({ id, name: uniqueMembers.find(member => member.id === id)?.display_name ?? id })), artifactCount: new Set(related.flatMap(record => record.artifact_refs ?? []).map(artifact => artifact.id)).size, detail: challenge.description || challenge.requirements || '暂无题目说明', progress: solution?.solution?.conclusion ?? progress.summary, recordCount: progress.count, participantCount: people.length, history: Boolean(challenge.tombstone || ['completed', 'cancelled'].includes(challenge.work_status)), ownerMissing: Boolean(challenge.owner_id && !memberIds.has(challenge.owner_id)), candidate: candidates.length ? candidateText(candidates.at(-1)!) : null, verification: accepted ? verification.test_only ? 'simulated' : 'accepted' : verification.status === 'accepted' ? 'unknown' : String(verification.status ?? 'unknown'), verificationText: verification.status === 'accepted' && !accepted ? '验证来源待核实' : verificationLabel(verification) } });
  }
  const positioned = orbitLayout(nodes, uniqueChallenges);
  const positionedById = new Map(positioned.map((node) => [node.id, node]));
  const edges: CtfGraphEdge[] = [];
  function add(relation: 'team' | 'claim' | 'collaborate', sourceId: string, targetId: string) {
    const source = positionedById.get(sourceId), target = positionedById.get(targetId);
    if (!source || !target) return;
    const sourceSize = ctfNodeSize(source), targetSize = ctfNodeSize(target);
    const dx = target.position.x + targetSize.width / 2 - source.position.x - sourceSize.width / 2;
    const dy = target.position.y + targetSize.height / 2 - source.position.y - sourceSize.height / 2;
    const horizontal = Math.abs(dx) > Math.abs(dy) * 1.2;
    const from = horizontal ? dx > 0 ? 'right' : 'left' : dy > 0 ? 'bottom' : 'top';
    const to = horizontal ? dx > 0 ? 'left' : 'right' : dy > 0 ? 'top' : 'bottom';
    edges.push({ id: `${relation}:${source.data.entityId}:${target.data.entityId}`, source: sourceId, target: targetId, sourceHandle: `source:${from}`, targetHandle: `target:${to}`, type: 'default', data: { relation }, selectable: false, focusable: false, style: { stroke: '#b8c7dc', strokeWidth: relation === 'team' ? 1 : 1.2, strokeDasharray: relation === 'collaborate' ? '5 5' : undefined }, markerEnd: undefined });
  }
  // Team membership is structural; assignment history comes from saved events.
  if (lead) for (const member of uniqueMembers) if (member.role !== 'lead') add('team', `agent:${lead.id}`, `agent:${member.id}`);
  for (const challenge of uniqueChallenges) {
    if (challenge.owner_id) add('claim', `agent:${challenge.owner_id}`, `challenge:${challenge.id}`);
    for (const id of new Set(challenge.collaborator_ids)) if (id !== challenge.owner_id) add('collaborate', `agent:${id}`, `challenge:${challenge.id}`);
  }
  return { nodes: positioned, edges };
}

export function connectionPoint(node: CtfGraphNode, toward: { x: number; y: number }) {
  const { width, height } = ctfNodeSize(node);
  const center = { x: node.position.x + width / 2, y: node.position.y + height / 2 };
  const dx = toward.x - center.x, dy = toward.y - center.y;
  if (!dx && !dy) return center;
  const rx = width / 2 + 3, ry = height / 2 + 3;
  let ratio = Math.min(dx ? rx / Math.abs(dx) : Infinity, dy ? ry / Math.abs(dy) : Infinity);
  if (node.type === 'ctf-agent') {
    const cap = Math.max(0, rx - ry), top = dy ? ry / Math.abs(dy) : Infinity;
    if (Math.abs(dx * top) <= cap) ratio = top;
    else {
      const cx = Math.sign(dx) * cap, squared = dx * dx + dy * dy;
      ratio = (dx * cx + Math.sqrt((dx * cx) ** 2 - squared * (cx * cx - ry * ry))) / squared;
    }
  }
  return { x: center.x + dx * ratio, y: center.y + dy * ratio };
}
