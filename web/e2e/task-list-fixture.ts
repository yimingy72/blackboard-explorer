import type { Route } from '@playwright/test';

export const taskListRoute = /\/api\/tasks(?:\/page)?(?:\?.*)?$/;
export function taskListResponse(tasks: Array<Record<string, unknown>>, url: string) {
  const { pathname, searchParams } = new URL(url);
  if (!pathname.endsWith('/page')) return tasks;
  const q = (searchParams.get('q') ?? '').trim().toLocaleLowerCase();
  const status = searchParams.get('status');
  const matching = tasks.filter(task => `${task.name ?? ''} ${task.goal ?? ''} ${task.id}`.toLocaleLowerCase().includes(q));
  const status_counts: Record<string, number> = {};
  for (const task of matching) status_counts[String(task.status)] = (status_counts[String(task.status)] ?? 0) + 1;
  const states = status === 'active' ? ['created','provisioning','running','closing'] : status === 'ended' ? ['finished','stopped'] : [status];
  const filtered = status ? matching.filter(task => states.includes(String(task.status))) : matching;
  const limit = Number(searchParams.get('limit') ?? 20), offset = Number(searchParams.get('offset') ?? 0);
  return { items: filtered.slice(offset, offset + limit), total: filtered.length, status_counts, limit, offset };
}
export function fulfillTaskList(route: Route, tasks: Array<Record<string, unknown>>) {
  return route.fulfill({ json: taskListResponse(tasks, route.request().url()) });
}
