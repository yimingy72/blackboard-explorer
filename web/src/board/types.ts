import type { components } from '../api/schema';

export type BoardEvent = components['schemas']['Event'];
export type BoardEventType = BoardEvent['type'];

export type TaskStatus =
  | 'created'
  | 'provisioning'
  | 'running'
  | 'closing'
  | 'finished'
  | 'failed'
  | 'stopped';

export interface BoardAcceptance {
  id: string;
  desc: string;
  status: 'met' | 'unmet';
  reason: string | null;
  missing: string | null;
  evidence_facts: string[];
  judged_version: number | null;
  completion_basis?: string;
  completion_reason?: string | null;
}

export interface BoardTask {
  goal: string;
  status: TaskStatus;
  domain_context: string | null;
  acceptance: Array<{ id: string; desc: string }>;
  budget: Record<string, unknown>;
  usage: Record<string, number>;
  report_uri: string | null;
  workspace_uri?: string | null;
  fail_reason: string | null;
  version: number;
  params?: Record<string, unknown>;
  closingReason?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
}

export interface BoardFact {
  id: string;
  version: number;
  kind: 'observation' | 'inference' | 'structure';
  statement: string;
  author: string;
  provenance: 'tool_backed' | 'self_reported';
  status: 'proposed' | 'disputed';
  reliedBy: number;
  derivedFrom: string[];
  disputes: string[];
  resolves: string | null;
  result: 'confirmed' | 'rejected' | 'inconclusive' | null;
  satisfies: string[];
  evidence: unknown[];
}

export interface BoardIntent {
  id: string;
  version: number;
  statement: string;
  basedOn: string[];
  expected: string;
  method: string;
  relatesTo: string[];
  retryOf: string | null;
  author: string;
  status: 'open' | 'claimed' | 'closed';
  holder: string | null;
  result: 'confirmed' | 'rejected' | 'inconclusive' | null;
  closedBy: string | null;
  resultFacts: string[];
  attempts: number;
  notes?: Array<{ by: string; at: string; text: string }>;
}

export interface BoardAgent {
  id: string;
  taskType: 'explore' | 'derive' | 'close';
  isSeed: boolean;
  deriveParallel?: boolean;
  deriveReview?: boolean;
  closeMode: 'judge' | 'final' | null;
  status: 'running' | 'concluding' | 'finished' | 'failed';
  steps: number;
  contextTokens: number;
  intentId: string | null;
  usage: Record<string, number>;
  lastSeenVersion: number;
  graceCallsLeft: number | null;
  endReason: string | null;
  receipt?: unknown;
  concludeReason?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
}

export interface BoardToolCall {
  id: string;
  agentId: string;
  tool: string;
  args: Record<string, unknown>;
  resultHead: string;
  resultUri: string | null;
  createdAt: string;
  version: number;
}

export interface BoardState {
  task: BoardTask | null;
  facts: Record<string, BoardFact>;
  intents: Record<string, BoardIntent>;
  agents: Record<string, BoardAgent>;
  acceptance: Record<string, BoardAcceptance>;
  toolCalls?: Record<string, BoardToolCall>;
}
