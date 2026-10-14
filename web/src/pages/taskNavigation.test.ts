import { describe, expect, it } from 'vitest';
import { taskListUrl } from './taskNavigation';

describe('task list return location', () => {
  it('preserves the list query and page', () => {
    expect(taskListUrl({ returnTo: '/tasks?q=monthly&page=2' })).toBe('/tasks?q=monthly&page=2');
  });
  it('uses the list for missing or unrelated navigation state', () => {
    for (const state of [null, undefined, { returnTo: 2 }, { returnTo: '//example.test' }, { returnTo: '/tasks/new' }, { returnTo: '/tasks-extra' }]) {
      expect(taskListUrl(state)).toBe('/tasks');
    }
  });
});
