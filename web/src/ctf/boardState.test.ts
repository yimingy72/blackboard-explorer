import { describe, expect, it, vi } from 'vitest';
import { diffBoard, loadBoardState, saveBoardState, snapshotBoard, type BoardState } from './boardState';
import type { CtfChallenge, CtfMember, CtfRecord } from './types';

const members: CtfMember[] = ['lead', 'a', 'b'].map((id) => ({ id, display_name: id, role: id === 'lead' ? 'lead' : 'teammate', lifecycle: 'active', run_state: 'running', generation: 1 }));
const challenge: CtfChallenge = { id: 'c1', title: 'Synthetic title', description: 'Synthetic body', owner_id: 'a', collaborator_ids: [], work_status: 'in_progress', revision: 1, verification: {}, target: {} };
function record(version: number, overrides: Partial<CtfRecord> = {}): CtfRecord {
  return { id: `r${version}`, challenge_id: 'c1', author_id: 'a', kind: 'note', body: 'Synthetic note', created_version: version, artifact_refs: [], ...overrides };
}
function memoryStorage() {
  const values = new Map<string, string>();
  return { values, getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
}
const state: BoardState = { positions: { 'agent:a': { x: -400, y: 900 }, 'challenge:c1': { x: 1e7, y: -1e7 } }, pinned: ['agent:a', 'agent:a'], viewport: { x: -10, y: 50, zoom: 0.15 }, collapsed: true };

describe('board state storage', () => {
  it('isolates tasks, keeps negative coordinates, and persists only layout fields', () => {
    const storage = memoryStorage();
    const extra = { ...state, title: 'Do not store', positions: { 'agent:a': { x: -400, y: 900, body: 'Do not store' } } };
    expect(saveBoardState('task/one', extra, storage)).toBe(true);
    expect(loadBoardState('task/one', storage)).toEqual({ ...state, pinned: ['agent:a'], positions: { 'agent:a': { x: -400, y: 900 } } });
    expect(loadBoardState('task/two', storage)).toBeNull();
    expect([...storage.values.values()][0]).not.toContain('Do not store');
    expect([...storage.values.values()][0]).toContain('"version":1');
  });

  it('handles bad JSON, unsupported schemas, unavailable storage, and quota errors', () => {
    expect(loadBoardState('task', { getItem: () => '{' })).toBeNull();
    expect(loadBoardState('task', { getItem: () => JSON.stringify({ version: 2, ...state }) })).toBeNull();
    expect(loadBoardState('task', { getItem: () => { throw new Error('Denied'); } })).toBeNull();
    expect(saveBoardState('task', state, { setItem: () => { throw new Error('Quota'); } })).toBe(false);
    vi.stubGlobal('localStorage', undefined);
    expect(loadBoardState('task')).toBeNull();
    expect(saveBoardState('task', state)).toBe(false);
    vi.unstubAllGlobals();
  });

  it.each([
    { ...state, positions: { 'other:a': { x: 0, y: 0 } } },
    { ...state, positions: { 'agent:a': { x: Infinity, y: 0 } } },
    { ...state, positions: { 'agent:a': { x: 1e7 + 1, y: 0 } } },
    { ...state, viewport: { x: 0, y: 0, zoom: 0.149 } },
    { ...state, viewport: { x: 0, y: 0, zoom: 1.501 } },
    { ...state, pinned: ['a'] },
    { ...state, pinned: Array.from({ length: 2001 }, () => 'agent:a') },
  ])('rejects invalid layout values %#', (invalid) => {
    const storage = memoryStorage();
    expect(saveBoardState('task', invalid, storage)).toBe(false);
    expect(loadBoardState('task', { getItem: () => JSON.stringify({ version: 1, ...invalid }) })).toBeNull();
    expect(storage.values.size).toBe(0);
  });
});

describe('board event differences', () => {
  it('moves just changed relationships and their old/new participants without inventing dispatch', () => {
    const before = snapshotBoard([challenge], []);
    const after = snapshotBoard([{ ...challenge, revision: 2, owner_id: 'b', collaborator_ids: ['a', 'a', 'missing'] }], []);
    const diff = diffBoard(before, after, members);
    expect(diff.movedIds.sort()).toEqual(['agent:a', 'agent:b', 'challenge:c1']);
    expect(diff.signals).toEqual([{ id: 'claim:c1:b:2', source: 'agent:b', target: 'challenge:c1', kind: 'claim', reverse: false }]);
    expect(diff.signals.some((signal) => signal.source === 'agent:lead')).toBe(false);
  });

  it('does not replay baseline records, duplicate IDs, older pages, or record edits', () => {
    const baseline = snapshotBoard([challenge], [record(10)]);
    expect(diffBoard(baseline, baseline, members)).toEqual({ movedIds: [], signals: [] });
    const after = snapshotBoard([challenge], [record(2), record(11), record(11), record(12, { id: 'r10' }), record(9)]);
    const diff = diffBoard(baseline, after, members);
    expect(diff.signals).toEqual([{ id: 'progress:r11', source: 'agent:a', target: 'challenge:c1', kind: 'progress', reverse: false }]);
    expect(diff.movedIds).toEqual([]);
    expect(diffBoard(after, snapshotBoard([challenge], [record(2), record(11), record(12, { id: 'r10' }), record(9)]), members).signals).toEqual([]);
  });

  it('deduplicates snapshots by highest revision/version without retaining record text', () => {
    const latest = { ...challenge, revision: 4, owner_id: 'b' };
    const snapshot = snapshotBoard([latest, challenge], [record(8, { id: 'same', author_id: 'b' }), record(2, { id: 'same' })]);
    expect(snapshot.challenges.get('c1')).toMatchObject({ ownerId: 'b', revision: 4 });
    expect(snapshot.records.get('same')).toEqual({ authorId: 'b', challengeId: 'c1', version: 8 });
    expect(snapshot.maxRecordVersion).toBe(8);
    expect(JSON.stringify([...snapshot.challenges.values()])).not.toContain('Synthetic');
    expect(JSON.stringify([...snapshot.records.values()])).not.toContain('Synthetic');
  });

  it('keeps work completion and credible acceptance as separate reverse signals', () => {
    const before = snapshotBoard([challenge], []);
    const completed = snapshotBoard([{ ...challenge, revision: 2, work_status: 'completed', verification: { status: 'candidate', flag: 'flag{synthetic}' } }], []);
    expect(diffBoard(before, completed, members).signals.map((signal) => [signal.kind, signal.reverse])).toEqual([['completed', true]]);
    expect(diffBoard(before, completed, members).movedIds).toEqual(['challenge:c1']);
    const accepted = snapshotBoard([{ ...challenge, revision: 3, work_status: 'completed', verification: { status: 'accepted', source: 'user' } }], []);
    expect(diffBoard(completed, accepted, members).signals.map((signal) => [signal.kind, signal.reverse])).toEqual([['accepted', true]]);
    expect(diffBoard(completed, accepted, members).movedIds).toEqual([]);
    expect(diffBoard(accepted, accepted, members).signals).toEqual([]);
  });

  it.each([
    { status: 'candidate', source: 'platform', flag: 'flag{synthetic}' },
    { status: 'accepted', source: 'platform', test_only: true },
    { status: 'accepted', source: 'user', test_only: true },
    { status: 'accepted', source: 'agent' },
    { status: 'accepted' },
  ])('does not promote candidate, simulated or untrusted verification %#', (verification) => {
    const diff = diffBoard(snapshotBoard([challenge], []), snapshotBoard([{ ...challenge, revision: 2, verification }], [record(2, { kind: 'verification', verification: { status: 'accepted', source: 'user' }, summary_applied: false })]), members);
    expect(diff.signals.map((signal) => signal.kind)).toEqual(['progress']);
  });

  it('accepts real platform verification but never fabricates an unknown endpoint', () => {
    const before = snapshotBoard([challenge], []);
    const valid = snapshotBoard([{ ...challenge, revision: 2, verification: { status: 'accepted', source: 'platform', test_only: false } }], []);
    expect(diffBoard(before, valid, members).signals[0].kind).toBe('accepted');
    const unknown = snapshotBoard([{ ...challenge, owner_id: 'missing', revision: 2, work_status: 'completed', verification: { status: 'accepted', source: 'user' } }], [record(2, { author_id: 'missing' }), record(3, { challenge_id: 'missing' })]);
    expect(diffBoard(before, unknown, members).signals).toEqual([]);
    expect(diffBoard(before, unknown, members).movedIds).not.toContain('agent:missing');
  });

  it('ignores stale revisions and archived challenges', () => {
    const before = snapshotBoard([{ ...challenge, revision: 5 }], []);
    for (const next of [{ ...challenge, revision: 4, owner_id: 'b', work_status: 'completed' }, { ...challenge, revision: 6, tombstone: true, work_status: 'completed' }]) {
      expect(diffBoard(before, snapshotBoard([next], []), members)).toEqual({ movedIds: [], signals: [] });
    }
  });
});
