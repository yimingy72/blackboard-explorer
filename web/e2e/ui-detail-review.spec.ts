import { taskListRoute, fulfillTaskList } from './task-list-fixture';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { GOAL, TASK_ID, installMockApi, pushMockBoardEvent } from './fixtures';
import { CTF_ID, installCtfApi } from './ctf-fixtures';
import { installConversationFixture } from './conversation-fixture';

const screenshots = '../.data/ui-detail-review/e2e';

async function capture(page: Page, filename: string) {
  await mkdir(screenshots, { recursive: true });
  await page.screenshot({ path: `${screenshots}/${filename}`, animations: 'disabled', fullPage: true });
}

async function expectNoHorizontalOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
}

async function expectInitialFit(canvas: Locator, count: number) {
  await expect.poll(() => canvas.evaluate((element, expected) => {
    const bounds = element.getBoundingClientRect();
    const nodes = [...element.querySelectorAll('.react-flow__node')];
    return nodes.length === expected && nodes.every(node => {
      const rect = node.getBoundingClientRect();
      return rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1
        && rect.top >= bounds.top - 1 && rect.bottom <= bounds.bottom + 1;
    });
  }, count)).toBe(true);
}

test('全局导航保持位置，查看与新建不刷新文档，终态报告直接可见', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/tasks');
  await page.getByRole('button', { name: '展开导航', exact: true }).click();
  const nav = page.getByRole('navigation', { name: '主导航' });
  const configuration = nav.getByRole('link', { name: 'Agent 配置', exact: true });
  await expect(configuration).toBeVisible();
  const configY = (await configuration.boundingBox())!.y;
  const documents: string[] = [];
  page.on('request', request => { if (request.resourceType() === 'document') documents.push(request.url()); });
  await page.getByRole('listitem', { name: GOAL, exact: true }).getByRole('link', { name: GOAL, exact: true }).click();
  await expect(page.getByRole('heading', { name: GOAL, exact: true })).toBeVisible();
  await expect(nav.getByRole('link')).toHaveCount(2);
  await expect(nav.getByRole('link', { name: '协作画布', exact: true })).toHaveCount(0);
  await expect(nav.getByRole('link', { name: '任务', exact: true })).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('button', { name: '收起导航', exact: true })).toHaveAttribute('aria-expanded', 'true');
  expect((await configuration.boundingBox())!.y).toBe(configY);
  await expect(page.getByRole('button', { name: '查看报告', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看报告', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}/report$`));
  await page.goBack();
  await capture(page, 'stable-navigation-1440.png');
  await page.getByRole('button', { name: '返回任务', exact: true }).click();
  expect((await configuration.boundingBox())!.y).toBe(configY);
  await configuration.click();
  await expect(page.getByLabel('完整系统提示词', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '新建任务', exact: true }).click();
  await expect(page.getByLabel('任务目标', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /返回任务$/ }).click();
  expect(documents).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});

test('任务搜索和分页写入 URL，详情与浏览器返回恢复原列表位置', async ({ page }) => {
  const mock = await installMockApi(page);
  const tasks = Array.from({ length: 25 }, (_, index) => ({
    id: index === 24 ? TASK_ID : `22222222-2222-4222-8222-${String(index).padStart(12, '0')}`,
    name: `月度核对 ${String(index + 1).padStart(2, '0')}`, goal: GOAL, mode: 'blackboard', status: 'finished',
    acceptance_state: {}, usage: { cost: 0.02 }, cost_currency: 'CNY', budget: { max_cost: 10 },
    active_seconds: 600, active_since: null, created_at: '2026-10-10T08:00:00Z',
  }));
  await page.route(taskListRoute, route => fulfillTaskList(route, tasks));
  await page.goto('/tasks');
  await page.getByLabel('查找任务', { exact: true }).fill('月度核对');
  await expect.poll(() => new URL(page.url()).searchParams.get('q')).toBe('月度核对');
  await page.getByTitle('2', { exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('page')).toBe('2');
  const listUrl = page.url();
  const target = page.getByRole('link', { name: '月度核对 25', exact: true });
  await expect(target).toBeVisible();
  await expect(page.getByRole('list', { name: '任务列表', exact: true }).getByRole('listitem')).toHaveCount(5);
  await target.click();
  await expect(page.getByRole('heading', { name: GOAL, exact: true })).toBeVisible();
  await page.getByRole('button', { name: '返回任务', exact: true }).click();
  await expect(page).toHaveURL(listUrl);
  await expect(page.getByLabel('查找任务', { exact: true })).toHaveValue('月度核对');
  await expect(target).toBeVisible();
  await expect(page.getByRole('list', { name: '任务列表', exact: true }).getByRole('listitem')).toHaveCount(5);
  await target.click();
  await page.goBack();
  await expect(page).toHaveURL(listUrl);
  await expect(target).toBeVisible();
  await capture(page, 'search-page-restored-1440.png');
  expect(mock.unexpected).toEqual([]);
});

for (const width of [1440, 390, 650]) {
  test(`${width}px 任务记录显示状态、费用上限与耗时，未知费用不会冒充零`, async ({ page }) => {
    const mock = await installMockApi(page);
    const tasks = [
      { id: TASK_ID, name: '未知费用任务', mode: 'blackboard', goal: GOAL, status: 'finished', usage: {}, budget: { max_cost: 10 }, cost_currency: 'CNY', active_seconds: 600, active_since: null, created_at: '2026-10-10T08:00:00Z' },
      { id: CTF_ID, name: '零费用任务', mode: 'ctf', goal: GOAL, status: 'created', usage: { cost: 0 }, budget: { max_cost: 20 }, cost_currency: 'CNY', active_seconds: 0, active_since: null, created_at: '2026-10-10T08:00:00Z' },
    ];
    await page.route(taskListRoute, route => fulfillTaskList(route, tasks));
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/tasks');
    const unknown = page.getByRole('link', { name: '未知费用任务', exact: true });
    const zero = page.getByRole('link', { name: '零费用任务', exact: true });
    const records = page.getByRole('list', { name: '任务列表', exact: true }).getByRole('listitem');
    const unknownRecord = records.filter({ has: unknown });
    const zeroRecord = records.filter({ has: zero });
    await expect(unknownRecord).toContainText('已结束');
    await expect(unknownRecord.getByText('—', { exact: true })).toBeVisible();
    await expect(unknownRecord.getByText('¥10.00', { exact: true })).toBeVisible();
    await expect(unknownRecord).not.toContainText('¥0.000');
    await expect(unknownRecord).toContainText(/10.*分/);
    await expect(zeroRecord.getByText('¥0.000', { exact: true })).toBeVisible();
    await expect(zeroRecord.getByText('¥20.00', { exact: true })).toBeVisible();
    await expect(zeroRecord).toContainText('待启动');
    await expect(unknownRecord.getByText('已结束', { exact: true })).toBeInViewport();
    await expect(unknown).toBeInViewport();
    const operations = unknownRecord.getByRole('button', { name: '任务操作：未知费用任务', exact: true });
    await expect(operations).toBeInViewport();
    await operations.click();
    await expect(page.getByRole('menuitem', { name: '删除任务', exact: true })).toBeInViewport();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('columnheader', { name: '验收', exact: true })).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
    if (width <= 650) {
      await expect(page.getByRole('table')).toHaveCount(0);
      await expect.poll(() => unknownRecord.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    }
    await capture(page, `task-records-${width}.png`);
    expect(mock.unexpected).toEqual([]);
  });
}

test('CTF 首次适配后第一次点击节点、详情调宽与容器缩放均保持视角', async ({ page }) => {
  const mock = await installCtfApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/tasks/${CTF_ID}`);
  const canvas = page.getByRole('main', { name: 'CTF 团队协作画布' });
  await expectInitialFit(canvas, 5);
  const viewport = page.locator('.react-flow__viewport');
  const transform = await viewport.getAttribute('style');
  await page.getByRole('button', { name: 'Agent：Lead', exact: true }).click();
  const chat = page.getByRole('complementary', { name: 'Lead 会话', exact: true });
  await expect(chat).toBeVisible();
  await expect(viewport).toHaveAttribute('style', transform!);
  await page.getByRole('separator', { name: '调整详情面板宽度' }).press('ArrowLeft');
  await expect(viewport).toHaveAttribute('style', transform!);
  await chat.getByRole('button', { name: '关闭详情', exact: true }).click();
  await expect(viewport).toHaveAttribute('style', transform!);
  await page.setViewportSize({ width: 1200, height: 900 });
  await expect(viewport).toHaveAttribute('style', transform!);
  await capture(page, 'ctf-preserved-viewport.png');
  expect(mock.unexpected).toEqual([]);
});

for (const mode of ['blackboard', 'ctf'] as const) {
  test(`${mode} 会话跨899px保持同一DOM、工具展开、阅读位置与草稿，覆盖时画布不可Tab`, async ({ page }) => {
    const longResult = Array.from({ length: 100 }, (_, index) => `公开模拟结果 ${index}: 核对依据已保存。`).join('\n');
    const mock = mode === 'ctf' ? await installCtfApi(page, { evidenceText: longResult }) : await installConversationFixture(page);
    if (mode === 'blackboard' && 'events' in mock) {
      const tool = mock.events.find(event => event.type === 'tool_call.recorded')!;
      tool.payload = { ...tool.payload, result_head: longResult };
    }
    const taskId = mode === 'ctf' ? CTF_ID : TASK_ID;
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/tasks/${taskId}`);
    const opener = mode === 'ctf' ? page.getByRole('button', { name: 'Agent：Web 分析员', exact: true })
      : page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ });
    await opener.click();
    const chat = page.getByRole('complementary', { name: mode === 'ctf' ? 'Web 分析员 会话' : 'Agent 1 对话记录', exact: true });
    const identity = await chat.elementHandle();
    const tool = chat.getByRole('button', { name: mode === 'ctf' ? /^read_artifact/ : /^exec_command/ });
    await tool.click();
    await expect(tool).toHaveAttribute('aria-expanded', 'true');
    const result = chat.getByRole('region', { name: mode === 'ctf' ? 'read_artifact 参数与结果' : 'exec_command 参数与结果', exact: true }).locator('pre').last();
    await expect(result).toContainText('公开模拟结果 99');
    expect((await result.boundingBox())?.width).toBeGreaterThan(300);
    const reading = await result.elementHandle();
    await result.evaluate(element => { element.scrollTop = 120; });
    await expect.poll(() => result.evaluate(element => element.scrollTop)).toBe(120);
    await chat.getByRole('textbox').fill('未发送的断点切换草稿');
    if (mode === 'blackboard') {
      const evidence = chat.getByRole('button', { name: /^命令输出/ });
      const bounds = await evidence.boundingBox();
      expect(bounds?.width).toBeGreaterThan(300);
      expect(bounds?.height).toBeLessThan(80);
    }
    for (const width of [899, 768, 390, 900, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await expect(chat).toBeVisible();
      expect(await chat.evaluate((element, previous) => element === previous, identity)).toBe(true);
      expect(await result.evaluate((element, previous) => element === previous, reading)).toBe(true);
      await expect(tool).toHaveAttribute('aria-expanded', 'true');
      await expect(chat.getByRole('textbox')).toHaveValue('未发送的断点切换草稿');
      await expect.poll(() => result.evaluate(element => element.scrollTop)).toBe(120);
      await expectNoHorizontalOverflow(page);
      if (width < 900) {
        const canvas = page.locator('.react-flow').first();
        expect(await canvas.evaluate(element => Boolean(element.closest('[inert]')))).toBe(true);
        await chat.getByRole('textbox').focus();
        for (let index = 0; index < 12; index += 1) {
          await page.keyboard.press('Tab');
          expect(await page.evaluate(() => Boolean(document.activeElement?.closest('.react-flow')))).toBe(false);
        }
      }
    }
    expect((await result.boundingBox())?.width).toBeGreaterThan(300);
    await result.scrollIntoViewIfNeeded();
    await capture(page, `${mode}-retained-conversation.png`);
    expect(mock.unexpected).toEqual([]);
    if ('sent' in mock) expect(mock.sent).toEqual([]);
  });
}

test('普通黑板新增现有节点的关系时保留节点位置和用户视角', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/tasks/${TASK_ID}`);
  const canvas = page.getByRole('region', { name: '黑板关系图' }).first();
  await expectInitialFit(canvas, 3);
  await expect(page.getByText('实时连接正常', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __bbxMockEventSources: EventTarget[] }).__bbxMockEventSources.length)).toBe(1);
  await page.getByRole('button', { name: '放大画布', exact: true }).click();
  await expect.poll(() => page.locator('.react-flow__viewport').evaluate(element => new DOMMatrixReadOnly((element as HTMLElement).style.transform).a)).toBeCloseTo(1.2, 4);
  await page.getByRole('button', { name: '显示全部关系', exact: true }).click();
  const viewport = page.locator('.react-flow__viewport');
  const transform = await viewport.getAttribute('style');
  const positions = await canvas.locator('.react-flow__node').evaluateAll(nodes => nodes.map(node => ({ id: node.getAttribute('data-id'), position: (node as HTMLElement).style.transform })));
  const posted = mock.events.find(event => event.type === 'fact.posted')!;
  await pushMockBoardEvent(page, { ...posted, version: mock.events.at(-1)!.version + 1, payload: { ...posted.payload, resolves: 'I1', result: 'confirmed' } });
  await expect(canvas.locator('.react-flow__edge[data-id="resolves:I1:F1"]')).toHaveCount(1);
  await expect(viewport).toHaveAttribute('style', transform!);
  await expect.poll(() => canvas.locator('.react-flow__node').evaluateAll(nodes => nodes.map(node => ({ id: node.getAttribute('data-id'), position: (node as HTMLElement).style.transform })))).toEqual(positions);
  expect(mock.unexpected).toEqual([]);
});
