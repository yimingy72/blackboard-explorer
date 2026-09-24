import { expect, test } from '@playwright/test';
import {
  COMMAND_SENTINEL,
  FUTURE_NOTE,
  FUTURE_RECEIPT,
  GOAL,
  TASK_ID,
  TEXT_SENTINEL,
  installMockApi,
} from './fixtures';

test('登录后显示任务列表', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/login');
  await page.getByLabel('账号').fill('tester');
  await page.getByLabel('密码').fill('fixture-password');
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page).toHaveURL(/\/tasks$/);
  await expect(page.getByRole('heading', { name: '任务', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: `查看任务：${GOAL}` })).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});

test('事实详情展示六类证据，文本与命令输出可预览', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto(`/tasks/${TASK_ID}`);
  const canvas = page.getByRole('region', { name: '黑板关系图' });
  await canvas.getByRole('button', { name: /事实 F1，已提出/ }).click();
  const detail = page.getByRole('complementary', { name: '对象详情' });
  await expect(detail.getByRole('heading', { name: '数据库连接池出现等待' })).toBeVisible();
  for (const kind of ['文本', '命令输出', 'HTTP', '日志', '代码引用', '脚本']) {
    await expect(detail.locator('summary').filter({ hasText: kind }).first()).toBeVisible();
  }
  await detail.locator('summary').filter({ hasText: '现场文本' }).click();
  await expect(detail.getByText(TEXT_SENTINEL)).toBeVisible();
  await detail.locator('summary').filter({ hasText: '命令执行记录' }).click();
  await expect(detail.getByText(COMMAND_SENTINEL)).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});

test('历史时间轴不泄漏后续交接笔记和 Agent 回执，记录可回到实时', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('tab', { name: '时间轴' }).click();
  const slider = page.getByRole('slider', { name: '回放版本' });
  await slider.focus();
  await slider.press('Home');
  await expect(page.getByRole('progressbar', { name: '金额预算已用比例' })).toHaveAttribute('value', '0');
  for (let index = 0; index < 4; index += 1) await slider.press('ArrowRight');
  await expect(page.getByText('历史回放 · v5').first()).toBeVisible();

  const canvas = page.getByRole('region', { name: '黑板关系图' });
  await canvas.getByRole('button', { name: /意图 I1，调查中/ }).click();
  const detail = page.getByRole('complementary', { name: '历史快照详情' });
  await expect(detail.getByRole('heading', { name: '检查连接池上限' })).toBeVisible();
  await expect(detail).not.toContainText(FUTURE_NOTE);

  await page.getByRole('tab', { name: /Agent 记录/ }).click();
  await page.getByRole('tabpanel').getByRole('button', { name: 'agent-1' }).click();
  await expect(detail).not.toContainText(FUTURE_RECEIPT);
  await page.getByRole('tab', { name: '时间轴' }).click();
  await page.getByRole('button', { name: '返回实时' }).last().click();
  await expect(page.getByRole('complementary', { name: '对象详情' })).toContainText(FUTURE_RECEIPT);
  await page.getByRole('tab', { name: '裁定历史' }).click();
  await expect(page.getByRole('tabpanel')).toContainText('连接池证据充分');
  expect(mock.unexpected).toEqual([]);
});

test('工作区目录、文件预览与 Markdown 报告', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('tab', { name: '工作区' }).click();
  const tree = page.getByRole('region', { name: '工作区目录' });
  await tree.getByText('agents/').click();
  await tree.getByText('agent-1/').click();
  await tree.getByRole('button', { name: 'notes.txt' }).click();
  await expect(page.getByRole('region', { name: '文件预览' })).toContainText('归档文件预览内容');
  await page.getByRole('link', { name: '查看报告' }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}/report$`));
  await expect(page.getByRole('article', { name: '最终报告正文' }).getByRole('heading', { name: '最终报告' })).toBeVisible();
  await expect(page.getByRole('article', { name: '最终报告正文' })).toContainText('连接池上限');
  expect(mock.unexpected).toEqual([]);
});

test('Profile YAML 校验、版本比较、发布与本浏览器默认', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  await expect(page.getByRole('heading', { name: 'Agent 配置' })).toBeVisible();
  await page.getByRole('button', { name: /^v1/ }).click();
  await page.getByRole('button', { name: '对比版本' }).click();
  const diff = page.getByRole('region', { name: 'YAML 版本差异' });
  await expect(diff).toContainText('旧版探索模板');
  await expect(diff).toContainText('新版探索模板');
  await page.getByRole('button', { name: '查看', exact: true }).click();
  await page.getByRole('button', { name: '编辑并发布新版本' }).click();
  const editor = page.getByLabel('完整 AgentProfile YAML');
  const source = await editor.inputValue();
  await editor.fill('models: [');
  await page.getByRole('button', { name: '检查格式' }).click();
  await expect(page.getByRole('alert')).toContainText('YAML 格式错误');
  await editor.fill(source.replace('旧版探索模板', '已调整的探索模板'));
  await page.getByRole('button', { name: '发布新版本' }).click();
  await expect(page.getByRole('heading', { name: 'default · v3' })).toBeVisible();
  await page.getByRole('button', { name: '设为本浏览器默认' }).click();
  await expect(page.getByRole('status')).toContainText('本浏览器的新建任务默认');
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('bbx.default-profile.v1') ?? 'null')))
    .toEqual({ name: 'default', version: 3 });
  await page.goto('/tasks/new');
  await expect(page.getByLabel('版本', { exact: true })).toHaveValue('3');
  expect(mock.unexpected).toEqual([]);
});

test('超过 300 个对象默认折叠，手机视口无页面横向溢出', async ({ page }) => {
  test.setTimeout(90_000);
  const mock = await installMockApi(page, { largeGraph: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/tasks/${TASK_ID}`);
  await expect(page.getByRole('checkbox', { name: '折叠已关闭分支' })).toBeChecked();
  await expect(page.getByText('显示 151 / 302 个对象')).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth))
    .toBeLessThanOrEqual(1);
  expect(mock.unexpected).toEqual([]);
});
