import { describe, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import * as eventStream from '../api/events';

import { connectBoard, useBoardStore } from './store';
import { boardFromSnapshot, type BoardSnapshot } from './snapshot';
import type { BoardEvent } from './types';

const taskOne = '11111111-1111-4111-8111-111111111111';
const taskTwo = '22222222-2222-4222-8222-222222222222';

function created(taskId: string, version: number): BoardEvent {
  return {
    version,
    task_id: taskId,
    type: 'task.created',
    actor: 'scheduler',
    created_at: '2026-01-01T00:00:00Z',
    payload: { goal: taskId, acceptance: [], budget: {}, usage: {} },
  };
}

describe('board event store', () => {
  it('starts from a current snapshot and advances with a later event',()=>{
    useBoardStore.getState().reset(taskOne);
    const hydrated=boardFromSnapshot({task:{version:4,goal:taskOne,status:'running',acceptance:[],budget:{},usage:{}},facts:{},intents:{},agents:{},last_change_version:4});
    useBoardStore.getState().hydrate(taskOne,hydrated.state);
    expect(useBoardStore.getState().state.task?.status).toBe('running');
    useBoardStore.getState().append(taskOne,[{...created(taskOne,5),type:'task.finished',payload:{}}]);
    expect(useBoardStore.getState().state.task?.status).toBe('finished');
    expect(useBoardStore.getState().events.map(event=>event.version)).toEqual([5]);
    useBoardStore.getState().reset(null);
  });

  it('advances new events without mutating past snapshots and repairs an older gap',()=>{
    useBoardStore.getState().reset(taskOne);
    const first=created(taskOne,1);
    useBoardStore.getState().append(taskOne,[first]);
    const before=useBoardStore.getState().state;
    const ended={...first,version:3,type:'task.finished' as const,payload:{}};
    useBoardStore.getState().append(taskOne,[ended]);
    expect(before.task?.status).toBe('created');
    const endedState=useBoardStore.getState().state;
    useBoardStore.getState().append(taskOne,[ended]);
    expect(useBoardStore.getState().state).toBe(endedState);
    useBoardStore.getState().append(taskOne,[{...first,version:2,type:'task.running' as const,payload:{}}]);
    expect(useBoardStore.getState().events.map(event=>event.version)).toEqual([1,2,3]);
    expect(useBoardStore.getState().state.task?.status).toBe('finished');
    useBoardStore.getState().reset(null);
  });
  it('preserves the full ordered log, drops duplicates, and clears on task switch', () => {
    useBoardStore.getState().reset(taskOne);
    const first = created(taskOne, 1);
    const second = { ...first, version: 2, type: 'task.running' as const, payload: { status: 'running' } };
    useBoardStore.getState().append(taskOne, [second, first, second]);
    expect(useBoardStore.getState().events.map((event) => event.version)).toEqual([1, 2]);
    expect(useBoardStore.getState().state.task?.status).toBe('running');

    useBoardStore.getState().reset(taskTwo);
    useBoardStore.getState().append(taskOne, [first]);
    expect(useBoardStore.getState().events).toEqual([]);
    expect(useBoardStore.getState().state.task).toBeNull();
    useBoardStore.getState().append(taskTwo, [created(taskOne, 4), created(taskTwo, 5)]);
    expect(useBoardStore.getState().events.map((event) => event.version)).toEqual([5]);
    expect(useBoardStore.getState().state.task?.goal).toBe(taskTwo);
    useBoardStore.getState().reset(null);
  });
});

it('keeps snapshot facts when an SSE gap arrives out of order', () => {
  useBoardStore.getState().reset(taskOne);
  const { state } = boardFromSnapshot({
    task: { version: 10, goal: 'Snapshot goal', status: 'running', acceptance: [], budget: {}, usage: {} },
    facts: { F1: { id: 'F1', author: 'agent-1', statement: 'Existing fact', version: 2 } }, intents: {}, agents: {},
  });
  useBoardStore.getState().hydrate(taskOne, state);
  useBoardStore.getState().append(taskOne, [{ ...created(taskOne, 12), type: 'task.finished', payload: {} }]);
  useBoardStore.getState().append(taskOne, [{ ...created(taskOne, 11), type: 'task.closing', payload: { reason: 'budget' } }]);
  expect(useBoardStore.getState().state.task?.goal).toBe('Snapshot goal');
  expect(useBoardStore.getState().state.task?.status).toBe('finished');
  expect(useBoardStore.getState().state.facts.F1.statement).toBe('Existing fact');
  useBoardStore.getState().append(taskOne, [created(taskOne, 1)]);
  expect(useBoardStore.getState().state.task?.goal).toBe('Snapshot goal');
  useBoardStore.getState().reset(null);
});

it('ignores an old history response after navigating away and back to the same task', async () => {
  let resolve!: (events: BoardEvent[]) => void;
  const history = vi.spyOn(api, 'getEvents').mockImplementation(() => new Promise(done => { resolve = done; }));
  try {
    useBoardStore.getState().reset(taskOne);
    const pending = useBoardStore.getState().ensureHistory(taskOne);
    const signal = history.mock.calls[0][2];
    expect(signal?.aborted).toBe(false);
    useBoardStore.getState().reset(taskTwo);
    expect(signal?.aborted).toBe(true);
    useBoardStore.getState().reset(taskOne);
    useBoardStore.getState().append(taskOne, [{ ...created(taskOne, 15), payload: { goal: 'Current visit' } }]);
    resolve([created(taskOne, 1)]);
    await pending;
    expect(useBoardStore.getState().events.map(event => event.version)).toEqual([15]);
    expect(useBoardStore.getState().historyLoaded).toBe(false);
  } finally { history.mockRestore(); useBoardStore.getState().reset(null); }
});

it('finds duplicate versions without scanning full history and preserves stable references', () => {
  const history = Array.from({ length: 10000 }, (_, index) => ({ ...created(taskOne, index + 1), type: 'agent.trace.recorded' as const }));
  let visits = 0;
  const events = new Proxy(history, { get(target, key, receiver) {
    if (typeof key === 'string' && /^\d+$/.test(key)) visits++;
    return Reflect.get(target, key, receiver);
  } });
  useBoardStore.getState().reset(taskOne);
  useBoardStore.setState({ events });
  const before = useBoardStore.getState().state;
  useBoardStore.getState().append(taskOne, [history[4000], history[9999], history[4000]]);
  expect(visits).toBeLessThan(40);
  expect(useBoardStore.getState().state).toBe(before);
  expect(useBoardStore.getState().events).toBe(events);
  useBoardStore.getState().reset(null);
});

it('does not let an older aborted history clear ownership of a newer request', async () => {
  const replies: Array<(events: BoardEvent[]) => void> = [];
  const history = vi.spyOn(api, 'getEvents').mockImplementation(() => new Promise(done => replies.push(done)));
  try {
    useBoardStore.getState().reset(taskOne);
    const old = useBoardStore.getState().ensureHistory(taskOne);
    useBoardStore.getState().reset(taskTwo);
    const latest = useBoardStore.getState().ensureHistory(taskTwo);
    replies[0]([created(taskOne, 1)]);
    await old;
    expect(history.mock.calls[1][2]?.aborted).toBe(false);
    useBoardStore.getState().reset(null);
    expect(history.mock.calls[1][2]?.aborted).toBe(true);
    replies[1]([created(taskTwo, 1)]);
    await latest;
  } finally { history.mockRestore(); useBoardStore.getState().reset(null); }
});

it('aborts a disconnected snapshot and ignores its late response', async () => {
  let resolve!: (snapshot: BoardSnapshot) => void;
  const snapshot = vi.spyOn(api, 'getState').mockImplementation(() => new Promise(done => { resolve = done; }));
  const stream = vi.spyOn(eventStream, 'openBoardStream');
  try {
    const disconnect = connectBoard(taskOne);
    const signal = snapshot.mock.calls[0][1];
    expect(signal?.aborted).toBe(false);
    disconnect?.();
    expect(signal?.aborted).toBe(true);
    resolve({ task: { version: 1 }, facts: {}, intents: {}, agents: {} });
    await Promise.resolve();
    expect(useBoardStore.getState().taskId).toBeNull();
    expect(stream).not.toHaveBeenCalled();
  } finally { snapshot.mockRestore(); stream.mockRestore(); useBoardStore.getState().reset(null); }
});

it('isolates stale stream callbacks and cleanup when a newer task owns the store', async () => {
  const handlers: eventStream.StreamHandlers[] = [];
  const snapshot = vi.spyOn(api, 'getState').mockResolvedValue({ task: { version: 1 }, facts: {}, intents: {}, agents: {} });
  const close = vi.fn();
  const stream = vi.spyOn(eventStream, 'openBoardStream').mockImplementation((_task, _since, callbacks) => {
    handlers.push(callbacks);
    return { close } as unknown as EventSource;
  });
  let disconnectFirst: (() => void) | undefined, disconnectSecond: (() => void) | undefined;
  try {
    disconnectFirst = connectBoard(taskOne);
    await Promise.resolve();
    disconnectSecond = connectBoard(taskTwo);
    await Promise.resolve();
    handlers[0].onConnection('closed');
    handlers[0].onError?.(new Error('late failure'));
    disconnectFirst?.();
    expect(useBoardStore.getState().taskId).toBe(taskTwo);
    expect(useBoardStore.getState().error).toBeNull();
    expect(useBoardStore.getState().connection).toBe('connecting');
    expect(snapshot.mock.calls[0][1]?.aborted).toBe(true);
    expect(snapshot.mock.calls[1][1]?.aborted).toBe(false);
  } finally {
    disconnectSecond?.(); snapshot.mockRestore(); stream.mockRestore(); useBoardStore.getState().reset(null);
  }
});
