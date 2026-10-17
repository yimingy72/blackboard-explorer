import type { CtfArtifact, CtfChallenge, CtfEvent, CtfRecord, CtfState } from './types';

export const LIVE_EVENT_LIMIT = 512;

export function appendCtfEvents(current: readonly CtfEvent[], incoming: readonly CtfEvent[]): CtfEvent[] {
  if (!incoming.length) return current as CtfEvent[];
  const byVersion = new Map(current.map((event) => [event.version, event]));
  for (const event of incoming) byVersion.set(event.version, event);
  return [...byVersion.values()]
    .sort((left, right) => left.version - right.version)
    .slice(-LIVE_EVENT_LIMIT);
}

export function mergeCtfEvents(...groups: ReadonlyArray<readonly CtfEvent[]>): CtfEvent[] {
  const byVersion = new Map<number, CtfEvent>();
  for (const group of groups) for (const event of group) byVersion.set(event.version, event);
  return [...byVersion.values()].sort((left, right) => left.version - right.version);
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function result(event: CtfEvent): Record<string, unknown> | null {
  return object(object(event.payload)?.result);
}

export function ctfEventsNeedSnapshot(events: readonly CtfEvent[]): boolean {
  return events.some((event) =>
    event.type.startsWith('task.')
    || event.type === 'ctf.started'
    || event.type === 'ctf.recovered'
    || event.type.startsWith('ctf.conclusion.')
    || event.type === 'budget.updated'
    || event.type === 'cost.reconciled');
}

/** Fold event payloads into the current snapshot without refetching the whole CTF state. */
export function applyCtfEvents(state: CtfState | undefined, events: readonly CtfEvent[]): CtfState | undefined {
  if (!state || !events.length) return state;
  const members = new Map((state.members ?? []).map((member) => [member.id, member]));
  const challenges = new Map((state.challenges ?? []).map((challenge) => [challenge.id, challenge]));
  const records = new Map((state.records ?? []).map((record) => [record.id, record]));
  const artifacts = new Map((state.artifacts ?? []).map((artifact) => [artifact.id, artifact]));
  let membersChanged = false, challengesChanged = false, recordsChanged = false, artifactsChanged = false;

  for (const event of events) {
    if (event.version <= (state.event_version ?? 0)) continue;
    const payload = object(event.payload) ?? {};
    if (event.type === 'ctf.member.created') {
      const member = payload as unknown as CtfState['members'][number];
      if (typeof member.id === 'string' && member.id) {
        members.set(member.id, member);
        membersChanged = true;
      }
    } else if (event.type === 'ctf.member.state_changed' || event.type === 'ctf.member.removed') {
      const id = typeof payload.id === 'string' ? payload.id : '';
      const current = members.get(id);
      if (current && typeof payload.lifecycle === 'string') {
        members.set(id, { ...current, lifecycle: payload.lifecycle as typeof current.lifecycle });
        membersChanged = true;
      }
    } else if (event.type === 'ctf.turn.started') {
      const id = typeof payload.agent_id === 'string' ? payload.agent_id : '';
      const current = members.get(id);
      if (current) {
        members.set(id, {
          ...current,
          run_state: 'running',
          current_turn_id: typeof payload.id === 'string' ? payload.id : current.current_turn_id,
          generation: typeof payload.generation === 'number' ? payload.generation : current.generation,
        });
        membersChanged = true;
      }
    } else if (event.type === 'ctf.turn.finished') {
      const id = typeof payload.agent_id === 'string' ? payload.agent_id : '';
      const current = members.get(id);
      if (current) {
        members.set(id, { ...current, run_state: 'idle', current_turn_id: null } as typeof current);
        membersChanged = true;
      }
    } else if (event.type === 'ctf.challenge.created' || event.type === 'ctf.challenge.updated') {
      const challenge = result(event) as unknown as CtfChallenge | null;
      if (challenge && typeof challenge.id === 'string' && challenge.id) {
        challenges.set(challenge.id, challenge);
        challengesChanged = true;
      }
    } else if (event.type === 'ctf.record.appended') {
      const record = result(event) as unknown as CtfRecord | null;
      if (record && typeof record.id === 'string' && record.id && record.kind !== 'artifact_registration') {
        records.set(record.id, record);
        recordsChanged = true;
      }
    } else if (event.type === 'ctf.artifact.registered') {
      const value = result(event);
      const artifact = object(value?.artifact) ?? value;
      if (artifact && typeof artifact.id === 'string' && artifact.id) {
        artifacts.set(artifact.id, artifact as unknown as CtfArtifact);
        artifactsChanged = true;
      }
    }
  }

  if (!membersChanged && !challengesChanged && !recordsChanged && !artifactsChanged) return state;
  return {
    ...state,
    ...(membersChanged ? { members: [...members.values()] } : {}),
    ...(challengesChanged ? { challenges: [...challenges.values()] } : {}),
    ...(recordsChanged ? { records: [...records.values()] } : {}),
    ...(artifactsChanged ? { artifacts: [...artifacts.values()] } : {}),
  };
}
