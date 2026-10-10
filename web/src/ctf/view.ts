import type { CtfEvent, CtfMember, CtfMessage, CtfReasoning, CtfSession, CtfToolCall } from './types';

export const workLabels: Record<string, string> = { pending: '待领取', in_progress: '进行中', blocked: '需要增援', completed: '负责人已完成', cancelled: '已取消' };
export const phaseLabels: Record<string, string> = { created: '待启动', provisioning: '准备中', running: '进行中', closing: '收尾中', finished: '已结束', stopped: '已停止', failed: '失败' };
export function memberStatus(member: CtfMember): string {
  if (member.lifecycle === 'removed') return '已移除 · 历史保留';
  if (member.run_state === 'stopping') return '正在停止';
  if (member.lifecycle === 'stopped') return '已停止';
  return ({ running: '工作中', idle: '等待任务', failed: '执行失败', interrupted: '执行中断', provisioning: '准备中' } as Record<string, string>)[member.run_state] ?? member.run_state;
}
export function deliveryLabel(message: CtfMessage): string {
  if (message.deferred) return '收尾后待处理';
  return ({ queued: '等待送达', leased: '正在投递', delivered: '已送达', cancelled: '已取消', failed: '投递失败' } as Record<string, string>)[message.status] ?? message.status;
}
export function composeHint(phase: string, member: CtfMember): string {
  if (isReviewPhase(phase)) return '只读复盘：可查阅记录与证据，费用单独记录；消息不会续跑任务或恢复成员。';
  if (member.lifecycle === 'removed') return '该成员已移除，仅保留会话历史。';
  if (phase === 'closing') return '任务正在收尾，新消息会保留为待处理，不再启动执行。';
  if (member.lifecycle === 'stopped') return '消息会排队保留；成员只有在明确恢复后才继续执行。';
  return member.role === 'lead' ? '告诉 Lead 下一步目标、补充信息或调整方向。' : '消息直接送达此成员，不需要经 Lead 转发。';
}
export function canSend(phase: string, member: CtfMember): boolean {
  return isReviewPhase(phase) || member.lifecycle !== 'removed' && ['created', 'provisioning', 'running', 'closing'].includes(phase);
}
export function verificationLabel(verification: Record<string, unknown>): string {
  const status = String(verification.status ?? 'unknown');
  const label = ({ unknown: '待核实', candidate: '候选答案', accepted: '已通过', rejected: '未通过', pending: '待验证' } as Record<string, string>)[status] ?? status;
  const source = verification.source === 'platform' ? (verification.test_only ? '模拟平台' : '平台') : verification.source === 'user' ? '人工' : '待核实';
  return verification.source === 'platform' || verification.source === 'user' ? `${source} · ${label}` : label;
}
export function acceptCursor(taskId: string, cursor: number, event: CtfEvent): number {
  return event.task_id === taskId && Number.isInteger(event.version) && event.version > cursor ? event.version : cursor;
}
export function mergeCtfEvents(current: CtfEvent[], incoming: CtfEvent[]): CtfEvent[] {
  const all = new Map(current.map((event) => [event.version, event]));
  incoming.forEach((event) => all.set(event.version, event));
  return [...all.values()].sort((a, b) => a.version - b.version);
}
function object(value: unknown): Record<string, unknown> { return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function toolValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(toolValue);
  if (value !== null && typeof value === 'object') return Object.fromEntries(Object.entries(value).filter(([key]) => !['protected_data', 'encrypted_content', 'raw_representation'].includes(key)).map(([key, item]) => [key, toolValue(item)]));
  return value;
}
function applyToolResult(call: CtfToolCall, content: Record<string, unknown>): boolean {
  if (content.type !== 'function_result' || content.call_id !== call.id) return false;
  const validItems = Array.isArray(content.items) && content.items.every((item) => typeof object(item).type === 'string');
  const hasException = typeof content.exception === 'string' && content.exception !== '' || content.exception !== null && typeof content.exception === 'object' && !Array.isArray(content.exception);
  if (content.result === undefined && !validItems && !hasException) return false;
  call.result = toolValue(content.result !== undefined ? content.result : validItems ? content.items : content.exception);
  const items = validItems ? (content.items as unknown[]).map(object) : [];
  call.status = hasException || items.some((value) => value.type === 'error') ? 'failed' : 'completed';
  return true;
}
export type ChatEntry = { id: string; speaker: string; body: string; assistant: boolean; delivery?: CtfMessage; tools: number; reasoning: CtfReasoning[]; toolCalls: CtfToolCall[] };
export function conversationEntries(session: CtfSession | null | undefined, messages: CtfMessage[], memberId: string): ChatEntry[] {
  const state = object(session?.session.state);
  const raw = object(state.in_memory).messages;
  const seen = new Set<string>();
  const entries: ChatEntry[] = [];
  const calls = new Map<string, CtfToolCall>();
  for (const [index, item] of (Array.isArray(raw) ? raw : []).entries()) {
    const message = object(item);
    const role = String(message.role ?? '');
    const contents = Array.isArray(message.contents) ? message.contents.map(object) : [];
    if (role === 'tool') {
      for (const content of contents) {
        if (content.type !== 'function_result' || typeof content.call_id !== 'string') continue;
        const call = calls.get(content.call_id);
        if (!call) continue;
        if (applyToolResult(call, content)) calls.delete(content.call_id);
      }
      continue;
    }
    if (!['user', 'assistant'].includes(role)) continue;
    const id = String(message.message_id ?? message.id ?? `session-${index}`);
    if (seen.has(id)) continue;
    const delivery = messages.find((value) => value.id === id);
    const source = object(object(message.additional_properties).ctf_source);
    const text = contents.filter((content) => typeof content.text === 'string' && (!content.type || content.type === 'text')).map((content) => String(content.text)).join('\n');
    const reasoning: CtfReasoning[] = [];
    const toolCalls: CtfToolCall[] = [];
    if (role === 'assistant') for (const [contentIndex, content] of contents.entries()) {
      const properties = object(content.additional_properties);
      if (typeof properties.deepseek_reasoning === 'string' && properties.deepseek_reasoning.trim()) reasoning.push({ text: properties.deepseek_reasoning, kind: 'reasoning' });
      if (content.type === 'text_reasoning' && typeof content.text === 'string' && content.text.trim()) reasoning.push({ text: content.text, kind: properties.reasoning_text === true ? 'reasoning' : 'summary' });
      if (content.type !== 'function_call') continue;
      const call: CtfToolCall = { id: typeof content.call_id === 'string' ? content.call_id : typeof content.id === 'string' ? content.id : `${id}:tool-${contentIndex}`, name: typeof content.name === 'string' ? content.name : '未命名工具', arguments: toolValue(content.arguments), status: 'pending' };
      toolCalls.push(call);
      if (typeof content.call_id === 'string') calls.set(content.call_id, call);
    }
    const tools = toolCalls.length;
    const body = delivery?.body ?? (source.id && text.includes('正文（任务材料）：\n') ? text.split('正文（任务材料）：\n').slice(1).join('正文（任务材料）：\n') : text);
    if (!body.trim() && !tools && !reasoning.length) continue;
    seen.add(id);
    entries.push({ id, speaker: role === 'assistant' ? memberId : String(source.sender_id ?? 'user'), body, assistant: role === 'assistant', delivery, tools, reasoning, toolCalls });
  }
  const savedResults = object(state.bbx_tool_results);
  for (const [callId, call] of calls) applyToolResult(call, object(savedResults[callId]));
  const reviewResults = new Set<string>();
  for (const message of messages) {
    if (seen.has(message.id)) continue;
    if (message.kind === 'review_result') {
      // Match only the reply's own input span, never globally identical answer text.
      const input = entries.findIndex((entry) => entry.id === message.reply_to);
      const span = input < 0 ? [] : entries.slice(input + 1);
      const boundary = span.findIndex((entry) => !entry.assistant);
      const answers = boundary < 0 ? span : span.slice(0, boundary);
      const represented = answers.some((entry) => entry.assistant && (entry.body === message.body || message.body.length === 20000 && entry.body.startsWith(message.body)));
      const resultKey = message.source_turn_id ? `${message.source_turn_id}:${message.body}` : message.id;
      if (represented || reviewResults.has(resultKey)) { reviewResults.add(resultKey); continue; }
      reviewResults.add(resultKey);
    }
    seen.add(message.id);
    entries.push({ id: message.id, speaker: message.sender_id, body: message.body, assistant: message.kind === 'review_result', delivery: message, tools: 0, reasoning: [], toolCalls: [] });
  }
  return entries;
}

export function recoveryBlocker(task: { status: string; ctf_phase?: string | null; cleanup_ready?: boolean; workspace_uri?: string | null }): string | null {
  if (!['finished', 'stopped', 'failed'].includes(task.status)) return '本轮仍在执行或收尾，请等待结束后再续跑。';
  if (!task.cleanup_ready) return '归档和本机清理尚未完成。若保存失败，执行环境将保留以供重试。';
  if (!task.workspace_uri) return '没有可恢复的已登记归档，暂不能续跑。';
  return null;
}

export function isReviewPhase(phase: string): boolean {
  return ['finished', 'failed', 'stopped'].includes(phase);
}
