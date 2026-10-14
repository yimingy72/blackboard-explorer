import type { CtfEvent, CtfMember, CtfRecord } from './types';
import { workLabels } from './view';

export type CtfChallengeProgress = { latest: CtfRecord | null; count: number; summary: string };

type TimelineBase = {
  id: string;
  version: number;
  createdAt: string | undefined;
  actorId: string;
  title: string;
  detail: string;
};

export type CtfTimelineEntry =
  | (TimelineBase & { type: 'record'; record: CtfRecord })
  | (TimelineBase & {
      type: 'assignment';
      beforeOwner: string | null;
      afterOwner: string | null;
      addedCollaborators: string[];
      removedCollaborators: string[];
    })
  | (TimelineBase & { type: 'status'; beforeStatus: string; afterStatus: string })
  | (TimelineBase & { type: 'created' });

function plainText(text: string): string {
  return text
    .replace(/^\s*```[^\n]*$/gm, '')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)/gm, '')
    .replace(/(\*\*|__|~~|`)(.*?)\1/g, '$2')
    .replace(/\*([^*\n]+)\*/g, '$1')
    .replace(/\s+/g, ' ')
    .trim();
}

function recordSummary(record: CtfRecord): string {
  return record.progress?.summary ?? (record.solution?.conclusion ? plainText(record.solution.conclusion) : plainText(record.body) || (typeof record.verification?.summary === 'string' ? plainText(record.verification.summary) : ''));
}

function relatedRecords(records: CtfRecord[], challengeId: string): CtfRecord[] {
  return records.filter((record) => record.challenge_id === challengeId).sort((a, b) => a.created_version - b.created_version);
}

export function challengeProgress(records: CtfRecord[], challengeId: string): CtfChallengeProgress {
  const related = relatedRecords(records, challengeId).filter(record => record.kind !== 'solution');
  const latest = related.at(-1) ?? null;
  return { latest, count: related.length, summary: latest ? recordSummary(latest) : '' };
}

function recordTitle(record: CtfRecord): string {
  if (record.progress) return record.progress.title;
  if (record.solution) return '提交解题流程';
  const heading = record.body.match(/^\s*#{1,6}\s+(.+)$/m)?.[1];
  const firstSentence = recordSummary(record).match(/^[^。！？!?\n]+[。！？!?]?/)?.[0];
  const labels: Record<string, string> = { note: '进展记录', correction: '更正记录', help_request: '协作求助', artifact: '产物记录', verification: '验证记录', target: '目标操作' };
  return heading ? plainText(heading) : firstSentence || labels[record.kind] || '共享记录';
}

type AssignmentSnapshot = { id: string; title?: string; owner_id: string | null; collaborator_ids: string[]; work_status?: string };

function assignmentSnapshot(value: unknown): AssignmentSnapshot | null {
  if (!value || typeof value !== 'object') return null;
  const snapshot = value as Record<string, unknown>;
  if (typeof snapshot.id !== 'string' || !(snapshot.owner_id === null || typeof snapshot.owner_id === 'string') || !Array.isArray(snapshot.collaborator_ids) || !snapshot.collaborator_ids.every((id) => typeof id === 'string')) return null;
  return { id: snapshot.id, title: typeof snapshot.title === 'string' ? snapshot.title : undefined, owner_id: snapshot.owner_id, collaborator_ids: [...new Set(snapshot.collaborator_ids)], work_status: typeof snapshot.work_status === 'string' ? snapshot.work_status : undefined };
}

export function challengeTimeline(challengeId: string, records: CtfRecord[], events: CtfEvent[], members: CtfMember[]): CtfTimelineEntry[] {
  const names = new Map(members.map((member) => [member.id, member.display_name]));
  const name = (id: string | null) => id === null ? '未分配' : names.get(id) ?? id;
  const entries: CtfTimelineEntry[] = relatedRecords(records, challengeId).map((record) => ({
    type: 'record', id: `record:${record.id}`, version: record.created_version, createdAt: record.created_at,
    actorId: record.author_id, title: recordTitle(record), detail: recordSummary(record), record,
  }));
  let previous: AssignmentSnapshot | null = null;
  const orderedEvents = [...new Map(events.map((event) => [event.version, event])).values()].sort((a, b) => a.version - b.version);
  for (const event of orderedEvents) {
    if (event.type !== 'ctf.challenge.created' && event.type !== 'ctf.challenge.updated') continue;
    const current = assignmentSnapshot(event.payload.result);
    if (!current || current.id !== challengeId) {
      // An incomplete same-challenge event cannot provide a safe comparison baseline.
      if (event.object_id === challengeId) previous = null;
      continue;
    }
    const base = { id: `event:${event.version}`, version: event.version, createdAt: event.created_at, actorId: event.actor ?? 'unknown' };
    if (event.type === 'ctf.challenge.created') {
      const initial = [current.title ?? ''];
      if (current.owner_id) initial.push(`创建时主责：${name(current.owner_id)}`);
      if (current.collaborator_ids.length) initial.push(`创建时协作：${current.collaborator_ids.map(name).join('、')}`);
      entries.push({ ...base, type: 'created', title: '创建题目', detail: initial.filter(Boolean).join('；') });
    } else if (previous) {
      const before = previous;
      const ownerChanged = before.owner_id !== current.owner_id;
      const addedCollaborators = current.collaborator_ids.filter((id) => !before.collaborator_ids.includes(id));
      const removedCollaborators = before.collaborator_ids.filter((id) => !current.collaborator_ids.includes(id));
      const collaboratorsChanged = addedCollaborators.length > 0 || removedCollaborators.length > 0;
      const statusChanged = before.work_status !== undefined && current.work_status !== undefined && before.work_status !== current.work_status;
      const statusDetail = statusChanged ? `工作状态：${workLabels[before.work_status!] ?? before.work_status} → ${workLabels[current.work_status!] ?? current.work_status}` : '';
      if (ownerChanged || collaboratorsChanged) {
        const details: string[] = [];
        if (ownerChanged) details.push(`负责人：${name(before.owner_id)} → ${name(current.owner_id)}`);
        if (addedCollaborators.length) details.push(`加入协作：${addedCollaborators.map(name).join('、')}`);
        if (removedCollaborators.length) details.push(`退出协作：${removedCollaborators.map(name).join('、')}`);
        if (statusChanged) details.push(statusDetail);
        entries.push({ ...base, type: 'assignment', title: ownerChanged ? collaboratorsChanged ? '协作分配调整' : '负责人调整' : '协作者调整', detail: details.join('；'), beforeOwner: before.owner_id, afterOwner: current.owner_id, addedCollaborators, removedCollaborators });
      } else if (statusChanged) {
        entries.push({ ...base, id: `${base.id}:status`, type: 'status', title: current.work_status === 'completed' ? '工作交付状态更新' : '工作状态更新', detail: statusDetail, beforeStatus: before.work_status!, afterStatus: current.work_status! });
      }
    }
    previous = current;
  }
  return entries.sort((a, b) => a.version - b.version);
}
