import { expect, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { CTF_ID, installCtfApi, openCtfMenu } from './ctf-fixtures';

test('CTF 默认大画布，详情面板可拖动、键盘伸缩、复位和 Esc 关闭', async ({ page }) => {
  const mock = await installCtfApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/tasks/${CTF_ID}`);
  await expect(page.locator('#main-content > div > header')).toContainText('¥0.040 / ¥10.00');
  const canvas = page.getByRole('main', { name: 'CTF 团队协作画布' });
  await expect(page.getByRole('complementary')).toHaveCount(0);
  expect((await canvas.boundingBox())?.width).toBeGreaterThan(1300);
  const agent = page.getByRole('button', { name: 'Agent：Lead' });
  await agent.click();
  const panel = page.getByRole('complementary', { name: 'Lead 会话详情' });
  const handle = page.getByRole('separator', { name: '调整详情面板宽度' });
  await expect(panel).toBeVisible();
  await expect(panel.getByRole('heading', { name: 'Lead', exact: true })).toBeFocused();
  await expect(handle).toHaveAttribute('aria-valuenow', '520');
  await handle.focus();
  await handle.press('ArrowLeft');
  await expect(handle).toHaveAttribute('aria-valuenow', '540');
  await expect.poll(async () => Math.round((await panel.boundingBox())?.width ?? 0)).toBe(540);
  await handle.press('Home');
  await expect(handle).toHaveAttribute('aria-valuenow', '360');
  const bounds = await handle.boundingBox();
  if (!bounds) throw new Error('Missing resize handle');
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await page.mouse.move(bounds.x - 140, bounds.y + bounds.height / 2, { steps: 8 });
  await page.mouse.up();
  expect(Number(await handle.getAttribute('aria-valuenow'))).toBeGreaterThan(490);
  await handle.dblclick();
  await expect(handle).toHaveAttribute('aria-valuenow', '520');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await handle.press('Escape');
  await expect(page.getByRole('complementary')).toHaveCount(0);
  await expect(agent).toBeFocused();
  expect((await canvas.boundingBox())?.width).toBeGreaterThan(1300);
  expect(mock.unexpected).toEqual([]);
});

test('题目分类键盘切换、文件预览和讨论入口保持上下文', async ({ page }) => {
  const mock = await installCtfApi(page);
  await page.goto(`/tasks/${CTF_ID}`);
  await page.getByRole('button', { name: '任务：模拟 Web 题' }).click();
  const panel = page.getByRole('complementary', { name: '题目详情' });
  const overview = panel.getByRole('tab', { name: '概览', exact: true });
  await overview.focus();
  await overview.press('ArrowRight');
  await expect(panel.getByRole('tab', { name: /记录\s*3/ })).toBeFocused();
  await expect(panel.getByRole('tabpanel')).toContainText('空输入导致解码失败');
  await page.keyboard.press('End');
  await expect(panel.getByRole('tab', { name: /文件\s*1/ })).toBeFocused();
  await panel.getByRole('button', { name: /probe.py/ }).click();
  await panel.locator('summary').filter({ hasText: '文件信息' }).click();
  await expect(panel).toContainText('/workspace/shared/probe.py');
  await panel.locator('summary').filter({ hasText: 'probe.py' }).click();
  await expect(panel.getByRole('link', { name: '下载完整证据' })).toHaveAttribute('href', new RegExp('probe.py'));
  await expect(panel).toContainText('Mock public evidence');
  await panel.getByRole('button', { name: '与 Lead 讨论', exact: true }).click();
  await expect(page.getByRole('complementary', { name: 'Lead 会话详情' })).toContainText('已分派两道模拟题');
  await expect(page.getByRole('complementary')).toHaveCount(1);
  expect(mock.unexpected).toEqual([]);
});

test('画布缩放和平移在轮询、选择节点和关闭面板后保留', async ({ page }) => {
  const mock = await installCtfApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/tasks/${CTF_ID}`);
  const viewport = page.locator('.react-flow__viewport');
  await expect(page.locator('.react-flow__node')).toHaveCount(6);
  const initial = await viewport.getAttribute('style');
  await page.getByRole('button', { name: '放大画布' }).click();
  await expect(viewport).not.toHaveAttribute('style', initial ?? '');
  const zoomed = await viewport.getAttribute('style');
  await page.waitForResponse((response) => new URL(response.url()).pathname.endsWith('/ctf/state'));
  await expect(viewport).toHaveAttribute('style', zoomed ?? '');

  const pane = page.locator('.react-flow__pane');
  const bounds = await pane.boundingBox();
  if (!bounds) throw new Error('Missing canvas pane');
  await page.mouse.move(bounds.x + bounds.width * 0.7, bounds.y + bounds.height * 0.8);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * 0.7 - 70, bounds.y + bounds.height * 0.8 - 35, { steps: 5 });
  await page.mouse.up();
  const panned = await viewport.getAttribute('style');
  expect(panned).not.toBe(zoomed);
  await page.waitForResponse((response) => new URL(response.url()).pathname.endsWith('/ctf/state'));
  await expect(viewport).toHaveAttribute('style', panned ?? '');
  await page.getByRole('button', { name: 'Agent：Lead' }).click();
  await expect(page.getByRole('complementary', { name: 'Lead 会话详情' })).toBeVisible();
  await expect(viewport).toHaveAttribute('style', panned ?? '');
  await page.getByRole('button', { name: '关闭详情' }).click();
  await expect(page.getByRole('complementary')).toHaveCount(0);
  await expect(viewport).toHaveAttribute('style', panned ?? '');
  expect(mock.unexpected).toEqual([]);
});

test('CTF 在 1440、768 和 390px 保留大画布、会话与题目实际截图', async ({ page }) => {
  const mock = await installCtfApi(page);
  const output = '../docs/tasks/ui-unification-screenshots';
  await mkdir(output, { recursive: true });
  for (const width of [1440, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`/tasks/${CTF_ID}`);
    await expect(page.locator('.react-flow__node')).toHaveCount(6);
    await expect(page.getByRole('complementary')).toHaveCount(0);
    await page.screenshot({ animations: 'disabled', path: `${output}/ctf-canvas-${width}.png` });
    await page.getByRole('button', { name: 'Agent：Web 分析员' }).click();
    const agent = page.getByRole('complementary', { name: 'Web 分析员 会话详情' });
    await expect(agent.getByRole('textbox')).toBeInViewport();
    if (width >= 1024) await expect.poll(() => page.getByRole('main', { name: 'CTF 团队协作画布' }).evaluate((element) => {
      const board = element.getBoundingClientRect();
      const nodes = [...element.querySelectorAll('.react-flow__node')];
      return nodes.length === 6 && nodes.every((node) => {
        const bounds = node.getBoundingClientRect();
        return bounds.left >= board.left - 1 && bounds.right <= board.right + 1 && bounds.top >= board.top - 1 && bounds.bottom <= board.bottom + 1;
      });
    })).toBe(true);
    await agent.locator('summary').filter({ hasText: 'read_artifact' }).click();
    await expect(agent).toContainText('公开结果：空输入解码失败');
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ animations: 'disabled', path: `${output}/ctf-conversation-${width}.png` });
    await agent.getByRole('button', { name: '关闭详情' }).click();
    await expect(page.getByRole('complementary')).toHaveCount(0);
    await page.getByRole('button', { name: '任务：模拟 Web 题' }).click();
    const challenge = page.getByRole('complementary', { name: '题目详情' });
    await expect(challenge.getByRole('tab', { name: '概览', exact: true })).toHaveAttribute('aria-selected', 'true');
    await page.screenshot({ animations: 'disabled', path: `${output}/ctf-overview-${width}.png` });
    await challenge.getByRole('tab', { name: /记录/ }).click();
    await expect(challenge).toContainText('空输入导致解码失败');
    await page.screenshot({ animations: 'disabled', path: `${output}/ctf-records-${width}.png` });
    await challenge.getByRole('button', { name: '关闭详情' }).click();
    await expect(page.getByRole('complementary')).toHaveCount(0);
  }
  expect(mock.unexpected).toEqual([]);
});

test('系统事件读取失败明确反馈，重试后恢复事件列表', async ({ page }) => {
  const mock = await installCtfApi(page);
  let fail = true;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  await page.route(`**/api/tasks/${CTF_ID}/events?*`, async (route) => {
    await pending;
    if (fail) return route.fulfill({ status: 503, json: { detail: '模拟事件读取暂时失败' } });
    return route.fallback();
  });
  try {
    await page.goto(`/tasks/${CTF_ID}`);
    await openCtfMenu(page);
    await page.getByRole('button', { name: '系统事件', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '系统事件', exact: true });
    await expect(dialog.getByRole('status')).toContainText('正在读取事件');
    await expect(dialog).not.toContainText('尚无事件记录');
    release();
    await expect(dialog.getByRole('alert')).toContainText('模拟事件读取暂时失败');
    await expect(dialog).not.toContainText('尚无事件记录');
    await expect(dialog.locator('ol > li')).toHaveCount(0);
    fail = false;
    await dialog.getByRole('button', { name: '重试', exact: true }).click();
    await expect(dialog.getByRole('alert')).toHaveCount(0);
    await expect(dialog.locator('ol > li')).toHaveCount(4);
    await expect(dialog).toContainText('ctf.turn.started');
    await expect(dialog).toContainText('tool_call.recorded');
    await expect(dialog).not.toContainText('尚无事件记录');
    await page.keyboard.press('Escape');
    await expect(dialog).not.toBeVisible();
    expect(mock.unexpected).toEqual([]);
  } finally { release(); }
});
