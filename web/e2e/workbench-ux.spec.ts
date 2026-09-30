import { mkdir } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import { TASK_ID, installMockApi } from './fixtures';
import { BOARD_UPDATE, INITIAL_CONTEXT, MODEL_ERROR, MODEL_OUTPUT, MODEL_REASONING, installConversationFixture } from './conversation-fixture';

test('Agent 常驻列表打开上下文、同步、模型输出与工具记录，回放隔离未来内容', async ({ page }) => {
  const mock = await installConversationFixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ }).click();
  const conversation = page.getByRole('complementary', { name: 'Agent 1 对话记录' });
  await expect(conversation).toBeVisible();
  for (const kind of ['初始上下文', '黑板同步注入', '模型回复', '模型请求失败']) {
    const entry = conversation.getByRole('article').filter({ hasText: kind });
    await entry.getByRole('button', { name: '查看正文' }).click();
  }
  await expect(conversation.locator('pre').filter({ hasText: INITIAL_CONTEXT })).toBeVisible();
  await expect(conversation.locator('pre').filter({ hasText: BOARD_UPDATE })).toBeVisible();
  await expect(conversation.locator('p').filter({ hasText: MODEL_OUTPUT })).toBeVisible();
  await expect(conversation.locator('pre').filter({ hasText: MODEL_ERROR })).toBeVisible();
  await conversation.locator('summary').filter({ hasText: '实际返回的推理' }).click();
  await expect(conversation.locator('pre').filter({ hasText: MODEL_REASONING })).toBeVisible();
  await expect(conversation).toContainText('工具调用');
  const graph = page.getByRole('region', { name: '黑板关系图' }).first();
  const rect = await graph.boundingBox();
  expect(rect?.height).toBeGreaterThan(500);
  const panelHeader = await conversation.locator('header').boundingBox();
  expect(panelHeader?.y).toBeGreaterThanOrEqual((rect?.y ?? 0) - 1);
  await mkdir('../.data/qa', { recursive: true });
  await page.screenshot({ path: '../.data/qa/workbench-desktop.png', fullPage: true });

  await page.getByRole('button', { name: '复盘记录', exact: true }).click();
  await page.getByRole('tab', { name: '时间轴' }).click();
  const slider = page.getByRole('slider', { name: '回放版本' });
  await slider.focus();
  await slider.press('Home');
  for (let index = 0; index < 3; index += 1) await slider.press('ArrowRight');
  await page.getByRole('button', { name: '关闭复盘记录' }).click();
  await page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ }).click();
  await expect(conversation).not.toContainText(BOARD_UPDATE);
  await expect(conversation).not.toContainText(MODEL_OUTPUT);
  expect(mock.unexpected).toEqual([]);
});

test('创建表单支持长验收条件，桌面与窄屏不横向溢出', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/tasks/new');
  await page.getByLabel('任务目标').fill('核对本月业务汇总与原始明细，列出不一致项并解释原因');
  await page.getByLabel('领域背景').fill('资料包括两份 CSV 与一份统计规则。按规则检查日期、去重和汇总口径，所有结论需关联原始行。');
  await page.getByLabel('验收条件 1', { exact: true }).fill('逐项列出差异，引用原始行和对应规则。\n统计每类差异数量，并说明仍待确认的事项。');
  await mkdir('../.data/qa', { recursive: true });
  await page.screenshot({ path: '../.data/qa/new-task-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  await expect(page.getByLabel('验收条件 1', { exact: true })).toHaveValue(/\n/);
  await expect.poll(() => page.getByLabel('验收条件 1', { exact: true }).evaluate((element) => element.scrollHeight - element.clientHeight)).toBeLessThanOrEqual(2);
  await page.screenshot({ path: '../.data/qa/new-task-mobile.png', fullPage: true });
  expect(mock.unexpected).toEqual([]);
});

test('拓扑与 Agent 条目联动，时长增长，切换任务后编号和对话独立', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-09-24T08:10:00Z'));
  const mock = await installConversationFixture(page, { running: true });
  const secondId = '22222222-2222-4222-8222-222222222222';
  const secondTask = {
    id: secondId, goal: '第二个任务', status: 'running', acceptance_state: {}, usage: {},
    agents: [], report_uri: null, workspace_uri: null, agent_profile: 'default',
    agent_profile_version: 2, created_at: '2026-09-24T08:00:00Z',
  };
  const secondEvents = mock.events.slice(0, 3).map((event) => ({
    ...event, task_id: secondId,
    payload: event.type === 'agent.spawned' ? { id: 'agent-9', task_type: 'derive' }
      : event.type === 'task.created' ? { ...event.payload, goal: secondTask.goal } : event.payload,
  }));
  await page.route('**/api/tasks', (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify([secondTask]) }));
  await page.route(`**/api/tasks/${secondId}`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(secondTask) }));
  await page.route(`**/api/tasks/${secondId}/events?*`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(secondEvents) }));
  await page.goto(`/tasks/${TASK_ID}`);
  await expect(page.getByText('累计运行 9 分 50 秒', { exact: true })).toBeVisible();
  await page.clock.setFixedTime(new Date('2026-09-24T08:10:02Z'));
  await expect(page.getByText('累计运行 9 分 52 秒', { exact: true })).toBeVisible();
  await page.getByRole('region', { name: '黑板关系图' }).first().getByRole('button', { name: /^Agent 1，运行中/ }).click();
  await expect(page.getByRole('complementary', { name: 'Agent 1 对话记录' })).toBeVisible();
  await page.getByRole('button', { name: '关闭对话' }).click();
  await expect(page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ })).toBeFocused();
  await page.getByRole('link', { name: '← 返回任务' }).click();
  await page.getByRole('link', { name: '查看任务：第二个任务' }).click();
  const agents = page.getByRole('group', { name: '全部 Agent' });
  await expect(agents).toContainText('Agent 1');
  await expect(agents).not.toContainText('Agent 9');
  await agents.getByRole('button', { name: /Agent 1/ }).click();
  const conversation = page.getByRole('complementary', { name: 'Agent 1 对话记录' });
  await expect(conversation).toContainText('对话记录尚未到达');
  await expect(conversation).not.toContainText(MODEL_OUTPUT);
  expect(mock.unexpected).toEqual([]);
});
