import type { AgentMessage } from '../api/client';

export type ProgressRef = { kind: 'task' | 'fact' | 'intent' | 'acceptance' | 'event' | 'evidence'; id: string; version: number; evidence_uri?: string | null; sha256?: string | null; read_at?: number };
export type ProgressStep = { text: string; source_refs: ProgressRef[] };
export type ProgressNode = {
  id: string; parent_id: string | null; order: number; kind: 'goal' | 'direction' | 'finding' | 'attempt' | 'gap';
  title: string; question: string; summary: string; significance: string; next_step: string; limitations: string;
  key_steps: ProgressStep[]; source_refs: ProgressRef[]; assessment?: string; archived?: boolean;
  effective_state?: 'exploring' | 'supported' | 'needs_evidence' | 'excluded' | 'sources_changed' | 'achieved';
  contributors?: string[];
};
export type ProgressView = {
  enabled: boolean; status: string; overview: string; nodes: ProgressNode[]; revision: number;
  source_version: number; desired_source_version: number; published_version: number; run_number: number;
  updated_at: string | null; error: string | null;
  budget: { max_cost: string; spent: string; reserved: string; unknown: string };
};
export type ProgressSettings = { revision: number; prompt: string; model_id: string | null; enabled_tools: string[] };
export type ProgressMessage = AgentMessage & { node_id?: string | null };
