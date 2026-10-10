import { selectMode, taskAction, settleModal } from './ui';
import { expect, test } from '@playwright/test';
import { GOAL, TASK_ID, installMockApi } from './fixtures';

test('主要页面共享按钮反馈、动画时长和可见键盘焦点', async ({ page }) => {
  const mock = await installMockApi(page);
  let expectedTokens: { duration: string; easing: string } | undefined;
  let expectedDurations: number[] | undefined;
  for (const url of ['/login', '/tasks', '/tasks/new', '/profiles', `/tasks/${TASK_ID}/report`]) {
    await page.goto(url);
    if (url === '/login') {
      await page.getByLabel('账号', { exact: true }).fill('tester');
      await page.getByLabel('密码', { exact: true }).fill('fixture-password');
    }
    const control = url === '/login' ? page.getByRole('button', { name: /登录$/ }) : page.getByRole('button', { name: /新建任务/ });
    await expect(control).toBeVisible();
    await page.mouse.move(0, 0);
    const style = await control.evaluate((element) => {
      const root = getComputedStyle(document.documentElement);
      const computed = getComputedStyle(element);
      return { tokens: { duration: root.getPropertyValue('--duration-fast').trim(), easing: root.getPropertyValue('--ease-out').trim() },
        durations: computed.transitionDuration.split(',').map((value) => Number.parseFloat(value)),
        background: computed.backgroundColor, transform: computed.transform, shadow: computed.boxShadow };
    });
    const milliseconds = Number.parseFloat(style.tokens.duration);
    expect(milliseconds).toBeGreaterThan(0);
    expect(milliseconds).toBeLessThanOrEqual(300);
    expect(style.tokens.easing).toContain('cubic-bezier');
    expectedTokens ??= style.tokens;
    expect(style.tokens).toEqual(expectedTokens);
    expect(Math.max(...style.durations)).toBeGreaterThan(0);
    expect(Math.max(...style.durations)).toBeLessThanOrEqual(0.3);
    expectedDurations ??= style.durations;
    expect(style.durations).toEqual(expectedDurations);
    await control.focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await expect(control).toBeFocused();
    expect(await control.evaluate((element) => getComputedStyle(element).outlineStyle)).toBe('solid');
    const bounds = await control.boundingBox();
    if (!bounds) throw new Error(`Missing control bounds on ${url}`);
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.down();
    try {
      await expect.poll(() => control.evaluate((element) => {
        const computed = getComputedStyle(element);
        return { background: computed.backgroundColor, transform: computed.transform, shadow: computed.boxShadow };
      })).not.toEqual({ background: style.background, transform: style.transform, shadow: style.shadow });
    } finally {
      await page.mouse.move(0, 0);
      await page.mouse.up();
    }
  }
  expect(mock.unexpected).toEqual([]);
});

test('减少动态偏好关闭主要页面控件与面板的过渡', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const url of ['/login', '/tasks', '/tasks/new', '/profiles', `/tasks/${TASK_ID}`, `/tasks/${TASK_ID}/report`]) {
    await page.goto(url);
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
    const durations = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>('button, a, summary, dialog, [role="tabpanel"]')]
      .filter((element) => element.getClientRects().length > 0)
      .flatMap((element) => {
        const style = getComputedStyle(element);
        return [...style.transitionDuration.split(','), ...style.animationDuration.split(',')].map((value) => Number.parseFloat(value) * 1000);
      }));
    expect(durations.length).toBeGreaterThan(0);
    expect(Math.max(...durations)).toBeLessThanOrEqual(0.01);
  }
  expect(mock.unexpected).toEqual([]);
});

test('跨页面导航、配置保存、折叠和复盘键盘操作保留业务行为', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/tasks');
  await page.getByRole('button', { name: /新建任务/ }).click();
  await expect(page.locator('#main-content')).toBeFocused();
  await selectMode(page, 'CTF 团队');
  await expect(page.getByLabel('队友上限（不含 Lead）')).toHaveValue('4');
  await selectMode(page, '黑板探索');
  const help = page.getByRole('button', { name: '目标域名（可选）', exact: true });
  await help.press('Enter');
  await expect(help).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByLabel('任务所需域名（记录）')).toBeVisible();
  await help.press('Enter');
  await expect(help).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByLabel('任务所需域名（记录）')).not.toBeVisible();
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: 'Agent 配置', exact: true }).click();
  await expect(page.locator('#main-content')).toBeFocused();
  await page.getByLabel('完整系统提示词').fill('Consistency fixture prompt');
  await page.getByRole('button', { name: '保存并应用', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已保存并应用');
  expect(mock.workerSaves).toHaveLength(1);
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '任务', exact: true }).click();
  await page.getByRole('link', { name: `查看任务：${GOAL}` }).click();
  await taskAction(page, '复盘记录');
  const drawer = page.getByRole('dialog', { name: '复盘记录' });
  await settleModal(drawer);
  const firstTab = drawer.getByRole('tab', { name: /事件流$/ });
  await firstTab.focus();
  await firstTab.press('ArrowRight');
  await expect(drawer.getByRole('tab', { name: /裁定历史$/ })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(drawer.getByRole('tab', { name: /裁定历史$/ })).toHaveAttribute('aria-selected', 'true');
  await expect(drawer.getByRole('tabpanel')).toContainText('连接池证据充分');
  await page.keyboard.press('Home');
  await page.keyboard.press('Enter');
  const event = drawer.getByRole('button').filter({ hasText: /^v\d+/ }).first();
  await event.press('Enter');
  await expect(event).toHaveAttribute('aria-expanded', 'true');
  await event.press('Enter');
  await expect(event).toHaveAttribute('aria-expanded', 'false');
  await page.keyboard.press('Escape');
  await expect(drawer).not.toBeVisible();
  await expect(page.getByRole('button', { name: '更多任务操作', exact: true })).toBeFocused();
  expect(mock.unexpected).toEqual([]);
});
