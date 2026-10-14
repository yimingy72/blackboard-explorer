import { describe, expect, it } from 'vitest';
import { challengeSolution, challengeState, challengeTitle, solutionMarkdown } from './challengeView';
import { challengeProgress, challengeTimeline } from './progress';
import type { CtfChallenge, CtfRecord, CtfSolution } from './types';

const challenge: CtfChallenge = { id: 'c1', title: 'Archive Puzzle [misc/medium]', description: '', owner_id: 'owner', collaborator_ids: [], work_status: 'completed', revision: 3, verification: { status: 'candidate', source: 'agent' }, target: {}, solution_record_id: 's1' };
const solution: CtfSolution = { conclusion: '阶段交付，仍有未解决事项。', approach: '核对材料后比较两种观察。', steps: [{ kind: 'attempt', title: '边界输入', summary: '该尝试未得到一致证据。', result: 'ruled_out', record_ids: ['r1'], artifact_ids: ['a1'] }], remaining: ['需要补充独立的观察。'], reproduction: '使用已登记脚本和输出。' };
const record: CtfRecord = { id: 's1', challenge_id: 'c1', author_id: 'owner', kind: 'solution', body: solution.conclusion, solution, artifact_refs: [], created_version: 5 };

describe('challenge presentation', () => {
  it('does not promote completion, candidate or simulated acceptance into solved', () => {
    expect(challengeState(challenge)).toBe('delivered');
    for (const verification of [{ status: 'accepted', source: 'agent' }, { status: 'accepted', source: 'platform', test_only: true }, { status: 'accepted' }]) expect(challengeState({ ...challenge, verification })).toBe('delivered');
    expect(challengeState({ ...challenge, verification: { status: 'accepted', source: 'platform' } })).toBe('solved');
    expect(challengeState({ ...challenge, verification: { status: 'accepted', source: 'user' } })).toBe('solved');
  });
  it('keeps reopened, cancelled and archived work distinct even with historical success', () => {
    const verification = { status: 'accepted', source: 'platform' };
    expect(challengeState({ ...challenge, work_status: 'in_progress', verification })).toBe('working');
    expect(challengeState({ ...challenge, work_status: 'cancelled', verification })).toBe('cancelled');
    expect(challengeState({ ...challenge, tombstone: true, verification })).toBe('archived');
  });
  it('only splits explicit category and difficulty suffixes and preserves other titles', () => {
    expect(challengeTitle(challenge.title)).toEqual({ title: 'Archive Puzzle', metadata: ['misc', 'medium'] });
    expect(challengeTitle('Archive [web,very-easy]')).toEqual({ title: 'Archive', metadata: ['web', 'very-easy'] });
    for (const title of ['目标 [输入/输出]', '目标 [phase/unknown]', '一个包含 [crypto/medium] 的描述']) expect(challengeTitle(title)).toEqual({ title, metadata: [] });
  });
  it('uses the server pointer without confusing a newer unrelated or old solution', () => {
    const foreign = { ...record, id: 'foreign', challenge_id: 'other', created_version: 100 };
    const previous = { ...record, id: 's0', created_version: 1 };
    expect(challengeSolution(challenge, [previous, foreign, record])).toEqual({ current: record, previous });
    expect(challengeSolution({ ...challenge, solution_record_id: null }, [record])).toEqual({ current: null, previous: record });
    expect(challengeSolution({ ...challenge, solution_record_id: 'not-loaded' }, [record]).current).toBeNull();
  });
  it('does not count the final synthesis as another raw progress record', () => {
    const progress: CtfRecord = { ...record, id: 'r1', kind: 'note', solution: null, body: '观察原文', progress: { stage: 'attempt', title: '边界核对', summary: '结论尚不确定。' }, created_version: 2 };
    expect(challengeProgress([progress, record], 'c1')).toEqual({ latest: progress, count: 1, summary: '结论尚不确定。' });
    expect(challengeTimeline('c1', [progress], [], [])[0]).toMatchObject({ title: '边界核对', detail: '结论尚不确定。' });
  });
  it('exports failed attempts, remaining work and original references without inventing a breakthrough', () => {
    const markdown = solutionMarkdown(challenge.title, solution);
    expect(markdown).toContain('排除：该尝试未得到一致证据。');
    expect(markdown).toContain('记录 r1；文件 a1');
    expect(markdown).toContain('需要补充独立的观察。');
    expect(markdown).not.toContain('突破');
  });
});
