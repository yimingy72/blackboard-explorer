import type { TaskView } from '../api/client';

export type CtfMember = {
  id: string; agent_id?: string; display_name: string; role: 'lead' | 'teammate';
  lifecycle: 'active' | 'stopped' | 'removed'; run_state: string; generation: number;
};
export type CtfChallenge = {
  id: string; title: string; description: string; connection?: string | null;
  requirements?: string | null; external_id?: string | null; owner_id: string | null;
  collaborator_ids: string[]; work_status: string; revision: number; tombstone?: boolean;
  verification: Record<string, unknown>; target: Record<string, unknown>;
};
export type CtfArtifact = { id: string; uri: string; filename: string; path: string; size: number; sha256: string; created_version?: number };
export type CtfRecord = {
  id: string; challenge_id: string; author_id: string; kind: string; body: string;
  created_version: number; created_at?: string; verification?: Record<string, unknown>; target?: Record<string, unknown>; platform_call?: Record<string, unknown>; summary_applied?: boolean; artifact_refs: CtfArtifact[];
  attempted_routes?: string | null; observations_and_basis?: string | null;
  failure_conditions?: string | null; current_blocker?: string | null; help_needed?: string | null;
  no_artifacts_reason?: string | null;
};
export type CtfMessage = {
  id: string; sender_id: string; sender_kind: string; recipient_id: string; kind: string;
  reply_to?: string | null; source_turn_id?: string | null; purpose?: string; body: string; status: string; deferred: boolean; created_at?: string;
};
export type CtfSession = { session: Record<string, unknown>; revision: number };
export type CtfReasoning = { text: string; kind: 'reasoning' | 'summary' };
export type CtfToolCall = { id: string; name: string; arguments: unknown; result?: unknown; status: 'pending' | 'completed' | 'failed' };
export type CtfState = {
  task: TaskView & { ctf_cleanup?: { phase?: 'archiving' | 'destroying' | 'failed' | 'complete'; error?: string | null }; ctf_review_usage?: { cost?: string | number; output_tokens?: number }; ctf_phase?: string | null; ctf_options?: { max_teammates?: number } | null; ctf_conclusion?: Record<string, unknown> | null };
  members: CtfMember[]; challenges?: CtfChallenge[]; records?: CtfRecord[]; artifacts?: CtfArtifact[];
};
export type CtfEvent = { task_id: string; version: number; type: string; actor?: string; object_id?: string | null; payload: Record<string, unknown>; created_at?: string };
