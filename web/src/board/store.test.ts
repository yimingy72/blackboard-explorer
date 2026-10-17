import { describe, expect, it } from 'vitest';

import { useBoardStore } from './store';
import { boardFromSnapshot } from './snapshot';
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
