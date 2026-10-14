import type { BoardEvent, BoardState, BoardToolCall } from './types';
import type { AgentMessage } from '../api/client';

export type TraceKind = 'initial_context' | 'board_update' | 'model_output' | 'model_error';
export type TraceEntry = {
  type: 'trace'; version: number; at: string; kind: TraceKind;
  step: number; uri: string; summary: string; deriveRound?: number;
};
export type ToolEntry = { type: 'tool'; version: number; at: string; call: BoardToolCall };
export type ChatEntry = { type: 'chat'; version: number; at: string; message: AgentMessage };
export type ConversationEntry = TraceEntry | ToolEntry | ChatEntry;

function isTraceKind(value: unknown): value is TraceKind {
  return value === 'initial_context' || value === 'board_update' || value === 'model_output' || value === 'model_error';
}

export function conversationEntries(agentId: string, events: readonly BoardEvent[], state: BoardState, messages: readonly AgentMessage[] = [], historical = false): ConversationEntry[] {
  const traces: TraceEntry[] = [];
  const cutoff = Math.max(0, ...events.map((event) => event.version));
  for (const event of events) {
    if (event.type !== 'agent.trace.recorded') continue;
    const payload: Record<string, unknown> = event.payload ?? {};
    if (payload.agent_id !== agentId || !isTraceKind(payload.kind) || typeof payload.uri !== 'string' || !payload.uri) continue;
    traces.push({
      type: 'trace', version: event.version, at: event.created_at,
      kind: payload.kind, step: typeof payload.step === 'number' ? payload.step : 0,
      uri: payload.uri, summary: typeof payload.summary === 'string' ? payload.summary : '',
      deriveRound: typeof payload.derive_round === 'number' && Number.isInteger(payload.derive_round) && payload.derive_round >= 1
        ? payload.derive_round : state.agents[agentId]?.taskType === 'derive' ? 1 : undefined,
    });
  }
  const tools: ToolEntry[] = Object.values(state.toolCalls ?? {})
    .filter((call) => call.agentId === agentId && call.version <= cutoff)
    .map((call) => ({ type: 'tool', version: call.version, at: call.createdAt, call }));
  const chats: ChatEntry[] = [];
  const messageEvents = new Map<string, BoardEvent>();
  const messageProgress = new Map<string, BoardEvent>();
  for (const event of events) {
    if (!String(event.type).startsWith('agent.message.')) continue;
    const id = event.payload?.id;
    if (typeof id === 'string') { if (!messageEvents.has(id)) messageEvents.set(id, event); messageProgress.set(id, event); }
    const replyTo = event.payload?.reply_to;
    if (typeof replyTo === 'string') messageProgress.set(replyTo, event);
  }
  for (const message of messages) {
    if (message.agent_id !== agentId) continue;
    const event = messageEvents.get(message.id);
    if (historical && !event) continue;
    let shown = message;
    if (historical) {
      const progress = messageProgress.get(message.id);
      const type = String(progress?.type);
      shown = { ...message, status: 'queued', error: null };
      if (type.endsWith('.failed')) shown.status = 'failed';
      else if (type.endsWith('.replied')) shown.status = 'completed';
      else if (type.endsWith('.delivered')) shown.status = 'delivered';
      shown.error = type.endsWith('.failed') ? String(progress?.payload?.error ?? '回复失败') : null;
    }
    chats.push({ type: 'chat', version: event?.version ?? Number.MAX_SAFE_INTEGER, at: message.created_at, message: shown });
  }
  return [...traces, ...tools, ...chats].sort((a, b) => a.version - b.version || Date.parse(a.at) - Date.parse(b.at));
}
