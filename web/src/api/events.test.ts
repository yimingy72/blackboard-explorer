import { describe, expect, it, vi } from 'vitest';

import { BOARD_EVENT_TYPES, openBoardStream } from './events';
import type { BoardEvent } from '../board/types';

const taskId = '11111111-1111-4111-8111-111111111111';

class FakeEventSource {
  readonly listeners = new Map<string, Array<(event: Event) => void>>();
  closed = false;
  readyState = 0;

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    const callback = typeof listener === 'function' ? listener : listener.handleEvent.bind(listener);
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), callback]);
  }

  emit(type: string, data = ''): void {
    const event = { data } as unknown as Event;
    for (const callback of this.listeners.get(type) ?? []) callback(event);
  }

  close(): void {
    this.closed = true;
  }
}

describe('openBoardStream', () => {
  it('listens to named events and leaves native reconnect on the same EventSource', () => {
    const source = new FakeEventSource();
    const factory = vi.fn((url: string) => {
      expect(url).toBe(`/api/tasks/${taskId}/stream?since=12`);
      return source as unknown as EventSource;
    });
    const received: BoardEvent[] = [];
    const statuses: string[] = [];
    const errors: Error[] = [];
    const connection = openBoardStream(taskId, 12, {
      onEvent: (event) => received.push(event),
      onConnection: (status) => statuses.push(status),
      onError: (error) => errors.push(error),
    }, factory);

    expect(BOARD_EVENT_TYPES.every((type) => source.listeners.has(type))).toBe(true);
    expect(source.listeners.has('message')).toBe(false);
    source.emit('open');
    source.emit('fact.posted', JSON.stringify({ version: 13, task_id: taskId, type: 'fact.posted', actor: 'agent-1', created_at: '2026-01-01T00:00:00Z', payload: { id: 'F1' } }));
    source.emit('error');
    source.emit('open');
    source.emit('fact.disputed', JSON.stringify({ version: 14, task_id: taskId, type: 'fact.disputed', actor: 'system', created_at: '2026-01-01T00:00:01Z', payload: { fact_id: 'F1' } }));
    source.emit('agent.trace.recorded', JSON.stringify({ version: 15, task_id: taskId, type: 'agent.trace.recorded', actor: 'agent-1', created_at: '2026-01-01T00:00:02Z', payload: { agent_id: 'agent-1', kind: 'model_output', uri: 'traces/test/output.json', step: 1, summary: 'New reply' } }));
    expect(received.map((event) => event.version)).toEqual([13, 14, 15]);
    expect(statuses).toEqual(['connecting', 'open', 'reconnecting', 'open']);
    expect(factory).toHaveBeenCalledTimes(1);
    expect(connection).toBe(source);
    expect(errors).toEqual([]);
  });

  it('drops events from another task and reports malformed frames', () => {
    const source = new FakeEventSource();
    const onEvent = vi.fn();
    const onError = vi.fn();
    openBoardStream(taskId, 0, { onEvent, onConnection: vi.fn(), onError },
      () => source as unknown as EventSource);
    source.emit('fact.posted', JSON.stringify({ version: 1, task_id: 'other', type: 'fact.posted' }));
    source.emit('fact.posted', '{');
    expect(onEvent).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(2);
  });

  it('reports a terminal CLOSED error instead of waiting for reconnect', () => {
    const source = new FakeEventSource();
    const factory = vi.fn(() => source as unknown as EventSource);
    const onConnection = vi.fn();
    const onError = vi.fn();
    openBoardStream(taskId, 0, { onEvent: vi.fn(), onConnection, onError }, factory);
    source.readyState = 2;
    source.emit('error');
    expect(onConnection.mock.calls.map(([status]) => status)).toEqual(['connecting', 'closed']);
    expect(onError).toHaveBeenCalledWith(new Error('实时连接已关闭，请重新连接或登录。'));
    expect(factory).toHaveBeenCalledTimes(1);
  });
});
