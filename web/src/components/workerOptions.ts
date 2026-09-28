import type { WorkerRole, WorkerTools } from '../api/client';

export const workers: { id: WorkerRole; title: string; description: string }[] = [
  { id: 'explore', title: 'Explore · 探索', description: '调查并提交事实与意图；包含种子启动和认领后的工作规则。' },
  { id: 'derive', title: 'Derive · 推导', description: '根据已有事实与验收缺口提出新意图。' },
  { id: 'close', title: 'Close · 裁定与终结', description: '核对证据、裁定验收并生成报告。' },
];
export const builtin: Record<WorkerRole, string[]> = {
  explore: ['post_fact', 'post_intent', 'claim', 'release', 'get', 'search', 'read_evidence', 'execute_command'],
  derive: ['post_intent', 'get', 'search', 'read_evidence'],
  close: ['submit_close', 'get', 'search', 'read_evidence'],
};
export const required: Record<WorkerRole, string[]> = {
  explore: ['post_fact', 'release'], derive: ['post_intent'], close: ['submit_close', 'get', 'read_evidence'],
};
export const defaultTools = (role: WorkerRole): WorkerTools => ({ builtin: builtin[role], mcp_servers: [] });

