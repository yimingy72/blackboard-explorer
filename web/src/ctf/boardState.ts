import type { CtfChallenge, CtfMember, CtfRecord } from './types';

import { loadCanvasState, saveCanvasState, type CanvasViewState } from '../components/canvasState';
export type BoardState = CanvasViewState;
export type BoardSignal = {
  id: string; source: string; target: string;
  kind: 'claim' | 'progress' | 'completed' | 'accepted'; reverse: boolean;
};
type ChallengeSnapshot = {
  ownerId: string | null; participants: string[]; status: string;
  revision: number; accepted: boolean; archived: boolean;
};
export type BoardSnapshot = {
  challenges: Map<string, ChallengeSnapshot>;
  records: Map<string, { challengeId: string; authorId: string; version: number }>;
  maxRecordVersion: number;
};

export function loadBoardState(taskId: string, storage?: Pick<Storage, 'getItem'>): BoardState | null {
  return loadCanvasState('ctf-board', taskId, storage);
}
export function saveBoardState(taskId: string, value: BoardState, storage?: Pick<Storage, 'setItem'>): boolean {
  return saveCanvasState('ctf-board', taskId, value, storage);
}

function trustedAccepted(verification: Record<string, unknown>): boolean {
  return verification.status === 'accepted' && !verification.test_only && (verification.source === 'platform' || verification.source === 'user');
}

export function snapshotBoard(challenges: CtfChallenge[], records: CtfRecord[]): BoardSnapshot {
  const snapshot: BoardSnapshot = { challenges: new Map(), records: new Map(), maxRecordVersion: 0 };
  for (const challenge of challenges) {
    if ((snapshot.challenges.get(challenge.id)?.revision ?? -1) > challenge.revision) continue;
    snapshot.challenges.set(challenge.id, {
      ownerId: challenge.owner_id,
      participants: [...new Set([challenge.owner_id, ...challenge.collaborator_ids].filter((id): id is string => Boolean(id)))].sort(),
      status: challenge.work_status, revision: challenge.revision,
      accepted: trustedAccepted(challenge.verification ?? {}), archived: Boolean(challenge.tombstone),
    });
  }
  for (const record of records) {
    if (!Number.isSafeInteger(record.created_version) || record.created_version < 0) continue;
    snapshot.maxRecordVersion = Math.max(snapshot.maxRecordVersion, record.created_version);
    if ((snapshot.records.get(record.id)?.version ?? -1) > record.created_version) continue;
    snapshot.records.set(record.id, { challengeId: record.challenge_id, authorId: record.author_id, version: record.created_version });
  }
  return snapshot;
}

export function diffBoard(before: BoardSnapshot, after: BoardSnapshot, members: CtfMember[]): { movedIds: string[]; signals: BoardSignal[] } {
  const memberIds = new Set(members.map((member) => member.id));
  const movedIds = new Set<string>();
  const signals = new Map<string, BoardSignal>();
  function signal(id: string, memberId: string | null, challengeId: string, kind: BoardSignal['kind'], reverse = false) {
    if (!memberId || !memberIds.has(memberId) || !after.challenges.has(challengeId)) return;
    const source = `agent:${memberId}`, target = `challenge:${challengeId}`;
    signals.set(id, { id, source, target, kind, reverse });
  }
  for (const [id, current] of after.challenges) {
    const previous = before.challenges.get(id);
    if (!previous || current.revision < previous.revision || current.archived) continue;
    const participantsChanged = previous.participants.join('\0') !== current.participants.join('\0');
    if (previous.ownerId !== current.ownerId || participantsChanged) {
      movedIds.add(`challenge:${id}`);
      for (const memberId of new Set([...previous.participants, ...current.participants])) {
        if (memberIds.has(memberId)) movedIds.add(`agent:${memberId}`);
      }
      for (const memberId of current.participants) {
        if (!previous.participants.includes(memberId) || (memberId === current.ownerId && previous.ownerId !== memberId)) {
          signal(`claim:${id}:${memberId}:${current.revision}`, memberId, id, 'claim');
        }
      }
    }
    if (current.status === 'completed' && previous.status !== 'completed') {
      movedIds.add(`challenge:${id}`);
      signal(`completed:${id}:${current.revision}`, current.ownerId, id, 'completed', true);
    }
    if (current.accepted && !previous.accepted) signal(`accepted:${id}:${current.revision}`, current.ownerId, id, 'accepted', true);
  }
  for (const [id, record] of after.records) {
    // A newly fetched old page is history, even when its record IDs were not seen before.
    if (before.records.has(id) || record.version <= before.maxRecordVersion || after.challenges.get(record.challengeId)?.archived) continue;
    signal(`progress:${id}`, record.authorId, record.challengeId, 'progress');
  }
  return { movedIds: [...movedIds], signals: [...signals.values()] };
}
