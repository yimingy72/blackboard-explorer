import { describe, expect, it } from 'vitest';

import { appendCtfEvents, applyCtfEvents, ctfEventsNeedSnapshot, LIVE_EVENT_LIMIT, mergeCtfEvents } from './live';
import type { CtfEvent, CtfState } from './types';

const taskId = '11111111-1111-4111-8111-111111111111';

function event(version: number, type: string, payload: Record<string, unknown>): CtfEvent {
  return { task_id: taskId, version, type, actor: 'system', object_id: null, payload };
}

function state(): CtfState {
  return {
    event_version: 10,
    task: { id: taskId, status: 'running' } as CtfState['task'],
    members: [{ id: 'lead', display_name: 'Lead', role: 'lead', lifecycle: 'active', run_state: 'idle', generation: 0 }],
    challenges: [{ id: 'c1', title: 'One', description: '', owner_id: null, collaborator_ids: [], work_status: 'pending', revision: 1, verification: {}, target: {} }],
    records: [],
    artifacts: [],
  };
}

describe('CTF live events', () => {
  it('deduplicates live versions and bounds volatile memory', () => {
    const current = Array.from({ length: LIVE_EVENT_LIMIT }, (_, index) => event(index + 1, 'agent.trace.recorded', { index }));
    const result = appendCtfEvents(current, [event(10, 'agent.trace.recorded', { duplicate: true }), event(LIVE_EVENT_LIMIT + 1, 'agent.trace.recorded', { next: true })]);
    expect(result).toHaveLength(LIVE_EVENT_LIMIT);
    expect(result.at(-1)?.version).toBe(LIVE_EVENT_LIMIT + 1);
    expect(result.find((item) => item.version === 10)?.payload).toEqual({ duplicate: true });
  });

  it('merges on-demand history with newer live events without duplication', () => {
    const merged = mergeCtfEvents(
      [event(3, 'task.running', {}), event(4, 'ctf.started', {})],
      [event(4, 'ctf.started', {}), event(5, 'ctf.turn.started', {})],
    );
    expect(merged.map((item) => item.version)).toEqual([3, 4, 5]);
  });

  it('folds roster, challenge, and record payloads into one snapshot update', () => {
    const before = state();
    const after = applyCtfEvents(before, [
      event(11, 'ctf.turn.started', { id: 'turn-1', agent_id: 'lead', generation: 1 }),
      event(12, 'ctf.challenge.updated', { result: { ...before.challenges?.[0], revision: 2, owner_id: 'lead', work_status: 'in_progress' } }),
      event(13, 'ctf.record.appended', { result: { id: 'r1', challenge_id: 'c1', author_id: 'lead', kind: 'progress', body: 'step', created_version: 13, artifact_refs: [] } }),
    ]);
    expect(after).not.toBe(before);
    expect(after?.members[0]).toMatchObject({ run_state: 'running', current_turn_id: 'turn-1' });
    expect(after?.challenges?.[0]).toMatchObject({ revision: 2, owner_id: 'lead' });
    expect(after?.records?.[0]).toMatchObject({ id: 'r1', created_version: 13 });
    expect(before.members[0].run_state).toBe('idle');
  });

  it('leaves snapshots untouched for trace-only traffic and flags task state changes', () => {
    const before = state();
    const trace = event(11, 'agent.trace.recorded', { kind: 'model_output' });
    expect(applyCtfEvents(before, [trace])).toBe(before);
    expect(ctfEventsNeedSnapshot([trace])).toBe(false);
    expect(ctfEventsNeedSnapshot([event(12, 'ctf.conclusion.finalized', { conclusion: {} })])).toBe(true);
  });
});

it('does not replay a queued event that the newer snapshot already contains', () => {
  const before = state();
  before.event_version = 20;
  const after = applyCtfEvents(before, [event(15, 'ctf.turn.started', { id: 'old-turn', agent_id: 'lead', generation: 1 })]);
  expect(after).toBe(before);
  expect(after?.members[0].run_state).toBe('idle');
});

it('does not traverse object collections for diagnostic or already snapshotted events', () => {
  const before = state();
  let visits = 0;
  function counted<T>(values: T[]): T[] {
    return new Proxy(values, { get(target, key, receiver) {
      if (typeof key === 'string' && /^\d+$/.test(key)) visits++;
      return Reflect.get(target, key, receiver);
    } });
  }
  before.members = counted(Array.from({ length: 1000 }, (_, index) => ({ ...before.members[0], id: `m${index}` })));
  before.challenges = counted(Array.from({ length: 1000 }, (_, index) => ({ ...before.challenges![0], id: `c${index}` })));
  before.records = counted(Array.from({ length: 1000 }, (_, index) => ({ id: `r${index}` } as NonNullable<CtfState['records']>[number])));
  before.artifacts = counted(Array.from({ length: 1000 }, (_, index) => ({ id: `a${index}` } as NonNullable<CtfState['artifacts']>[number])));
  expect(applyCtfEvents(before, [
    event(11, 'agent.trace.recorded', {}), event(12, 'tool_call.recorded', {}),
    event(5, 'ctf.turn.started', { agent_id: 'm0', id: 'obsolete' }),
  ])).toBe(before);
  expect(visits).toBe(0);
});

it('copies only a changed collection while retaining batch order and untouched references', () => {
  const before = state();
  let unrelatedReads = 0;
  before.members.push({ ...before.members[0], id: 'untouched' });
  before.records = new Proxy(Array.from({ length: 1000 }, (_, index) => ({ id: `r${index}` } as NonNullable<CtfState['records']>[number])), {
    get(target, key, receiver) {
      if (typeof key === 'string' && /^\d+$/.test(key)) unrelatedReads++;
      return Reflect.get(target, key, receiver);
    },
  });
  const after = applyCtfEvents(before, [
    event(11, 'ctf.turn.started', { agent_id: 'lead', id: 'first', generation: 1 }),
    event(12, 'ctf.turn.finished', { agent_id: 'lead' }),
    event(13, 'ctf.turn.started', { agent_id: 'lead', id: 'last', generation: 2 }),
  ]);
  expect(after?.members[0]).toMatchObject({ current_turn_id: 'last', generation: 2, run_state: 'running' });
  expect(after?.members).not.toBe(before.members);
  expect(after?.members[1]).toBe(before.members[1]);
  expect(after?.records).toBe(before.records);
  expect(after?.challenges).toBe(before.challenges);
  expect(after?.artifacts).toBe(before.artifacts);
  expect(unrelatedReads).toBe(0);
});
