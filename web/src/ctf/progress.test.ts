import { describe, expect, it } from 'vitest';
import { challengeProgress, challengeTimeline } from './progress';
import type { CtfEvent, CtfMember, CtfRecord } from './types';

const members: CtfMember[] = [
  { id: 'lead', display_name: 'Lead', role: 'lead', lifecycle: 'active', run_state: 'idle', generation: 1 },
  { id: 'a', display_name: '分析员', role: 'teammate', lifecycle: 'active', run_state: 'running', generation: 1 },
  { id: 'b', display_name: '验证员', role: 'teammate', lifecycle: 'active', run_state: 'running', generation: 1 },
];

function record(version: number, overrides: Partial<CtfRecord> = {}): CtfRecord {
  return { id: `r${version}`, challenge_id: 'c1', author_id: 'a', kind: 'note', body: '检查登录接口。尚需验证。', created_version: version, created_at: `2026-10-12T03:00:${String(version).padStart(2, '0')}Z`, artifact_refs: [], ...overrides };
}

function event(version: number, owner: string | null = null, collaborators: string[] = [], overrides: Partial<CtfEvent> = {}): CtfEvent {
  return {
    task_id: 'task', version, type: version === 1 ? 'ctf.challenge.created' : 'ctf.challenge.updated', actor: 'lead', object_id: 'c1', created_at: `2026-10-12T03:00:${String(version).padStart(2, '0')}Z`,
    payload: { result: { id: 'c1', title: '合成题目', owner_id: owner, collaborator_ids: collaborators, revision: version, work_status: 'in_progress', verification: {} } }, ...overrides,
  };
}

function statusEvent(version: number, status: string | undefined, owner: string | null = 'a'): CtfEvent {
  const snapshot = event(version, owner);
  snapshot.payload.result = { ...(snapshot.payload.result as Record<string, unknown>), work_status: status };
  return snapshot;
}

describe('challengeProgress', () => {
  it('uses the latest version across agents without promoting an old candidate', () => {
    const candidate = record(3, { kind: 'verification', verification: { status: 'candidate', summary: '旧候选' }, body: '旧候选' });
    const latest = record(9, { author_id: 'b', body: '## 新检查\n**接口返回** [403](https://example.test)\n- 继续定位' });
    const input = [latest, record(20, { challenge_id: 'other' }), candidate, record(5)];
    expect(challengeProgress(input, 'c1')).toEqual({ latest, count: 3, summary: '新检查 接口返回 403 继续定位' });
    expect(input[0]).toBe(latest);
  });

  it('returns an empty progress for a challenge without records', () => {
    expect(challengeProgress([record(1, { challenge_id: 'other' })], 'c1')).toEqual({ latest: null, count: 0, summary: '' });
  });

  it('preserves an unapplied verification as history without inventing accepted state', () => {
    const latest = record(8, { body: '', kind: 'verification', summary_applied: false, verification: { status: 'accepted', source: 'user', summary: '**历史确认**' } });
    const progress = challengeProgress([record(2), latest], 'c1');
    expect(progress.summary).toBe('历史确认');
    expect(progress.latest?.summary_applied).toBe(false);
    expect(progress).not.toHaveProperty('status');
  });
});

describe('challengeTimeline', () => {
  it('records actual blocked and completed transitions without implying answer acceptance', () => {
    const completed = statusEvent(6, 'completed');
    const entries = challengeTimeline('c1', [], [completed, statusEvent(1, 'in_progress'), statusEvent(4, 'blocked')], members);
    expect(entries).toHaveLength(3);
    expect(entries[1]).toMatchObject({ id: 'event:4:status', type: 'status', beforeStatus: 'in_progress', afterStatus: 'blocked', title: '工作状态更新', detail: '工作状态：进行中 → 需要增援' });
    expect(entries[2]).toMatchObject({ id: 'event:6:status', type: 'status', actorId: 'lead', createdAt: completed.created_at, beforeStatus: 'blocked', afterStatus: 'completed', title: '工作交付状态更新', detail: '工作状态：需要增援 → 已交付' });
    expect(entries[2]).not.toHaveProperty('verification');
    expect(entries[2].title).not.toContain('通过');
  });

  it('merges a status transition into the same assignment event without duplicate entries', () => {
    const entries = challengeTimeline('c1', [], [statusEvent(1, 'pending', null), statusEvent(2, 'in_progress', 'a')], members);
    expect(entries).toHaveLength(2);
    expect(entries[1]).toMatchObject({ id: 'event:2', type: 'assignment', beforeOwner: null, afterOwner: 'a', detail: '负责人：未分配 → 分析员；工作状态：待领取 → 进行中' });
  });

  it('requires both actual state values and a previous snapshot for status history', () => {
    expect(challengeTimeline('c1', [], [statusEvent(2, 'completed')], members)).toEqual([]);
    expect(challengeTimeline('c1', [], [statusEvent(1, undefined), statusEvent(2, 'completed')], members).map((entry) => entry.type)).toEqual(['created']);
    expect(challengeTimeline('c1', [], [statusEvent(1, 'in_progress'), statusEvent(2, undefined)], members).map((entry) => entry.type)).toEqual(['created']);
    expect(challengeTimeline('c1', [], [statusEvent(1, 'completed'), statusEvent(2, 'completed')], members).map((entry) => entry.type)).toEqual(['created']);
  });

  it('shows creation separately from an actual owner and collaborator change', () => {
    const timeline = challengeTimeline('c1', [], [event(1), event(3, 'a', ['b'])], members);
    expect(timeline[0]).toMatchObject({ type: 'created', title: '创建题目', detail: '合成题目' });
    expect(timeline[1]).toMatchObject({ type: 'assignment', actorId: 'lead', beforeOwner: null, afterOwner: 'a', addedCollaborators: ['b'], removedCollaborators: [], detail: '负责人：未分配 → 分析员；加入协作：验证员' });
  });

  it('does not infer dispatch from a lead role, repeated snapshots or verification updates', () => {
    const changedVerification = event(4, 'a', ['b']);
    (changedVerification.payload.result as Record<string, unknown>).verification = { status: 'candidate' };
    expect(challengeTimeline('c1', [], [event(1, 'a', ['b']), event(2, 'a', ['b']), changedVerification], members).map((entry) => entry.type)).toEqual(['created']);
    expect(challengeTimeline('c1', [], [event(1, null, ['a', 'b']), event(2, null, ['b', 'a'])], members)).toHaveLength(1);
  });

  it('does not invent changes without a previous complete snapshot', () => {
    expect(challengeTimeline('c1', [], [event(2, 'a', ['b'])], members)).toEqual([]);
    const incomplete = event(3, 'a', [], { payload: { result: { id: 'c1', owner_id: 'b' } } });
    expect(challengeTimeline('c1', [], [event(2, 'a'), incomplete, event(4, 'b')], members)).toEqual([]);
  });

  it('retains system removal and unknown participant identifiers', () => {
    const entries = challengeTimeline('c1', [], [event(1, 'former-agent', ['b']), event(2, null, [], { actor: 'system' })], members);
    expect(entries[1]).toMatchObject({ type: 'assignment', actorId: 'system', beforeOwner: 'former-agent', afterOwner: null, removedCollaborators: ['b'], detail: '负责人：former-agent → 未分配；退出协作：验证员' });
  });

  it('merges unordered events and multi-agent records by version without mutating input', () => {
    const first = event(1);
    const update = event(4, 'a', ['b']);
    const events = [update, first, update];
    const records = [record(8, { author_id: 'b' }), record(2, { body: '# 已检查接口\n正文' }), record(5, { challenge_id: 'other' })];
    const entries = challengeTimeline('c1', records, events, members);
    expect(entries.map((entry) => entry.version)).toEqual([1, 2, 4, 8]);
    expect(entries[1]).toMatchObject({ type: 'record', actorId: 'a', title: '已检查接口' });
    expect(entries[3]).toMatchObject({ type: 'record', actorId: 'b', title: '检查登录接口。' });
    expect(events[0]).toBe(update);
    expect(records[0].created_version).toBe(8);
  });

  it('preserves one record with multiple attachments, author, time and verification provenance', () => {
    const entryRecord = record(5, {
      author_id: 'b', kind: 'verification', summary_applied: false,
      verification: { status: 'accepted', source: 'platform', test_only: true, call_id: 'call-1' },
      artifact_refs: ['a1', 'a2'].map((id) => ({ id, filename: `${id}.txt`, path: `/workspace/shared/${id}.txt`, uri: `evidence/task/${id}/file`, size: 12, sha256: 'a'.repeat(64) })),
    });
    const entries = challengeTimeline('c1', [entryRecord], [], members);
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ type: 'record', actorId: 'b', createdAt: entryRecord.created_at });
    expect(entries[0].type === 'record' && entries[0].record).toBe(entryRecord);
  });
});
