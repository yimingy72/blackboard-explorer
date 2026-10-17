import { expect, test } from '@playwright/test';
import { GOAL, TASK_ID, installMockApi } from './fixtures';

// Run against a production build with BBX_E2E_PREVIEW_DIR=dist.
test('records cold page script weight for each primary route', async ({ browser, baseURL }, info) => {
  const samples = [];
  for (const [name, path, heading] of [
    ['login', '/login', '进入工作台'], ['tasks', '/tasks', '任务'],
    ['create', '/tasks/new', '创建'], ['settings', '/profiles', 'Agent 配置'],
    ['board', `/tasks/${TASK_ID}`, GOAL], ['report', `/tasks/${TASK_ID}/report`, '最终报告'],
  ]) {
    const context = await browser.newContext({ baseURL });
    const page = await context.newPage();
    const workers: string[] = [];
    page.on('worker', worker => workers.push(new URL(worker.url()).pathname));
    await installMockApi(page);
    await page.goto(path);
    await expect(page.getByRole('heading').filter({ hasText: heading }).first()).toBeVisible();
    if (name === 'board') await expect(page.locator('.react-flow__node[data-id="F1"]')).toBeVisible();
    const metrics = await page.evaluate(() => {
      const scripts = performance.getEntriesByType('resource').filter(entry => /\.js(?:\?|$)/.test(entry.name)) as PerformanceResourceTiming[];
      return {
        scripts: scripts.map(entry => ({ name: new URL(entry.name).pathname, bytes: entry.decodedBodySize })),
        scriptBytes: scripts.reduce((sum, entry) => sum + entry.decodedBodySize, 0),
        domReady: (performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming).domContentLoadedEventEnd,
      };
    });
    samples.push({ page: name, ...metrics, workers });
    await page.screenshot({ path: info.outputPath(`${name}.png`) });
    await context.close();
  }
  await info.attach('page-loading.json', { body: JSON.stringify(samples, null, 2), contentType: 'application/json' });
});

test('keeps the current page during route code loading and preserves list position', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/tasks?q=订单');
  await expect(page.getByRole('link', { name: `查看任务：${GOAL}` })).toBeVisible();
  // Hold both Vite dev modules and production route chunks before the navigation starts.
  let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  await page.route(/(?:\/src\/pages\/ProfilesPage\.tsx|\/assets\/ProfilesPage-[^/]+\.js)(?:\?.*)?$/, async route => { await ready; await route.continue(); });
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: 'Agent 配置', exact: true }).click();
  await expect(page.locator('#main-content')).toHaveAttribute('aria-busy', 'true');
  await expect(page.getByRole('link', { name: `查看任务：${GOAL}` })).toBeVisible();
  await expect(page.getByRole('status', { name: '页面内容加载中' })).toHaveCount(0);
  release();
  await expect(page.getByRole('heading', { name: 'Agent 配置', exact: true })).toBeVisible();
  await expect(page.locator('#main-content')).toHaveAttribute('aria-busy', 'false');
  await page.goBack();
  await expect(page.getByLabel('查找任务')).toHaveValue('订单');
  expect(mock.unexpected).toEqual([]);
});

test('a completed Agent stops polling, while queued review replies stay live', async ({ page }) => {
  await installMockApi(page);
  let reads = 0;
  let pending = false;
  await page.route(`**/api/tasks/${TASK_ID}/agents/agent-1/messages`, route => {
    reads += 1;
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ mode: 'review', session_available: true, session_origin: 'native', messages: pending ? [{ id: 'waiting', role: 'user', content: '复盘', status: 'queued', created_at: '2026-10-01T00:00:00Z' }] : [] }) });
  });
  await page.goto(`/tasks/${TASK_ID}`);
  const agent = page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ });
  await agent.click();
  await expect(page.getByRole('complementary', { name: 'Agent 1 对话记录' }).getByRole('textbox')).toBeEnabled();
  const initialReads = reads;
  expect(initialReads).toBeGreaterThan(0);
  await page.clock.install();
  await page.clock.fastForward(12000);
  expect(reads).toBe(initialReads);
  await page.getByRole('button', { name: '关闭对话', exact: true }).click();
  pending = true;
  await agent.click();
  await expect.poll(() => reads).toBeGreaterThan(1);
  const previous = reads;
  await page.clock.fastForward(4000);
  await expect.poll(() => reads).toBeGreaterThan(previous);
});

test('an in-flight CTF snapshot cannot erase a newer assignment event', async ({ page }) => {
  const { installCtfApi, CTF_ID, openCtfMenu } = await import('./ctf-fixtures');
  const { pushMockBoardEvent } = await import('./fixtures');
  await installMockApi(page);
  const mock = await installCtfApi(page);
  let release!: () => void;
  let hold: Promise<void> | undefined;
  let reads = 0;
  const snapshot = { event_version: 10, task: mock.task, members: mock.members, challenges: mock.challenges, records: mock.records, artifacts: [] };
  await page.route(`**/api/tasks/${CTF_ID}/ctf/state`, async route => {
    reads += 1;
    if (hold) await hold;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(snapshot) });
  });
  await page.goto(`/tasks/${CTF_ID}`);
  await expect(page.getByRole('button', { name: '任务：模拟 Web 题' })).toBeVisible();
  const initialReads = reads;
  hold = new Promise<void>(resolve => { release = resolve; });
  await openCtfMenu(page);
  await page.getByRole('menuitem', { name: '刷新', exact: true }).click();
  await expect.poll(() => reads).toBeGreaterThan(initialReads);
  await pushMockBoardEvent(page, { task_id: CTF_ID, version: 11, type: 'ctf.challenge.updated', actor: 'lead', object_id: 'C1', addressed_to: null, created_at: '2026-10-17T00:00:00Z', payload: { result: { ...mock.challenges[0], title: '实时更新后的题目', revision: 2 } } });
  await expect(page.getByRole('button', { name: '任务：实时更新后的题目' })).toBeVisible();
  release();
  await page.waitForResponse(response => response.url().endsWith('/ctf/state'));
  await expect(page.getByRole('button', { name: '任务：实时更新后的题目' })).toBeVisible();
});
