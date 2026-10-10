import { taskAction, conversationAction, settleModal } from './ui';
import { mkdir } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import { installMockApi, TASK_ID } from './fixtures';

test('Agent 颜色对应创建对象，贡献可定位，旧正文分段且原文保持', async ({ page }) => {
  const mock = await installMockApi(page);
  const legacy = '共享资料已准备：（1）源文件位于 `/workspace/shared/monthly-data`，字段说明可供其他探索者复用。（2）运行 `python verify.py --input source.csv` 核对差异，结果文件保留原始行编号。';
  const extra = mock.events[2];
  const raw = mock.events.flatMap((event) => {
    if (event.type === 'agent.spawned') return [event, { ...event, payload: { id: 'agent-2', task_type: 'explore' }, object_id: 'agent-2' }];
    if (event.type === 'fact.posted') return [
      { ...event, payload: { ...event.payload, statement: legacy } },
      { ...event, object_id: 'F2', actor: 'agent-2', payload: { ...event.payload, id: 'F2', author: 'agent-2', statement: '已核对另一份资料的日期范围。' } },
    ];
    if (event.type === 'intent.posted') return [{ ...event, payload: { ...event.payload, claim: false } }, { ...event, type: 'intent.claimed', payload: { intent_id: 'I1', holder: 'agent-2' } }];
    if (event.type === 'agent.finished') return [event, { ...extra, type: 'agent.finished', payload: { agent_id: 'agent-2', end_reason: 'normal' } }];
    return [event];
  });
  const events = raw.map((event, index) => ({ ...event, version: index + 1 }));
  await page.route(`**/api/tasks/${TASK_ID}/events?*`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(events) }));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/tasks/${TASK_ID}`);
  const canvas = page.getByRole('region', { name: '黑板关系图' }).first();
  const agents = page.getByRole('group', { name: '全部 Agent' });
  const first = agents.getByRole('button', { name: /Agent 1/ });
  const second = agents.getByRole('button', { name: /Agent 2/ });
  await expect(first).toContainText('1 Fact · 1 Intent');
  await expect(second).toContainText('1 Fact · 0 Intent');
  const tint1 = await first.evaluate((node) => getComputedStyle(node).backgroundColor);
  const tint2 = await second.evaluate((node) => getComputedStyle(node).backgroundColor);
  expect(tint1).not.toBe(tint2);
  for (const [label, color] of [[/事实 F1，/, tint1], [/意图 I1，/, tint1], [/事实 F2，/, tint2]] as const) {
    const node = canvas.getByRole('button', { name: label });
    await expect(node).toBeVisible();
    expect(await node.evaluate((button) => getComputedStyle(button.parentElement!).backgroundColor)).toBe(color);
  }
  await expect(page.getByRole('tab', { name: /Agent 记录/ })).toHaveCount(0);
  await expect(page.getByRole('tab', { name: '事件流' })).not.toBeVisible();
  await first.click();
  const conversation = page.getByRole('complementary', { name: 'Agent 1 对话记录' });
  await conversationAction(page, conversation, '运行信息');
  const info = page.getByRole('dialog', { name: 'Agent 1 · 运行信息', exact: true });
  await expect(info).toContainText('1 Fact · 1 Intent');
  await info.getByRole('button', { name: 'F1', exact: true }).click();
  const detail = page.getByRole('complementary', { name: '对象详情' });
  await expect(detail.locator('ol').first().locator('li')).toHaveCount(2);
  await expect(detail.locator('code').filter({ hasText: 'python verify.py --input source.csv' })).toBeVisible();
  await detail.getByRole('button', { name: '原文', exact: true }).click();
  await expect(detail.locator('pre').first()).toHaveText(legacy);
  await detail.getByRole('button', { name: '阅读排版', exact: true }).click();
  await mkdir('../.data/qa', { recursive: true });
  await page.screenshot({ path: '../.data/qa/contribution-desktop.png', fullPage: true });
  await taskAction(page, '复盘记录');
  const dialog = page.getByRole('dialog', { name: '复盘记录' });
  await expect(dialog).toBeVisible();
  await settleModal(dialog); await dialog.focus(); await dialog.press('Escape');
  await expect(dialog).not.toBeVisible();
  const bounds = await canvas.boundingBox();
  expect((bounds?.y ?? 0) + (bounds?.height ?? 0)).toBeGreaterThan(980);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: '../.data/qa/contribution-mobile.png', fullPage: true });
  expect(mock.unexpected).toEqual([]);
});
