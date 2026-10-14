import type { CtfChallenge, CtfRecord, CtfSolution } from './types';

export type ChallengeState = 'pending' | 'working' | 'blocked' | 'delivered' | 'solved' | 'cancelled' | 'archived';
export const challengeStateLabels: Record<ChallengeState, string> = { pending: '待分配', working: '解题中', blocked: '受阻', delivered: '已交付', solved: '已解出', cancelled: '已取消', archived: '已归档' };
export const stageLabels = { approach: '思路', attempt: '尝试', breakthrough: '突破', result: '结果' };
export const stepResultLabels = { supported: '成立', ruled_out: '排除', inconclusive: '未定' };

export function challengeState(challenge: Pick<CtfChallenge, 'work_status' | 'verification' | 'tombstone'>): ChallengeState {
  if (challenge.tombstone) return 'archived';
  if (challenge.work_status === 'cancelled') return 'cancelled';
  if (challenge.work_status === 'completed') {
    const verification = challenge.verification ?? {};
    return verification.status === 'accepted' && verification.test_only !== true && ['platform', 'user'].includes(String(verification.source)) ? 'solved' : 'delivered';
  }
  return challenge.work_status === 'blocked' ? 'blocked' : challenge.work_status === 'in_progress' ? 'working' : 'pending';
}

export function challengeTitle(title: string): { title: string; metadata: string[] } {
  const match = title.match(/^(.+?)\s+\[([a-z][a-z -]*)\s*[/,]\s*(very[- ]easy|very[- ]hard|easy|medium|hard|expert)\]$/i);
  return match ? { title: match[1], metadata: [match[2].trim(), match[3].trim()] } : { title, metadata: [] };
}

export function challengeSolution(challenge: CtfChallenge, records: CtfRecord[]): { current: CtfRecord | null; previous: CtfRecord | null } {
  const related = records.filter(record => record.challenge_id === challenge.id && record.kind === 'solution' && record.solution).sort((a, b) => b.created_version - a.created_version);
  const current = challenge.solution_record_id ? related.find(record => record.id === challenge.solution_record_id) ?? null : null;
  return { current, previous: related.find(record => record.id !== current?.id) ?? null };
}

export function solutionMarkdown(title: string, solution: CtfSolution): string {
  const steps = solution.steps.map((step, index) => {
    const refs = [...step.record_ids.map(id => `记录 ${id}`), ...step.artifact_ids.map(id => `文件 ${id}`)];
    return `### ${index + 1}. ${stageLabels[step.kind]} · ${step.title}\n\n${stepResultLabels[step.result]}：${step.summary}${refs.length ? `\n\n依据：${refs.join('；')}` : ''}`;
  });
  return [`# ${title} · 解题流程`, `## 思路\n\n${solution.approach}`, ...steps, `## 结论\n\n${solution.conclusion}`, ...(solution.remaining.length ? [`## 未解决事项\n\n${solution.remaining.map(item => `- ${item}`).join('\n')}`] : []), `## 复现入口\n\n${solution.reproduction}`].join('\n\n');
}
