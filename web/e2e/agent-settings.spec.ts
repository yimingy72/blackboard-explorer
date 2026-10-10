import { selectOption, expandSection } from './ui';
import { expect, test } from '@playwright/test';
import { installMockApi } from './fixtures';

test('模型上下文容量保存、回显与清除，拒绝小数提交', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  await page.getByRole('tab', { name: /模型配置$/ }).click();
  await selectOption(page, page.getByRole('combobox', { name: '模型连接', exact: true }), '审查模型');
  const input = page.getByLabel('上下文大小（token）');
  await expect(input).toBeEmpty();
  await input.fill('500000');
  await expandSection(page.getByRole('button', { name: '连接与上下文说明', exact: true }));
  await expect(page.getByText(/新任务交接阈值 400,000/)).toBeVisible();
  await page.getByRole('button', { name: /保存$/ }).click();
  expect(mock.modelSaves[0]).toMatchObject({ context_window: 500000 });
  await expect(input).toHaveValue('500000');
  await input.fill('1.5');
  await page.getByRole('button', { name: /保存$/ }).click();
  expect(mock.modelSaves).toHaveLength(1);
  await input.fill('');
  await page.getByRole('button', { name: /保存$/ }).click();
  expect(mock.modelSaves[1]).toMatchObject({ context_window: null });
  expect(mock.unexpected).toEqual([]);
});

test('Worker 保存中的配置页签锁定，返回后保留最新快照和下一次修订', async ({ page }) => {
  const mock = await installMockApi(page);
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let started = false;
  await page.route('**/api/settings/workers/explore', async (route) => {
    if (route.request().method() === 'PUT' && !started) {
      started = true;
      await pending;
    }
    return route.fallback();
  });
  try {
    await page.goto('/profiles');
    const prompt = page.getByLabel('完整系统提示词');
    await prompt.fill('最新保存的探索提示词');
    await page.getByRole('button', { name: '保存并应用', exact: true }).click();
    await expect.poll(() => started).toBe(true);
    await expect(prompt).toBeDisabled();
    for (const name of ['Worker 配置', 'CTF 配置', '模型配置', 'MCP 工具']) {
      await expect(page.getByRole('tab', { name, exact: true })).toBeDisabled();
    }
    await page.getByRole('tab', { name: /模型配置$/ })
      .evaluate((element: HTMLElement) => element.click());
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(prompt).toHaveValue('最新保存的探索提示词');
    release();
    await expect(page.getByRole('status')).toContainText('已保存并应用');
    expect(mock.workerSaves).toHaveLength(1);
    expect(mock.workerSaves[0]).toMatchObject({ expected_revision: 2, prompt: '最新保存的探索提示词' });
    await page.getByRole('tab', { name: /模型配置$/ }).click();
    await expect(page.getByRole('combobox', { name: '模型连接', exact: true })).toBeVisible();
    await page.getByRole('tab', { name: /Worker 配置$/ }).click();
    await expect(prompt).toHaveValue('最新保存的探索提示词');
    await prompt.fill('下一次探索提示词');
    await page.getByRole('button', { name: '保存并应用', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('已保存并应用');
    expect(mock.workerSaves).toHaveLength(2);
    expect(mock.workerSaves[1]).toMatchObject({ expected_revision: 3, prompt: '下一次探索提示词' });
    expect(mock.unexpected).toEqual([]);
  } finally { release(); }
});

test('配置布局、键盘角色切换、草稿保护及各入口窄屏显示', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.goto('/profiles');
  const prompt = page.getByLabel('完整系统提示词');
  await expect.poll(async () => (await prompt.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(300);
  await prompt.fill('# 工作职责\n\n围绕用户目标开展调查，将可复核的发现及时发布到共享黑板。\n\n## 调查与协作\n\n- 开始前阅读任务目标、验收条件和当前黑板。\n- 证据充分后提交 Fact，说明来源与依据。\n- 发现独立方向时提出 Intent，供其他 Agent 认领。\n- 遇到结束指令，整理证据、发布发现并完成交接。\n\n## 当前任务\n\n{{ goal }}\n\n{{ acceptance_status }}');
  const explore = page.getByRole('radio', { name: 'Explore', exact: true });
  await explore.focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('radio', { name: 'Derive', exact: true })).toBeChecked();
  await page.keyboard.press('ArrowLeft');
  await expect(explore).toBeChecked();
  await expect(prompt).toContainText('工作职责');
  await page.screenshot({ path: '../.data/checkpoints/agent-settings/worker-desktop.png', fullPage: true });
  await page.getByRole('tab', { name: /模型配置$/ }).click();
  await page.getByRole('dialog', { name: '当前配置尚未保存，确定放弃本次修改吗？' }).getByRole('button', { name: '继续编辑' }).click();
  await expect(prompt).toBeVisible();
  await page.getByRole('button', { name: '保存并应用' }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: '../.data/checkpoints/agent-settings/worker-mobile.png', fullPage: true });
  await page.getByRole('tab', { name: /模型配置$/ }).click();
  await selectOption(page, page.getByRole('combobox', { name: '模型连接', exact: true }), '审查模型');
  await page.getByLabel('上下文大小（token）').fill('500000');
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: '../.data/checkpoints/agent-settings/models-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.screenshot({ path: '../.data/checkpoints/agent-settings/models-desktop.png', fullPage: true });
  await page.getByRole('button', { name: /保存$/ }).click();
  await page.getByRole('tab', { name: /MCP 工具$/ }).click();
  await selectOption(page, page.getByRole('combobox', { name: 'MCP 服务', exact: true }), '资料检索');
  await page.screenshot({ path: '../.data/checkpoints/agent-settings/mcp-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  expect(mock.unexpected).toEqual([]);
});
