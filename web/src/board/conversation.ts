import type { BoardEvent, BoardState, BoardToolCall } from './types';

export type TraceKind = 'initial_context' | 'board_update' | 'model_output';
export type TraceEntry = {
  type: 'trace'; version: number; at: string; kind: TraceKind;
  step: number; uri: string; summary: string;
};
export type ToolEntry = { type: 'tool'; version: number; at: string; call: BoardToolCall };
export type ConversationEntry = TraceEntry | ToolEntry;

function isTraceKind(value: unknown): value is TraceKind {
  return value === 'initial_context' || value === 'board_update' || value === 'model_output';
}

export function conversationEntries(agentId: string, events: readonly BoardEvent[], state: BoardState): ConversationEntry[] {
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
    });
  }
  const tools: ToolEntry[] = Object.values(state.toolCalls ?? {})
    .filter((call) => call.agentId === agentId && call.version <= cutoff)
    .map((call) => ({ type: 'tool', version: call.version, at: call.createdAt, call }));
  return [...traces, ...tools].sort((a, b) => a.version - b.version || (a.type === 'trace' ? -1 : 1));
}
