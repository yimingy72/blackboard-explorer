import { describe, expect, it } from 'vitest';
import { acceptCursor, canSend, composeHint, conversationEntries, deliveryLabel, memberStatus, mergeCtfEvents, recoveryBlocker, verificationLabel, workLabels } from './view';
import type { CtfMember, CtfMessage } from './types';
const member: CtfMember = { id: 'member-1', role: 'teammate', display_name: 'Web', lifecycle: 'active', run_state: 'idle', generation: 1 };
const message: CtfMessage = { id: 'message-1', sender_id: 'user', sender_kind: 'user', recipient_id: 'member-1', kind: 'message', body: 'Check this route', status: 'queued', deferred: false };
describe('CTF workbench boundaries', () => {
  it('ignores foreign-task, duplicate and out-of-order events', () => {
    const event = { task_id: 'task', version: 8, type: 'ctf.message.posted', payload: {} };
    expect(acceptCursor('task', 7, event)).toBe(8);
    expect(acceptCursor('other', 7, event)).toBe(7);
    expect(acceptCursor('task', 8, event)).toBe(8);
    expect(acceptCursor('task', 9, event)).toBe(9);
  });
  it('merges reconnect history without duplicating versions', () => {
    const first = { task_id: 'task', version: 8, type: 'ctf.turn.started', payload: {} };
    const second = { task_id: 'task', version: 9, type: 'ctf.turn.finished', payload: {} };
    expect(mergeCtfEvents([first], [first, second, first]).map((event) => event.version)).toEqual([8, 9]);
  });
  it('keeps stopped messaging queued without implying resume', () => {
    const stopped = { ...member, lifecycle: 'stopped' as const };
    expect(canSend('running', stopped)).toBe(true);
    expect(composeHint('running', stopped)).toContain('明确恢复');
    expect(deliveryLabel(message)).toBe('等待送达');
    expect(memberStatus(stopped)).toBe('已停止');
  });
  it('marks closing messages deferred and terminal review separate from execution', () => {
    expect(canSend('closing', member)).toBe(true);
    expect(deliveryLabel({ ...message, deferred: true })).toBe('收尾后待处理');
    expect(composeHint('closing', member)).toContain('不再启动执行');
    for (const phase of ['finished', 'stopped', 'failed']) {
      expect(canSend(phase, member)).toBe(true);
      expect(canSend(phase, { ...member, lifecycle: 'removed' })).toBe(true);
      expect(composeHint(phase, member)).toContain('只读复盘');
    }
    expect(canSend('running', { ...member, lifecycle: 'removed' })).toBe(false);
    expect(memberStatus({ ...member, lifecycle: 'removed' })).toContain('历史保留');
  });
  it('distinguishes owner claims, manual and simulated platform verification', () => {
    expect(workLabels.completed).toBe('负责人已完成');
    expect(verificationLabel({ status: 'accepted', source: 'platform', test_only: true })).toBe('模拟平台 · 已通过');
    expect(verificationLabel({ status: 'accepted', source: 'user' })).toBe('人工 · 已通过');
    expect(verificationLabel({ status: 'candidate', source: 'member' })).toBe('候选答案');
  });
  it('deduplicates mailbox messages already stored in session and retains pending messages', () => {
    const session = { revision: 2, session: { state: { in_memory: { messages: [
      { role: 'user', message_id: message.id, contents: [{ type: 'text', text: 'source wrapper' }] },
      { role: 'assistant', message_id: 'answer', contents: [{ type: 'text', text: 'Working on it' }, { type: 'function_call' }] },
    ] } } } };
    const entries = conversationEntries(session, [message, { ...message, id: 'pending' }], member.id);
    expect(entries.map((item) => item.id)).toEqual(['message-1', 'answer', 'pending']);
    expect(entries[0].body).toBe(message.body);
    expect(entries[1]).toMatchObject({ speaker: member.id, assistant: true, tools: 1 });
    expect(conversationEntries(null, [message, message], member.id)).toHaveLength(1);
  });
  it('keeps provider reasoning and summaries separate from answers and opaque data', () => {
    const session = { revision: 1, session: { state: { in_memory: { messages: [
      { role: 'user', message_id: 'input', contents: [{ type: 'text', text: 'Question', additional_properties: { deepseek_reasoning: 'User-authored metadata' } }] },
      { role: 'assistant', message_id: 'reasoning-only', contents: [
        { type: 'text_reasoning', text: 'Provider text', protected_data: 'opaque-provider-data', additional_properties: { reasoning_text: true }, raw_representation: { text: 'private-raw-object' } },
        { type: 'text_reasoning', text: 'Provider summary' },
      ] },
      { role: 'assistant', message_id: 'answer', contents: [{ type: 'text', text: 'Answer', additional_properties: { deepseek_reasoning: 'DeepSeek text', encrypted_content: 'opaque-provider-data' } }] },
      { role: 'assistant', message_id: 'opaque-only', contents: [{ type: 'text_reasoning', text: '', protected_data: 'opaque-provider-data' }, { type: 'usage', usage_details: { reasoning_output_token_count: 42 } }] },
    ] } } } };
    const entries = conversationEntries(session, [], member.id);
    expect(entries.map((entry) => entry.id)).toEqual(['input', 'reasoning-only', 'answer']);
    expect(entries[0].reasoning).toEqual([]);
    expect(entries[1]).toMatchObject({ body: '', assistant: true, reasoning: [{ text: 'Provider text', kind: 'reasoning' }, { text: 'Provider summary', kind: 'summary' }] });
    expect(entries[2]).toMatchObject({ body: 'Answer', reasoning: [{ text: 'DeepSeek text', kind: 'reasoning' }] });
    expect(JSON.stringify(entries)).not.toContain('opaque-provider-data');
    expect(JSON.stringify(entries)).not.toContain('private-raw-object');
  });
  it('pairs parallel tool results by call id and preserves arguments and structured output', () => {
    const argumentsText = '{\n  "command": "inspect"\n}';
    const session = { revision: 1, session: { state: { in_memory: { messages: [
      { role: 'assistant', message_id: 'tool-turn', contents: [
        { type: 'function_call', call_id: 'call-a', name: 'execute_command', arguments: argumentsText, raw_representation: { value: 'private-raw-object' } },
        { type: 'function_call', call_id: 'call-b', name: 'list_records', arguments: { limit: 10, nested: { protected_data: 'opaque-provider-data', category: 'note' } } },
        { type: 'function_call', call_id: 'call-c', name: 'pending_tool', arguments: {} },
      ] },
      { role: 'tool', message_id: 'results', contents: [
        { type: 'function_result', call_id: 'call-b', result: { rows: [{ id: 'record', encrypted_content: 'opaque-provider-data' }], raw_representation: { value: 'private-raw-object' } } },
        { type: 'function_result', call_id: 'call-a', result: 'line one\nline two' },
        { type: 'function_result', call_id: 'unmatched', result: 'unrelated result' },
      ] },
    ] } } } };
    const entries = conversationEntries(session, [], member.id);
    expect(entries).toHaveLength(1);
    expect(entries[0].tools).toBe(3);
    expect(entries[0].toolCalls).toEqual([
      { id: 'call-a', name: 'execute_command', arguments: argumentsText, result: 'line one\nline two', status: 'completed' },
      { id: 'call-b', name: 'list_records', arguments: { limit: 10, nested: { category: 'note' } }, result: { rows: [{ id: 'record' }] }, status: 'completed' },
      { id: 'call-c', name: 'pending_tool', arguments: {}, status: 'pending' },
    ]);
    expect(JSON.stringify(entries)).not.toContain('opaque-provider-data');
    expect(JSON.stringify(entries)).not.toContain('private-raw-object');
    expect(JSON.stringify(entries)).not.toContain('unrelated result');
    expect(session.session.state.in_memory.messages[1].contents[0]).toHaveProperty('result.rows.0.encrypted_content', 'opaque-provider-data');
  });
  it('keeps missing and failed tool outcomes distinct and ignores results before their calls', () => {
    const session = { revision: 1, session: { state: { in_memory: { messages: [
      { role: 'tool', contents: [{ type: 'function_result', call_id: 'late', result: 'old result' }] },
      { role: 'assistant', message_id: 'calls', contents: [
        { type: 'function_call', call_id: 'failed', name: 'failed_tool', arguments: {} },
        { type: 'function_call', call_id: 'error-items', name: 'error_tool', arguments: {} },
        { type: 'function_call', call_id: 'late', name: 'pending_tool', arguments: {} },
      ] },
      { role: 'tool', contents: [
        { type: 'function_result', call_id: 'failed', exception: 'redacted', result: { error: { message: 'tool failed', protected_data: 'opaque-provider-data' } } },
        { type: 'function_result', call_id: 'error-items', items: [{ type: 'error', message: 'reported error', raw_representation: 'private-raw-object' }] },
      ] },
      { role: 'tool', contents: [{ type: 'function_result', call_id: 'failed', result: 'duplicate result' }] },
    ] } } } };
    const [entry] = conversationEntries(session, [], member.id);
    expect(entry.toolCalls).toEqual([
      { id: 'failed', name: 'failed_tool', arguments: {}, result: { error: { message: 'tool failed' } }, status: 'failed' },
      { id: 'error-items', name: 'error_tool', arguments: {}, result: [{ type: 'error', message: 'reported error' }], status: 'failed' },
      { id: 'late', name: 'pending_tool', arguments: {}, status: 'pending' },
    ]);
  });
  it('retains exception-only results safely and supports serialized text item arrays', () => {
    const session = { revision: 1, session: { state: { in_memory: { messages: [
      { role: 'assistant', message_id: 'calls', contents: [
        { type: 'function_call', call_id: 'exception-only', name: 'failed_tool', arguments: {} },
        { type: 'function_call', call_id: 'text-items', name: 'text_tool', arguments: {} },
        { type: 'function_call', call_id: 'normal-result', name: 'result_tool', arguments: {} },
      ] },
      { role: 'tool', contents: [
        { type: 'function_result', call_id: 'exception-only', exception: { message: 'tool exception', protected_data: 'opaque-provider-data', details: { raw_representation: 'private-raw-object', code: 'tool_error' } } },
        { type: 'function_result', call_id: 'text-items', items: [{ type: 'text', text: 'line one\nline two', protected_data: 'opaque-provider-data' }, { type: 'text', text: 'second item' }] },
        { type: 'function_result', call_id: 'normal-result', result: null, items: [{ type: 'text', text: 'fallback text' }], exception: 'redacted' },
      ] },
    ] } } } };
    const [entry] = conversationEntries(session, [], member.id);
    expect(entry.toolCalls).toEqual([
      { id: 'exception-only', name: 'failed_tool', arguments: {}, result: { message: 'tool exception', details: { code: 'tool_error' } }, status: 'failed' },
      { id: 'text-items', name: 'text_tool', arguments: {}, result: [{ type: 'text', text: 'line one\nline two' }, { type: 'text', text: 'second item' }], status: 'completed' },
      { id: 'normal-result', name: 'result_tool', arguments: {}, result: null, status: 'failed' },
    ]);
  });
  it('uses saved checkpoint results for pending calls while preserving history results', () => {
    const session = { revision: 1, session: { state: {
      in_memory: { messages: [
        { role: 'assistant', message_id: 'calls', contents: [
          { type: 'function_call', call_id: 'saved-only', name: 'saved_tool', arguments: {} },
          { type: 'function_call', call_id: 'history', name: 'history_tool', arguments: {} },
          { type: 'function_call', call_id: 'saved-error', name: 'error_tool', arguments: {} },
        ] },
        { role: 'tool', contents: [{ type: 'function_result', call_id: 'history', result: 'history result' }] },
      ] },
      bbx_tool_results: {
        'saved-only': { type: 'function_result', call_id: 'saved-only', result: 'saved result', items: [{ type: 'text', text: 'saved result' }], protected_data: 'opaque-provider-data' },
        history: { type: 'function_result', call_id: 'history', result: 'stale saved result', exception: 'redacted' },
        'saved-error': { type: 'function_result', call_id: 'saved-error', exception: { message: 'saved exception', encrypted_content: 'opaque-provider-data' } },
        unknown: { type: 'function_result', call_id: 'unknown', result: 'unrelated saved result' },
      },
    } } };
    const [entry] = conversationEntries(session, [], member.id);
    expect(entry.toolCalls).toEqual([
      { id: 'saved-only', name: 'saved_tool', arguments: {}, result: 'saved result', status: 'completed' },
      { id: 'history', name: 'history_tool', arguments: {}, result: 'history result', status: 'completed' },
      { id: 'saved-error', name: 'error_tool', arguments: {}, result: { message: 'saved exception' }, status: 'failed' },
    ]);
    expect(JSON.stringify(entry)).not.toContain('opaque-provider-data');
    expect(JSON.stringify(entry)).not.toContain('unrelated saved result');
  });
  it('keeps pending calls unchanged for unknown keys and malformed checkpoint results', () => {
    const ids = ['missing-id', 'wrong-id', 'wrong-type', 'missing-output', 'bad-items', 'unknown-key'];
    const session = { revision: 1, session: { state: {
      in_memory: { messages: [{ role: 'assistant', message_id: 'calls', contents: ids.map((id) => ({ type: 'function_call', call_id: id, name: 'pending_tool', arguments: {} })) }] },
      bbx_tool_results: {
        'missing-id': { type: 'function_result', result: 'missing id' },
        'wrong-id': { type: 'function_result', call_id: 'different-id', result: 'mismatched id' },
        'wrong-type': { type: 'text', call_id: 'wrong-type', text: 'wrong type' },
        'missing-output': { type: 'function_result', call_id: 'missing-output' },
        'bad-items': { type: 'function_result', call_id: 'bad-items', items: ['not a content item'] },
        unrelated: { type: 'function_result', call_id: 'unknown-key', result: 'wrong map key' },
      },
    } } };
    const [entry] = conversationEntries(session, [], member.id);
    expect(entry.toolCalls).toEqual(ids.map((id) => ({ id, name: 'pending_tool', arguments: {}, status: 'pending' })));
  });
});

describe('CTF explicit recovery', () => {
  it('requires terminal state, cleanup and a recoverable archive independently', () => {
    expect(recoveryBlocker({ status: 'running', cleanup_ready: true, workspace_uri: 'archive' })).toContain('等待结束');
    expect(recoveryBlocker({ status: 'stopped', ctf_phase: 'closed', cleanup_ready: false, workspace_uri: 'archive' })).toContain('清理尚未完成');
    expect(recoveryBlocker({ status: 'failed', cleanup_ready: true, workspace_uri: null })).toContain('没有可恢复');
    expect(recoveryBlocker({ status: 'finished', ctf_phase: 'closed', cleanup_ready: true, workspace_uri: 'archive' })).toBeNull();
  });
});

describe('review result identity', () => {
  it('hides a mailbox receipt only within its referenced input span', () => {
    const session = { revision: 2, session: { state: { in_memory: { messages: [
      { role: 'user', message_id: 'question-1', contents: [{ type: 'text', text: 'Question one' }] },
      { role: 'assistant', message_id: 'answer-1', contents: [{ type: 'text', text: 'Same text' }] },
      { role: 'user', message_id: 'question-2', contents: [{ type: 'text', text: 'Question two' }] },
      { role: 'assistant', message_id: 'answer-2', contents: [{ type: 'text', text: 'Same text' }] },
    ] } } } };
    const result = { ...message, kind: 'review_result', sender_id: member.id, body: 'Same text', reply_to: 'question-1', source_turn_id: 'turn-1' };
    const entries = conversationEntries(session, [result, { ...result, id: 'receipt-2', reply_to: 'question-2', source_turn_id: 'turn-2' }], member.id);
    expect(entries.filter((entry) => entry.body === 'Same text')).toHaveLength(2);
    expect(conversationEntries(session, [{ ...result, reply_to: 'missing', source_turn_id: 'turn-3' }], member.id)).toHaveLength(5);
    expect(conversationEntries(session, [{ ...result, body: 'Different final answer' }], member.id)).toHaveLength(5);
  });
});
