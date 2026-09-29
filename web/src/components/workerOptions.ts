import type { WorkerRole, WorkerTools } from '../api/client';

export const workers: { id: WorkerRole; title: string; summary: string; description: string }[] = [
  { id: 'explore', title: '探索', summary: '调查、取证与提出方向', description: '调查并提交事实与意图；包含种子启动和认领后的工作规则。' },
  { id: 'derive', title: '推导', summary: '关联事实，发现新线索', description: '复核完成依据，并从已有事实和验收缺口中寻找必要的新方向。' },
  { id: 'close', title: '裁定与终结', summary: '核对目标，整理最终报告', description: '核对证据、裁定验收并生成报告。' },
];
export const builtin: Record<WorkerRole, string[]> = {
  explore: ['post_fact', 'post_intent', 'claim', 'release', 'get', 'search', 'read_evidence', 'view_image', 'execute_command'],
  derive: ['post_intent', 'get', 'search', 'read_evidence', 'view_image'],
  close: ['submit_close', 'get', 'search', 'read_evidence', 'view_image'],
};
export const required: Record<WorkerRole, string[]> = {
  explore: ['post_fact', 'release'], derive: ['post_intent'], close: ['submit_close', 'get', 'read_evidence'],
};
export const defaultTools = (role: WorkerRole): WorkerTools => ({
  builtin: builtin[role],
  mcp_servers: [],
});
