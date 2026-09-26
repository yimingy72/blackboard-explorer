import type { Page } from '@playwright/test';
import { installMockApi, TASK_ID } from './fixtures';

export const INITIAL_CONTEXT = '目标：核对月度汇总。验收：差异必须附原始行依据。';
export const BOARD_UPDATE = '[黑板更新] F2 已提交：两份汇总的日期口径不同。';
export const MODEL_OUTPUT = '已核对日期列，接下来检查原始行与汇总规则。';
export const MODEL_REASONING = '先核对两份资料的日期范围，再检查差异是否来自统计口径。';

/** Adds provider-style trace records without any external model request. */
export async function installConversationFixture(page: Page, options: { running?: boolean } = {}) {
  const mock = await installMockApi(page);
  type FixtureEvent = typeof mock.events[number];
  const bodies = new Map<string, { text: string; reasoning?: string }>();
  const raw: FixtureEvent[] = [];
  const trace = (kind: string, text: string, step: number, reasoning?: string): FixtureEvent => {
    const uri = `traces/${TASK_ID}/agent-1/${kind}.json`;
    bodies.set(uri, { text, ...(reasoning ? { reasoning } : {}) });
    return {
      ...mock.events[2], type: 'agent.trace.recorded',
      payload: { agent_id: 'agent-1', kind, step, uri, summary: text },
    };
  };
  for (const event of mock.events) {
    if (options.running && event.type === 'agent.finished') break;
    raw.push(event);
    if (event.type === 'agent.spawned') raw.push(trace('initial_context', INITIAL_CONTEXT, 0));
    if (event.type === 'fact.posted') raw.push(trace('board_update', BOARD_UPDATE, 1));
    if (event.type === 'tool_call.recorded') raw.push(trace('model_output', MODEL_OUTPUT, 2, MODEL_REASONING));
  }
  const events = raw.map((event, index) => ({
    ...event, version: index + 1,
    created_at: new Date(Date.parse('2026-09-24T08:00:00Z') + index * 10_000).toISOString(),
  }));
  await page.route(`**/api/tasks/${TASK_ID}/events?*`, (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify(events),
  }));
  await page.route('**/api/evidence?*', async (route) => {
    const uri = new URL(route.request().url()).searchParams.get('uri') ?? '';
    const body = bodies.get(uri);
    if (!body) return route.fallback();
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  });
  return { ...mock, events };
}
