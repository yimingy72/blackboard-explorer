import { mkdir } from 'node:fs/promises';
import { expect, test, type Page } from '@playwright/test';
import { GOAL, TASK_ID, installMockApi } from './fixtures';
import { installConversationFixture } from './conversation-fixture';

const output = '../.data/checkpoints/frontend-polish/after';
const prompt = '# 探索职责\n\n围绕任务目标开展调查，把可复核的发现及时发布到共享黑板。\n\n## 工作方式\n\n- 先阅读目标、验收条件和已有事实。\n- 核实资料来源，保留必要证据。\n- 独立方向及时提出意图，交由其他 Agent 并行推进。\n- 共享文件先复制，再在自己的工作目录修改。\n\n## 当前任务\n\n{{ goal }}\n\n{{ acceptance_status }}';

async function noOverflow(page: Page) {
  try {
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  } catch (error) {
    const layout = await page.evaluate(() => ({ url: location.pathname, width: innerWidth, overflow: document.documentElement.scrollWidth,
      elements: [...document.querySelectorAll('body *')].filter((element) => element.getBoundingClientRect().right > innerWidth + 1).map((element) => ({ tag: element.tagName, className: element.className, right: element.getBoundingClientRect().right, width: element.getBoundingClientRect().width })).slice(0, 20) }));
    await test.info().attach('overflow-layout', { body: JSON.stringify(layout), contentType: 'application/json' });
    throw error;
  }
}

test('全站页面、配置入口和详情在桌面与窄屏保持可读且不横向溢出', async ({ page }) => {
  test.setTimeout(90_000);
  const mock = await installMockApi(page, { workerPrompt: prompt });
  await mkdir(output, { recursive: true });
  for (const width of [1440, 768, 390, 360]) {
    await page.setViewportSize({ width, height: width > 900 ? 1000 : 844 });
    for (const [name, url, heading] of [
      ['login', '/login', '进入工作台'], ['tasks', '/tasks', '任务'],
      ['create', '/tasks/new', '创建探索任务'], ['workers', '/profiles', 'Agent 配置'],
      ['report', `/tasks/${TASK_ID}/report`, '最终报告'],
    ]) {
      await page.goto(url);
      await expect(page.getByRole('heading', { name: heading, exact: true }).first()).toBeVisible();
      await noOverflow(page);
      await page.screenshot({ path: `${output}/${name}-${width}.png`, fullPage: true });
    }
    await page.goto('/profiles');
    for (const [name, label, item] of [['models', '模型配置', /审查模型/], ['mcp', 'MCP 工具', /资料检索/]] as const) {
      await page.getByRole('button', { name: label, exact: true }).click();
      await page.getByRole('button', { name: item }).click();
      await noOverflow(page);
      await page.screenshot({ path: `${output}/${name}-${width}.png`, fullPage: true });
    }
    await page.goto(`/tasks/${TASK_ID}`);
    const canvas = page.getByRole('region', { name: '黑板关系图' }).first();
    await expect(canvas.getByRole('button', { name: /事实 F1，已提出/ })).toBeVisible();
    await noOverflow(page);
    expect(await page.evaluate(() => document.documentElement.scrollHeight - innerHeight)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: `${output}/workbench-${width}.png` });
    await canvas.getByRole('button', { name: /事实 F1，已提出/ }).click();
    const detail = page.getByRole('complementary', { name: '对象详情' });
    await expect(detail).toBeVisible();
    await noOverflow(page);
    const bounds = await detail.boundingBox();
    expect(bounds?.x).toBeGreaterThanOrEqual(0);
    expect((bounds?.y ?? 0) + (bounds?.height ?? 0)).toBeLessThanOrEqual(width > 900 ? 1001 : 845);
    await page.screenshot({ path: `${output}/detail-${width}.png` });
    await page.getByRole('button', { name: '复盘记录', exact: true }).click();
    const records = page.getByRole('dialog', { name: '复盘记录' });
    await expect(records).toBeVisible();
    await page.screenshot({ path: `${output}/records-${width}.png` });
    await page.keyboard.press('Escape');
    await expect(records).not.toBeVisible();
  }
  expect(mock.unexpected).toEqual([]);
});

test('登录错误可恢复、密码可核对、键盘可以跳过导航且基础文字对比度达标', async ({ page }) => {
  const mock = await installMockApi(page);
  let fail = true;
  await page.route('**/api/login', (route) => fail
    ? route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: '账号或密码不正确' }) })
    : route.fallback());
  await page.goto('/login');
  const password = page.getByLabel('密码', { exact: true });
  await page.getByLabel('账号', { exact: true }).fill('tester');
  await password.fill('fixture-password');
  await page.getByRole('button', { name: '显示', exact: true }).click();
  await expect(password).toHaveAttribute('type', 'text');
  await page.getByRole('button', { name: '隐藏', exact: true }).click();
  await expect(password).toHaveAttribute('type', 'password');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('账号或密码不正确');
  await expect(password).toHaveAttribute('aria-invalid', 'true');
  fail = false;
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '任务', exact: true })).toBeVisible();
  const skip = page.getByRole('link', { name: '跳到主要内容' });
  await skip.focus();
  await expect(skip).toBeInViewport();
  await skip.press('Enter');
  await expect(page.getByRole('main')).toBeFocused();
  const ratios = await page.evaluate(() => {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext('2d')!;
    const style = getComputedStyle(document.documentElement);
    function luminance(token: string) {
      ctx.fillStyle = style.getPropertyValue(token);
      ctx.fillRect(0, 0, 1, 1);
      const rgb = Array.from(ctx.getImageData(0, 0, 1, 1).data).slice(0, 3).map((value) => {
        const channel = value / 255;
        return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
      });
      return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
    }
    return [['--color-muted', '--color-bg', 4.5], ['--color-muted', '--color-surface', 4.5], ['--color-primary', '--color-bg', 4.5], ['--color-ink', '--color-bg', 4.5], ['--color-control-line', '--color-bg', 3]].map(([a, b, minimum]) => {
      const x = luminance(String(a)), y = luminance(String(b));
      return { ratio: (Math.max(x, y) + .05) / (Math.min(x, y) + .05), minimum: Number(minimum) };
    });
  });
  for (const { ratio, minimum } of ratios) expect(ratio).toBeGreaterThanOrEqual(minimum);
  await test.info().attach('text-contrast-ratios', { body: JSON.stringify(ratios), contentType: 'application/json' });
  expect(mock.unexpected).toEqual([]);
});

test('MCP清单的慢请求不能覆盖后选择的服务，加载状态明确', async ({ page }) => {
  const mock = await installMockApi(page);
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => { release = resolve; });
  await page.route('**/api/platform/mcp-servers/reference/versions/1/tools', async (route) => {
    await delayed;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ tools: [{ name: 'outdated_tool', description: '旧请求' }] }) });
  });
  await page.route('**/api/platform/mcp-servers/analysis/versions/1/tools', (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ tools: [{ name: 'current_tool', description: '当前服务' }] }) }));
  try {
    await page.goto('/profiles');
    await page.getByRole('checkbox', { name: '资料检索', exact: true }).check();
    await page.getByRole('checkbox', { name: '分析工具', exact: true }).check();
    await page.getByRole('button', { name: '读取 资料检索 工具清单', exact: true }).click();
    await expect(page.getByRole('button', { name: '正在读取 资料检索 工具…' })).toBeDisabled();
    await page.getByRole('button', { name: '读取 分析工具 工具清单', exact: true }).click();
    await expect(page.getByRole('checkbox', { name: /current_tool/ })).toBeVisible();
    const response = page.waitForResponse((value) => value.url().includes('/reference/versions/1/tools'));
    release(); await response;
    await expect(page.getByRole('checkbox', { name: /current_tool/ })).toBeVisible();
    await expect(page.getByRole('checkbox', { name: /outdated_tool/ })).toHaveCount(0);
    expect(mock.unexpected).toEqual([]);
  } finally { release(); }
});

test('Agent回复按Markdown阅读且保留原文，窄屏输入和桌面长页保存操作可达', async ({ page }) => {
  await installConversationFixture(page);
  const source = '已完成第一轮核对。\n\n## 发现\n\n1. **日期口径**不同。\n2. 保留原始行依据。\n\n```python\nfor row in rows:\n    print(row["date"])\n```\n\n![外部图片](https://images.invalid/never-load.png)\n<script>window.replyInjected=true</script>';
  await page.route('**/api/evidence?*', async (route) => {
    if (!new URL(route.request().url()).searchParams.get('uri')?.endsWith('/model_output.json')) return route.fallback();
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ text: source }) });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ }).click();
  const pane = page.getByRole('complementary', { name: 'Agent 1 对话记录' });
  const entry = pane.locator('li').filter({ hasText: '模型回复' });
  await entry.getByRole('button', { name: '查看正文' }).click();
  await expect(entry.getByRole('heading', { name: '发现' })).toBeVisible();
  await expect(entry.locator('ol > li')).toHaveCount(2);
  await expect(entry.locator('pre code')).toContainText('    print(row["date"])');
  await expect(entry.locator('img, script')).toHaveCount(0);
  expect(await page.evaluate(() => 'replyInjected' in window)).toBe(false);
  await mkdir(output, { recursive: true });
  await entry.getByRole('heading', { name: '发现' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/agent-markdown-390.png` });
  await entry.locator('summary').filter({ hasText: '原始文本' }).click();
  await expect(entry.locator('pre').filter({ hasText: '<script>' })).toHaveText(source);
  await expect(pane.getByRole('textbox')).toBeVisible();
  await expect(pane.getByRole('button', { name: '发送', exact: true })).toBeInViewport({ ratio: 1 });
  await noOverflow(page);
  await page.screenshot({ path: `${output}/agent-390.png` });
  await page.getByRole('button', { name: '关闭对话' }).click();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/profiles');
  await expect(page.getByRole('button', { name: '保存并应用' })).toBeInViewport();
  await page.getByLabel('完整系统提示词').fill(prompt);
  await expect(page.getByRole('button', { name: '保存并应用' })).toBeEnabled();
  await page.getByRole('button', { name: '保存并应用' }).click();
  await expect(page.getByRole('status')).toContainText('已保存并应用');
  await page.goto('/tasks');
  await expect(page.getByRole('link', { name: `查看任务：${GOAL}` })).toBeVisible();
});

test('中等断点保存栏不被导航遮挡，短屏Agent发送按钮能完整点击', async ({ page }) => {
  await installMockApi(page, { workerPrompt: prompt });
  await page.setViewportSize({ width: 700, height: 844 });
  await page.goto('/profiles');
  await page.getByLabel('完整系统提示词').fill(prompt + '\n\n补充工作规则。');
  await page.evaluate(() => window.scrollTo(0, 700));
  const save = page.getByRole('button', { name: '保存并应用' });
  const reached = await save.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
    return hit === element || hit !== null && element.contains(hit);
  });
  expect(reached).toBe(true);
  await save.click();
  await expect(page.getByRole('status')).toContainText('已保存并应用');
  await page.goto('/tasks/new');
  expect(await page.evaluate(() => scrollY)).toBe(0);
  await expect(page.getByRole('link', { name: '跳到主要内容' })).not.toBeInViewport();
  await page.setViewportSize({ width: 390, height: 600 });
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ }).click();
  const pane = page.getByRole('complementary', { name: 'Agent 1 对话记录' });
  let sent = 0;
  await page.route(`**/api/tasks/${TASK_ID}/agents/agent-1/messages*`, (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    sent += 1;
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ id: 'fixture-message', content: '复盘问题', role: 'user', status: 'queued' }) });
  });
  await pane.getByRole('textbox').fill('复盘问题');
  const send = pane.getByRole('button', { name: '发送', exact: true });
  await send.scrollIntoViewIfNeeded();
  await expect(send).toBeInViewport({ ratio: 1 });
  await send.click();
  expect(sent).toBe(1);
  await mkdir(output, { recursive: true });
  await page.screenshot({ path: `${output}/agent-short-390.png` });
});

test('长任务目标保留全文且不挤走画布', async ({ page }) => {
  const mock = await installMockApi(page);
  const goal = '核对各月份的数据与原始资料，并保留每一项结论的证据。'.repeat(40);
  const events = mock.events.map((event) => event.type === 'task.created' ? { ...event, payload: { ...event.payload, goal } } : event);
  await page.route(`**/api/tasks/${TASK_ID}/events?*`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(events) }));
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto(`/tasks/${TASK_ID}`);
    const heading = page.getByRole('heading', { level: 1 });
    await expect(heading).toHaveText(goal.slice(0, 100));
    await expect(heading).toHaveAttribute('title', goal.slice(0, 100));
    expect((await heading.boundingBox())?.height).toBeLessThanOrEqual(73);
    const canvas = page.getByRole('region', { name: '黑板关系图' }).first();
    expect((await canvas.boundingBox())?.height).toBeGreaterThanOrEqual(180);
    await noOverflow(page);
    await page.getByRole('button', { name: '任务内容', exact: true }).click();
    const details = page.getByRole('complementary', { name: '对象详情' });
    await details.getByRole('button', { name: '展开目标全文' }).click();
    await details.getByRole('button', { name: '原文', exact: true }).first().click();
    await expect(details.locator('pre').first()).toHaveText(goal);
  }
  expect(mock.unexpected).toEqual([]);
});
