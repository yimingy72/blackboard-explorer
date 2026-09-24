import { describe, expect, it } from 'vitest';

import { useBoardStore } from './store';
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
